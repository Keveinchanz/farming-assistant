#!/usr/bin/env python
"""任务 2.8.7 —— 两阶段端到端评估：检测框 → 裁剪 → 分类，与单阶段同图对比。

## 口径

和 40 号完全一致，才能和「单阶段整图 top-1 = 74.49%（test 3798 张）」直接比：

    **整图 top-1** = 这张图最终给出的那一个类别，是否落在该图的 GT 类别集合里。

三个读数，同一批检测框、同一批权重，只换「最终类别怎么定」：

| 读数 | 最终类别怎么来 |
|---|---|
| `single_stage` | 检测模型自己框上带的类别里，置信度最高的那个（= 现行线上逻辑） |
| `two_stage_cls` | 每个框裁出来过分类器，取分类概率最高的那个框的类别 |
| `two_stage_fused` | 检测置信度 × 分类概率，取最大的那个框的类别（把「像不像虫」也算进去） |

融合用乘法而不是加法：两者都是概率，乘起来是「这个框既是虫子、又是这个物种」的
联合置信度，量纲一致；加法需要人为配权重，配出来的数字说不清道理。

## 内存

流式处理：每 `--chunk` 张图走一遍「检测 → 裁剪 → 分类」，处理完立刻释放，
不把 3798 张图或 5000 个裁剪同时放内存（这台机器只有 15 GB）。

## 用法

    python scripts/47_eval_two_stage.py --split test
    python scripts/47_eval_two_stage.py --split val --cls-ckpt models/pest_cls/resnet50_224.pt
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.twostage")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CROP_PAD = 0.25      # 与 45 号一致：训练和评估必须同一个裁剪口径
CROP_SIZE = 256
NUM_CLASSES = 102


def _ground_truth(split: str) -> dict[str, set[int]]:
    lbl = path_of("pest_dataset") / "labels" / split
    gt: dict[str, set[int]] = {}
    for f in sorted(lbl.glob("*.txt")):
        cs = set()
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            p = line.split()
            if not p:
                continue
            try:
                cs.add(int(float(p[0])))
            except ValueError:
                continue
        gt[f.stem] = cs
    return gt


def _images(split: str) -> list[Path]:
    d = path_of("pest_dataset") / "images" / split
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)


def _square_box(x1: float, y1: float, x2: float, y2: float, pad: float,
                w: int, h: int) -> tuple[int, int, int, int]:
    """像素框 -> 外扩 pad 的正方形像素框，越界平移回图内（与 45 号同口径）。"""
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    side = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * pad)
    side = min(max(side, 16.0), float(min(w, h)))
    bx1 = min(max(cx - side / 2, 0.0), w - side)
    by1 = min(max(cy - side / 2, 0.0), h - side)
    return int(round(bx1)), int(round(by1)), int(round(bx1 + side)), int(round(by1 + side))


def _load_classifier(ckpt: Path, device):
    import torch
    from torchvision import models
    blob = torch.load(ckpt, map_location="cpu", weights_only=False)
    arch = blob.get("arch", "resnet50")
    fn = {"resnet50": models.resnet50, "resnet34": models.resnet34,
          "resnet18": models.resnet18}[arch]
    model = fn(weights=None)
    model.fc = torch.nn.Sequential(torch.nn.Dropout(0.2),
                                   torch.nn.Linear(model.fc.in_features, NUM_CLASSES))
    model.load_state_dict(blob["state_dict"])
    model.eval().to(device)
    return model, blob.get("size", 224)


def _eval_tf(size: int):
    from torchvision import transforms as T
    return T.Compose([
        T.Resize(int(size * 1.14)),
        T.CenterCrop(size),
        T.ToTensor(),
        T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="两阶段（检测→裁剪→分类）端到端评估")
    ap.add_argument("--split", default="test")
    ap.add_argument("--det-ckpt", default=None, help="默认 models/checkpoints/pest_best.pt")
    ap.add_argument("--cls-ckpt", default="models/pest_cls/resnet50_224.pt")
    ap.add_argument("--imgsz", type=int, default=640, help="检测输入尺寸")
    ap.add_argument("--conf", nargs="+", type=float, default=[0.001, 0.25],
                    help="检测阈值；0.001 对齐 mAP/top-k 口径，0.25 是线上口径")
    ap.add_argument("--chunk", type=int, default=48,
                    help="每批图数。默认 48（不是 200）：这张机器只有 15 GB 内存，"
                         "chunk 越大峰值越高，而吞吐几乎不变")
    ap.add_argument("--cls-batch", type=int, default=48)
    ap.add_argument("--det-batch", type=int, default=4, help="检测侧 batch，越小越省显存")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cascade-gate", type=float, default=None,
                    help="用别处（val）选定的 gate 复核，给了就不在本次 split 上调参")
    ap.add_argument("--cascade-margin", type=float, default=None)
    ap.add_argument("--device", default="0")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import cv2
    import numpy as np
    import torch
    from PIL import Image
    from ultralytics import YOLO

    device = torch.device("cuda" if (args.device != "cpu" and torch.cuda.is_available()) else "cpu")
    det_ckpt = Path(args.det_ckpt) if args.det_ckpt else (path_of("checkpoints_dir") / "pest_best.pt")
    cls_ckpt = Path(args.cls_ckpt)
    if not det_ckpt.exists():
        raise SystemExit(f"检测权重不存在：{det_ckpt}")
    if not cls_ckpt.exists():
        raise SystemExit(f"分类权重不存在：{cls_ckpt}（先跑 scripts/46_train_pest_cls.py）")

    gt = _ground_truth(args.split)
    images = _images(args.split)
    if args.limit:
        images = images[: args.limit]
        keep = {p.stem for p in images}
        gt = {k: v for k, v in gt.items() if k in keep}
    logger.info("两阶段评估：%s · %d 张 · 检测 %s · 分类 %s",
                args.split, len(images), det_ckpt.name, cls_ckpt.name)

    detector = YOLO(str(det_ckpt))
    classifier, cls_size = _load_classifier(cls_ckpt, device)
    tf = _eval_tf(cls_size)
    logger.info("分类器 arch 输入 %d", cls_size)

    idx2label = json.loads(path_of("pest_label_map").read_text(encoding="utf-8"))["index_to_label"]

    def _label(c: int) -> str:
        return idx2label.get(str(c), {}).get("label_cn", f"<{c}>")

    # 每个阈值一套统计
    stats = {c: {"n": 0, "single": 0, "cls": 0, "fused": 0, "no_det": 0} for c in args.conf}
    conf_by_true: dict[int, Counter] = defaultdict(Counter)   # 融合后的错认统计
    casc_records: list[tuple[int, int, float, float, set[int]]] = []
    casc_thr = min(args.conf)   # 级联在「别漏框」的低阈值上收集，gate 自己再做门槛
    for start in range(0, len(images), args.chunk):
        part = images[start:start + args.chunk]
        results = detector.predict(source=[str(p) for p in part], conf=min(args.conf),
                                   iou=0.7, imgsz=args.imgsz, batch=args.det_batch,
                                   device=args.device, workers=0, stream=True, verbose=False)
        crops, meta = [], []   # crops: 待分类小图；meta: (stem, det_conf, det_cls)
        per_image: dict[str, list] = {}
        for r in results:
            stem = Path(r.path).stem
            boxes = getattr(r, "boxes", None)
            recs = []
            img = None
            if boxes is not None and len(boxes):
                xyxy = boxes.xyxy.tolist()
                cls = boxes.cls.tolist()
                cf = boxes.conf.tolist()
                if xyxy:
                    img = cv2.imread(r.path)
                for i in range(len(cls)):
                    recs.append([int(cls[i]), float(cf[i])])
                    if img is None:
                        continue
                    h, w = img.shape[:2]
                    x1, y1, x2, y2 = _square_box(*xyxy[i], CROP_PAD, w, h)
                    patch = img[y1:y2, x1:x2]
                    if patch.size == 0:
                        crops.append(np.zeros((CROP_SIZE, CROP_SIZE, 3), np.uint8))
                    else:
                        patch = cv2.resize(patch, (CROP_SIZE, CROP_SIZE),
                                           interpolation=cv2.INTER_AREA)
                        crops.append(cv2.cvtColor(patch, cv2.COLOR_BGR2RGB))
                    meta.append((stem, i))
            per_image[stem] = recs
            del r, img

        # 批量分类
        probs_all = []
        for i in range(0, len(crops), args.cls_batch):
            batch = crops[i:i + args.cls_batch]
            tens = torch.stack([tf(Image.fromarray(a)) for a in batch]).to(device)
            with torch.no_grad(), torch.amp.autocast("cuda", enabled=device.type == "cuda"):
                logits = classifier(tens)
            probs_all.append(torch.softmax(logits, 1).float().cpu())
            del tens, logits
        probs = torch.cat(probs_all) if probs_all else torch.zeros((0, NUM_CLASSES))

        prob_by: dict[str, list] = defaultdict(list)
        for k, (stem, idx) in enumerate(meta):
            prob_by[stem].append((idx, probs[k]))

        for stem in per_image:
            classes = gt.get(stem, set())
            if not classes:
                continue
            recs = per_image[stem]
            for thr in args.conf:
                s = stats[thr]
                s["n"] += 1
                keep = [r for r in recs if r[1] >= thr]
                if not keep:
                    s["no_det"] += 1
                    continue
                # 单阶段：检测分最高
                top_det = max(keep, key=lambda r: r[1])
                if top_det[0] in classes:
                    s["single"] += 1
                # 两阶段
                pl = prob_by.get(stem, [])
                if not pl:
                    continue
                cls_best = max(pl, key=lambda ip: float(ip[1].max()))
                c_top = int(cls_best[1].argmax())
                if c_top in classes:
                    s["cls"] += 1
                fused_best, fused_c = -1.0, -1
                for idx, pv in pl:
                    if recs[idx][1] < thr:
                        continue
                    c = int(pv.argmax())
                    v = recs[idx][1] * float(pv[c])
                    if v > fused_best:
                        fused_best, fused_c = v, c
                if fused_c in classes:
                    s["fused"] += 1
                elif len(classes) == 1:
                    conf_by_true[next(iter(classes))][fused_c] += 1
                # 级联用：记下「单阶段选了什么、两阶段选了什么、检测有多犹豫」
                if thr == casc_thr and len(keep) >= 1:
                    ranked = sorted(keep, key=lambda r: -r[1])
                    top_c, second_c = ranked[0][1], (ranked[1][1] if len(ranked) > 1 else 0.0)
                    casc_records.append((int(top_det[0]), int(fused_c),
                                         float(top_c), float(second_c), classes))

        del crops, meta, per_image, prob_by, probs
        import gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("  %d/%d 完成", min(start + args.chunk, len(images)), len(images))

    rows = []
    for thr in args.conf:
        s = stats[thr]
        n = max(1, s["n"])
        rows.append({
            "conf": thr,
            "num_images": s["n"],
            "single_stage_top1": round(s["single"] / n, 4),
            "two_stage_cls_top1": round(s["cls"] / n, 4),
            "two_stage_fused_top1": round(s["fused"] / n, 4),
            "no_detection_rate": round(s["no_det"] / n, 4),
            "delta_fused_vs_single": round((s["fused"] - s["single"]) / n, 4),
        })
        logger.info("conf=%.3f 单阶段 %.4f → 两阶段(分类) %.4f / (融合) %.4f",
                    thr, rows[-1]["single_stage_top1"], rows[-1]["two_stage_cls_top1"],
                    rows[-1]["two_stage_fused_top1"])

    # ---- 级联：平时信检测器，只在它「犹豫」时才请分类器改判
    #
    # 单阶段已经把 75% 判对了，全量替换成两阶段反而掉点 —— 那就不替换，只做补丁：
    # 检测最高分低于 gate、或与次高分只差 margin（说明它自己也没底）时才交给分类器。
    # 两个参数都是**后验扫出来的**，本脚本在 val 上选、在 test 上报，别拿 test 的数字
    # 反过来挑参数。
    cascade_rows = []
    if casc_records:
        total = len(casc_records)
        base_hit = sum(1 for r in casc_records if r[0] in r[4])
        for gi in range(15):                       # gate: 0.20 ~ 0.90
            gate = round(0.20 + 0.05 * gi, 2)
            for margin in (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40):
                hit = used = 0
                for single_c, fused_c, top_c, second_c, classes in casc_records:
                    hesitate = (top_c < gate) or ((top_c - second_c) < margin)
                    used += int(hesitate)
                    c = fused_c if hesitate else single_c
                    if c in classes:
                        hit += 1
                cascade_rows.append({
                    "gate": gate,
                    "margin": margin,
                    "top1": round(hit / total, 4),
                    "intervention_rate": round(used / total, 4),
                    "gain_vs_single": round((hit - base_hit) / total, 4),
                })
        best_casc = max(cascade_rows, key=lambda r: (r["top1"], -r["intervention_rate"]))
        logger.info("级联最优：gate=%.2f margin=%.2f → top1=%.4f（单阶段 %.4f，介入 %.1f%%）",
                    best_casc["gate"], best_casc["margin"], best_casc["top1"],
                    round(base_hit / total, 4), best_casc["intervention_rate"] * 100)
        # 用别处（val）选定的参数在本次 split 上复核：test 只报这个数，不报 test 自己的最优
        if args.cascade_gate is not None and args.cascade_margin is not None:
            g, mg = args.cascade_gate, args.cascade_margin
            hit = used = 0
            for single_c, fused_c, top_c, second_c, classes in casc_records:
                hesitate = (top_c < g) or ((top_c - second_c) < mg)
                used += int(hesitate)
                c = fused_c if hesitate else single_c
                if c in classes:
                    hit += 1
            best_casc = {
                "gate": g, "margin": mg, "top1": round(hit / total, 4),
                "intervention_rate": round(used / total, 4),
                "gain_vs_single": round((hit - base_hit) / total, 4),
                "source": "参数取自 val，本 split 上仅复核",
            }
            logger.info("按 val 选定参数 gate=%.2f margin=%.2f 复核：top1=%.4f（单阶段 %.4f）",
                        g, mg, best_casc["top1"], round(base_hit / total, 4))
    else:
        best_casc = None

    worst = [{"index": c, "label_cn": _label(c), "errors": n,
              "often_as": [{"index": k, "label_cn": _label(k), "n": v}
                           for k, v in cnt.most_common(3)]}
             for c, cnt in sorted(conf_by_true.items(), key=lambda kv: -sum(kv[1].values()))[:12]]

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "split": args.split,
        "num_images": len(gt),
        "det_checkpoint": str(det_ckpt),
        "cls_checkpoint": str(cls_ckpt),
        "crop": {"pad": CROP_PAD, "size": CROP_SIZE},
        "note": ("整图 top-1 口径与 40 号一致；single_stage 复现现行线上逻辑，两阶段只换最终类别来源。"
                 "cascade 的 gate/margin 是扫出来的，只能在 val 上选参数、在 test 上报结果。"),
        "rows": rows,
        "cascade_best": best_casc,
        "cascade_grid": sorted(cascade_rows, key=lambda r: -r["top1"])[:15],
        "worst_classes_after_fusion": worst,
    }
    out = Path(args.out) if args.out else (
        path_of("outputs_dir") / "reports" / f"pest_two_stage_{args.split}.json")
    ensure_dir(out.parent)
    write_json(out, payload)
    logger.info("写出 %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
