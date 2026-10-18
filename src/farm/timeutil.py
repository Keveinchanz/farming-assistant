"""阶段 7 —— 时间口径。

项目里对「时间」有两套完全不同的用法，这里刻意分成两个函数、两种格式，
不让它们互相冒充：

    · **时刻**（会话是否过期、登录失败发生在几点）—— 必须是绝对时间，存 ISO-8601 UTC。
      用本地时间表示时刻，一旦机器时区变了、或跨夏令时地区部署，过期判断就会算错。
      → utc_now() / parse_utc()

    · **日历日**（播种日、农事记录日期、逐日气温）—— 是「哪一天」，不是「哪一刻」。
      积温按日累加，用户说「昨天施的肥」指的是他所在地的那一天。
      → today_local() / is_date() / days_between()

为什么固定用 UTC+8 而不是 zoneinfo("Asia/Shanghai")：
    中国自 1991 年起不再实行夏令时，Asia/Shanghai 全年恒为 UTC+8，一个固定偏移
    就是精确解。而 Windows 不自带 IANA 时区库，ZoneInfo("Asia/Shanghai") 会抛
    ZoneInfoNotFoundError，除非额外装 tzdata —— 为一件恒定的事引入一个依赖不值得。
    （若将来要支持跨时区用户，这里换回 zoneinfo 即可，调用方不用改。）
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

# 与 configs/default.yaml 的 runtime.timezone 一致
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ 时刻（UTC）

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """存库用的时刻字符串。截到秒 —— 秒以下对会话过期没有意义，还会让日志难读。"""
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """把 utc_now_iso() 写出的字符串解析回带时区的 datetime。

    兼容两种写法：'...Z'（本项目）与 '...+00:00'（Python 早版本的 isoformat）。
    """
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ 日历日（UTC+8）

def today_local() -> date:
    return datetime.now(CN_TZ).date()


def today_str() -> str:
    return today_local().isoformat()


def is_date(value: str) -> bool:
    """是否是 'YYYY-MM-DD' 且真实存在（挡掉 2026-02-30 这种）。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def to_date(value: str) -> date:
    """严格解析日历日；不合法就抛 ValueError（调用方负责转成中文提示）。"""
    if not is_date(value):
        raise ValueError(f"不是合法的日期（应为 YYYY-MM-DD）：{value!r}")
    return date.fromisoformat(value)


def days_between(start: str, end: str) -> int:
    """两个日历日之间的天数差（end - start）。同日为 0，允许为负。"""
    return (to_date(end) - to_date(start)).days


def date_range(start: str, end: str) -> list[str]:
    """闭区间 [start, end] 的每一天。用于补天气缺口。"""
    d0, d1 = to_date(start), to_date(end)
    if d1 < d0:
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
