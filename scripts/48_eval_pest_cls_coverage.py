#!/usr/bin/env python
"""任务 2.8.7 —— 零框类的分类可达性评测。

## 为什么需要这个脚本

检测头设了 102 类，但 IP102 的检测标注只有 18976 张带框图，5 个类在 train/val/test
三个划分里一个框都没有：

    59 viteus_vitifoliae          葡萄根瘤蚜
    60 colomerus_vitis            葡萄缺节瘿螨
    63 polyphagotars_onemus_latus 侧多食跗线螨
    75 phyllocoptes_oleiverus     橄榄瘿螨
    80 parlatoria_zizyphus_lucus  黑片盾蚧

这 5 类的检测输出头从未见过正样本，整图识别流程「检测出框 → 裁切 → 分类」走到第一步
就断了。但它们在 IP102 **分类集**里有图，102 类齐全。

本脚本用已交付的虫害分类器直接吃分类集原图，量出这 5 类在纯分类路径上能到多少。
若能认，则第三档成立：检测分支负责可框的 97 类，无框类走整图分类兜底。

## 口径

- 分类器的训练集是 `classification/train`，本脚本评的是 `classification/test`，无泄漏。
- 42 号感知哈希标出的 `leaked` 同时剔除，避免与检测集近重复的图混进评估。
- 预处理与 46 号一致：Resize(255) + CenterCrop(224)，ImageNet 归一化。

## 用法

    python scripts/48_eval_pest_cls_coverage.py
    python scripts/48_eval_pest_cls_coverage.py --classes 59,60,63,75,80
    python scripts/48_eval_pest_cls_coverage.py --all --limit-per-class 60
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import write_json                 # noqa: E402
from src.common.logging_utils import setup_logger          # noqa: E402
from src.common.paths import path_of                       # noqa: E402

logger = setup_logger("agri.pest.cls_coverage")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
NUM_CLASSES = 102
ZERO_BOX = [59, 60, 63, 75, 80]


def _names() -> list[str]:
    """从 data/pest/data.yaml 读 102 类名，索引即类别号。"""
    y = Path("data/pest/data.yaml").read_text(encoding="utf-8")
    out = []
    for line in y.splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def _leaked_paths() -> set[str]:
    f = path_of("outputs_dir") / "reports" / "pest_phash_index.json"
    if not f.exists():
        return set()
    data = json.loads(f.read_text(encoding="utf-8"))
    return {str(Path(e["path"]).resolve()) for e in data.get("leaked", [])}


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
    ap = argparse.ArgumentParser(description="零框类的分类可达性评测")
    ap.add_argument("--ckpt", default="models/pest_cls/resnet50_224.pt")
    ap.add_argument("--split", default="test", choices=["train", "val", "test"])
    ap.add_argument("--classes", default=",".join(str(c) for c in ZERO_BOX))
    ap.add_argument("--all", action="store_true", help="评全部 102 类")
    ap.add_argument("--limit-per-class", type=int, default=0)
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--out", default="pest_cls_coverage.json")
    args = ap.parse_args()

    import torch
    from PIL import Image
    from torchvision import transforms as T

    ckpt_path = Path(args.ckpt)
    if not ckpt_path.exists():
        raise SystemExit("分类权重不存在：%s" % ckpt_path)
    ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    arch = ck.get("arch", "resnet50")
    size = int(ck.get("size", 224))
    num_classes = int(ck.get("num_classes", NUM_CLASSES))

    model = _build_model(arch, num_classes)
    model.load_state_dict(ck["state_dict"])
    model.eval()

    names = _names()
    leaked = _leaked_paths()
    root = path_of("pest_cls_raw") / "extracted" / "classification" / args.split

    targets = list(range(NUM_CLASSES)) if args.all else [int(x) for x in args.classes.split(",")]

    tf = T.Compose([
        T.Resize(int(size * 1.14)),
        T.CenterCrop(size),
        T.ToTensor(),
        T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])

    per_class: dict[int, dict] = {}
    buf, buf_y = [], []

    def flush():
        if not buf:
            return []
        x = torch.stack(buf)
        with torch.no_grad():
            logits = model(x)
        top5 = logits.topk(min(5, logits.size(1)), dim=1).indices
        pred1 = top5[:, 0]
        rows = []
        for i, y in enumerate(buf_y):
            rows.append((y, int(pred1[i]), [int(v) for v in top5[i]]))
        buf.clear()
        buf_y.clear()
        return rows

    for c in targets:
        d = root / str(c)
        if not d.is_dir():
            per_class[c] = {"label_cn": names[c] if c < len(names) else str(c),
                            "num": 0, "note": "目录不存在"}
            continue
        files = [p for p in sorted(d.iterdir()) if p.suffix.lower() in IMAGE_EXT]
        files = [p for p in files if str(p.resolve()) not in leaked]
        if args.limit_per_class:
            files = files[: args.limit_per_class]
        hit1 = hit5 = 0
        confusions: dict[int, int] = {}
        for p in files:
            try:
                with Image.open(p) as im:
                    buf.append(tf(im.convert("RGB")))
            except Exception:
                continue
            buf_y.append(c)
            if len(buf) >= args.batch:
                for y, p1, t5 in flush():
                    hit1 += int(p1 == y)
                    hit5 += int(y in t5)
                    if p1 != y:
                        confusions[p1] = confusions.get(p1, 0) + 1
        for y, p1, t5 in flush():
            hit1 += int(p1 == y)
            hit5 += int(y in t5)
            if p1 != y:
                confusions[p1] = confusions.get(p1, 0) + 1
        n = len(files)
        per_class[c] = {
            "label_cn": names[c] if c < len(names) else str(c),
            "label_latin": names[c] if c < len(names) else str(c),
            "num": n,
            "top1": round(hit1 / n, 4) if n else 0.0,
            "top5": round(hit5 / n, 4) if n else 0.0,
            "often_as": [
                {"index": k, "label": names[k] if k < len(names) else str(k), "n": v}
                for k, v in sorted(confusions.items(), key=lambda kv: -kv[1])[:4]
            ],
        }
        logger.info("类 %3d %-38s n=%-5d top1=%.4f top5=%.4f",
                    c, per_class[c]["label_cn"], n, per_class[c]["top1"], per_class[c]["top5"])

    zero = [v for k, v in per_class.items() if k in ZERO_BOX and v["num"] > 0]
    tot = sum(v["num"] for v in per_class.values() if v["num"])
    h1 = sum(v["top1"] * v["num"] for v in per_class.values() if v["num"])
    h5 = sum(v["top5"] * v["num"] for v in per_class.values() if v["num"])
    report = {
        "split": args.split,
        "ckpt": str(ckpt_path),
        "arch": arch,
        "size": size,
        "num_classes": num_classes,
        "scope": "all" if args.all else "zero_box",
        "leaked_excluded": len(leaked),
        "per_class": {str(k): v for k, v in per_class.items()},
        "zero_box_summary": {
            "classes": ZERO_BOX,
            "num_images": sum(v["num"] for v in zero),
            "macro_top1": round(sum(v["top1"] for v in zero) / len(zero), 4) if zero else 0.0,
            "macro_top5": round(sum(v["top5"] for v in zero) / len(zero), 4) if zero else 0.0,
            "weighted_top1": round(sum(v["top1"] * v["num"] for v in zero) / max(sum(v["num"] for v in zero), 1), 4) if zero else 0.0,
            "weighted_top5": round(sum(v["top5"] * v["num"] for v in zero) / max(sum(v["num"] for v in zero), 1), 4) if zero else 0.0,
        },
    }
    if args.all:
        report["overall"] = {
            "num_images": tot,
            "top1": round(h1 / tot, 4) if tot else 0.0,
            "top5": round(h5 / tot, 4) if tot else 0.0,
        }
        logger.info("全部 %d 类：n=%d top1=%.4f top5=%.4f",
                    len(per_class), tot, report["overall"]["top1"], report["overall"]["top5"])

    out = path_of("outputs_dir") / "reports" / args.out
    write_json(out, report)
    logger.info("写出 %s", out)
    print(json.dumps({k: v for k, v in report.items() if k != "per_class"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
