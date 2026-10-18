"""阶段 7 / 7.8.3 —— 语音文字 → 结构化农事字段。

**规则优先，LLM 只兜底。** 顺序不能反：
    · 规则解析是确定性的、可测的、离线可用的。农田里信号不好是常态，
      一句「昨天施了 20 斤尿素」不该因为连不上大模型就解析不出来；
    · LLM 兜底只处理规则盖不到的说法，且**输出同样要过词表校验** ——
      大模型编出来的「打了三斤敌敌畏」不能因为语句通顺就被采信。

解析不出来的处理（7.8.2）：**返回原文 + 空字段 + 明确原因，绝不猜**。
界面上用户看到的是「没听出是哪块田 / 没听出是哪种农事」，然后自己补，
而不是被塞进一个瞎猜的值再点确认。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from src.common.logging_utils import setup_logger
from src.farm.timeutil import is_date, today_local

logger = setup_logger("agri.farm.voice")

# 动作词表（7.8.3）→ 落库用的 logs.type
ACTION_WORDS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("施肥", "施了", "撒了", "追肥", "上了肥", "下肥"), "fertilize", "施肥"),
    (("打药", "喷药", "打了", "喷了", "打了一遍"), "spray", "打药"),
    (("浇水", "浇了", "灌溉", "灌了", "放水"), "irrigate", "灌溉"),
    (("播种", "种了", "下种", "栽了", "插秧"), "sow", "播种"),
    (("收割", "收了", "采摘", "摘了", "割了"), "harvest", "收获"),
)

# 单位词表（7.8.3）。数量保留原样文本（「20 斤」），不做单位换算 ——
# 换算需要密度/浓度信息，猜出来的数字比原话更糟。
UNIT_WORDS = ("公斤", "千克", "斤", "克", "袋", "瓶", "亩", "升", "桶", "包")

# 常见农资名，用来把「20 斤尿素」里的「尿素」认出来。
# **不放单字词**：曾经这里有个「水」，于是「今天收了水稻」会被认成「水」——
# 单字名词会命中在更长的词里面（水稻、水果），认出来的东西还不如不认。
ITEM_WORDS = (
    "尿素", "复合肥", "氮肥", "磷肥", "钾肥", "化肥", "有机肥", "农家肥", "追肥",
    "农药", "杀虫剂", "杀菌剂", "除草剂", "敌敌畏", "多菌灵", "代森锰锌",
    "种子", "秧苗", "地膜", "石灰",
)

# 「三天前」这类相对日期
RELATIVE_DAYS = (
    ("今天", 0), ("今日", 0), ("昨天", 1), ("昨日", 1), ("前天", 2),
    ("大前天", 3), ("三天前", 3),
)

WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + "|".join(UNIT_WORDS) + r")")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]")
_DAYS_AGO_RE = re.compile(r"(\d{1,3})\s*天前")
_HOURS_AGO_RE = re.compile(r"(\d{1,3})\s*(?:个)?小时前")
_LAST_WEEK_RE = re.compile(r"上周([一二三四五六日天])")


# ---------------------------------------------------------------- 日期

def parse_date(text: str, *, today: date | None = None) -> tuple[str, str]:
    """从话里解析日期，返回 (YYYY-MM-DD, 命中的原词)；解析不出返回 ('', '')。

    解析不出时**返回空串而不是今天** —— 「没听出是哪天」和「就是今天」是两件事，
    默认成今天会让用户在不注意时记错日期。
    """
    today = today or today_local()

    m = _HOURS_AGO_RE.search(text)
    if m:
        # 「两小时前」跨零点时其实是昨天。按小时数往回推最不容易错。
        from datetime import datetime
        moment = datetime.combine(today, datetime.min.time()) - timedelta(hours=int(m.group(1)))
        return moment.date().isoformat(), m.group(0)

    for word, back in RELATIVE_DAYS:
        if word in text:
            return (today - timedelta(days=back)).isoformat(), word

    m = _DAYS_AGO_RE.search(text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat(), m.group(0)

    m = _LAST_WEEK_RE.search(text)
    if m:
        target = WEEKDAYS[m.group(1)]
        # 本周一为基准，再往前一周
        this_monday = today - timedelta(days=today.weekday())
        return (this_monday - timedelta(days=7) + timedelta(days=target)).isoformat(), m.group(0)

    m = _MONTH_DAY_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            guess = date(today.year, month, day)
        except ValueError:
            return "", ""
        # 「4月15号」在 10 月说，指的是今年 4 月；但在 2 月说「12月20号」，
        # 只能是去年 —— 未来的日期不可能是"已经做过的事"。
        if guess > today:
            try:
                guess = date(today.year - 1, month, day)
            except ValueError:
                return "", ""
        return guess.isoformat(), m.group(0)

    if is_date(text.strip()):
        return text.strip(), text.strip()

    return "", ""


# ---------------------------------------------------------------- 动作 / 数量 / 物品

def parse_action(text: str) -> tuple[str, str, str]:
    """返回 (type, 中文名, 命中词)；认不出返回 ('', '', '')。"""
    for words, type_, name in ACTION_WORDS:
        for word in words:
            if word in text:
                return type_, name, word
    return "", "", ""


def parse_amount(text: str) -> tuple[str, str]:
    """返回 (数量文本, 命中词)。保留原样，不做单位换算。"""
    m = _AMOUNT_RE.search(text)
    return (m.group(0).replace(" ", ""), m.group(0)) if m else ("", "")


def parse_item(text: str, *, amount: str = "", action_word: str = "") -> str:
    """认物品名。先查词表；查不到就取数量词后面那一小段（最多 4 个字）。"""
    for word in ITEM_WORDS:
        if word in text:
            return word

    if amount:
        i = text.find(amount)
        if i >= 0:
            tail = text[i + len(amount):]
            # 砍掉后面的语气词与标点
            tail = re.split(r"[，,。.！!？?、\s]", tail)[0]
            tail = re.sub(r"(了|吧|呗|啊|呢|的|一下)$", "", tail.strip())
            if 1 <= len(tail) <= 6:
                return tail

    # 「打了除草剂」这类：动作词后面紧跟的名词
    if action_word:
        i = text.find(action_word)
        if i >= 0:
            tail = re.split(r"[，,。.！!？?、\s]", text[i + len(action_word):])[0]
            tail = re.sub(r"(了|吧|呗|啊|呢)$", "", tail.strip())
            if 1 <= len(tail) <= 6:
                return tail
    return ""


def match_field(text: str, fields: list[dict]) -> tuple[str, str, list[str]]:
    """把话里的田块名对上一块田，返回 (field_id, 田块名, 歧义候选名列表)。

    **这里不能用「谁的名字在话里出现过」这种布尔判断。** 示例田的名字是
    「示例田 · 水稻」「示例田 · 玉米」「示例田 · 油菜」，三块田共享「示例田」这个前缀 ——
    说「给示例田水稻施肥」时三块田都"命中"了，而先遍历到谁就返回谁。
    实测就出过这个错：那句话被记到了玉米田上。

    改成**按区分度打分**：名字的每个子串只要在话里出现就计分，
    得分 = 长度² ÷ 有多少块田的名字也含这个子串。
    「示例田」三块田都有 → 9/3 = 3 分；「水稻」只有一块田有 → 4/1 = 4 分。
    于是水稻田总得 7 分、玉米田 3 分，选水稻 —— 与人的判断一致。

    最高分并列时**返回空**（并把候选名交出去），由用户自己选：
    宁可多问一句，也不能把农事记到别人家的田里。
    """
    if not fields:
        return "", "", []

    scores: dict[str, float] = {}
    for f in fields:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        score = 0.0
        for size in range(min(len(name), 8), 1, -1):
            for i in range(len(name) - size + 1):
                piece = name[i:i + size]
                if piece in text:
                    shared = sum(1 for g in fields if piece in (g.get("name") or ""))
                    score += (size * size) / max(shared, 1)
        if score > 0:
            scores[f["id"]] = score

    if not scores:
        return "", "", []

    best = max(scores.values())
    winners = [fid for fid, sc in scores.items() if abs(sc - best) < 1e-9]
    if len(winners) > 1:
        names = [f.get("name", "") for f in fields if f["id"] in winners]
        return "", "", names

    fid = winners[0]
    return fid, next(f.get("name", "") for f in fields if f["id"] == fid), []


# ---------------------------------------------------------------- 主入口

def parse(text: str, fields: list[dict], *, today: date | None = None) -> dict:
    """解析一句话，返回 VoiceParseData 的形状。

    `ok` 表示**够不够落库**：至少要认出「哪块田 + 哪种农事」。
    日期认不出时用今天兜底（并如实写进 reason 提示用户核对），
    因为它是最容易被漏说的一部分（「给东头那块施了肥」完全可以不提时间）。
    """
    text = (text or "").strip()
    matched: list[str] = []

    if not text:
        return _empty("没有听到内容，请重新说一遍")

    type_, type_name, action_word = parse_action(text)
    if action_word:
        matched.append(action_word)

    field_id, field_name, ambiguous = match_field(text, fields)
    if field_name:
        matched.append(field_name)

    date_str, date_word = parse_date(text, today=today)
    if date_word:
        matched.append(date_word)

    amount, amount_word = parse_amount(text)
    if amount_word:
        matched.append(amount_word)

    item = parse_item(text, amount=amount, action_word=action_word)
    if item:
        matched.append(item)

    notes: list[str] = []
    if not type_:
        notes.append("没听出是哪种农事（施肥 / 打药 / 灌溉 / 播种 / 收获）")
    if ambiguous:
        notes.append("说的田块名对上了好几块（" + "、".join(ambiguous) + "），请手动选一块")
    elif not field_id:
        notes.append("没听出是哪块田")

    if not date_str:
        date_str = (today or today_local()).isoformat()
        notes.append(f"没听出日期，先按下今天（{date_str}）算，请核对")

    ok = bool(type_ and field_id)
    if not ok and not notes:
        notes.append("这句话里没有能落库的农事信息")

    return {
        "text": text,
        "fields": {
            "field_id": field_id, "field_name": field_name,
            "type": type_, "type_name": type_name,
            "date": date_str, "item": item, "amount": amount,
        },
        "ok": ok,
        "reason": "；".join(notes),
        "matched": matched,
    }


def _empty(reason: str) -> dict:
    return {
        "text": "", "fields": {
            "field_id": "", "field_name": "", "type": "", "type_name": "",
            "date": "", "item": "", "amount": "",
        },
        "ok": False, "reason": reason, "matched": [],
    }


# ---------------------------------------------------------------- LLM 兜底

LLM_PROMPT = """你是农事记录解析器。从农户的一句话里抽出结构化字段，只输出 JSON。

