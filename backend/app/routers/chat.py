"""快速版对话接口 —— 一次请求把「图片 / 语音 / 文字」压成一条简化的病情介绍。

**与 /api/diagnose/multimodal 的分工**

那个接口面向诊断页，返回完整报告（候选闭集、证据链、治疗分节、虫害框、来源标注），
前端还要自己排版成六大块。快速版是仿微信的聊天框，一条气泡里只放得下三样东西：

    一句能直接读懂的判断  +  三条能立刻照做的要点  +  一张能点开的知识卡片

所以本接口负责「压缩」而不是「重复实现」：识别、语音转写、RAG、大模型全部沿用
`src/llm/fusion.py` 那一条链路，本文件只做两件事 —— 把报告压成短句、把结论对到
`knowledge/library` 里的具体卡片 id（前端拿它跳到 `library.html?open=<id>`）。

**降级策略**：fusion 内部已经是「哪一路挂了都不让整体失败」，这里再兜一层 ——
卡片匹配不到就只回文字，不编一个卡片 id 出来。

**线程模型**：同步 `def`。fusion 里是 ONNX 推理与同步 requests，写成 `async def`
会阻塞事件循环（与 `routers/assistant.py` 相同的理由）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile

from backend.app import deps
from backend.app.core.config import get_settings
from backend.app.core.errors import CODE_BAD_REQUEST, CODE_UPSTREAM_ERROR, ApiError
from src.common.logging_utils import setup_logger
from src.llm.prompts import QA_SYSTEM_PROMPT

logger = setup_logger("agri.api.chat")

router = APIRouter(prefix="/api/chat", tags=["快速对话"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/bmp", "image/webp", "image/jpg"}

MAX_POINTS = 3          # 气泡里最多三条要点：再多就不叫「简化版」了
POINT_CHARS = 62        # 单条要点压到两行以内（结论卡加宽后 46 字会断在半句）
SUMMARY_CHARS = 150     # 判断句上限
CARD_LIMIT = 3

# 这些小节讲的是「为什么这么判断」，不是「现在该做什么」，不能当成行动要点
BACKGROUND_TITLES = ("发病规律与判断依据", "症状与判断依据", "判断依据")

# 这些是「没有结论」的说法，不是病名（mock 兜底报告会写进 disease_name）
UNKNOWN_NAMES = {"无法判断", "信息不足", "不确定", "未知", "无法识别", "无"}


# ------------------------------------------------------------------ 文本压缩

def _one_line(text: Any, limit: int) -> str:
    """压成一行、截到 limit 字，尽量断在句读处，而不是硬切半个词。"""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in ("。", "；", "！", "？", "，", ";", "."):
        i = cut.rfind(sep)
        if i >= limit * 0.55:
            # 断在分号/逗号上时收尾用省略号，别留下「……透光；」这种半句带标点
            body = cut[:i].rstrip("，、；,;：: ")
            return body + ("。" if sep in "。！？" else "…")
    return cut.rstrip("，、；,;：: ") + "…"


def _plain_answer(text: Any) -> str:
    """把大模型返回的「JSON 串」还原成一句话。

    追问已经改用 `QA_SYSTEM_PROMPT`（要求输出纯文本），但模型仍可能不听话，
    返回 `{"answer": "先别急着打药……"}` 这类内容。直接塞进气泡就是把 JSON
    给农户看，所以这里再拆一层；万一拆不出来，原样返回也比丢掉强。
    """
    s = str(text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return s
        if isinstance(data, dict):
            for key in ("answer", "reply", "content", "text", "summary"):
                v = data.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return s


def _read(file: UploadFile, *, kind: str) -> bytes:
    """同步读取上传文件并校验大小（本函数所在路由是同步 def）。"""
    data = file.file.read()
    if not data:
        raise ApiError(f"{kind}文件为空", CODE_BAD_REQUEST)
    if len(data) > get_settings().max_upload_bytes:
        raise ApiError(
            f"{kind}文件过大（{len(data) / 1024 / 1024:.1f} MB），"
            f"上限 {get_settings().max_upload_mb} MB",
            CODE_BAD_REQUEST,
        )
    return data


# ------------------------------------------------------------------ 卡片匹配

def _card_brief(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title") or "",
        "category": item.get("category") or "",
        "category_name": item.get("category_name") or "",
        "crop": item.get("crop") or "",
        "summary": _one_line(item.get("summary"), 80),
        "url": f"/library.html?open={item.get('id')}",
    }


def _score_card(item: dict, *, name: str, disease_name: str, crop: str,
                pest_names: list[str]) -> int:
    """给每张卡片打一个「跟这次结论有多贴」的分。

    打分而不是精确匹配，是因为两侧的名字来源不同：检测器给「稻纵卷叶螟」，
    卡片表里 `condition` 也是「稻纵卷叶螟」，但病害那侧模型给的是拼好的
    「水稻稻瘟病」，而卡片把它拆成 `crop=水稻` + `condition=稻瘟病`。
    两种拆法都要能对上，所以按「全名相等 > 病名结尾 > 病名包含」分档。
    """
    cond = str(item.get("condition") or "")
    it_crop = str(item.get("crop") or "")
    if not cond:
        return 0

    if item.get("category") == "insect":
        # 虫害卡只认虫子本名：按作物命中会把「水稻稻纵卷叶螟」答成别的虫
        return 120 if cond in pest_names else 0

    # 作物对不上就直接排除：库里「圆斑病」有玉米和苹果两张卡，只按病名后缀匹配会把
    # 苹果那张也带出来 —— 农户点进去看到的是另一种作物的防治办法，这比不给卡片更糟。
    if it_crop and crop and it_crop != crop:
        return 0

    score = 0
    if it_crop and it_crop + cond == disease_name:
        score += 100
    elif cond and disease_name.endswith(cond):
        score += 45
    elif cond and len(cond) >= 3 and cond in disease_name:
        score += 30
    if score and it_crop and it_crop == crop:
        score += 20
    return score


def _pick_cards(items: list[dict], *, name: str, disease_name: str, crop: str,
                pest_names: list[str]) -> list[dict]:
    scored = []
    for it in items:
        s = _score_card(it, name=name, disease_name=disease_name, crop=crop,
                        pest_names=pest_names)
        if s >= 45:                      # 只留「确实是这个病/这个虫」的，宁缺勿滥
            scored.append((s, it))
    scored.sort(key=lambda p: (-p[0], str(p[1].get("id"))))
    return [_card_brief(it) for _, it in scored[:CARD_LIMIT]]


def _keyword_cards(items: list[dict], text: str) -> list[dict]:
    """纯文字输入时按「病害名 / 作物名」检索卡片。

    中文没有空格，用分词不如直接扫词表：卡片表里的 `condition` 就是一份现成的
    病害名录（399 条），`condition in text` 反而比通用分词准。
    """
    cond_hits, care_hits = [], []
    for it in items:
        cond = str(it.get("condition") or "")
        crop = str(it.get("crop") or "")
        if cond and len(cond) >= 3 and cond in text:
            cond_hits.append(it)
        elif crop and it.get("category") == "care" and crop in text:
            care_hits.append(it)

    # 病名命中的优先按名字长度排（「稻瘟病」比「瘟病」更具体），养护卡垫底
    cond_hits.sort(key=lambda it: -len(str(it.get("condition") or "")))
    picked = cond_hits[:CARD_LIMIT]
    if not picked and care_hits:
        picked = care_hits[:1]
    return [_card_brief(it) for it in picked]


# ------------------------------------------------------------------ 政策问答

# 政策类问题的入口词。这里刻意不收「农药」「保险」这类单词：
# 「这病打什么农药」是农技问题，不能被当成《禁限用农药》政策去答。
POLICY_TRIGGERS = (
    "政策", "补贴", "补助", "惠农", "扶持", "申报", "申领", "发放", "资金",
    "农机购置", "耕地地力", "高标准农田", "黑土地", "单产提升", "经营主体",
    "家庭农场", "合作社", "农业保险", "保险", "贷款", "信贷", "贴息",
    "科普", "培训", "禁限用", "农药经营", "乡村振兴", "粮油", "冬小麦", "抗旱",
)

# 关键词 -> 政策 id。这套映射是从 data/policy_list.json 的 10 篇原文里人工归纳的：
# 向量检索适合长文本问答，但政策只有 10 篇、问题又高度集中在几个事项上，
# 用词表命中比embedding更可控，也更容易在答辩时解释「为什么是这一篇」。
POLICY_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("耕地地力", "地力保护", "农机购置", "一喷三防", "带状复合", "生产者补贴",
      "稻谷补贴", "轮作休耕", "强农惠农富农政策清单"), "pol_001"),
    (("单产提升", "高产高效", "高标准农田", "黑土地", "耕地质量", "绿色高产"), "pol_002"),
    (("家庭农场", "合作社", "经营主体", "社会化服务", "一码通"), "pol_003"),
    (("农业保险", "保费补贴", "保险"), "pol_004"),
    (("信贷", "贷款", "贴息", "债券", "资金支农", "投入机制"), "pol_005"),
    (("乡村振兴", "粮油生产", "畜牧", "渔业", "果蔬"), "pol_006"),
    (("科普",), "pol_007"),
    (("禁限用", "违禁农药", "农药经营", "假农药", "农药安全使用"), "pol_008"),
    (("冬小麦", "促弱转壮", "晚播"), "pol_009"),
    (("大豆", "高温干旱", "抗旱", "防灾减灾"), "pol_010"),
)

POLICY_TYPE_NAMES = {"subsidy": "补贴补助", "subject": "经营主体", "service": "技术服务", "other": "其他"}


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度。只用来判断「这句话里是不是提到了这一条」。"""
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, ch2 in enumerate(b, 1):
            if ch == ch2:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _point_matches(title: str, text: str) -> bool:
    """条目名是否被这句话点到。

    除了整名包含，还要认「说了大半」的情况：条目叫「农机购置与应用补贴」，
    农户嘴上说的是「农机购置补贴」，四个字的重合已经足够确定是同一件事。
    """
    if len(title) < 4:
        return False
    return title in text or _lcs_len(title, text) >= 4


