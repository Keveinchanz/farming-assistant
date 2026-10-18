#!/usr/bin/env python
"""任务 1.1 —— 整理数据集：按「作物大类-病虫害名称」二级目录分类归档。

产出：
    data/processed/dataset/<作物大类>/<病虫害名称>/<目录号>_<序号>.jpg
    data/processed/manifest.json
    outputs/reports/unparsed_crops.txt

用法：
    python scripts/01_organize_data.py            # 增量归档（已存在的同尺寸文件跳过）
    python scripts/01_organize_data.py --force    # 全量重新复制
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger   # noqa: E402
from src.data.organize import organize              # noqa: E402

logger = setup_logger("agri.organize")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 1.1 数据整理归档")
    parser.add_argument("--force", action="store_true", help="忽略已存在文件，全量重新复制")
    args = parser.parse_args()

    manifest = organize(force=args.force)

    logger.info("=" * 62)
    logger.info("归档完成：%d 个类别 / %d 个作物大类 / %d 张图片",
                manifest["num_classes"], manifest["num_crops"], manifest["num_images"])
    logger.info("清单文件：data/processed/manifest.json")
    logger.info("=" * 62)
    logger.info("按作物大类分布：")
    for crop, info in sorted(manifest["crops"].items(), key=lambda kv: -kv[1]["num_images"]):
        logger.info("  %-8s %-6s %3d 类 %6d 张", crop, info["category"],
                    info["num_classes"], info["num_images"])

    if manifest["num_images"] == 0:
        logger.error("未归档任何图片，请检查 Sample/ 目录")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
