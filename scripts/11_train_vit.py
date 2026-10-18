#!/usr/bin/env python
"""任务 2.2 / 2.3 / 2.4 —— 训练 ViT-B/16。

注意：8GB 显存下 ViT-B/16 需要 batch_size=16 + 梯度累积 2 + 梯度检查点（已在配置里设好），
否则会 OOM。首次运行会从 HuggingFace 下载约 330MB 的预训练权重到 models/pretrained/hf/。

本项目以 ResNet50 为主线交付（后端默认加载 ONNX 版 ResNet50）；
ViT 训练耗时更长，按需自行运行：
    python scripts/11_train_vit.py --probe      # 先实测吞吐与显存
    python scripts/11_train_vit.py              # 再完整训练
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger          # noqa: E402
from src.train.runner import run_epoch_probe, run_training  # noqa: E402

logger = setup_logger("agri.train.vit")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 2.2/2.3/2.4 训练 ViT-B/16")
    parser.add_argument("--config", default="train_vit")
    parser.add_argument("--run-name", default="vit")
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    if args.probe:
        run_epoch_probe(args.config, run_name=f"{args.run_name}_probe", epochs=1)
        return 0

    summary = run_training(args.config, run_name=args.run_name, epochs=args.epochs)
    logger.info("完成：最佳 val_acc=%.4f @ epoch %d", summary["best_val_acc"], summary["best_epoch"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