def _policy_rank(items: list[dict], text: str) -> list[tuple[int, str]]:
    """给每篇政策打一个「跟这句话有多相关」的分，返回 (分数, 政策 id) 降序。

    同分时按数据里的原始顺序排（补贴清单在第一篇），而不是按 id 排 ——
    否则「今年有哪些补贴」会因为 id 靠后而挑到一篇新闻稿。
    """
    scores: dict[str, int] = {}
    for keywords, pid in POLICY_HINTS:
        for kw in keywords:
            if kw in text:
                scores[pid] = scores.get(pid, 0) + len(kw) * 8

    # 用户可能直接念出某一条的名称（「耕地地力保护补贴补给谁」）
    for it in items:
        for p in it.get("points") or []:
            title = str(p.get("title") or "")
            if _point_matches(title, text):
                scores[it["id"]] = scores.get(it["id"], 0) + 40 + len(title) * 4

    # 问「补贴」时把补贴类政策往前排：这是类型语义，不是内容重合
    if any(w in text for w in ("补贴", "补助", "惠农")):
        for it in items:
            if it.get("type") == "subsidy":
                scores[it["id"]] = scores.get(it["id"], 0) + 12

    order = {it.get("id"): i for i, it in enumerate(items)}
    ranked = sorted(((v, k) for k, v in scores.items()),
                    key=lambda pair: (-pair[0], order.get(pair[1], len(items))))
    return ranked


