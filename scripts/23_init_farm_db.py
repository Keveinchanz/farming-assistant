#!/usr/bin/env python
"""任务 7.0.10（数据侧）—— 建立农田数据库并迁移到最新版本。

阶段 7 引入的项目里**第一个可变数据**：账号、田块、农事记录、诊断与收获。
它落在 data/farm/farm.db 这个 sqlite 文件里（路径见 configs/default.yaml 的 paths.farm_db）。

本脚本做的事情（幂等，可以反复跑）：
    1. 建库文件与目录（data/farm/）
    2. 按 PRAGMA user_version 顺序执行 src/farm/migrations/*.sql，只补缺的那几条
    3. 打印最终版本号与各表行数

服务启动时（backend/app/main.py 的 lifespan）也会自动做同样的事，
所以「忘了跑这个脚本」不会导致农田功能不可用；它的价值在于
① 部署后不需要拉起服务就能验收表结构；② 想看清楚迁移到底做了什么。

用法：
    python scripts/23_init_farm_db.py
    python scripts/23_init_farm_db.py --stats      # 顺便打印各表行数
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402
from src.farm import db  # noqa: E402

logger = setup_logger("agri.farm.init")

TABLES = (
    "users", "sessions", "user_settings", "login_attempts",
    "fields", "logs", "daily_temp", "climate_normals",
    "diagnoses", "growth_snapshots", "harvest_records", "reminder_snoozes",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="建立/迁移农田数据库")
    parser.add_argument("--stats", action="store_true", help="打印各表行数")
    args = parser.parse_args()

    path = db.farm_db_path()
    logger.info("数据库位置：%s", path)

    with db.connect(path) as conn:
        before = db.applied_version(conn)
        done = db.migrate(conn)
        after = db.applied_version(conn)

        logger.info("版本：v%d -> v%d%s", before, after,
                    "（已是最新）" if not done else f"（执行了 {len(done)} 条迁移）")

        missing = [t for t in TABLES if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
        if missing:
            logger.error("缺少数据表：%s", "、".join(missing))
            return 1
        logger.info("全部 %d 张表就位", len(TABLES))

        if args.stats:
            for table in TABLES:
                n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                logger.info("  %-18s %d 行", table, n)

    logger.info("完成。接下来可以：python scripts/20_run_backend.py --no-browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
