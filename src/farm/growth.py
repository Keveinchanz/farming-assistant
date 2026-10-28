"""阶段 7 / 7.3 + 7.6.2 —— 生长引擎：取天气、累积温、定阶段、算 3D 参数。

这是把前面几个纯模块串起来的一层：
    weather.py（联网取数） → repo.py（落库缓存） → gdd.py（纯数学） → 本模块（编排）

**降级不编造**是本模块的核心规则（7.2 三条 + 清单开头的「降级不编造」）：
    历史接口拿不到 → 用库里已缓存的那些天，并标明「截至 X 日」
    缓存也是空的 → confidence=低，只显示已累积积温（也就是 0），并且**照样返回结构**，
                   让前端能画出「暂无数据」而不是崩掉
    预报拿不到   → 该用气候基准值的地方用基准值，天气类提醒直接不出（见 advisor.py）
无论走哪条路，返回的 degraded / source_note / confidence 都如实说明，
绝不用一个看起来正常的数字把降级藏起来。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from src.common.logging_utils import setup_logger
from src.farm import gdd as gdd_mod
from src.farm import repo, weather
from src.farm.stages import crop_config
from src.farm.timeutil import days_between, today_local, today_str

logger = setup_logger("agri.farm.growth")

# 缓存"新鲜"到什么程度就算够用。2 天：archive 接口本身有几天的延迟，
# 而预报接口的 past_days 能补最近这几天 —— 留这么一点余量，避免每开一次页面就联网。
MAX_STALE_DAYS = 2


# ---------------------------------------------------------------- 天气 + 缓存

def ensure_history(
    conn: sqlite3.Connection, user_id: int, field: dict, *, today: str | None = None
) -> dict:
    """确保 daily_temp 里有[播种日, 今天]的逐日气温，返回一份状态说明。

    返回 {rows, degraded, degraded_reason, source_note, as_of, confidence}。
    rows 是库里最终可用的数据（可能为空 —— 那是降级到极限的情况，不是错误）。
    """
    today = today or today_str()
    sow = field["sow_date"]
    fid, uid = field["id"], user_id

    # 先看缓存覆盖到哪里
    cached_latest = repo.latest_temp_date(conn, uid, fid)
    need_fetch = True
    if cached_latest:
        need_fetch = days_between(cached_latest, today) > MAX_STALE_DAYS
        # 缓存"够新"但缺最高/最低气温（从 v3 升级上来的老行）也要重拉：
        # 否则 harvest_model 的 11 个特征里有一半永远是 0，
        # 而模型不会报错，只会给出离谱的日期。
        if not need_fetch and not repo.latest_temp_has_detail(conn, uid, fid):
            need_fetch = True
            logger.info("田块 %s 的缓存缺少最高/最低气温，重新拉取以补全", fid)

    if need_fetch and field.get("lat") is not None and field.get("lng") is not None:
        try:
            # 历史接口只问到 5 天前：era5 有延迟，越靠近今天的日子越可能缺
            archive_end = (today_local() - timedelta(days=5)).isoformat()
            if days_between(sow, archive_end) >= 0:
                archive = weather.fetch_history(field["lat"], field["lng"], sow, archive_end)
            else:
                archive = []          # 播种日就在最近几天，历史接口还没这一天
            forecast = weather.fetch_forecast(field["lat"], field["lng"], past_days=7, forecast_days=1)
            merged = weather.merge_history(archive, forecast, until=today)

            # 落库：archive 的部分标 archive，只有预报补上来的那几天标 forecast
            archive_dates = {r["date"] for r in archive}
            repo.upsert_daily_temp(
                conn, uid, fid, [r for r in merged if r["date"] in archive_dates], source="archive"
            )
            repo.upsert_daily_temp(
                conn, uid, fid, [r for r in merged if r["date"] not in archive_dates], source="forecast"
            )
            logger.info("田块 %s 天气已更新至 %s（历史 %d 天 + 预报补 %d 天）",
                        fid, today, len(archive), len(merged) - len(archive))
        except weather.WeatherUnavailable as e:
            # 联网失败不是错误，是降级：下面统一按缓存处理
            logger.warning("天气取数失败，改用缓存：%s", e)
            return _cached_status(conn, uid, fid, sow, today, reason=str(e))

    return _cached_status(conn, uid, fid, sow, today, reason="")


def status_from_rows(
    rows: list[dict], sow: str, today: str, *,
    reason: str = "", source: str = "cache",
) -> dict:
    """由逐日气温行构造状态说明；纯函数，不碰数据库。

    `ensure_history()` 原来把这段逻辑写在 `_cached_status()` 里，技能层需要
    在不登录、不建库的前提下复用，所以抽出来。过滤 `[sow, today]`、按日期去重、
    缺 `tmean` 时用 `(tmax + tmin) / 2` 补齐 —— 与线上缓存整理出的结构完全一致。
    """
    by_date: dict[str, dict] = {}
    for row in rows or []:
        day = str(row.get("date") or "")
        if not day or day < sow or day > today:
            continue
        item = dict(row)
        if item.get("tmean") is None and item.get("tmax") is not None and item.get("tmin") is not None:
            item["tmean"] = (float(item["tmax"]) + float(item["tmin"])) / 2
        by_date[day] = item

    rows = [by_date[d] for d in sorted(by_date)]
    as_of = rows[-1]["date"] if rows else ""

    if not rows:
        return {
            "rows": [],
            "degraded": True,
            "degraded_reason": reason or "没有该田块的气温数据",
            "source_note": "暂无气温数据，无法计算积温",
            "as_of": "",
            "confidence": "低",
            "source": source,
        }

    expected_days = days_between(sow, today) + 1
    covered = len(rows)
    stale_days = days_between(as_of, today) if as_of else expected_days

    if covered < expected_days:
        # 缺天的两种情况，措辞必须分开：一种是历史拉不下来（有网但接口不配合），
        # 一种是我们压根没拉到那么早（播种日比缓存还早）。用户能看到的区别是
        # 「截至 X 日」这句话 —— 它告诉他积温算到哪一天为止。
        note = f"气温数据截至 {as_of}（{covered}/{expected_days} 天）"
        conf = "中" if stale_days <= 5 else "低"
        return {
            "rows": rows, "degraded": True,
            "degraded_reason": reason or "部分日期的气温缺失",
            "source_note": note, "as_of": as_of, "confidence": conf,
            "source": source,
        }

    return {
        "rows": rows, "degraded": False, "degraded_reason": "",
        "source_note": f"气温数据截至 {as_of}", "as_of": as_of, "confidence": "高",
        "source": source,
    }


def _cached_status(
    conn: sqlite3.Connection, uid: int, fid: str, sow: str, today: str, *, reason: str
) -> dict:
    """把库里的缓存整理成状态说明。**这里是「降级不编造」落地的唯一位置。**"""
    rows = repo.list_daily_temp(conn, uid, fid, start=sow, end=today)
    return status_from_rows(rows, sow, today, reason=reason, source="cache")


# ---------------------------------------------------------------- 生长状态

def compute_growth(field: dict, status: dict, *, today: str | None = None) -> dict:
    """由天气行算出 GrowthData 的形状（7.3.3）。"""
    today = today or today_str()
    crop = field["crop"]
    cfg = crop_config(crop)

    series = gdd_mod.accumulate(status["rows"], cfg)
    gdd_cum = float(series[-1]["gdd_cum"]) if series else 0.0
    matched = gdd_mod.match_stage(crop, gdd_cum)

    return {
        "field_id": field["id"],
        "crop": crop,
        "sow_date": field["sow_date"],
        "days_since_sow": days_between(field["sow_date"], today),
        "gdd_cum": gdd_cum,
        "gdd_remain": matched["gdd_remain"],
        "maturity_gdd": matched["maturity_gdd"],
        "stage": matched["stage"],
        "stage_index": matched["stage_index"],
        "stage_gdd": matched["stage_gdd"],
        "next_stage": matched["next_stage"],
        "next_gdd": matched["next_gdd"],
        "progress": matched["progress"],
        "matured": matched["matured"],
        "actions": matched["actions"],
        "confidence": status["confidence"],
        "as_of": status["as_of"],
        "source_note": status["source_note"],
    }


# ---------------------------------------------------------------- 3D 参数（7.6）

# 关键帧：进度 → 形态。中间值线性插值，颜色在 RGB 空间插值。
#
# 水稻这一组**直接照抄 7.6.4 的表**（清单里唯一给了具体数字的一张）。
# 玉米与油菜的表清单没给，按 7.6.5 的完熟形态描述 + 两种作物的实际形态补的：
#   · 玉米更高（成株约 2.3 m）、叶更窄更直立，成熟时中下部叶枯、苞叶黄白松散、籽粒带黑层；
#   · 油菜矮得多（约 1.5 m），成熟时角果 80% 变黄、叶片脱落、茎变黄 ——
#     「叶片脱落」用 dryLeafRatio 表达（1.0 时 0.85），不是把叶片数改成 0
#     （leafCount 是"长过多少片"，单调不降，7.12 有测试锁它）。
KEYFRAMES: dict[str, list[dict]] = {
    # 注意这里**没有** "有没有穗" 这个字段：那件事由生育期表决定（见 EAR_STAGE），
    # 曾经两张表各写一份阈值，结果 3D 和阶段名对不上。
    "水稻": [
        {"p": 0.0, "height": 10, "leaf": 2,  "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.1, "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.2, "height": 25, "leaf": 5,  "leafColor": "#7cb342", "name": "中绿", "droop": 0.2, "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.4, "height": 50, "leaf": 8,  "leafColor": "#558b2f", "name": "深绿", "droop": 0.3, "stem": "#689f38", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.6, "height": 75, "leaf": 10, "leafColor": "#558b2f", "name": "深绿", "droop": 0.4, "stem": "#689f38", "dry": 0.05, "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.8, "height": 90, "leaf": 12, "leafColor": "#afb42b", "name": "黄绿", "droop": 0.5, "stem": "#9e9d24", "dry": 0.15, "earSize": 0.5, "earColor": "#c5e1a5", "earDroop": 0.2},
        {"p": 0.9, "height": 95, "leaf": 12, "leafColor": "#c0ca33", "name": "黄绿", "droop": 0.6, "stem": "#afb42b", "dry": 0.3,  "earSize": 0.8, "earColor": "#e8c547", "earDroop": 0.5},
        {"p": 1.0, "height": 100, "leaf": 12, "leafColor": "#d4a017", "name": "金黄", "droop": 0.8, "stem": "#c9a227", "dry": 0.6, "earSize": 1.0, "earColor": "#e0b12f", "earDroop": 0.7},
    ],
    "玉米": [
        {"p": 0.0, "height": 8,   "leaf": 2,  "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.05, "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.2, "height": 30,  "leaf": 4,  "leafColor": "#7cb342", "name": "中绿", "droop": 0.1,  "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.4, "height": 90,  "leaf": 7,  "leafColor": "#558b2f", "name": "深绿", "droop": 0.15, "stem": "#689f38", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.6, "height": 160, "leaf": 10, "leafColor": "#43a047", "name": "深绿", "droop": 0.2,  "stem": "#689f38", "dry": 0.02, "earSize": 0.3, "earColor": "#cddc39", "earDroop": 0.05},
        {"p": 0.8, "height": 210, "leaf": 12, "leafColor": "#afb42b", "name": "黄绿", "droop": 0.3,  "stem": "#9e9d24", "dry": 0.15, "earSize": 0.6, "earColor": "#e6c34a", "earDroop": 0.15},
        {"p": 0.9, "height": 225, "leaf": 12, "leafColor": "#c0a72b", "name": "黄绿", "droop": 0.35, "stem": "#afb42b", "dry": 0.35, "earSize": 0.8, "earColor": "#e8b93b", "earDroop": 0.25},
        {"p": 1.0, "height": 230, "leaf": 12, "leafColor": "#d4a017", "name": "金黄", "droop": 0.45, "stem": "#c9a227", "dry": 0.6, "earSize": 1.0, "earColor": "#e0a92e", "earDroop": 0.35},
    ],
    "油菜": [
        {"p": 0.0, "height": 5,   "leaf": 2,  "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.1,  "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.2, "height": 20,  "leaf": 5,  "leafColor": "#7cb342", "name": "中绿", "droop": 0.15, "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.4, "height": 50,  "leaf": 8,  "leafColor": "#558b2f", "name": "深绿", "droop": 0.2,  "stem": "#689f38", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.6, "height": 90,  "leaf": 10, "leafColor": "#43a047", "name": "深绿", "droop": 0.25, "stem": "#689f38", "dry": 0.05, "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        # 油菜的「穗」是角果：开花期（进度 ~0.78）开始出现，颜色先黄（花瓣）后转金黄（角果）
        {"p": 0.8, "height": 130, "leaf": 12, "leafColor": "#c0ca33", "name": "黄绿", "droop": 0.35, "stem": "#9e9d24", "dry": 0.2,  "earSize": 0.5, "earColor": "#e6d84a", "earDroop": 0.1},
        {"p": 0.9, "height": 145, "leaf": 12, "leafColor": "#d4c62b", "name": "黄绿", "droop": 0.45, "stem": "#afb42b", "dry": 0.4,  "earSize": 0.8, "earColor": "#d9d24a", "earDroop": 0.2},
        {"p": 1.0, "height": 150, "leaf": 12, "leafColor": "#d4a017", "name": "金黄", "droop": 0.6,  "stem": "#c9a227", "dry": 0.85, "earSize": 1.0, "earColor": "#d9a826", "earDroop": 0.3},
    ],
    # 下面四个是后加的作物（小麦/大豆/棉花/马铃薯）。形态按各自实际长相补：
    #   · 小麦矮挺、叶窄，「穗」是麦穗（抽穗期出现）；
    #   · 大豆中等高、叶宽，「穗」是豆荚（结荚期出现）；
    #   · 棉花最高、叶大，「穗」是棉铃（花铃期出现，成熟吐絮时发白）；
    #   · 马铃薯最矮，**没有地上穗**（产品器官是地下块茎），故不进 EAR_STAGE。
    # 与前三张表同一套字段；株高/叶片数必须单调不降（tests 有回归锁）。
    "小麦": [
        {"p": 0.0, "height": 8,  "leaf": 2,  "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.15, "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.2, "height": 22, "leaf": 5,  "leafColor": "#7cb342", "name": "中绿", "droop": 0.25, "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.4, "height": 45, "leaf": 7,  "leafColor": "#558b2f", "name": "深绿", "droop": 0.3,  "stem": "#689f38", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.6, "height": 70, "leaf": 9,  "leafColor": "#558b2f", "name": "深绿", "droop": 0.35, "stem": "#689f38", "dry": 0.03, "earSize": 0.2, "earColor": "#dcdc7a", "earDroop": 0.0},
        {"p": 0.8, "height": 88, "leaf": 10, "leafColor": "#afb42b", "name": "黄绿", "droop": 0.4,  "stem": "#9e9d24", "dry": 0.15, "earSize": 0.6, "earColor": "#e6d24a", "earDroop": 0.1},
        {"p": 0.9, "height": 93, "leaf": 10, "leafColor": "#c0ca33", "name": "黄绿", "droop": 0.45, "stem": "#afb42b", "dry": 0.3,  "earSize": 0.85, "earColor": "#e8c547", "earDroop": 0.2},
        {"p": 1.0, "height": 95, "leaf": 10, "leafColor": "#d4a017", "name": "金黄", "droop": 0.55, "stem": "#c9a227", "dry": 0.6,  "earSize": 1.0, "earColor": "#e0b12f", "earDroop": 0.35},
    ],
    "大豆": [
        {"p": 0.0, "height": 6,  "leaf": 2, "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.1,  "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.2, "height": 20, "leaf": 4, "leafColor": "#7cb342", "name": "中绿", "droop": 0.15, "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.4, "height": 40, "leaf": 6, "leafColor": "#558b2f", "name": "深绿", "droop": 0.2,  "stem": "#689f38", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.6, "height": 60, "leaf": 8, "leafColor": "#43a047", "name": "深绿", "droop": 0.25, "stem": "#689f38", "dry": 0.02, "earSize": 0.3, "earColor": "#8bc34a", "earDroop": 0.1},
        {"p": 0.8, "height": 75, "leaf": 9, "leafColor": "#afb42b", "name": "黄绿", "droop": 0.3,  "stem": "#9e9d24", "dry": 0.2,  "earSize": 0.7, "earColor": "#c9b24a", "earDroop": 0.2},
        {"p": 0.9, "height": 80, "leaf": 9, "leafColor": "#c0a72b", "name": "黄绿", "droop": 0.35, "stem": "#afb42b", "dry": 0.45, "earSize": 0.9, "earColor": "#cdb04a", "earDroop": 0.25},
        {"p": 1.0, "height": 82, "leaf": 9, "leafColor": "#d4a017", "name": "金黄", "droop": 0.45, "stem": "#c9a227", "dry": 0.75, "earSize": 1.0, "earColor": "#d9b24a", "earDroop": 0.3},
    ],
    "棉花": [
        {"p": 0.0, "height": 8,   "leaf": 2,  "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.1,  "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#e0e0e0", "earDroop": 0.0},
        {"p": 0.2, "height": 28,  "leaf": 5,  "leafColor": "#7cb342", "name": "中绿", "droop": 0.15, "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#e0e0e0", "earDroop": 0.0},
        {"p": 0.4, "height": 55,  "leaf": 8,  "leafColor": "#558b2f", "name": "深绿", "droop": 0.2,  "stem": "#689f38", "dry": 0.0,  "earSize": 0.2, "earColor": "#f0f0f0", "earDroop": 0.0},
        {"p": 0.6, "height": 85,  "leaf": 10, "leafColor": "#43a047", "name": "深绿", "droop": 0.25, "stem": "#689f38", "dry": 0.03, "earSize": 0.6, "earColor": "#f5f5f5", "earDroop": 0.05},
        {"p": 0.8, "height": 105, "leaf": 11, "leafColor": "#7cb342", "name": "中绿", "droop": 0.3,  "stem": "#7cb342", "dry": 0.2,  "earSize": 0.9, "earColor": "#fafafa", "earDroop": 0.1},
        {"p": 0.9, "height": 112, "leaf": 11, "leafColor": "#8d6e63", "name": "褐绿", "droop": 0.5,  "stem": "#a1887f", "dry": 0.55, "earSize": 1.0, "earColor": "#ffffff", "earDroop": 0.15},
        {"p": 1.0, "height": 115, "leaf": 11, "leafColor": "#a1887f", "name": "枯褐", "droop": 0.7,  "stem": "#8d6e63", "dry": 0.7,  "earSize": 1.0, "earColor": "#f0f0f0", "earDroop": 0.2},
    ],
    "马铃薯": [
        {"p": 0.0, "height": 5,  "leaf": 2, "leafColor": "#9ccc65", "name": "嫩绿", "droop": 0.15, "stem": "#8bc34a", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.2, "height": 18, "leaf": 4, "leafColor": "#7cb342", "name": "中绿", "droop": 0.2,  "stem": "#7cb342", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.4, "height": 35, "leaf": 6, "leafColor": "#558b2f", "name": "深绿", "droop": 0.25, "stem": "#689f38", "dry": 0.0,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.6, "height": 50, "leaf": 8, "leafColor": "#43a047", "name": "深绿", "droop": 0.3,  "stem": "#689f38", "dry": 0.05, "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.8, "height": 60, "leaf": 9, "leafColor": "#7cb342", "name": "中绿", "droop": 0.35, "stem": "#7cb342", "dry": 0.25, "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 0.9, "height": 62, "leaf": 9, "leafColor": "#c0ca33", "name": "黄绿", "droop": 0.45, "stem": "#afb42b", "dry": 0.5,  "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
        {"p": 1.0, "height": 62, "leaf": 9, "leafColor": "#a1887f", "name": "枯褐", "droop": 0.6,  "stem": "#8d6e63", "dry": 0.85, "earSize": 0.0, "earColor": "#c5e1a5", "earDroop": 0.0},
    ],
}

# 完熟的两个硬指标（7.6.5）：只在**接近/到达**成熟时才置位，不能从头到尾都亮着，
# 否则「籽粒有黑层」这种信息就失去了指示意义。
BLACK_LAYER_AT = 0.95     # 玉米籽粒黑层
MILK_LINE_GONE_AT = 0.90  # 玉米乳线消失

# 「什么时候开始有穗」——**由生育期表推导，而不是再写一份进度阈值**。
#
# 这里是踩过的一个坑：原先 hasEar 直接取关键帧的阶跃值（玉米在 0.6 才亮），
# 而玉米的抽穗期阈值恰好是 0.5 —— 于是演示田出现了「阶段写着抽穗期、3D 却没有穗」
# 的自相矛盾。两份阈值各写一遍必然会飘，所以改成查生育期表：
# 一旦进入该作物的抽穗/开花阶段，植株就有穗。
EAR_STAGE = {"水稻": "抽穗期", "玉米": "抽穗期", "油菜": "开花期",
             "小麦": "抽穗期", "大豆": "结荚期", "棉花": "花铃期"}
# 注意：马铃薯**不在此表**。它的产品器官是地下块茎，地上没有「穗」，
# 于是 _ear_from_index 返回 len(names)，hasEar 恒为 False —— 这是有意为之，不是漏写。


def _ear_from_index(crop: str) -> int:
    """进入「有穗」的那个阶段序号。"""
    from src.farm.stages import stage_names

    names = stage_names(crop)
    stage = EAR_STAGE.get(crop)
    return names.index(stage) if stage in names else len(names)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_color(c1: str, c2: str, t: float) -> str:
    """在 RGB 空间插值两个 #rrggbb。"""
    def rgb(c: str) -> tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    r1, g1, b1 = rgb(c1)
    r2, g2, b2 = rgb(c2)
    return "#%02x%02x%02x" % (
        round(_lerp(r1, r2, t)), round(_lerp(g1, g2, t)), round(_lerp(b1, b2, t))
    )


