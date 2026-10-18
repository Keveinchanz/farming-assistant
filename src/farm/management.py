"""施肥记录 → 对成熟（收获）日期的影响量。

【为什么要这个模块】

生长模型只吃「作物配置 + 逐日气温 + 播期」三样东西，`logs` 里那些 `type='fertilize'`
的记录**从来没有进过任何计算**。于是同一个作物、同一个播期、同一天，施了三遍肥和
一遍没施，页面给出的收获日一模一样 —— 这显然不对。这里补上那条链路。

【曲线是 U 形，不是单调的 —— 这一点曾经搞错过】

第一版想当然地写成「施肥 → 推迟」的单调曲线，被推翻了两次。查文献之后正确的形状是：

    缺素（尤其缺氮、缺磷）→ **推迟**成熟
    养分适量            → **提前**成熟
    过量（尤其氮过量）   → 又**推迟**成熟（贪青晚熟）

三条腿各有出处，逐条列在下面的 SOURCES 里。特别要说明「适量提前」那条：
水稻的营养试验显示**磷多量区幼穗分化最早、抽穗期最早，氮多量区最迟**，
养分供应充足时茎尖由营养生长转向生殖生长更早；加拿大 Beaverlodge 的小麦试验
（1950–52）里施磷酸铵也缩短了抽穗与成熟天数。

【量级：文献区间是 2–22 天，本模型取区间中段偏下】

同一条「增施氮肥推迟成熟」的结论，不同试验里的天数差着一个数量级：三江 2 号
+2 天，双季稻控释氮肥 +3~7 天，通粳 981 六天，寒地过量施氮约六天，
而福香占那篇（见 SOURCES）做到 207 kg·hm⁻² 时两年平均推迟 22 天。
本模型**不是任何一篇的复刻**，取的是中段偏下的一组锚点，并且明确写成常数，
方便日后按本地的试验数据重标定。

【零点：未施肥】

所有偏移都以「未施肥」为 0 的参照系 —— 不是「推荐量」。
因为用户看到的第一句话是「你什么都没记，那就按没施肥算」，这一点必须前后一致。

【钾不参与偏移】

钾的作用方向找不到可靠的一致性证据（有研究直接说磷钾对生育期影响不大），
所以这里只把 K₂O 算出来放进返回值供查看，**不**让它移动收获日。
算不准就别算 —— 编一个方向出来会让用户照着它做决定。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from src.common.logging_utils import setup_logger

logger = setup_logger("agri.farm.management")

# ---------------------------------------------------------------- 肥料成分表

# 商品重量 → 纯养分质量的比例（不是百分数的写法，直接用 0.46 = 46%）。
# 取的是国标/行业常规含量：尿素 46% N、过磷酸钙 16% P₂O₅、氯化钾 60% K₂O 等。
# 「有机肥 / 农家肥」养分低且因物料而异，这里取一个保守的典型值并在 note 里
# 说明这是估算 —— 它本来也不是靠养分含量起作用的。
FERTILIZERS: dict[str, tuple[float, float, float]] = {
    "尿素":       (0.46, 0.00, 0.00),
    "碳酸氢铵":   (0.17, 0.00, 0.00),
    "硫酸铵":     (0.21, 0.00, 0.00),
    "硝酸铵":     (0.34, 0.00, 0.00),
    "氯化铵":     (0.25, 0.00, 0.00),
    "复合肥":     (0.15, 0.15, 0.15),   # 15-15-15 通用型
    "过磷酸钙":   (0.00, 0.16, 0.00),
    "钙镁磷肥":   (0.00, 0.17, 0.00),
    "磷酸二铵":   (0.18, 0.46, 0.00),
    "氯化钾":     (0.00, 0.00, 0.60),
    "硫酸钾":     (0.00, 0.00, 0.50),
    "有机肥":     (0.010, 0.005, 0.005),
    "农家肥":     (0.005, 0.003, 0.005),
}

# 口语别名 → 成分表里的键。只放**能指认到具体产品**的词：
#   · 「氮肥 / 磷肥 / 钾肥」是养分类型，按该类型最常用的单品折算，并在 note 里说明；
#   · 「化肥」太泛，按 15-15-15 通用复合肥折算，同样在 note 里说明。
# 不认识的农资名（比如「某某牌叶面肥」）**不猜**，记进 unparsed 由界面如实提示。
ALIASES: dict[str, str] = {
    "氮肥": "尿素",
    "磷肥": "过磷酸钙",
    "钾肥": "氯化钾",
    "化肥": "复合肥",
    "碳铵": "碳酸氢铵",
    "尿素硝铵": "硝酸铵",
}

# 肥料名可能在更长的一段文本里出现（item/note 都是自由文本），
# 所以按**长度降序**做包含匹配：先让「复合肥」「过磷酸钙」这种长名命中，
# 免得被「钾肥」「磷肥」这类短名从中间截胡。
# 词表就是上面两张表的**并集** —— 不含 voice_parse.ITEM_WORDS 里的农药、种子、
# 地膜那些非肥料的词，它们在这里出现只会污染判断。
_NAME_ORDER = sorted({*FERTILIZERS, *ALIASES}, key=len, reverse=True)

# ---------------------------------------------------------------- 数量解析

# 质量单位 → 千克。只收质量单位：「袋 / 包 / 瓶 / 桶」没有标准重量，
# 换算它们要猜每袋多少公斤，猜出来的数字比不换算更糟（见 7.8.3 的同一原则）。
MASS_UNITS: dict[str, float] = {
    "吨": 1000.0,
    "公斤": 1.0,
    "千克": 1.0,
    "kg": 1.0,
    "KG": 1.0,
    "斤": 0.5,
    "克": 0.001,
    "g": 0.001,
}

# 非质量单位的计数词：认得出，但换算不了 —— 单独报出来，别当成 0 悄悄吞掉
COUNT_UNITS = ("袋", "包", "瓶", "桶", "筐", "车", "把")

_AMOUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(MASS_UNITS, key=len, reverse=True)) + r")"
)
_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + "|".join(COUNT_UNITS) + r")")
_PER_MU_RE = re.compile(r"每亩|亩均|/\s*亩|一亩地?")


def normalize_item(text: str) -> str | None:
    """从自由文本里认出肥料名，返回成分表的键；认不出返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    for name in _NAME_ORDER:
        if name in t:
            return ALIASES.get(name, name)
    return None


