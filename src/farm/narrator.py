"""阶段 7 / 7.5 —— 大模型叙述层。

**职责边界（7.5 写得很明确，这里逐条遵守）**：
    做   —— 翻译人话、解释「为什么是这天」、对比历史、回答追问
    不做 —— 算积温、算收获日、编造农事

这条边界不是洁癖。积温与收获日已经由 gdd.py / harvest_model.py 算好了，
大模型再算一遍只会给出**另一个数**，而用户无从判断该信哪个。所以本模块把
结构化结果当**输入**，只负责把它讲成人话。提示词里也明确禁止它改数字。

**无 Key 时走模板**，且字段完全一致（7.5 的要求）—— 调用方不需要知道
这次是模型写的还是模板拼的，只有 `source` 字段如实说明。

**反幻觉**：病名必须落在 label_map.json ∪ pest_label_map.json 这个闭集里。
这是本项目一贯的做法（诊断那边靠候选列表做闭集），但叙述层没有候选列表
（它要聊的是整块田的情况），所以改成**生成后校验**：扫出所有像病名/虫名的
词，凡不在闭集里的一律判为编造，并整段退回模板 —— 与其发一段真假掺半的话，
不如退到一段朴素但每句都有据可依的模板话。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from src.common.io_utils import read_json
from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.farm.timeutil import today_str

logger = setup_logger("agri.farm.narrator")

# 像「具体病名/虫名」的词：2~8 个汉字，以病/虫/螟/虱/蛾/螨/蝇/蚜结尾。
#
# 下限取 2 而不是 3：「稻瘟病」这种三字名（2+病）是很常见的真实病名，
# 用 {3,8} 会整类漏掉，而漏掉恰恰意味着**放过一个编造的名字**。
# 当然 2 也会套住「这种病」这类通用说法 —— 那些由下面的 GENERIC 兜住。
_NAME_RE = re.compile(r"[一-龥]{2,8}(?:病|虫|螟|虱|蛾|螨|蝇|蚜)")

# 通用说法白名单：它们字面上符合上面的模式，但指的是「病」这件事本身，
# 不是某个具体病名。不放进来的话，一句正常的「这种病要早防」会被判成编造。
GENERIC = {
    "这种病", "这类病", "该病", "此病", "本病", "生病", "发病", "有病", "得病",
    "防病", "治病", "抗病", "感病", "什么病", "啥病", "咋病", "看下病", "看看病",
    "病虫", "病虫害", "虫害", "害虫", "虫子", "成虫", "幼虫", "若虫", "起虫",
    "这种病害", "该病害", "防止病害", "预防病害", "农作物病",
    "防病虫害", "病虫害防治", "虫害防治", "病害防治", "防治病虫",
}

SYSTEM_PROMPT = """你是农技助手，负责把系统算好的数据讲成农户听得懂的话。

严格约束：
1. **不许改动任何数字**（积温、天数、日期、百分比、亩数）。它们由系统算出，你只负责解释。
2. **不许编造病名或虫名**。只能在给定的「已知病虫害」里提；列表为空就不要提任何病名。
3. 不许编造农事操作。施肥、打药这类建议只能来自给定的「系统建议」。
4. 不确定就少说。宁可短，也不许猜。