def model_params(crop: str, progress: float, stage_index: int = 0) -> dict:
    """按进度取 3D 形态参数（7.6.2.5 的「进度→参数映射表」）。

    关键帧之间线性插值，颜色在 RGB 空间插值。
    `stage_index` 只用来判断有没有穗（见 EAR_STAGE）—— 那件事必须与生育期表
    保持一致，不能另写一套进度阈值。
    """
    frames = KEYFRAMES.get(crop)
    if not frames:
        raise ValueError(f"没有 {crop} 的 3D 形态参数表")

    p = min(max(float(progress), 0.0), 1.0)

    # 兜底取首尾两帧：进度已夹紧在 0~1，而关键帧恰好覆盖 0~1，所以正常都会命中；
    # 万一将来有人改了关键帧的范围导致没命中，按首尾插值也比抛异常好 ——
    # 画不出植株不该让整个农田页打不开。
    lo, hi = frames[0], frames[-1]
    for i in range(len(frames) - 1):
        if frames[i]["p"] <= p <= frames[i + 1]["p"]:
            lo, hi = frames[i], frames[i + 1]
            break

    span = hi["p"] - lo["p"]
    t = 0.0 if span <= 1e-9 else (p - lo["p"]) / span

    return {
        "height": round(_lerp(lo["height"], hi["height"], t), 1),
        # 叶片数**插值**而不是阶跃：阶跃会让分蘖期的水稻只有 2 片叶（进度 19.6%
        # 卡在 0.2 那个台阶下面），而 7.6.4 的表在 0.2 处写的是 5 片 ——
        # 插值后的 5 片才对得上，视觉上也是连续生长的样子。
        # 单调性不受影响：非降序列的线性插值仍然非降（7.12 有测试锁它）。
        "leafCount": int(round(_lerp(lo["leaf"], hi["leaf"], t))),
        "leafColor": _lerp_color(lo["leafColor"], hi["leafColor"], t),
        "leafColorName": lo["name"] if t < 0.5 else hi["name"],
        "leafDroop": round(_lerp(lo["droop"], hi["droop"], t), 3),
        "stemColor": _lerp_color(lo["stem"], hi["stem"], t),
        # 有穗与否查生育期表（见 EAR_STAGE），与阶段名永远一致
        "hasEar": stage_index >= _ear_from_index(crop),
        "earSize": round(_lerp(lo["earSize"], hi["earSize"], t), 3),
        "earColor": _lerp_color(lo["earColor"], hi["earColor"], t),
        "earDroop": round(_lerp(lo["earDroop"], hi["earDroop"], t), 3),
        "dryLeafRatio": round(_lerp(lo["dry"], hi["dry"], t), 3),
        # 黑层与乳线是玉米专有标志；其余作物恒为 False（不是"缺失"，是确实没有这个概念）
        "hasBlackLayer": crop == "玉米" and p >= BLACK_LAYER_AT,
        "milkLineGone": crop == "玉米" and p >= MILK_LINE_GONE_AT,
    }