def _policy_source(pol: dict) -> dict:
    """政策来源：发布单位 + 原文标题 + 发布日期 + 原文地址，缺一不可。"""
    title = str(pol.get("title") or "")
    # 标题本身已经带《》（「关于印发《…》的通知」）就不再套一层书名号
    name = title if "《" in title else f"《{title}》"
    return {
        "label": f"{pol.get('publish_unit') or ''}{name}".strip(),
        "detail": str(pol.get("publish_date") or ""),
        "url": str(pol.get("source_url") or ""),
    }


def _policy_reply(text: str) -> dict | None:
    """政策类问题 -> 引用原文作答。不是政策问题就返回 None，交给后面的分支。"""
    raw = deps.load_json_cached("policy_list")
    items = list(raw.get("items") or [])
    if not items:
        return None

    ranked = _policy_rank(items, text)
    by_id = {it.get("id"): it for it in items}

    if not ranked:
        # 只说了「有哪些政策」这种泛问：说清收录范围，给政策页入口，不硬挑一篇
        if not any(w in text for w in ("政策", "惠农", "补贴", "补助")):
            return None
        return {
            "reply": {
                "kind": "policy",
                "title": f"{raw.get('year', 2026)} 年惠农政策（收录 {len(items)} 篇）",
                "confidence": 0.0,
                "text": (
                    f"平台收录了农业农村部及相关部门 {raw.get('year', 2026)} 年发布的 "
                    f"{len(items)} 篇政策原文，分为补贴补助、经营主体、技术服务、其他四类。"
                    "你可以按具体事项问我，比如「耕地地力保护补贴补给谁」「农业保险保什么」。"
                ),
                "points": [],
                "note": "",
                "card_url": "",
                "sources": [{
                    "label": "惠农政策库",
                    "detail": f"农业农村部政策原文 {len(items)} 篇",
                    "url": "/policy.html",
                }],
            },
            "cards": [],
            "context": "",
        }

    top = by_id.get(ranked[0][1]) or items[0]
    points = [p for p in (top.get("points") or []) if str(p.get("title") or "").strip()]
    quoted = [p for p in points if _point_matches(str(p.get("title") or ""), text)]
    # 没有直接点名某一条时，把与问句重合最多的条目排到前面：
    # 问「合作社有什么扶持」不该先列家庭农场那三条
    by_relevance = sorted(points, key=lambda p: -_lcs_len(str(p.get("title") or ""), text))
    picked = quoted[:2] if quoted else by_relevance[:3]

    if quoted:
        summary = (
            f"《{top.get('title')}》里「{quoted[0]['title']}」写的是："
            f"{_one_line(quoted[0].get('text'), 180)}"
        )
    elif points:
        names = "、".join(str(p["title"]) for p in by_relevance[:6])
        total = top.get("item_count") or len(points)
        summary = f"《{top.get('title')}》共 {total} 项，包括{names}等。"
    else:
        # pol_009 / pol_010 这类新闻稿没有分条标题，直接用正文首段
        first = (top.get("points") or [{}])[0]
        summary = _one_line(first.get("text") or top.get("summary") or top.get("lead"), 170)

    bullets = [
        f"{p['title']}：{_one_line(p.get('text'), POINT_CHARS)}" for p in picked
    ]

    sources = [_policy_source(top)]
    top_score = ranked[0][0]
    # 只有相关度接近时才算「相关文件」，否则会把一篇新闻稿挂在一条具体补贴后面
    for score, pid in ranked[1:2]:
        other = by_id.get(pid)
        if other and score >= top_score * 0.55 and other.get("id") != top.get("id"):
            sources.append(_policy_source(other))

    return {
        "reply": {
            "kind": "policy",
            "title": str(top.get("title") or "惠农政策"),
            "confidence": 0.0,
            "text": summary,
            "points": bullets,
            "note": "具体申报时间、材料与发放标准以当地农业农村部门通知为准；原文见下方来源。",
            "card_url": "",
            "sources": sources,
        },
        "cards": [],
        "context": "",   # 政策结论不写进病情上下文，避免下一句问病害时被带偏
    }


