#!/usr/bin/env python
"""任务 1.6 —— 采集 2026 年官方惠农政策条目（标题、核心内容、发布单位、发布日期）。

产出：data/policy_list.json
依赖：联网访问 jcs.moa.gov.cn / hzjjs.moa.gov.cn（实测可达）

用法：
    python scripts/05_fetch_policies.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger     # noqa: E402
from src.content.policy_fetch import build_and_save   # noqa: E402

logger = setup_logger("agri.policy")


def main() -> int:
    data = build_and_save()
    logger.info("=" * 62)
    logger.info("完成：共 %d 条政策，抓取成功 %d 条",
                data["total"], sum(1 for it in data["items"] if it["fetched"]))
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
