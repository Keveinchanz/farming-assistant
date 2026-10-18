#!/usr/bin/env python
"""任务 7.2.4 —— 预热天气缓存（演示前跑一次，避免第一次打开农田页等十几秒）。

做两件事：
    1. 把每块田从播种日到今天的逐日气温拉进 daily_temp（带 TTL 判断，新鲜就跳过）
    2. 把每个省市的**气候基准值**（5 年同日分组的均值与标准差）算好写进 climate_normals
       —— 那张表是全局共享的（不带 user_id），同一个城市只该拉一次

顺带把每块田的积温与阶段打出来。这同时是 configs/crop_stages.yaml 里那段
「阈值是这么标定出来的」的可复算依据 —— 示例田的播种日是「今天回推 30/70/200 天」，
所以任何时候跑这个脚本，看到的都应该是分蘖期 / 抽穗期 / 成熟期。

用法：
    python scripts/25_warm_farm_cache.py
    python scripts/25_warm_farm_cache.py --normals-only    # 只补气候基准值
    python scripts/25_warm_farm_cache.py --skip-normals    # 只拉逐日气温
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402
from src.farm import gdd, growth, repo, stages, weather  # noqa: E402
from src.farm.db import connect  # noqa: E402
from src.farm.timeutil import today_str  # noqa: E402

logger = setup_logger("agri.farm.warm")


def _warm_normals(conn, cities: set[tuple[str, str, float, float]]) -> int:
    """补气候基准值。cities 是 {(省, 市, lng, lat)}。"""
    count = 0
    for province, city, lng, lat in sorted(cities):
        if not (province and city and lng is not None and lat is not None):
            continue
        if repo.has_normals(conn, province, city):
            logger.info("气候基准值已存在，跳过：%s %s", province, city)
            continue
        try:
            normals = weather.fetch_normals(lng, lat)
            repo.upsert_normals(conn, province, city, normals)
            count += len(normals)
            logger.info("气候基准值已写入：%s %s（%d 天）", province, city, len(normals))
        except weather.WeatherUnavailable as e:
            # 拉不到不是致命错误：预报不可用时还有别的降级路径，
            # 但要让用户知道这条兜底数据现在是缺的。
            logger.warning("气候基准值拉取失败（%s %s）：%s", province, city, e)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="预热天气缓存")
    parser.add_argument("--normals-only", action="store_true", help="只补气候基准值")
    parser.add_argument("--skip-normals", action="store_true", help="只拉逐日气温")
    args = parser.parse_args()

    today = today_str()
    with connect() as conn:
        users = conn.execute("SELECT id, username FROM users ORDER BY id").fetchall()
        if not users:
            logger.warning("还没有任何账号。先在网页上注册一个（会自动预置三条示例田），"
                           "或运行 scripts/23_init_farm_db.py 建库后再来。")
            return 0

        cities: set[tuple[str, str, float, float]] = set()
        total_fields = 0

        for user in users:
            uid = user["id"]
            fields = repo.list_fields(conn, uid)
            if not fields:
                continue
            logger.info("用户 %s：%d 块田", user["username"], len(fields))

            for field in fields:
                total_fields += 1
                cities.add((field.get("province", ""), field.get("city", ""),
                            field.get("lng"), field.get("lat")))

                if args.normals_only:
                    continue

                status = growth.ensure_history(conn, uid, field)
                g = growth.compute_growth(field, status)
                series = gdd.accumulate(status["rows"], stages.crop_config(field["crop"]))
                logger.info("  %-16s %-3s 播后 %3d 天 | 积温 %6.0f/%6.0f | %-4s | 进度 %3.0f%% | %s",
                            field["name"], field["crop"], g["days_since_sow"],
                            g["gdd_cum"], g["maturity_gdd"], g["stage"],
                            g["progress"] * 100, status["source_note"])

        if not args.skip_normals:
            n = _warm_normals(conn, cities)
            logger.info("气候基准值合计写入 %d 天（%d 个城市）", n, len(cities))

        latest = conn.execute("SELECT MAX(date) AS d FROM daily_temp").fetchone()["d"]
        logger.info("逐日气温缓存已到 %s（今天 %s），共 %d 块田",
                    latest or "—", today, total_fields)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