允许的字段与取值范围（**只能用下面这些值，拿不准就留空**）：
  field_name  字符串，田块名，必须原句出现过
  type        sow|fertilize|spray|irrigate|harvest|other
  date        YYYY-MM-DD
  item        字符串，农资名
  amount      字符串，保留原样（如"20斤"）

只输出 JSON，不要解释。认不出来就对应字段留空字符串。"""


def parse_with_llm(text: str, base: dict, fields: list[dict], *, llm=None) -> dict:
    """规则没认全时，让大模型补一补（7.8.3 的 LLM 兜底）。

    **大模型的输出照样要过词表**：type 必须在 LOG_TYPES 里，
    field_name 必须真的是该用户的某块田，date 必须是合法日期。
    校验不过就丢掉那一个字段，而不是连带丢掉用户已经说对的部分。
    """
    if llm is None:
        try:
            from src.llm.factory import get_llm

            llm = get_llm()
        except Exception as e:  # noqa: BLE001
            logger.info("大模型不可用，跳过兜底解析：%s", e)
            return base

    names = "、".join(f.get("name", "") for f in fields) or "（尚无可选田块）"
    try:
        # 用 LLM 公开契约里的 answer()，不碰各 provider 私有的 _chat ——
        # 私有方法只有 DeepSeek 那份实现，Mock 上没有，走它等于把兜底路径
        # 绑死在某一个 provider 上。
        raw = llm.answer(
            f"可选田块：{names}\n\n农户的话：{text}",
            context=LLM_PROMPT,
        )
        data = _extract_json(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("大模型兜底解析失败：%s", e)
        return base

    if not data:
        return base

    from src.farm.repo import LOG_TYPES

    out = {**base, "fields": dict(base["fields"])}
    merged: list[str] = list(base["matched"])

    if not out["fields"]["type"] and data.get("type") in LOG_TYPES:
        out["fields"]["type"] = data["type"]
        merged.append(f"模型:{data['type']}")

    if not out["fields"]["field_id"] and data.get("field_name"):
        fid, name, _amb = match_field(str(data["field_name"]), fields)
        if fid:
            out["fields"]["field_id"] = fid
            out["fields"]["field_name"] = name
            merged.append(f"模型:{name}")

    if data.get("date") and is_date(str(data["date"])) and not base.get("_date_given"):
        out["fields"]["date"] = str(data["date"])
        merged.append(f"模型:{data['date']}")

    for key in ("item", "amount"):
        if not out["fields"][key] and data.get(key):
            out["fields"][key] = str(data[key])[:40]
            merged.append(f"模型:{data[key]}")

    out["matched"] = merged
    out["ok"] = bool(out["fields"]["type"] and out["fields"]["field_id"])
    if out["ok"]:
        out["reason"] = ""
    return out


def _extract_json(raw: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（模型常会包一层 ```json）。"""
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