# ------------------------------------------------------------------ 组装回复

def _points_from(report: dict) -> list[str]:
    out: list[str] = []
    for sec in report.get("treatment_sections") or []:
        title = str(sec.get("title") or "").strip()
        if title in BACKGROUND_TITLES:
            continue
        content = _one_line(sec.get("content"), POINT_CHARS)
        if not content and not title:
            continue
        out.append(f"{title}：{content}" if title else content)
        if len(out) >= MAX_POINTS:
            break
    return out


def _context_json(*, name: str, disease_name: str, crop: str, confidence: float,
                  summary: str, cards: list[dict]) -> str:
    """给下一轮追问带的最小上下文。

    只放结论和卡片 id，不放整份报告 —— 整份报告塞进提示词会把追问的上下文挤满，
    也让模型更爱复述而不是回答。
    """
    return json.dumps({
        "name": name,
        "disease_name": disease_name,
        "crop": crop,
        "confidence": confidence,
        "summary": summary,
        "card_ids": [c["id"] for c in cards],
    }, ensure_ascii=False)


def _diagnosis_reply(result: dict) -> dict:
    report = result.get("report") or {}
    primary = report.get("primary") or {}
    items = deps.load_library()["items"]

    kind = str(primary.get("kind") or "disease")
    name = str(primary.get("name") or report.get("disease_name") or "").strip()
    disease_name = str(report.get("disease_name") or "").strip()
    crop = str(primary.get("crop") or "").strip()
    if not crop:
        top1 = ((result.get("recognition") or {}).get("top1") or {})
        crop = str(top1.get("crop") or "")
    pest_names = [str(f.get("name") or "") for f in (report.get("pest_findings") or [])]

    cards = _pick_cards(items, name=name, disease_name=disease_name, crop=crop,
                        pest_names=pest_names)
    confidence = float(primary.get("confidence") or report.get("confidence") or 0.0)
    text = _one_line(report.get("summary"), SUMMARY_CHARS)

    # 健康类走独立出口：它不是「没判断出来」，而是一个**有边界的结论**。
    # 这里如果不拦，disease_name 是空的，会掉进下面的 unknown 分支，
    # 于是一片好叶子被答成「我暂时判断不出来」—— 农户的下一句就是「那到底有没有病」。
    if report.get("healthy"):
        crop_txt = f"{crop}叶片" if crop else "叶片"
        return {
            "reply": {
                "kind": "healthy",
                "title": f"未见明显病斑",
                "confidence": round(confidence, 4),
                "text": text or f"这张{crop_txt}看着是好的，暂时不用打药。",
                "points": _points_from(report)[:3],
                "note": "一张照片只代表这一片叶。3 天后同角度再拍一张对比，有变化再来问。",
                "card_url": "",
                "sources": [{
                    "label": "图像识别模型",
                    "detail": "PDD271 病害数据集（含 21 个健康类）",
                    "url": "",
                }],
            },
            "cards": [],
            "context": _context_json(name="健康", disease_name="", crop=crop,
                                     confidence=round(confidence, 4), summary=text, cards=[]),
        }

    if not name:
        # 信息不足（没匹配到病害、也没检出虫）：照实说。
        # 如果规则给了「怎么补拍」，那就不是一句道歉，而是一条能照着做的指示 ——
        # 给它一张卡（kind=unsure），否则「逆光拍叶背」这种最有用的信息会被气泡吞掉。
        tips = _points_from(report)
        return {
            "reply": {
                "kind": "unsure" if tips else "unknown",
                "title": "补拍一下" if tips else "",
                "confidence": 0.0,
                "text": text or "这张照片我没把握。补拍一张逆光叶背，比勉强给个病名有用。",
                "points": tips,
                "note": "判断不了的时候我会直说，不会硬给一个病名。",
                "card_url": "",
            },
            "cards": [],
            "context": "",
        }

    # 「无法判断」不是一个病名。mock 的兜底报告会把它写进 disease_name，
    # 不拦下来的话，界面上会出现一张标题叫「无法判断」的漂亮卡片 —— 那比直接说
    # 「没看清」更误导人。
    if name in UNKNOWN_NAMES or str(primary.get("kind")) == "unknown":
        return {
            "reply": {
                "kind": "unknown",
                "title": "",
                "confidence": 0.0,
                "text": text or "这次没看清。把叶子举到逆光下拍一张叶背 —— 很多虫和霉层藏在叶背，正面看不出来。",
                "points": [],          # 没结论就不要给通用方案，那会看着像结论
                "note": "判断不了的时候我会直说，不会硬给一个病名。",
                "card_url": "",
            },
            "cards": [],
            "context": "",
        }

    note = ""
    if kind == "pest":
        note = str(primary.get("note") or "")
    # 主结论是虫害时，病害那一路降级成一句附注（fusion 已经把它写进 primary.note）
    if not note and confidence and confidence < 0.5:
        # 只说「再拍一张」没用，要说清怎么拍：叶背和逆光才是病斑与虫体最藏不住的地方
        note = (f"判断把握不大（{confidence:.0%}）。把叶子举到逆光下补拍一张叶背，"
                "很多虫和霉层藏在叶背，正面看不出来。")

    # 回答末尾要能点回证据：命中的卡片给站内深链，模型给训练数据出处。
    sources: list[dict] = []
    if cards:
        top_card = cards[0]
        no = str(top_card.get("id") or "").split("_")[-1]
        sources.append({
            "label": f"知识图书库《{top_card['title']}》",
            "detail": f"卡片编号 #{no}" if no else "",
            "url": str(top_card.get("url") or ""),
        })
    sources.append({"label": "图像识别模型", "detail": "PDD271 病害数据集 / IP102 虫害数据集", "url": ""})

    return {
        "reply": {
            # 主结论是虫害就标 pest：标题写着「白背飞虱」、标签却写「病害诊断」，
            # 这种自相矛盾在农业场景里是要被追着问的。
            "kind": "pest" if kind == "pest" else "diagnosis",
            "title": name,
            "confidence": round(confidence, 4),
            "text": text,
            "points": _points_from(report),
            "note": note,
            "card_url": cards[0]["url"] if cards else "",
            "sources": sources,
        },
        "cards": cards,
        "context": _context_json(name=name, disease_name=disease_name, crop=crop,
                                 confidence=round(confidence, 4), summary=text, cards=cards),
    }


