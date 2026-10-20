"""页面意图路由：关键词优先，大模型兜底。

宠物助手只需要回答「这句话想去哪个功能页」。规则能覆盖绝大多数说法，命中就直接返回
（零延迟、不花 token）；只有规则拿不准时才让大模型判断。

**为什么把 `chat` 也放进闭集**：大模型可能给出一个不存在的页面名。这里只接受
`INTENTS` 里的键，其余一律当作闲聊，由调用方回退到模板话术。
"""

from __future__ import annotations

from urllib.parse import quote

# 意图 -> 目标页面。只使用仓库里真实存在的页面。
INTENTS: dict[str, str] = {
    "diagnose": "/diagnose.html",
    "library": "/library.html",
    "policy": "/policy.html",
    "farm": "/farm.html",
    "home": "/",
}

# 关键词按「越具体越靠前」排序；匹配时比较的是**命中的最长关键词长度**，
# 所以「虫害知识」会走 library（命中 2 字的「虫害」+「知识」）而不是 diagnose（命中 1 字的「虫」）。
KEYWORDS: dict[str, tuple[str, ...]] = {
    "policy": ("政策", "补贴", "补助", "惠农", "农业保险", "农机购置", "文件"),
    "farm": ("我的田", "田块", "农田", "积温", "收获", "农事", "农活", "记账",
             "施肥", "灌溉", "打药", "播种", "长势", "成熟"),
    "library": ("知识", "图书", "资料", "防治", "怎么治", "用什么药",
                "病害", "虫害", "查一下", "查查", "有什么药"),
    "diagnose": ("诊断", "看病", "识别", "拍照", "拍张", "照片",
                 "叶子", "病斑", "症状", "虫"),
    "home": ("首页", "地图", "当季", "top3"),
}

CHAT = "chat"


def _none() -> dict:
    return {"intent": CHAT, "confidence": 0.0, "source": "default", "route": "", "slots": {}}


def _query_terms(text: str) -> list[str]:
    """从农技词表里抽出查询词，用于 library 的 `?q=`。失败的场景一律返回空列表。"""
    try:
        from src.voice.lexicon import find_terms

        return find_terms(text, limit=3)
    except Exception:  # noqa: BLE001 —— 词表坏了不该让助手整体不可用
        return []


def classify_by_rules(text: str) -> dict:
    """纯规则分类。拿不准时返回 intent="chat"。"""
    text = (text or "").strip()
    if not text:
        return _none()

    best, best_len, best_hits = CHAT, 0, []
    for intent, words in KEYWORDS.items():
        hits = [w for w in words if w in text]
        if not hits:
            continue
        longest = max(len(w) for w in hits)
        if longest > best_len:
            best, best_len, best_hits = intent, longest, hits

    if best == CHAT:
        return _none()

    slots: dict[str, str] = {}
    route = INTENTS[best]
    if best == "library":
        terms = _query_terms(text)
        if terms:
            slots["query"] = terms[0]
            route = f"{route}?q={quote(terms[0])}"

    # 置信度只用于「规则是否足够确定」的判断：最长命中词越长、命中的词越多，越确定
    confidence = min(0.95, 0.55 + 0.08 * best_len + 0.05 * len(best_hits))
    return {
        "intent": best,
        "confidence": round(confidence, 3),
        "source": "rule",
        "route": route,
        "slots": slots,
    }


def route(text: str, *, llm=None, threshold: float = 0.55) -> dict:
    """规则优先；规则不确定时调大模型；都不行就当闲聊。"""
    rule = classify_by_rules(text)
    if rule["intent"] != CHAT and rule["confidence"] >= threshold:
        return rule

    if llm is not None:
        try:
            data = llm.classify_intent(text, list(INTENTS))
        except Exception:  # noqa: BLE001
            data = {}
        name = str((data or {}).get("intent") or "")
        if name in INTENTS:
            return {
                "intent": name,
                "confidence": float(data.get("confidence") or 0.7),
                "source": "llm",
                "route": INTENTS[name],
                "slots": {},
            }

    return rule
