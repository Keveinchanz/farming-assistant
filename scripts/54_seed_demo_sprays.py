#!/usr/bin/env python
"""给演示账号补几条施药记录，让「用药台账」在演示时不是空页。

**为什么需要**：台账的每一条都来自 type=spray 的农事记录。演示账号一条都没有，
打开台账就是空态 —— 等于这个功能不存在。所以预置七条，刻意覆盖三种状态：

    水稻 10-20 那次解禁 11-10  → 还在间隔期（台账顶部红条靠它）
    玉米 10-08 那次解禁 10-22  → 已过、可采收
    油菜两条都没填间隔期        → 未记录（是「不知道」，不是「可以摘」）

水稻那两次用的是同一个药（稻瘟灵），会触发「连续两次同一有效成分，建议轮换」。
药剂与登记证号都是真的（data/pesticide_registry.json 里查得到），日期相对今天算。

用法：
    python scripts/54_seed_demo_sprays.py               # 给演示账号补数据
    python scripts/54_seed_demo_sprays.py --dry-run     # 只看会补哪几条
    python scripts/54_seed_demo_sprays.py --today 2026-10-24 --username user
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402
from src.farm import db, ledger, repo, seed, users  # noqa: E402
from src.farm.timeutil import today_local  # noqa: E402

logger = setup_logger("agri.farm.seed_sprays")


def main() -> int:
    parser = argparse.ArgumentParser(description="给演示账号预置施药记录（用药台账）")
    parser.add_argument("--username", default=users.DEMO_ACCOUNT_USERNAME,
                        help="目标账号（默认演示账号 user）")
    parser.add_argument("--today", default="", help="以哪天为「今天」，留空用本机当天")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = parser.parse_args()

    path = db.farm_db_path()
    logger.info("数据库位置：%s", path)

    with db.connect(path) as conn:
        user = users.find_by_username(conn, args.username)
        if user is None:
            logger.error("账号不存在：%s（先启动一次服务，或跑 scripts/23_init_farm_db.py）",
                         args.username)
            return 1

        have = {f["crop"] for f in repo.list_fields(conn, user["id"])}
        need = {item["crop"] for item in seed.DEMO_SPRAYS}
        if need - have:
            logger.error("账号 %s 缺少这些作物的田块：%s（演示记录必须挂在真实田块上）",
                         args.username, "、".join(sorted(need - have)))
            return 1

        if args.dry_run:
            for item in seed.DEMO_SPRAYS:
                gap = "未填间隔期" if item["days"] is None else f"{item['days']} 天"
                logger.info("将补：%s · %d 天前 · %s（%s）· %s", item["crop"], item["back"],
                            item["item"], item["reg_no"], gap)
            return 0

        created = seed.seed_demo_sprays(conn, user["id"], today=args.today or None)
        logger.info("新增 %d 条（已存在的跳过）", len(created))

        data = ledger.build(conn, user["id"], today=args.today or today_local().isoformat())
        s = data["summary"]
        logger.info("区间 %s", data["period"]["label"])
        logger.info("施药 %d 次 / 药剂 %d 种 / 仍在间隔期 %d 块 / 未记录间隔期 %d 块",
                    s["spray_count"], s["product_count"], s["locked_fields"],
                    s["unknown_fields"])
        for row in data["fields"]:
            logger.info("  %-12s %-4s %-8s %s", row["name"], row["crop"],
                        row["status"], row["earliest_date"] or "—")
        for g in data["products"]:
            logger.info("  %-22s ×%d %s", g["product"], g["count"],
                        "（建议轮换）" if g["rotate_warning"] else "")

    logger.info("完成。打开 /ledger.html 就能看到台账。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
