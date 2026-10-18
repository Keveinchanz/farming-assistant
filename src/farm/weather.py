"""阶段 7 / 7.2 —— 天气接入（Open-Meteo）。

选 Open-Meteo 的理由（7.17 也明确不做和风天气）：**不用 Key**，
历史与预报两个接口都直接可用，省掉一套凭证配置与配额管理。

这个模块只管「联网取数」，不碰数据库，也不做降级决策 ——
取到了返回数据，取不到抛 WeatherUnavailable。要不要退回缓存、要不要把置信度
降成「低」，是 src/farm/growth.py 的事。这样分工的好处是：本模块可以被单独
测（包括断网时到底抛的是什么），而「降级不编造」这条规则只有一处实现。

一个实测过的坑，写在 fetch_history 的注释里：archive-api 的历史数据**不是实时的**
（ERA5 有几天延迟），所以「从播种日到今天」这一段不能只问历史接口 ——
最近几天必须用预报接口的 past_days 补，否则积温会凭空少掉最后几天，
而且越接近今天错得越多。
"""

from __future__ import annotations

import statistics
from typing import Any

from src.common.config import env
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.farm.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# 逐日变量，与 7.2 的参数表逐字一致
DAILY_VARS = (
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "relative_humidity_2m_mean",
)
TIMEZONE = "Asia/Shanghai"

DEFAULT_TIMEOUT = 20.0
# 气候基准值取几年。7.2.3 写的是 5 年同日分组
NORMALS_YEARS = 5


class WeatherUnavailable(Exception):
    """取数失败（断网、超时、接口报错、返回结构不符）。消息是给用户看的中文。"""


def _timeout() -> float:
    return float(env("FARM_WEATHER_TIMEOUT", DEFAULT_TIMEOUT, cast="float") or DEFAULT_TIMEOUT)


def _get(url: str, params: dict[str, Any]) -> dict:
    """发一次 GET 并解析 JSON。

    requests 在函数内 import：与 src/voice/baidu_provider.py、src/content/policy_fetch.py
    同一套做法 —— 依赖缺失或坏了的时候，是「这一次取数失败」，而不是整个模块导入失败。
    """
    try:
        import requests
    except Exception as e:  # noqa: BLE001
        raise WeatherUnavailable(f"天气服务不可用：缺少 requests 依赖（{e}）") from e

    try:
        resp = requests.get(url, params=params, timeout=_timeout())
    except Exception as e:  # noqa: BLE001
        raise WeatherUnavailable(f"天气服务连接失败：{e}") from e

    if resp.status_code != 200:
        # 把服务端给的 reason 一并带出来。限流（429）时它区分「分钟窗口」和
        # 「小时窗口」——两者的正确应对完全不同（一个等一分钟，一个等一小时），
        # 而只留一个状态码的话，调用方看到的永远只是「429」，没法分辨。
        hint = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("reason"):
                hint = f"（{body['reason']}）"
        except Exception:  # noqa: BLE001
            hint = ""
        raise WeatherUnavailable(f"天气服务返回 HTTP {resp.status_code}{hint}")

    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        raise WeatherUnavailable(f"天气服务返回的不是 JSON：{e}") from e

    if isinstance(payload, dict) and payload.get("error"):
        raise WeatherUnavailable(f"天气服务报错：{payload.get('reason') or payload['error']}")
    return payload


def _rows_from_daily(daily: dict) -> list[dict]:
    """把 Open-Meteo 的列式 daily 结构转成一行一天的记录。

    逐列可能短于 time，也可能含 null（该日缺测）。缺 tmax 或 tmin 的那一天直接
    **跳过**：积温需要日均温，只有半天的数据算不出日均温，硬凑一个 0 会让积温凭空少。
    """
    times = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    precip = daily.get("precipitation_sum") or []
    humid = daily.get("relative_humidity_2m_mean") or []

    def pick(seq: list, i: int):
        return seq[i] if i < len(seq) else None

    rows: list[dict] = []
    for i, day in enumerate(times):
        hi, lo = pick(tmax, i), pick(tmin, i)
        if hi is None or lo is None:
            continue
        rows.append({
            "date": day,
            "tmax": float(hi),
            "tmin": float(lo),
            "tmean": (float(hi) + float(lo)) / 2.0,
            "precip": float(pick(precip, i) or 0.0),
            "humidity": float(pick(humid, i) or 0.0),
        })
    return rows


def fetch_history(lat: float, lng: float, start: str, end: str) -> list[dict]:
    """历史逐日气温（archive-api）。start/end 为 'YYYY-MM-DD'，闭区间。

    实测提醒：这个接口有几天延迟，问「到今天」时最后几天常常是 null 或被截断。
    调用方必须再用 fetch_forecast(past_days=...) 补最近一段 —— 见 growth.py 的合并逻辑。
    """
    payload = _get(ARCHIVE_URL, {
        "latitude": lat,
        "longitude": lng,
        "start_date": start,
        "end_date": end,
        "daily": ",".join(DAILY_VARS),
        "timezone": TIMEZONE,
    })
    rows = _rows_from_daily(payload.get("daily") or {})
    logger.info("历史气温 %s~%s：%d 天", start, end, len(rows))
    return rows