输出 JSON：{"summary": "一句话现状", "why": "为什么是这个收获日期", "tips": ["...", "..."], "risks": ["..."]}
只输出 JSON，不要解释文字。"""


@lru_cache(maxsize=1)
def disease_lexicon() -> frozenset[str]:
    """闭集：271 类病害 + 102 类虫害的全部中文名。"""
    names: set[str] = set()
    for key in ("label_map", "pest_label_map"):
        try:
            data = read_json(path_of(key))
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 %s 失败，该表不参与闭集校验：%s", key, e)
            continue
        for item in (data.get("index_to_label") or {}).values():
            label = item.get("label_cn")
            if label:
                names.add(str(label))
    return frozenset(names)


def filter_hallucinated(text: str, *, allowed: frozenset[str] | None = None) -> tuple[str, list[str]]:
    """扫出不在闭集里的病名/虫名，返回 (剔除后的文本, 被剔除的词)。

    只做删除、不做替换：把「稻瘟病」换成别的名字同样是编造，
    而留一个空档会让句子的缺失暴露出来 —— 实际上调用方看到 removed 非空
    就会整段弃用、退回模板，所以这里的返回值主要供日志与测试使用。

    **判定规则是「后缀命中」而不是「整个匹配命中」**：正则是最左最长匹配，
    它会把前面的动词一起吞进来（「注意防治病虫害」整段匹配出来的是「注意防治病虫」）。
    如果拿整段去查闭集，一句正常的话会被判成编造、整段叙述退化成模板。
    所以改成：只要这个片段里**存在**一个已知的后缀（≥2 字）就算合法。
    这样「注意防治病虫」靠「病虫」放过，「叶片有甘薯瘟病」靠「甘薯瘟病」放过，
    而「得了玉米超级斑点病」的任何后缀都不在闭集里 —— 判为编造。
    """
    lexicon = allowed if allowed is not None else disease_lexicon()
    known = lexicon | GENERIC
    removed: list[str] = []

    def repl(m: re.Match) -> str:
        span = m.group(0)
        for size in range(2, len(span) + 1):
            if span[-size:] in known:
                return span          # 合法词（含前面的动词），原样保留
        removed.append(span)
        return ""

    return _NAME_RE.sub(repl, text or ""), removed


# ---------------------------------------------------------------- 模板（无 Key 时的实现）

def _template(field: dict, growth: dict, weather: dict, harvest: dict,
              reminders: list[dict]) -> dict:
    """没有大模型时的一句话叙述。**字段与模型输出完全一致**（7.5）。"""
    crop = field.get("crop", "")
    stage = growth.get("stage", "")
    progress = growth.get("progress", 0.0)
    days = growth.get("days_since_sow", 0)

    summary = (f"{field.get('name', '这块田')}种的是{crop}，播下第 {days} 天，"
               f"已累积积温 {growth.get('gdd_cum', 0):.0f} 度，"
               f"正处在「{stage}」阶段（全生育期约 {progress:.0%}）。")

    if growth.get("matured"):
        why = "积温已经达到成熟阈值，不再预测收获日期。"
    elif harvest.get("most_likely"):
        why = (f"按{harvest.get('basis', '积温法')}推算，最可能在 "
               f"{harvest['most_likely']} 前后收获"
               f"（区间 {harvest.get('earliest')} ~ {harvest.get('latest')}）。")
    else:
        why = harvest.get("basis") or "气温数据不足，暂时给不出收获日期。"

    tips = [r.get("action", "") for r in reminders if r.get("action")][:3]
    if not tips and growth.get("actions"):
        tips = list(growth["actions"])[:3]

    risks: list[str] = []
    if weather.get("degraded"):
        risks.append(weather.get("degraded_reason") or "天气数据不可用")
    if growth.get("confidence") == "低":
        risks.append("气温数据不完整，积温与阶段仅供参考")

    return {"summary": summary, "why": why, "tips": tips, "risks": risks}


# ---------------------------------------------------------------- 主入口

def _context(field: dict, growth: dict, weather: dict, harvest: dict,
             reminders: list[dict], logs: list[dict]) -> str:
    """给模型的事实清单。它只能基于这些说话。"""
    recent_logs = [
        f"{l.get('date')} {l.get('item') or l.get('type')}"
        for l in (logs or [])[:5]
    ]
    return json.dumps({
        "田块": field.get("name"),
        "作物": field.get("crop"),
        "亩数": field.get("area_mu"),
        "播种日": field.get("sow_date"),
        "播后天数": growth.get("days_since_sow"),
        "当前阶段": growth.get("stage"),
        "积温": round(growth.get("gdd_cum", 0)),
        "成熟需积温": growth.get("maturity_gdd"),
        "全生育期进度": f"{growth.get('progress', 0):.0%}",
        "天气": [{"date": d["date"], "tmax": d["tmax"], "tmin": d["tmin"],
                  "precip": d["precip"]} for d in (weather.get("days") or [])],
        "天气来源": weather.get("source_note"),
        "收获预测": {"最早": harvest.get("earliest"), "最可能": harvest.get("most_likely"),
                     "最晚": harvest.get("latest"), "依据": harvest.get("basis"),
                     "方法": harvest.get("method")},
        "系统建议": [r.get("action") for r in reminders],
        "最近农事": recent_logs,
        "已知病虫害": [],
    }, ensure_ascii=False)


def narrative(
    field: dict, growth: dict, weather: dict, harvest: dict,
    reminders: list[dict], logs: list[dict] | None = None, *, llm=None,
) -> dict:
    """生成叙述。返回 {"text", "why", "tips", "risks", "source"}。

    source ∈ {"llm", "template"}，无论哪条路，四个字段都在。
    """
    tpl = _template(field, growth, weather, harvest, reminders)

    if llm is None:
        try:
            from src.llm.factory import get_llm

            llm = get_llm()
        except Exception as e:  # noqa: BLE001
            logger.info("大模型不可用，叙述层走模板：%s", e)
            llm = None

    if llm is None or getattr(llm, "provider", "") == "mock":
        return {"text": tpl["summary"], "why": tpl["why"], "tips": tpl["tips"],
                "risks": tpl["risks"], "source": "template"}

    try:
        raw = llm.answer(
            f"请根据下面的数据，用两三句农户能听懂的话介绍这块田的现状。\n"
            f"{_context(field, growth, weather, harvest, reminders, logs or [])}",
            context=SYSTEM_PROMPT,
        )
        data = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("叙述层调用失败，退回模板：%s", e)
        return {"text": tpl["summary"], "why": tpl["why"], "tips": tpl["tips"],
                "risks": tpl["risks"], "source": "template"}

    if not data or not data.get("summary"):
        logger.info("叙述层输出无法解析，退回模板")
        return {"text": tpl["summary"], "why": tpl["why"], "tips": tpl["tips"],
                "risks": tpl["risks"], "source": "template"}

    # 反幻觉校验：整段文字（含 why/tips/risks）都要过闭集
    blob = " ".join(str(data.get(k, "")) for k in ("summary", "why")) + " " + \
           " ".join(str(x) for x in (data.get("tips") or []) + (data.get("risks") or []))
    _cleaned, removed = filter_hallucinated(blob)
    if removed:
        logger.warning("叙述层出现闭集外的病名/虫名，整段退回模板：%s", removed)
        return {"text": tpl["summary"], "why": tpl["why"], "tips": tpl["tips"],
                "risks": tpl["risks"], "source": "template"}

    return {
        "text": str(data.get("summary", "")).strip(),
        "why": str(data.get("why", "")).strip() or tpl["why"],
        "tips": [str(x) for x in (data.get("tips") or [])][:5] or tpl["tips"],
        "risks": [str(x) for x in (data.get("risks") or [])][:5] or tpl["risks"],
        "source": "llm",
    }


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(text[i:j + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None
