#!/usr/bin/env python
"""任务 1.2 —— 按 8:1:1 划分训练/验证/测试集，生成路径标签对照表。

产出：data/splits/{train,val,test}.csv

用法：
    python scripts/02_split_dataset.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger   # noqa: E402
from src.data.split import split_dataset            # noqa: E402

logger = setup_logger("agri.split")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 1.2 数据集划分")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--test-ratio", type=float, default=0.1)
    parser.add_argument("--val-cap", type=int, default=50, help="验证集单类上限（长尾大类）")
    parser.add_argument("--test-cap", type=int, default=50, help="测试集单类上限（长尾大类）")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    result = split_dataset(
        val_ratio=args.val_ratio,
        test_ratio=args.test_ratio,
        val_cap=args.val_cap,
        test_cap=args.test_cap,
        seed=args.seed,
    )

    total = len(result.train) + len(result.val) + len(result.test)
    logger.info("=" * 62)
    logger.info("划分完成：合计 %d 条样本，已写出 3 个 CSV", total)
    logger.info("=" * 62)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
