#!/usr/bin/env python
"""任务 2.8.8 —— 虫害分类器导出 ONNX（102 类）。

## 为什么要导出

线上检测走 onnxruntime，不装 torch。分类器原本只有 `models/pest_cls/resnet50_224.pt`，
只在 47 号的离线评测里用过，服务端拿不到。要让它参与线上推理，必须转成 ONNX。

## 导出约定

与 `models/onnx/inference_model.onnx`（病害 271 类）和 `pest_yolo.onnx`（检测）保持一致：

    输入   images   float32  [1, 3, 224, 224]   ImageNet 归一化，RGB
    输出   logits   float32  [1, 102]           未过 softmax 的原始得分
    opset  17

静态 batch=1：端侧一次问诊就是一张图，动态轴只会让 onnxruntime 多一层 shape 推断。

## 用法

    python scripts/49_export_pest_cls_onnx.py
    python scripts/49_export_pest_cls_onnx.py --ckpt models/pest_cls/resnet50_224.pt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import write_json                # noqa: E402
from src.common.logging_utils import setup_logger         # noqa: E402
from src.common.paths import path_of                      # noqa: E402

logger = setup_logger("agri.pest.cls_export")


def _build_model(arch: str, num_classes: int):
    import torch.nn as nn
    from torchvision import models

    os.environ.setdefault("TORCH_HOME", str(path_of("pretrained_dir")))
    if arch == "resnet50":
        m = models.resnet50(weights=None)
        dim = m.fc.in_features
    elif arch == "resnet34":
        m = models.resnet34(weights=None)
        dim = m.fc.in_features
    else:
        raise SystemExit("不支持的 arch：%s" % arch)
    m.fc = nn.Sequential(nn.Dropout(0.2), nn.Linear(dim, num_classes))
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="虫害分类器导出 ONNX")
    ap.add_argument("--ckpt", default="models/pest_cls/resnet50_224.pt")
    ap.add_argument("--out", default="models/onnx/pest_cls.onnx")
    ap.add_argument("--opset", type=int, default=17)
    ap.add_argument("--skip-check", action="store_true", help="不做 PyTorch 与 ONNX 的对齐校验")
    args = ap.parse_args()

    import torch

    ck_path = Path(args.ckpt)
    if not ck_path.exists():
        raise SystemExit("分类权重不存在：%s" % ck_path)
    ck = torch.load(ck_path, map_location="cpu", weights_only=False)
    arch = ck.get("arch", "resnet50")
    size = int(ck.get("size", 224))
    num_classes = int(ck.get("num_classes", 102))

    model = _build_model(arch, num_classes)
    model.load_state_dict(ck["state_dict"])
    model.eval()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    dummy = torch.randn(1, 3, size, size)
    torch.onnx.export(
        model, dummy, str(out),
        input_names=["images"], output_names=["logits"],
        opset_version=args.opset,
        do_constant_folding=True,
        dynamo=False,
    )
    logger.info("导出 %s（%.1f MB, opset=%d, 输入 1x3x%dx%d, 输出 1x%d）",
                out, out.stat().st_size / 1e6, args.opset, size, size, num_classes)

    report = {
        "ckpt": str(ck_path),
        "onnx": str(out),
        "arch": arch,
        "num_classes": num_classes,
        "input_size": size,
        "opset": args.opset,
        "onnx_size_mb": round(out.stat().st_size / 1e6, 1),
        "input_name": "images",
        "output_name": "logits",
        "onnx_dynamic": False,
    }

    if not args.skip_check:
        try:
            import numpy as np
            import onnxruntime as ort

            x = torch.randn(4, 3, size, size)
            with torch.no_grad():
                ref = model(x).numpy()
            sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
            got = sess.run(None, {"images": x.numpy()})[0]
            diff = float(np.abs(ref - got).max())
            agree = bool((ref.argmax(1) == got.argmax(1)).all())
            report["align_check"] = {"max_abs_diff": diff, "top1_agree": agree}
            logger.info("对齐校验：最大绝对误差 %.2e，Top-1 一致=%s", diff, agree)
        except ImportError:
            logger.warning("本环境没有 onnxruntime，跳过对齐校验")

    write_json(path_of("outputs_dir") / "reports" / "pest_cls_onnx.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
