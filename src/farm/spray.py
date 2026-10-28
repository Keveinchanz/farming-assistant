"""安全间隔期倒计时（用药台账的判定核心）。

**一条规矩**：这个模块只做加法与减法 —— 「施药日 + 标签上的间隔期 = 最早可采收日」。
它不推断间隔期，也不给默认值。间隔期来自瓶身标签，由施药人填；没填就是没记录，
台账里显示「未记录间隔期」，绝不退化成「打了就能摘」。

**为什么取「最晚的那一次」而不是「最近的那一次」**：一块田一季要打好几次药，
每次的药不同、间隔期也不同。10 月 1 日打了 7 天的药、10 月 10 日打了 30 天的药，
约束采收的是 10 月 10 日那次（解禁 11 月 9 日），不是最近一次的字面含义。
所以口径是：**所有记了间隔期的施药里，解禁日最晚的那一次说了算**。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

# 允许的最大间隔期。标签上最长的一般在 30～40 天（部分药剂到 45 天），
# 取 365 只是挡明显的手误（把「21」打成「210」会让这块田一整年不能收，
# 而用户只会觉得「系统坏了」），不是业务上限。
MAX_INTERVAL_DAYS = 365


def today_str() -> str:
    return date.today().isoformat()


def add_days(date_str: str, days: int) -> str:
    """'2026-10-20' + 21 → '2026-11-10'。"""
    d = date.fromisoformat(str(date_str)[:10])
    return (d + timedelta(days=int(days))).isoformat()


def days_between(start: str, end: str) -> int:
    """end - start，单位天。end 晚于 start 为正。"""
    a = date.fromisoformat(str(start)[:10])
    b = date.fromisoformat(str(end)[:10])
    return (b - a).days


def constraint(logs: Iterable[dict], today: str | None = None) -> dict | None:
    """从农事记录里算出「这块田当前受哪次施药约束」。

    返回 None 表示：没有任何一条「记了间隔期」的打药记录 —— 不是「没有约束」，
    而是「不知道」。调用方必须把这两种情况分开显示。
    """
    ref = today or today_str()
    best: dict | None = None

    for lg in logs or []:
        if lg.get("type") != "spray":
            continue
        days = lg.get("interval_days")
        if days is None or days == "":
            continue
        try:
            days = int(days)
        except (TypeError, ValueError):
            continue
        raw_date = str(lg.get("date") or "")[:10]
        if not raw_date:
            continue
        try:
            earliest = add_days(raw_date, days)
        except ValueError:
            continue

        if best is None or earliest > best["earliest_date"]:
            best = {
                "earliest_date": earliest,
                "interval_days": days,
                "date": raw_date,
                "item": lg.get("item") or "",
                "reg_no": lg.get("reg_no") or "",
                "log_id": lg.get("id") or "",
                "field_id": lg.get("field_id") or "",
            }

    if best is None:
        return None

    best["days_left"] = days_between(ref, best["earliest_date"])
    best["locked"] = best["days_left"] > 0
    return best


def annotate(logs: list[dict], today: str | None = None) -> list[dict]:
    """给每条记录补一个 interval_end（那次施药自己的解禁日），台账时间线用。

    interval_days 为空时 interval_end 是空串 —— 前端据此显示「未记录间隔期」，
    而不是画一个假的日期。
    """
    ref = today or today_str()
    out = []
    for lg in logs or []:
        item = dict(lg)
        days = item.get("interval_days")
        if item.get("type") == "spray" and days not in (None, ""):
            try:
                item["interval_end"] = add_days(item.get("date", ""), int(days))
                item["days_left"] = days_between(ref, item["interval_end"])
                item["locked"] = item["days_left"] > 0
            except (ValueError, TypeError):
                item["interval_end"] = ""
                item["days_left"] = None
                item["locked"] = False
        else:
            item["interval_end"] = ""
            item["days_left"] = None
            item["locked"] = False
        out.append(item)
    return out


def valid_interval(value: Any) -> int | None:
    """把请求里的间隔期规整成 int | None，非法值抛 ValueError。

    空串 / None 一律解释为「没记录」，不是 0。
    """
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("安全间隔期要填整数天（例如 21）；不记得就留空") from None
    if n < 0:
        raise ValueError("安全间隔期不能是负数")
    if n > MAX_INTERVAL_DAYS:
        raise ValueError(f"安全间隔期超过 {MAX_INTERVAL_DAYS} 天，请核对标签")
    return n
