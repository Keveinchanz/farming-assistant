#!/usr/bin/env python
"""给演示账号补几条「待回访」的诊断，让回访队列在演示时不是空的。

**为什么需要**：回访队列一条待办都没有时整块卡片是隐藏的（farm.js 的
`if (!all.length) card.hidden = true`）—— 这是对的，一块写着「0 条」的卡片
只是噪音；但演示时它意味着「这个功能看不见」。所以演示账号要预置几条：

    两条已过期（拖了 9 天、3 天）→ 队列最上面那两行，也是农技员真会漏掉的
    一条今天到期              → 「今天该回访谁」
    三条未来 7 天内            → 让队列有续，而不是清一色红

日期全部**相对今天**算（见 src/farm/seed.py 的 DEMO_FOLLOWUPS），
隔几周再演示也不会变成「已过期 400 天」。固定 id + 幂等：反复跑只补缺的。

用法：
    python scripts/53_seed_demo_followups.py               # 给演示账号补数据
    python scripts/53_seed_demo_followups.py --dry-run     # 只看会补哪几条
    python scripts/53_seed_demo_followups.py --today 2026-10-24 --username user
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402
from src.farm import db, repo, seed, users  # noqa: E402
from src.farm.timeutil import today_local  # noqa: E402

logger = setup_logger("agri.farm.seed_followups")


def main() -> int:
    parser = argparse.ArgumentParser(description="给演示账号预置待回访诊断")
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
        need = {item["crop"] for item in seed.DEMO_FOLLOWUPS}
        if need - have:
            logger.error("账号 %s 缺少这些作物的田块：%s（演示记录必须挂在真实田块上）",
                         args.username, "、".join(sorted(need - have)))
            return 1

        if args.dry_run:
            for item in seed.DEMO_FOLLOWUPS:
                logger.info("将补：%s · %s · %s（回访 %+d 天）", item["crop"], item["label"],
                            item["subject"], item["recheck_offset"])
            return 0

        created = seed.seed_demo_followups(conn, user["id"], today=args.today or None)
        logger.info("新增 %d 条（已存在的跳过）", len(created))

        groups = repo.list_followups(conn, user["id"], today=args.today or
                                     today_local().isoformat())
        label = {"overdue": "已过期", "today": "今天", "upcoming": "未来 7 天"}
        for key in ("overdue", "today", "upcoming"):
            rows = groups[key]
            logger.info("%s：%d 条", label[key], len(rows))
            for it in rows:
                logger.info("  %-10s %-6s %-8s 诊断 %s → 回访 %s（%+d 天）",
                            it["subject"] or "—", it["crop"], it["label"], it["date"],
                            it["recheck_date"], it["days_left"])

    logger.info("完成。打开 /farm.html 就能看到回访队列。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
