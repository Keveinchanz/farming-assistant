#!/usr/bin/env python
"""任务 2.6 —— 模型导出为 ONNX，用于后端推理部署。

产出：models/onnx/inference_model.onnx
      models/registry.json 里补上 onnx 路径（后端据此自动加载）

导出后会用同一输入比对 torch 与 onnxruntime 的输出，防止导出静默出错。

用法：
    python scripts/13_export_onnx.py
    python scripts/13_export_onnx.py --run-name vit --output models/onnx/vit.onnx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger     # noqa: E402
from src.export.export_onnx import export_onnx        # noqa: E402

logger = setup_logger("agri.export")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 2.6 导出 ONNX")
    parser.add_argument("--run-name", default="resnet50")
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--no-verify", action="store_true", help="跳过数值对齐校验（不推荐）")
    args = parser.parse_args()

    try:
        path = export_onnx(
            run_name=args.run_name,
            checkpoint=args.checkpoint,
            output=args.output,
            opset=args.opset,
            verify=not args.no_verify,
        )
    except AssertionError as e:
        logger.error("导出校验失败：%s", e)
        return 2

    logger.info("=" * 62)
    logger.info("导出完成：%s", path)
    logger.info("下一步：python scripts/20_run_backend.py 启动后端，接口会自动加载该 ONNX")
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