def _parse_context(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _context_prompt(ctx: dict) -> str:
    """把上一轮结论改写成提示词里的一句话，而不是让模型自己解析 JSON。"""
    name = str(ctx.get("name") or "")
    if not name:
        return ""
    summary = str(ctx.get("summary") or "")
    return f"上一轮判断：{name}（置信度 {float(ctx.get('confidence') or 0):.0%}）。{summary}"


# ------------------------------------------------------------------ 接口

@router.post(
    "/send",
    summary="快速版对话（图片 / 语音 / 文字 → 简化病情介绍）",
    description=(
        "multipart 表单，三个输入至少给一个：`image`（作物照片）、`audio`（16kHz WAV）、"
        "`text`（文字描述）。\n\n"
        "返回 `reply`（一句话判断 + 最多三条要点）、`cards`（命中的知识卡片，可直接跳到 "
        "`/library.html?open=<id>`）与 `context`（原样回传即可让下一轮追问带上下文）。\n\n"
        "纯文字且没有任何结论可依据时走闲聊分支；大模型不可用时按其模板作答，接口始终 200。"
    ),
)
def send(
    image: UploadFile | None = File(None, description="作物照片"),
    audio: UploadFile | None = File(None, description="语音描述（16kHz 单声道 WAV）"),
    text: str = Form("", description="文字描述"),
    dialect: str = Form("", description="方言 key，留空用默认"),
    context: str = Form("", description="上一轮返回的 context 字段，原样回传"),
):
    text = (text or "").strip()
    if image is None and audio is None and not text:
        raise ApiError("说一句话、或者拍张照片，我才能看", CODE_BAD_REQUEST)

    if image is not None and image.content_type not in ALLOWED_IMAGE_TYPES:
        raise ApiError(
            f"图片格式不支持：{image.content_type}；请上传 JPG / PNG / BMP / WebP",
            CODE_BAD_REQUEST,
        )

    image_bytes = _read(image, kind="图片") if image is not None else None
    audio_bytes = _read(audio, kind="音频") if audio is not None else None
    prior = _parse_context(context)

    # ---------------- 有图或有声：走完整诊断链路 ----------------
    if image_bytes or audio_bytes:
        try:
            result = deps.get_fusion().diagnose(
                image_bytes=image_bytes,
                audio_bytes=audio_bytes,
                user_text=text,
                dialect=dialect or "",
            )
        except ApiError:
            raise
        except ValueError as e:
            # 典型场景：音频格式不被语音接口接受，lib 层已给出人话说明
            raise ApiError(str(e), CODE_BAD_REQUEST) from e
        except Exception as e:  # noqa: BLE001
            logger.exception("快速版诊断失败")
            raise ApiError(f"诊断失败：{e}", CODE_UPSTREAM_ERROR, http_status=502) from e

        payload = _diagnosis_reply(result)
        asr = result.get("asr") or {}
        payload["asr"] = {
            "text": asr.get("text") or "",
            "dialect_name": asr.get("dialect_name") or "",
            "mock": bool(asr.get("mock")),
        } if audio_bytes else None
        # 说了话但一个字没转出来：大概率是没听清（也可能是语音服务没配），
        # 这时说「没看清照片」是答非所问，要换成对语音的说法。
        if (audio_bytes and payload["reply"]["kind"] == "unknown"
                and not (payload["asr"] or {}).get("text")):
            payload["reply"]["text"] = (
                "没听清你说什么，靠近一点再说一遍；也可以直接打字描述，"
                "或者拍张病叶的照片给我看。"
            )
        payload["degraded"] = result.get("degraded") or {}
        payload["provider"] = result.get("llm_provider") or ""
        logger.info("快速版诊断：%s（%.2f%%）命中卡片 %d 张",
                    payload["reply"]["title"] or "无结论",
                    payload["reply"]["confidence"] * 100, len(payload["cards"]))
        return {"code": 0, "msg": "ok", "data": payload}

    # ---------------- 纯文字 ----------------
    llm = deps.get_llm()
    items = deps.load_library()["items"]
    degraded = {"llm": False, "rag": False, "voice": False, "pest": False}

    # 0) 政策类问题：按原文作答，并附发布单位与原文地址。
    #    放在追问之前 —— 上一轮诊断过的病害不该影响「今年有什么补贴」这类问题。
    try:
        policy = _policy_reply(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("政策问答失败，回落到普通问答：%s", e)
        policy = None
    if policy:
        return {
            "code": 0, "msg": "ok",
            "data": {
                "reply": policy["reply"],
                "cards": policy["cards"],
                "context": "",
                "asr": None, "degraded": degraded,
                "provider": getattr(llm, "provider", ""),
            },
        }

    # 1) 上一轮有结论：这是一句追问（「要不要马上打药」），带着结论去答
    ctx_prompt = _context_prompt(prior)
    if ctx_prompt:
        answer = ""
        try:
            # 传问答提示：诊断那套要求「必须输出严格 JSON」，拿来追问会让模型
            # 把答案包成 JSON，气泡里显示的就是一坨花括号
            answer = _plain_answer(
                llm.answer(text, context=ctx_prompt, system=QA_SYSTEM_PROMPT)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("追问回答失败：%s", e)
            degraded["llm"] = True
        cards = [_card_brief(it) for it in items if it.get("id") in set(prior.get("card_ids") or [])]
        if not answer:
            answer = "这会儿没能连上大模型，下面这几张卡片里有你要的答案。"
        return {
            "code": 0, "msg": "ok",
            "data": {
                "reply": {
                    "kind": "answer", "title": "", "confidence": 0.0,
                    "text": _one_line(answer, 260), "points": [], "note": "",
                    "card_url": cards[0]["url"] if cards else "",
                },
                "cards": cards,
                # 追问不改变结论，上下文原样带回去，下一轮还能接着问
                "context": json.dumps(prior, ensure_ascii=False) if prior else "",
                "asr": None, "degraded": degraded,
                "provider": getattr(llm, "provider", ""),
            },
        }

    # 2) 没结论也没追问：先在知识库里按病名/作物找卡片
    cards = _keyword_cards(items, text)
    if cards:
        first = cards[0]
        reply_text = first["summary"] or f"知识库里有「{first['title']}」的防治办法。"
        no = str(first.get("id") or "").split("_")[-1]
        return {
            "code": 0, "msg": "ok",
            "data": {
                "reply": {
                    "kind": "answer",
                    "title": first["title"],
                    "confidence": 0.0,
                    "text": reply_text,
                    "points": [],
                    "note": "这是按你的描述在知识库里找的，拍张照片能判断得更准。",
                    "card_url": first["url"],
                    "sources": [{
                        "label": f"知识图书库《{first['title']}》",
                        "detail": f"卡片编号 #{no}" if no else "",
                        "url": first["url"],
                    }],
                },
                "cards": cards,
                "context": _context_json(
                    name=first["title"], disease_name=first["title"],
                    crop=first["crop"], confidence=0.0, summary=reply_text, cards=cards,
                ),
                "asr": None, "degraded": degraded,
                "provider": getattr(llm, "provider", ""),
            },
        }

    # 3) 什么都没命中：当普通聊天答，别硬套一个病名
    answer = ""
    try:
        answer = _plain_answer(llm.reply(text, context="快速版聊天"))
    except Exception as e:  # noqa: BLE001
        logger.warning("快速版闲聊失败：%s", e)
    if not answer:
        degraded["llm"] = True
        answer = "我是小禾。你可以拍张作物照片，或者说说叶子上有什么症状，我帮你看看是什么病。"
    return {
        "code": 0, "msg": "ok",
        "data": {
            "reply": {
                "kind": "chat", "title": "", "confidence": 0.0,
                "text": _one_line(answer, 260), "points": [], "note": "", "card_url": "",
            },
            "cards": [], "context": "", "asr": None,
            "degraded": degraded, "provider": getattr(llm, "provider", ""),
        },
    }
