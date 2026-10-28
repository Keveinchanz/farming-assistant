#!/usr/bin/env python
"""任务 2.8.5 —— 把检测真值框裁成「分类视角」样本，供两阶段（检测→裁剪→分类）训练。

## 为什么两阶段

单阶段 YOLOv8s 在 640 输入下，一张图里虫子只占几十个像素，102 类的**近缘物种**
（亚夜蛾 vs 甜菜夜蛾、斑蝥 vs 绿芫菁）在这种尺度上本来就难分。40 号报告的 top-1
错误几乎全集中在这类「长得像」的类别对上 —— 不是没找到虫，是**分不清是哪只**。

两阶段把两件事拆开：
  1. 检测仍走 640，只回答「虫在哪」（这块已经够用：实例召回 70%，无检出率 3.4%）；
  2. 把框裁出来 → 放大到 224/256 → 交给分类器。分类器看到的是「虫体占满画面」
     的图，正好也是 IP102 **分类集**（7.5 万张、102 类）的形态，那 7.5 万张终于能
     全用上（检测集只有 1.9 万张、1.6 万个框）。

本脚本只做第一步：用 `data/pest` 的**真框**裁出 `data/pest_crops/{train,val,test}/<类号>/`。
用真框而非伪框，是为了让分类器的训练样本不受检测误差污染；评估阶段才换成真检测框。

## 裁剪口径

以框中心取**正方形**，边长 = max(w,h) × (1 + 2·pad)，默认 pad=0.25（带一点周边
叶面/背景，和真检测框的观感一致）。越界就整体平移回图内；图比边长还小就取整幅。
输出统一 `--size` 见方（默认 256），JPEG q=92 —— 256 是为了一步到位支持后续
把分类输入从 224 提到 256（虫体细节更多），要不要用由训练脚本决定。

## 用法

    python scripts/45_build_pest_crops.py                 # 三个 split 全建
    python scripts/45_build_pest_crops.py --splits train  # 只建训练集
    python scripts/45_build_pest_crops.py --pad 0.15 --size 320
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.crops")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _read_boxes(label_file: Path) -> list[tuple[int, float, float, float, float]]:
    out = []
    for line in label_file.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            c = int(float(parts[0]))
            cx, cy, w, h = (float(v) for v in parts[1:5])
        except ValueError:
            continue
        if w <= 0 or h <= 0:
            continue
        out.append((c, cx, cy, w, h))
    return out


def _square_box(cx: float, cy: float, w: float, h: float, pad: float,
                img_w: int, img_h: int) -> tuple[int, int, int, int]:
    """归一化中心框 -> 像素正方形 (x1,y1,x2,y2)，越界平移回图内。"""
    side = max(w, h) * (1.0 + 2.0 * pad) * max(img_w, img_h)
    side = min(max(side, 16.0), float(min(img_w, img_h)))  # 不超过短边，避免重采样糊掉
    cx_px, cy_px = cx * img_w, cy * img_h
    x1 = cx_px - side / 2
    y1 = cy_px - side / 2
    x1 = min(max(x1, 0.0), img_w - side)
    y1 = min(max(y1, 0.0), img_h - side)
    x2, y2 = x1 + side, y1 + side
    return int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))


def build_split(split: str, root: Path, out_root: Path, *, pad: float, size: int,
                limit: int = 0) -> dict:
    import cv2

    img_dir = root / "images" / split
    lbl_dir = root / "labels" / split
    images = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMAGE_EXT)
    if limit:
        images = images[:limit]

    per_class: Counter[int] = Counter()
    n_img = n_box = n_skip = 0
    for i, img_path in enumerate(images, 1):
        lbl = lbl_dir / f"{img_path.stem}.txt"
        if not lbl.exists():
            continue
        boxes = _read_boxes(lbl)
        if not boxes:
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            n_skip += 1
            continue
        h, w = img.shape[:2]
        n_img += 1
        for j, (c, cx, cy, bw, bh) in enumerate(boxes):
            x1, y1, x2, y2 = _square_box(cx, cy, bw, bh, pad, w, h)
            crop = img[y1:y2, x1:x2]
            if crop.size == 0 or crop.shape[0] < 8 or crop.shape[1] < 8:
                n_skip += 1
                continue
            crop = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
            out_dir = ensure_dir(out_root / split / str(c))
            cv2.imwrite(str(out_dir / f"{img_path.stem}_{j}.jpg"), crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
            per_class[c] += 1
            n_box += 1
        if i % 2000 == 0:
            logger.info("  %s: %d/%d 张，已出 %d 个框", split, i, len(images), n_box)

    logger.info("%s 完成：%d 张图 / %d 个裁剪 / 跳过 %d / %d 类",
                split, n_img, n_box, n_skip, len(per_class))
    return {
        "split": split,
        "images": n_img,
        "crops": n_box,
        "skipped": n_skip,
        "num_classes": len(per_class),
        "per_class": {str(k): v for k, v in sorted(per_class.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="检测真值框 → 分类裁剪样本")
    ap.add_argument("--splits", nargs="+", default=["train", "val", "test"])
    ap.add_argument("--pad", type=float, default=0.25, help="框外扩比例（相对框长边）")
    ap.add_argument("--size", type=int, default=256, help="输出正方形边长")
    ap.add_argument("--limit", type=int, default=0, help="每个 split 只处理前 N 张（调试）")
    ap.add_argument("--out", default=None, help="默认 data/pest_crops")
    args = ap.parse_args()

    root = path_of("pest_dataset")
    out_root = Path(args.out) if args.out else (path_of("data_dir") / "pest_crops")
    logger.info("真框裁剪：%s → %s（pad=%.2f, size=%d）", root, out_root, args.pad, args.size)

    stats = []
    for split in args.splits:
        stats.append(build_split(split, root, out_root, pad=args.pad, size=args.size,
                                 limit=args.limit))

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(root),
        "output": str(out_root),
        "pad": args.pad,
        "size": args.size,
        "note": "用检测真值框裁正方形，训练两阶段分类器；不改动 data/pest。",
        "splits": stats,
    }
    out_json = path_of("outputs_dir") / "reports" / "pest_crops.json"
    ensure_dir(out_json.parent)
    write_json(out_json, payload)
    logger.info("写出 %s", out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
