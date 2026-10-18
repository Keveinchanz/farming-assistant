"""阶段 7 / 7.4 —— 农事建议引擎（短期提醒 + 收获建议 + 推荐收割窗口）。

**这个模块的第一原则是「不知道就不说」。** 7.4.1 的六条提醒里有四条依赖天气预报，
而天气是会降级的（7.2）。预报拿不到时，如果用气候基准值去套「未来 3 天有雨」，
就会对着用户斩钉截铁地说一件**我们并不知道**的事 —— 那比不给提醒糟糕得多。
所以本模块接收的是 growth.next_days() 的完整结果（带 source 字段），
只在 source == 'forecast' 时评估天气类规则，其余情况把跳过的原因写进 notes。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from src.common.logging_utils import setup_logger
from src.farm.stages import stage_actions
from src.farm.timeutil import today_local, today_str

logger = setup_logger("agri.farm.advisor")

# 阶段刚开始几天内提醒该阶段的农活（7.4.1 第一条：积温跨阶段阈值 → actions）
STAGE_FRESH_DAYS = 5
# 近期诊断的回顾窗口
DIAGNOSIS_LOOKBACK_DAYS = 10

RAIN_MM = 0.1          # 判定"有雨"的日降水量阈值（mm）
HOT_TMAX = 35.0        # 高温
HUMID = 85.0           # 高湿
DISEASE_TMIN, DISEASE_TMAX = 20.0, 30.0
# 到「该考虑收割了」的进度线。7.4.2 的表里第 5 行用的也是这个 0.8。
#
# 为什么天气那两行（「适合收割」/「雨前抢收」）必须也加这道门槛：
# 表是按优先级排列的，但它们没写进度条件 —— 而一个只长到全生育期 54% 的玉米，
# 天气再好也不该收。原样照抄会让页面在抽穗期的玉米上写「天气适合收割，可以安排」，
# 这是纯粹的错话。收获建议本就只在临近收获时才有意义。
NEAR_HARVEST = 0.8


def _advice(type_: str, action: str, *, reason: str = "", window: str = "",
            severity: str = "info", priority: str = "中") -> dict:
    """7.4.4 的输出结构。字段名是清单定死的，别改。

    `key` 是本项目加的：7.7 要求「[改天] snooze 2 天」，而暂存要按**具体的某条提醒**
    来记。用 type+action 拼一个稳定的标识 —— 不掺日期，否则明天同样的提醒就是另一个 key，
    暂存也就失效了。
    """
    return {
        "key": f"{type_}|{action}"[:120],
        "type": type_, "reason": reason, "action": action,
        "window": window, "severity": severity, "priority": priority,
    }


def _rain_days(days: list[dict]) -> list[str]:
    return [d["date"] for d in days if d.get("precip", 0.0) >= RAIN_MM]


def stage_entered_on(series: list[dict], stage_gdd: float) -> str | None:
    """积温**首次**达到该阶段阈值的那一天。

    有了它才能说「你刚进入分蘖期」，而不是每次打开页面都把同一批农活重复提醒一遍。
    """
    for row in series:
        if float(row.get("gdd_cum", 0.0)) >= stage_gdd:
            return row["date"]
    return None


# ---------------------------------------------------------------- 7.4.1 短期提醒

def reminders(
    conn: sqlite3.Connection,
    user_id: int,
    field: dict,
    growth: dict,
    series: list[dict],
    today_weather: dict,
    *,
    today: str | None = None,
) -> dict:
    """七条规则里的六条（7.4.1）。返回 RemindersData 的形状。"""
    today = today or today_str()
    out: list[dict] = []
    notes: list[str] = []

    forecast_days = today_weather.get("days") or []
    has_forecast = today_weather.get("source") == "forecast" and bool(forecast_days)
    if not has_forecast:
        notes.append(
            "天气数据不可用（当前为气候平均值或空），已跳过全部天气类提醒 —— "
            "拿不到预报时不做「未来有雨」这类判断"
        )

    # ① 刚跨过一个生育阶段 → 提醒该阶段的农活
    entered = stage_entered_on(series, growth.get("stage_gdd", 0.0))
    if entered and 0 <= (today_local() - date.fromisoformat(entered)).days <= STAGE_FRESH_DAYS:
        for action in growth.get("actions") or []:
            out.append(_advice(
                "stage", action,
                reason=f"{entered} 前后进入「{growth['stage']}」阶段",
                severity="warn", priority="高",
            ))

    if has_forecast:
        rain = _rain_days(forecast_days)
        actions_text = "".join(growth.get("actions") or [])

        # ② 未来 2 天有雨且该施肥 → 雨前施完
        soon_rain = [d for d in rain if d <= (today_local() + timedelta(days=2)).isoformat()]
        if soon_rain and "肥" in actions_text:
            out.append(_advice(
                "fertilize", "趁雨前把肥施完",
                reason=f"{'、'.join(soon_rain)} 有雨，施肥后遇雨容易流失",
                window=soon_rain[0], severity="warn", priority="高",
            ))

        # ③ 未来 3 天有雨且该打药 → 等雨停
        three_day_rain = [d for d in rain if d <= (today_local() + timedelta(days=3)).isoformat()]
        if three_day_rain and ("药" in actions_text or "防治" in actions_text):
            out.append(_advice(
                "spray", "等雨停后再打药",
                reason=f"{'、'.join(three_day_rain)} 有雨，药液会被冲掉",
                severity="warn", priority="中",
            ))

        # ④ 连续 3 天 tmax ≥ 35℃ → 注意灌水
        highs = [d for d in forecast_days if d.get("tmax", 0.0) >= HOT_TMAX]
        if len(highs) >= 3:
            out.append(_advice(
                "irrigate", "注意灌水降温",
                reason=f"{highs[0]['date']} 起连续 {len(highs)} 天最高温不低于 {HOT_TMAX:.0f}℃",
                severity="danger", priority="高",
            ))

        # ⑤ 湿度 ≥ 85% 且 20~30℃ → 易起病，去看叶子
        muggy = [d for d in forecast_days
                 if d.get("humidity", 0.0) >= HUMID and DISEASE_TMIN <= d.get("tmean", 0.0) <= DISEASE_TMAX]
        if muggy:
            out.append(_advice(
                "disease", "去田里看看叶片有没有病斑",
                reason=f"{muggy[0]['date']} 湿度 {muggy[0]['humidity']:.0f}%、"
                       f"气温 {muggy[0]['tmean']:.0f}℃，这种温湿条件容易起病",
                severity="warn", priority="中",
            ))

    # ⑥ 近期有诊断记录 → 注意复查（这条不依赖天气，任何时候都能给）
    from src.farm import repo

    recent = repo.recent_diagnosis_log(conn, user_id, field["id"], days=DIAGNOSIS_LOOKBACK_DAYS)
    if recent:
        out.append(_advice(
            "disease", "留意上次诊断过的地块，注意复查",
            reason=f"{recent['date']} 有过一次诊断记录"
                   + (f"（{recent['item']}）" if recent.get("item") else ""),
            severity="warn", priority="中",
        ))

    # ⑦ 过滤掉用户点过「改天」的（7.7 的交互）
    snoozed = repo.active_snoozes(conn, user_id, field["id"])
    if snoozed:
        before = len(out)
        out = [r for r in out if r["key"] not in snoozed]
        if before != len(out):
            notes.append(f"其中 {before - len(out)} 条被你标记为「改天」，暂不显示")

    return {
        "reminders": out, "count": len(out),
        "weather_used": has_forecast and any(r["type"] in ("fertilize", "spray", "irrigate", "disease")
                                             for r in out),
        "notes": notes,
    }


# ---------------------------------------------------------------- 7.4.2 收获建议

def harvest_advice(
    growth: dict,
    model_params: dict,
    harvest: dict,
    today_weather: dict,
    *,
    today: str | None = None,
) -> dict | None:
    """按 7.4.2 的表逐行判断，返回**第一个命中**的（表本身按优先级排序）。

    天气那两行同样只在真预报下评估；拿不到预报就跳到下面几行，
    而不是拿气候平均值假装知道「未来 3 天无雨」。
    """
    today = today or today_str()
    progress = float(growth.get("progress", 0.0))
    days = today_weather.get("days") or []
    has_forecast = today_weather.get("source") == "forecast" and bool(days)

    # 高：积温进度 ≥ 0.95
    if progress >= 0.95:
        return _advice(
            "harvest", "已完熟，准备收割",
            reason=f"积温进度 {progress:.0%}，已达成熟阈值",
            severity="danger", priority="高",
        )

    # 高：穗已低头（形态学证据，比积温更直接）
    if model_params.get("dryLeafRatio", 0) >= 0.5 and model_params.get("earDroop", 0) >= 0.5:
        return _advice(
            "harvest", "穗已低头、叶片转枯，近期安排收割",
            reason=f"枯叶比例 {model_params['dryLeafRatio']:.0%}、"
                   f"穗下垂 {model_params['earDroop']:.0%}",
            severity="danger", priority="高",
        )

    if has_forecast and progress >= NEAR_HARVEST:
        rain = _rain_days(days)
        humid = max((d.get("humidity", 0.0) for d in days), default=0.0)

        # 中：未来 3 天无雨且湿度 < 80%
        if not rain and humid < 80:
            return _advice(
                "harvest", "天气适合收割，可以安排",
                reason=f"未来 {len(days)} 天无雨、湿度 {humid:.0f}%",
                window=f"{days[0]['date']}~{days[-1]['date']}",
                severity="info", priority="中",
            )

        # 中：未来 3 天有雨
        if rain:
            return _advice(
                "harvest", "雨前抢收，或等雨停后再收",
                reason=f"{'、'.join(rain)} 有雨，收割后籽粒不易晾晒",
                severity="warn", priority="中",
            )

    # 低：0.8 ≤ 进度 < 0.95
    if 0.8 <= progress < 0.95:
        return _advice(
            "harvest", "7~10 天后准备收割",
            reason=f"积温进度 {progress:.0%}，接近成熟",
            severity="info", priority="低",
        )

    # 低：距预测收获 ≤ 7 天
    likely = harvest.get("most_likely")
    if likely:
        from src.farm.timeutil import days_between
        if 0 <= days_between(today, likely) <= 7:
            return _advice(
                "harvest", "关注天气，随时准备收割",
                reason=f"预计最可能收获日 {likely} 就在这几天内",
                severity="info", priority="低",
            )

    return None


# ---------------------------------------------------------------- 7.4.3 推荐收割窗口

def harvest_window(
    series: list[dict], harvest: dict, today_weather: dict, *,
    maturity_gdd: float, today: str | None = None,
) -> str:
    """推荐收割窗口（7.4.3）：

        下界 = max(积温成熟日, XGBoost q10)
        上界 = min(积温成熟日 + 10 天, XGBoost q90)
        窗口内有雨 → 雨前 2 天 或 雨停后 2 天

    「积温成熟日」是从积温序列里找**首次达到成熟阈值**的那天；
    它可能还没到（那时取预测区间），也可能是过去（已完熟，取预测区间）。
    """
    from src.farm.timeutil import days_between

    today = today or today_str()

    gdd_mature = next((r["date"] for r in series if float(r.get("gdd_cum", 0.0)) >= maturity_gdd), None)
    q10, q90 = harvest.get("earliest"), harvest.get("latest")

    lowers = [d for d in (gdd_mature, q10) if d]
    uppers = [d for d in (
        (date.fromisoformat(gdd_mature) + timedelta(days=10)).isoformat() if gdd_mature else None,
        q90,
    ) if d]
    if not lowers or not uppers:
        return ""

    lower = max(lowers)
    upper = min(uppers)
    if days_between(lower, upper) < 0:      # 两个来源对不上时退回预测区间
        lower, upper = (q10 or lower), (q90 or upper)

    # 整个窗口都已经过去了 —— 示例田的油菜就是这样（200 天前播种，早就熟透）。
    # 这时候报一串过去的日期是在误导人：它读起来像"建议在这几天收"，
    # 而那几天已经没了。直接说清楚。
    if days_between(upper, today) > 0:
        return f"已过预计成熟期（最后推荐 {upper}），建议尽快收割"

    # 窗口内有雨 → 挪到雨前 2 天或雨停后 2 天
    days = today_weather.get("days") or []
    if today_weather.get("source") == "forecast" and days:
        if any(lower <= d <= upper for d in _rain_days(days)):
            before = (date.fromisoformat(lower) - timedelta(days=2)).isoformat()
            after = (date.fromisoformat(upper) + timedelta(days=2)).isoformat()
            return f"{before}（雨前） 或 {after}（雨停后）"

    return f"{lower}~{upper}"