def compute_model(field: dict, growth: dict, status: dict, *, degraded: bool = False) -> dict:
    """把一个田块的 3D 参数（ModelData 的形状）拼出来。"""
    params = model_params(field["crop"], growth["progress"], growth.get("stage_index", 0))
    return {
        "field_id": field["id"],
        "crop": field["crop"],
        "progress": growth["progress"],
        "stage": growth["stage"],
        "matured": growth["matured"],
        "gdd_cum": growth["gdd_cum"],
        "as_of": status.get("as_of", ""),
        "degraded": degraded,
        **params,
    }


# ---------------------------------------------------------------- 未来天气（7.7 天气卡）

def next_days(conn: sqlite3.Connection, user_id: int, field: dict, *, days: int = 3) -> dict:
    """今明后 N 天的天气预报（WeatherData 的形状）。

    预报拿不到时退回**气候基准值**（7.2 降级第三条）：用该省该市同日的
    多年平均气温，并明确标注 source='normal' —— 那是气候平均，不是预报，
    前端与 advisor 都要能分清（advisor 据此不出「未来有雨」这类提醒）。
    """
    today = today_str()
    province, city = field.get("province", ""), field.get("city", "")
    # 必须在 try 之前初始化：预报请求成功但**一天都没落在今天之后**时，
    # 会走到下面的 _normals_weather，那时 reason 还没被赋值 —— 那是 NameError，
    # 而且只在「预报返回了但都是过去的日子」这种少见情况下才炸。
    reason = ""

    if field.get("lat") is not None and field.get("lng") is not None:
        try:
            rows = weather.fetch_forecast(field["lat"], field["lng"], past_days=0,
                                          forecast_days=max(days, 3))
            picked = [r for r in rows if r["date"] >= today][:days]
            if picked:
                return {
                    "province": province, "city": city, "days": picked,
                    "source": "forecast", "source_note": "预报数据来自 Open-Meteo",
                    "as_of": today, "degraded": False, "degraded_reason": "",
                }
            reason = "预报没有返回未来的日期"
        except weather.WeatherUnavailable as e:
            logger.warning("预报不可用，改用气候基准值：%s", e)
            reason = str(e)
    else:
        reason = "该田块没有经纬度，无法获取天气"

    return _normals_weather(conn, province, city, days=days, reason=reason)


