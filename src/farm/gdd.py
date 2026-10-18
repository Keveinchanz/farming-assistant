"""阶段 7 / 7.3.2 + 7.3.3 —— 有效积温计算与生育阶段匹配。

纯函数，不碰数据库也不联网，因此可以单独测（tests/test_units.py 里有几条）。

**四段式**（栾青等《基于线性生长假设的作物积温模型稳定性比较》的 Ae4 模型）：

    Tmean < Tbase          → 0
    Tbase ≤ Tmean ≤ Topt   → Tmean - Tbase
    Topt < Tmean ≤ Tu      → (Tu - Tmean) × (Topt - Tbase) / (Tu - Topt)
    Tmean > Tu             → 0

第三段是这套模型的关键：日均温超过最适温度后，积温**不再增加而是回落** ——
因为高温对作物发育不是「更快」，而是胁迫。用普通积温（直接减 Tbase）会把
盛夏算成超高积温，生育期整体提前，收获预测跟着一起错。

两条容易写错的边界：
    · **低温日只能是 0，不能是负数**。减出来是负的会让积温倒退，
      已经进入的阶段会自己退回去 —— 这是 7.12 专门列出来要测的一条。
    · 阶段匹配是**闭区间**：当前积温正好等于某阶段阈值时，就算已进入该阶段
      （7.3.3 的原话是「取最后一个 gdd <= 当前积温」）。写成 `<` 的话，
      示例田播种 30 天时的 400 度就不会落进分蘖期。
"""

from __future__ import annotations

from typing import Iterable

from src.farm.stages import crop_config


def daily_gdd(tmean: float, tbase: float, topt: float, tu: float) -> float:
    """单日有效积温（四段式）。参数由 stages.crop_config(crop) 提供。"""
    if tmean is None:
        return 0.0
    if tmean < tbase:
        return 0.0
    if tmean <= topt:
        return tmean - tbase
    if tmean <= tu:
        # Topt < Tu 由 stages 的校验保证，这里不会除以 0
        return (tu - tmean) * (topt - tbase) / (tu - topt)
    return 0.0


def accumulate(temps: Iterable[dict], cfg: dict) -> list[dict]:
    """把逐日气温累加成积温序列。

    入参 rows 需要 date / tmean（其余字段原样带出）。
    返回每行加上 `gdd_daily` 与 `gdd_cum`（截至当日的累积值）。

    只在**有数据的那些天**上累加：缺测的日子直接跳过（而不是补 0 或补均值）。
    补 0 会让积温偏小、阶段偏晚；补均值则是凭空造数据 —— 缺天这件事由调用方
    通过 confidence 如实告知用户。
    """
    total = 0.0
    out: list[dict] = []
    for row in temps:
        tmean = row.get("tmean")
        if tmean is None:
            continue
        add = daily_gdd(float(tmean), float(cfg["Tbase"]), float(cfg["Topt"]), float(cfg["Tu"]))
        total += add
        out.append({**row, "gdd_daily": round(add, 2), "gdd_cum": round(total, 2)})
    return out


def match_stage(crop: str, gdd_cum: float) -> dict:
    """按累积积温定位生育阶段（7.3.3）。

    返回 {stage, stage_index, progress, next_stage, next_gdd, maturity_gdd, matured}：
        · progress   —— 0~1 的全生育期进度，**封顶在 1.0**。
                        超过成熟阈值（例如示例田的油菜）不代表进度 275%，
                        而是「已完熟」，用 matured=True 表达。
        · next_stage —— 下一个阶段的名称；已完熟时为 None。
    """
    cfg = crop_config(crop)
    stages = cfg["stages"]
    maturity = float(cfg["maturity_gdd"])

    index = 0
    for i, st in enumerate(stages):
        if float(st["gdd"]) <= gdd_cum:      # 闭区间：等于阈值即算进入
            index = i
        else:
            break

    stage = stages[index]
    matured = gdd_cum >= maturity
    nxt = stages[index + 1] if index + 1 < len(stages) else None

    return {
        "stage": stage["name"],
        "stage_index": index,
        "stage_gdd": float(stage["gdd"]),
        "actions": list(stage.get("actions") or []),
        "progress": round(min(max(gdd_cum, 0.0) / maturity, 1.0), 4),
        "next_stage": nxt["name"] if nxt else None,
        "next_gdd": float(nxt["gdd"]) if nxt else None,
        "maturity_gdd": maturity,
        "matured": matured,
        # 距离成熟还差多少积温（已完熟为 0，不倒扣）
        "gdd_remain": round(max(maturity - gdd_cum, 0.0), 2),
    }


def gdd_at(temps: Iterable[dict], cfg: dict) -> float:
    """只取最终累积值（不关心逐日序列时用）。"""
    series = accumulate(temps, cfg)
    return float(series[-1]["gdd_cum"]) if series else 0.0