def parse_amount(text: str) -> tuple[float | None, str]:
    """解析数量，返回 (千克, 说明)。

    认不出质量单位时返回 (None, 原因) —— 与 7.8.3 语音解析同一条原则：
    「说出来的数字听清了，但单位换算不了」和「没说数量」是两件事，
    前者要如实告诉用户它没被计入。
    """
    t = (text or "").strip()
    if not t:
        return None, "没写数量"
    m = _AMOUNT_RE.search(t)
    if m:
        return float(m.group(1)) * MASS_UNITS[m.group(2)], ""
    m2 = _COUNT_RE.search(t)
    if m2:
        return None, f"「{m2.group(0)}」是计数不是质量，没有每{m2.group(2)}多少公斤的换算依据"
    return None, "没能从文本里认出数量"


def is_per_mu(text: str) -> bool:
    """文本里是否说明是「每亩」用量。

    只在出现「每亩 / 亩均 / 20斤/亩」这类**明确的亩用量说法**时才当真。
    写成「2 亩地，20 斤尿素」的不算 —— 那是整块地的总量，要按面积摊。
    """
    return bool(_PER_MU_RE.search(text or ""))


# ---------------------------------------------------------------- 汇总

def nutrition_from_logs(
    logs: list[dict[str, Any]], *, area_mu: float = 0.0, sow_date: str = ""
) -> dict:
    """把农事记录里的施肥条目汇总成 kg/亩的纯养分。

    · 只认 `type == 'fertilize'`；
    · 播种前的记录不计（清园、底肥在播前施的情况这里不涉及，示例田也没有）；
    · 没写「每亩」的按**整块田的总量**算，再除以面积；
    · 面积是 0（用户没填）时按 1 亩算，并置 `assumed_area` 让界面能说明这一点。
    """
    area = float(area_mu or 0.0)
    assumed_area = area <= 0
    if assumed_area:
        area = 1.0

    per_mu = [0.0, 0.0, 0.0]
    total = [0.0, 0.0, 0.0]
    used = 0
    unparsed: list[str] = []
    assumptions: list[str] = []

    for row in logs or []:
        if (row.get("type") or "") != "fertilize":
            continue
        day = str(row.get("date") or "")
        if sow_date and day and day < sow_date:
            continue

        item_text = " ".join(str(row.get(k) or "") for k in ("item", "note"))
        key = normalize_item(item_text)
        if key is None:
            unparsed.append((item_text.strip() or "（未写肥料名）"))
            continue

        kg, why = parse_amount(str(row.get("amount") or "") or item_text)
        if kg is None:
            unparsed.append(f"{key}（{why}）")
            continue

        pct = FERTILIZERS.get(key)
        if pct is None:
            unparsed.append(item_text.strip() or "（未识别）")
            continue
        if is_per_mu(item_text + " " + str(row.get("amount") or "")):
            for i in range(3):
                per_mu[i] += kg * pct[i]
        else:
            for i in range(3):
                total[i] += kg * pct[i]
        used += 1
        if key in ("复合肥",) and "化肥" in item_text and "复合肥" not in item_text:
            assumptions.append("记的是「化肥」，按 15-15-15 通用复合肥折算")
        elif key in ("尿素", "过磷酸钙", "氯化钾") and key not in item_text:
            assumptions.append(f"记的是养分类型，按该类型最常用的单品（{key}）折算")

    n = per_mu[0] + total[0] / area
    p = per_mu[1] + total[1] / area
    k = per_mu[2] + total[2] / area

    return {
        "has_records": used > 0,
        "records": used,
        "n": round(n, 3),
        "p2o5": round(p, 3),
        "k2o": round(k, 3),
        "area_mu": round(area, 3),
        "assumed_area": assumed_area,
        "unparsed": unparsed,
        "assumptions": sorted(set(assumptions)),
    }