def fetch_forecast(lat: float, lng: float, *, past_days: int = 7, forecast_days: int = 14) -> list[dict]:
    """预报逐日气温 + 最近 past_days 天（用来补历史接口的延迟缺口）。

    past_days 是 Open-Meteo 预报接口的真实参数（实测可取到 92 天），
    它给的是「已发生的最近几天」，正好填进 archive 的尾巴。
    """
    payload = _get(FORECAST_URL, {
        "latitude": lat,
        "longitude": lng,
        "daily": ",".join(DAILY_VARS),
        "timezone": TIMEZONE,
        "past_days": past_days,
        "forecast_days": forecast_days,
    })
    rows = _rows_from_daily(payload.get("daily") or {})
    logger.info("预报气温：%d 天（含过去 %d 天）", len(rows), past_days)
    return rows


def merge_history(archive: list[dict], forecast: list[dict], *, until: str) -> list[dict]:
    """合并两个来源，返回 <= until 的逐日记录（按日期去重、升序）。

    冲突时**以历史接口为准**：archive 是实况观测（ERA5），预报接口的 past_days
    那一截仍是模式值。历史接口缺的那几天（延迟）才用预报补。
    """
    by_date: dict[str, dict] = {}
    for row in forecast:          # 先放预报，再让历史覆盖
        if row["date"] <= until:
            by_date[row["date"]] = row
    for row in archive:
        if row["date"] <= until:
            by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def fetch_future(lat: float, lng: float, days: int = 14) -> list[dict]:
    """未来若干天的预报（advisor 判断「未来 3 天有雨」要用）。"""
    return [r for r in fetch_forecast(lat, lng, past_days=0, forecast_days=max(1, days))
            if r["date"] >= _today()]


def _today() -> str:
    from src.farm.timeutil import today_str

    return today_str()


def build_normals(rows: list[dict], *, years: int = NORMALS_YEARS) -> dict[str, tuple[float, float]]:
    """5 年同日分组算 mean/std（7.2.3）。

    返回 {'MM-DD': (平均值, 标准差)}。标准差用**样本标准差**（statistics.stdev，
    分母 n-1）：这里取的是「若干年的同日气温」这个样本，不是总体；
    只有一年的数据时 stdev 会抛异常，此时标准差记 0（单样本没有离散度可言，
    返回 0 比编一个数字诚实）。
    """
    buckets: dict[str, list[float]] = {}
    for row in rows:
        md = row["date"][5:]          # 'YYYY-MM-DD' -> 'MM-DD'
        buckets.setdefault(md, []).append(row["tmean"])

    normals: dict[str, tuple[float, float]] = {}
    for md, values in buckets.items():
        if len(values) > years:       # 同一天超过 years 条时只取最近的几条
            values = values[-years:]
        avg = statistics.fmean(values)
        std = statistics.stdev(values) if len(values) >= 2 else 0.0
        normals[md] = (avg, std)
    return normals


def fetch_normals(lat: float, lng: float, *, years: int = NORMALS_YEARS) -> dict[str, tuple[float, float]]:
    """拉取多年历史并算出气候基准值。一次请求拿整段（约 5×365 天）。

    这是本项目里**最重的一次天气请求**（约 200 KB），所以调用方一定要把结果落进
    climate_normals 表 —— 那是全局共享的（不带 user_id），同一个城市只该拉一次。
    """
    from src.farm.timeutil import today_local

    today = today_local()
    start = today.replace(year=today.year - years).isoformat()
    rows = fetch_history(lat, lng, start, today.isoformat())
    return build_normals(rows, years=years)


# ------------------------------------------------------------------ 连通性

_PROBE_TTL_SECONDS = 60
_probe_cache: dict[str, Any] = {"at": 0.0, "ok": False, "detail": ""}


def probe(*, force: bool = False) -> dict:
    """/api/health 用的轻量连通性探测（结果缓存 60 秒）。

    为什么不直接每次请求都打一次：health 会被频繁调用，而天气接口在国内偶尔抖动，
    每次真打一次会让「服务状态」页面本身变得很慢。缓存 60 秒足够反映当前状态，
    又不会把一个瞬时抖动放大成持续告警。
    """
    import time

    now = time.time()
    if not force and now - _probe_cache["at"] < _PROBE_TTL_SECONDS:
        return {"reachable": _probe_cache["ok"], "detail": _probe_cache["detail"],
                "cached": True}

    ok, detail = True, "Open-Meteo 可达"
    try:
        fetch_forecast(30.58, 114.30, past_days=0, forecast_days=1)
    except WeatherUnavailable as e:
        ok, detail = False, str(e)

    _probe_cache.update({"at": now, "ok": ok, "detail": detail})
    return {"reachable": ok, "detail": detail, "cached": False}
