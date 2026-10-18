#!/usr/bin/env python
"""任务 1.5 —— 整理全国各省病虫害 TOP3 数据（按省份、季节维度分类）。

产出：data/province_pest_top3.json

⚠ 该文件为结构化演示数据（demo_data=true）：官方公开资料只有全国维度趋势，
   没有逐省逐季明细。生成规则与免责说明见 src/content/province_pest.py 顶部注释。

用法：
    python scripts/04_gen_province_pest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger   # noqa: E402
from src.content.province_pest import build_and_save  # noqa: E402

logger = setup_logger("agri.province_pest")


def main() -> int:
    data = build_and_save()
    logger.info("=" * 62)
    logger.info("完成：%d 个省级行政区 × %d 个季节",
                len(data["provinces"]), len(data["seasons"]))
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