def field_nutrition(conn: sqlite3.Connection, user_id: int, field: dict) -> dict:
    """读这块田的施肥记录并汇总。只读，不写任何表。"""
    from src.farm import repo

    try:
        logs = repo.list_logs(conn, user_id, field_id=field.get("id"), limit=500)
    except Exception as e:  # noqa: BLE001
        # 读不到记录不该让整条收获预测挂掉 —— 退回「未记录施肥」并把原因带出去
        logger.warning("读取施肥记录失败，按未记录处理：%s", e)
        logs = []

    return nutrition_from_logs(
        logs,
        area_mu=float(field.get("area_mu") or 0.0),
        sow_date=str(field.get("sow_date") or ""),
    )


# ---------------------------------------------------------------- U 形曲线

# 氮的锚点：(实际氮 / 推荐氮, 天数)。零点 = 未施肥。
#   0.0 → +2.5（缺素。文献里严重缺氮能推迟两三周，这里取区间偏下的值，
#              因为"没记录施肥"不等于"真的没施过肥"，挪得越小越不容易误伤）
#   1.0 → −2.0（适量：养分供应充足，茎尖更早转向生殖生长）
#   2.0 → +7.0（超量一倍：贪青晚熟）
# 两段之间线性插值，r > 2 保持 +7 封顶。
N_ANCHORS: tuple[tuple[float, float], ...] = ((0.0, 2.5), (1.0, -2.0), (2.0, 7.0))

# 缺磷的附加项：磷没到推荐量时，按缺口比例最多再加 1.5 天。
# 之所以是**附加**而不是并进 U 形曲线：缺磷的作用与缺氮同向（都推迟），
# 但证据量级不同（缺磷明确延迟抽穗成熟，但拿不到"几亩缺多少磷推迟几天"的定量），
# 所以给一个小额的、封了顶的补充项，不让它主导结果。
P_PENALTY_MAX = 1.5

# 总偏移的硬夹子。上下都夹：往前的 −2 附近有「适量提前」的底，
# 往后的 +9 是给「严重缺素 + 超量施肥」这种叠加情形留的余量。
SHIFT_MIN, SHIFT_MAX = -3.0, 9.0

# 判定档位的氮充足度分界
DEFICIT_RATIO = 0.67      # 低于此 → 缺肥
EXCESS_RATIO = 1.5        # 高于此 → 过量

