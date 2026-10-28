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

# 留空间隔期的施药，在这个窗口内视为「仍可能约束当前采收」。
# 常见标签间隔期在 7~30 天，取 45 天留余量；更早的留空记录不再否掉「可采收」，
# 但仍计入「未记录间隔期」。
UNKNOWN_HOLD_DAYS = 45


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


def harvest_state(logs: Iterable[dict], today: str | None = None) -> dict:
    """这块田现在能不能采、依据是哪次施药 —— 台账、问诊单、导出共用的唯一判定。

    返回的 `status` 只有四档，顺序固定 **locked → unknown → ok → none**：

        locked   有记了间隔期的施药，解禁日还没到；
        unknown  最新一次施药没填间隔期，系统不知道它要隔几天 —— 这一档压过 ok；
        ok       记了间隔期的施药都已期满；
        none     一条施药记录都没有。

    **为什么 unknown 要压过 ok**：10-01 那次打了 7 天的药（10-08 已期满）、10-27 又打
    一次但没填间隔期 —— 旧记录仍算得出一个解禁日，若按「有约束就算 ok」判，页面和导出
    都会写「可采收」，而最新那次用的什么药、要隔几天，系统根本不知道。这不是文案打架，
    是一句会让人提前采收的错误结论。

    两条口径细节：
      · 留空记录只有在比已知那次更晚（或压根没有已知记录）时才算数；
      · 留空记录超过 UNKNOWN_HOLD_DAYS 仍是历史欠账，不再否掉结论，但仍计入 unknown。
    """
    ref = today or today_str()
    try:
        ref_date = date.fromisoformat(str(ref)[:10])
    except ValueError:
        ref_date = date.today()
        ref = ref_date.isoformat()

    logs = list(logs or [])
    sprays = [lg for lg in logs if lg.get("type") == "spray"]
    cons = constraint(sprays, ref)

    unknown = [lg for lg in sprays if lg.get("interval_days") in (None, "")]
    pending = None
    if unknown:
        newest = max(unknown, key=lambda x: (str(x.get("date") or ""),
                                             x.get("created_at") or ""))
        try:
            when = date.fromisoformat(str(newest.get("date") or "")[:10])
        except ValueError:
            when = None
        if when is not None and (ref_date - when).days <= UNKNOWN_HOLD_DAYS:
            pending = newest
    if pending and cons and str(pending.get("date") or "") <= str(cons.get("date") or ""):
        pending = None

    if cons and cons["locked"]:
        status = "locked"
    elif pending or (cons is None and unknown):
        status = "unknown"
    elif cons:
        status = "ok"
    else:
        status = "none"

    known = cons if status in ("locked", "ok") else None
    # 最近一次施药（按全量记录），用于「哪天打过什么」的说明
    last = None
    for lg in sorted(sprays, key=lambda x: (str(x.get("date") or ""),
                                            x.get("created_at") or ""), reverse=True):
        last = lg
        break

    return {
        "status": status,
        "locked": status == "locked",
        # 以下四项只在 locked / ok 时有值：unknown 时不填确定日期，
        # 否则旧药的解禁日会被当成这次新药的限制日期。
        "date": known["date"] if known else "",
        "item": known["item"] if known else "",
        "reg_no": known["reg_no"] if known else "",
        "interval_days": known["interval_days"] if known else None,
        "earliest_date": known["earliest_date"] if known else "",
        "days_left": known["days_left"] if known else None,
        "last_date": (last or {}).get("date", ""),
        "last_item": (last or {}).get("item", ""),
        "last_reg_no": (last or {}).get("reg_no", ""),
        # 留空那次本身：说明「哪一天那次没填」。
        # status=unknown 但没有 pending（留空记录已超过 UNKNOWN_HOLD_DAYS）时，
        # 退回最近一次施药 —— 页面上「未记录间隔期，需核对」后面总得跟一个日期，
        # 否则用户不知道该去翻哪一条。
        "pending_date": ((pending or (last if status == "unknown" else None)
                          or {}).get("date", "")),
        "pending_item": ((pending or (last if status == "unknown" else None)
                          or {}).get("item", "")),
        # 已期满的旧约束：仍可引用「上次 X 天已于 Y 期满」，但不能代表这次
        "last_known": ({
            "date": cons["date"], "item": cons["item"],
            "interval_days": cons["interval_days"],
            "earliest_date": cons["earliest_date"],
        } if cons else None),
    }


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
