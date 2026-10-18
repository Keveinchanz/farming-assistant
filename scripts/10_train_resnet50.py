#!/usr/bin/env python
"""任务 2.3 / 2.4 —— 训练 ResNet50。

产出：
    models/checkpoints/best_resnet50.pth   验证集最优权重
    models/checkpoints/last_resnet50.pth
    outputs/logs/train_resnet50.csv        逐 epoch 的 loss / 准确率
    models/registry.json                   模型元数据

用法（PowerShell）：
    conda activate az
    cd /d "D:\\code file\\insert"

    python scripts/10_train_resnet50.py                 # 按配置跑完整训练（40 epoch + 早停）
    python scripts/10_train_resnet50.py --epochs 2      # 快速试跑 2 个 epoch
    python scripts/10_train_resnet50.py --probe         # 只跑 1 epoch 实测吞吐并估算总时长
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger          # noqa: E402
from src.train.runner import run_epoch_probe, run_training  # noqa: E402

logger = setup_logger("agri.train")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 2.3/2.4 训练 ResNet50")
    parser.add_argument("--config", default="train_resnet50", help="configs/<name>.yaml")
    parser.add_argument("--run-name", default="resnet50", help="权重与日志的命名标识")
    parser.add_argument("--epochs", type=int, default=None, help="覆盖配置里的 epoch 数")
    parser.add_argument("--probe", action="store_true",
                        help="只跑 1 个 epoch 实测吞吐与显存，用于估算完整训练时长")
    args = parser.parse_args()

    if args.probe:
        # 试跑用独立命名，避免把正式权重与 registry 元数据覆盖成 1 个 epoch 的结果
        run_epoch_probe(args.config, run_name=f"{args.run_name}_probe", epochs=1)
        return 0

    summary = run_training(args.config, run_name=args.run_name, epochs=args.epochs)
    logger.info("=" * 62)
    logger.info("完成：最佳 val_acc=%.4f @ epoch %d，权重见 models/checkpoints/best_%s.pth",
                summary["best_val_acc"], summary["best_epoch"], args.run_name)
    logger.info("下一步：python scripts/12_evaluate.py  然后  python scripts/13_export_onnx.py")
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    # Windows 下 DataLoader 多进程要求入口受 __main__ 保护，否则子进程会重复执行本文件
    raise SystemExit(main())