SOURCES: tuple[str, ...] = (
    "王颖姮等《施氮量对优质稻“福香占”光合特性、产量及品质的影响》"
    "（中国水稻科学 2023, 37(1):89-101，DOI 10.16819/j.1001-7216.2023.220606）："
    "施氮 0/51.75/103.5/155.25/207 kg·hm⁻² 对应两年平均推迟成熟 0/5.5/9.5/14/22 天。",
    "水稻营养与组织学试验：磷多量区幼穗分化最早、抽穗期最早，氮多量区最迟 —— "
    "即养分适量时代谢更早转向生殖生长。",
    "缺磷明确延迟抽穗与成熟；加拿大 Beaverlodge 小麦多年试验（1950–52）中，"
    "施磷酸铵减少了抽穗与成熟天数。",
    "玉米营养生长期缺氮推迟抽雄吐丝，早期（六叶前）缺氮可推迟成熟达 3 周。",
    "其它施氮量试验的天数区间：三江 2 号 +2 天、双季稻控释氮肥 +3~7 天、"
    "通粳 981 约 6 天、寒地过量施氮约 6 天（同一施氮量在不同试验里差一个数量级，"
    "本模型取的是区间中段偏下，不是任何单篇的复刻）。",
)


def _interp(r: float, anchors: tuple[tuple[float, float], ...]) -> float:
    if r <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= r <= x1:
            return y0 + (y1 - y0) * (r - x0) / (x1 - x0)
    return anchors[-1][1]


def maturity_shift_days(nutrition: dict, crop_cfg: dict) -> dict:
    """算出这块田的施肥情况让成熟日移动多少天（正 = 推迟，负 = 提前）。

    返回的 days 是**浮点**，调用方按自己的粒度取整；未配置推荐施肥量的作物
    一律返回 0，并在 note 里说明理由 —— 不装作算过。
    """
    rec = (crop_cfg or {}).get("nutrition") or {}
    n_ref = float(rec.get("n") or 0.0)
    p_ref = float(rec.get("p2o5") or 0.0)

    base = {
        "days": 0.0, "level": "未知", "n_ratio": None, "p_ratio": None,
        "note": "", "sources": [],
    }

    if n_ref <= 0 or p_ref <= 0:
        base["note"] = "这个作物没有配置推荐施肥量，施肥影响未计入"
        return base

    n_act = float(nutrition.get("n") or 0.0)
    p_act = float(nutrition.get("p2o5") or 0.0)
    r_n = n_act / n_ref
    r_p = p_act / p_ref

    n_shift = _interp(r_n, N_ANCHORS)
    p_penalty = P_PENALTY_MAX * max(0.0, 1.0 - min(r_p, 1.0))
    days = max(SHIFT_MIN, min(SHIFT_MAX, n_shift + p_penalty))

    if not nutrition.get("has_records"):
        level = "未施肥"
    elif r_n < DEFICIT_RATIO or r_p < DEFICIT_RATIO:
        level = "缺肥"
    elif r_n > EXCESS_RATIO:
        level = "过量"
    else:
        level = "适量"

    note = _note_for(level, days, nutrition, r_n, r_p)

    return {
        "days": round(days, 2),
        "level": level,
        "n_ratio": round(r_n, 3),
        "p_ratio": round(r_p, 3),
        "note": note,
        "sources": list(SOURCES),
    }


def _note_for(level: str, days: float, nutrition: dict, r_n: float, r_p: float) -> str:
    """给界面用的一句话说明。说清「为什么动」，也要说清「没记的部分不算数」。"""
    if abs(days) < 0.5:
        head = "施肥情况对收获日的影响不足半天，未调整"
    elif days > 0:
        head = f"按记录的施肥情况，收获日推后约 {abs(days):.1f} 天"
    else:
        head = f"按记录的施肥情况，收获日提前约 {abs(days):.1f} 天"

    why = {
        "未施肥": "没有查到播种后的施肥记录，按缺素处理（缺氮、缺磷都会推迟成熟）",
        "缺肥": f"氮、磷都没到推荐量（氮 {r_n:.0%}、磷 {r_p:.0%}），缺素推迟成熟",
        "适量": f"氮、磷接近推荐量（氮 {r_n:.0%}、磷 {r_p:.0%}），养分适量时成熟略早",
        "过量": f"氮肥已超过推荐量（{r_n:.0%}），过量施氮会贪青晚熟",
    }.get(level, "")

    tail = []
    if nutrition.get("has_records"):
        tail.append(f"计入 {nutrition['records']} 条施肥记录")
    if nutrition.get("assumed_area"):
        tail.append("田块没填面积，按 1 亩折算")
    for a in nutrition.get("assumptions") or []:
        tail.append(a)
    if nutrition.get("unparsed"):
        tail.append(f"另有 {len(nutrition['unparsed'])} 条施肥记录没能识别出用量，未计入")

    return "；".join([p for p in [head, why, *tail] if p])