def _normals_weather(conn: sqlite3.Connection, province: str, city: str, *, days: int, reason: str) -> dict:
    """用气候基准值拼出未来 N 天。拿不到基准值就返回空 —— 宁可为空，不编数字。"""
    normals = repo.get_normals(conn, province, city) if (province and city) else {}
    today = today_local()
    rows = []
    for i in range(days):
        d = today + timedelta(days=i)
        md = d.strftime("%m-%d")
        if md not in normals:
            continue
        avg = normals[md]["avg"]
        std = normals[md]["std"]
        # 用平均值当中心、标准差当日较差的一半，给出一组**自洽**的极值。
        # 明确说明这是气候平均推出来的，不是预报。
        spread = max(std, 2.0)
        rows.append({
            "date": d.isoformat(), "tmax": round(avg + spread, 1),
            "tmin": round(avg - spread, 1), "tmean": round(avg, 1),
            "precip": 0.0, "humidity": 0.0,
        })

    if not rows:
        return {
            "province": province, "city": city, "days": [], "source": "none",
            "source_note": "天气数据不可用，且没有该地的气候基准值",
            "as_of": "", "degraded": True,
            "degraded_reason": reason or "天气数据不可用",
        }

    return {
        "province": province, "city": city, "days": rows, "source": "normal",
        "source_note": "预报不可用，以下为当地多年气候平均值（不是预报）",
        "as_of": today_str(), "degraded": True,
        "degraded_reason": reason or "预报不可用",
    }
