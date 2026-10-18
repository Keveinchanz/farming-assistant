#!/usr/bin/env python
"""任务 2.5 —— 模型评估：测试集准确率 / 精确率 / 召回率，绘制混淆矩阵。

产出：
    outputs/reports/eval_<run>.json
    outputs/figures/confusion_matrix_<run>.png

用法：
    python scripts/12_evaluate.py                       # 评估 ResNet50 测试集
    python scripts/12_evaluate.py --split val           # 评估验证集
    python scripts/12_evaluate.py --max-samples 300     # 只用 300 条快速看一眼
    python scripts/12_evaluate.py --run-name vit --config train_vit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger   # noqa: E402
from src.eval.evaluate import evaluate              # noqa: E402

logger = setup_logger("agri.evaluate")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 2.5 模型评估")
    parser.add_argument("--run-name", default="resnet50")
    parser.add_argument("--config", default="train_resnet50")
    parser.add_argument("--checkpoint", default=None, help="指定权重路径（默认取配置里的最佳权重）")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--max-samples", type=int, default=None, help="只评估前 N 条")
    args = parser.parse_args()

    report = evaluate(
        run_name=args.run_name,
        checkpoint=args.checkpoint,
        config_name=args.config,
        split=args.split,
        max_samples=args.max_samples,
    )
    return 0 if report["num_samples"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
