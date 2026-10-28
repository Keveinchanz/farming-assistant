#!/usr/bin/env python
"""虫害识别瓶颈扫描：imgsz × conf ×（已知作物先验）。

## 为什么单独一个脚本

40 号已经给出了交付口径（imgsz 640 / conf 0.25 / 整图 top-1 74.49%）。但那是一个
**单点**读数，看不出「还剩多少可以捡」。本脚本在同一批权重、同一份 test 上做三组
只读扫描，回答三个问题：

1. **分辨率**：640 → 960 → 1280，top-1 还有没有增益？IP102 的目标普遍很小，
   若 960/1280 明显更好，说明瓶颈在「小目标看不清」，该往 imgsz / 更大骨干上投。
2. **阈值**：conf 0.05~0.50 扫一遍。40 号的 0.25 是线上配置，未必是 top-1 的最优点
   —— 阈值低一点能多留住「对但分低」的框，高一点能压掉「错但分虚高」的框。
3. **类别先验**：若已知作物（用户在小程序里选过作物 / 图里 GT 属于哪几种作物），
   候选类就从 102 缩到该作物下的十几类。这里用 GT 作物做 **oracle 上界**，
   量化「场景约束」这条产品路线最多能换来多少点。

**只读**：不写 models/、不覆盖 outputs/reports 里的交付报告，输出到
`outputs/reports/pest_tta_sweep.json`。

## 用法

    python scripts/44_pest_tta_sweep.py --split test
    python scripts/44_pest_tta_sweep.py --imgsz 640 960 --limit 300      # 先小样本试水
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.sweep")

MAP_CONF = 0.001      # 「别漏框」的低阈值，与 18/35/40 号一致
DEPLOY_IOU = 0.7
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
REPORT_NAME = "pest_tta_sweep.json"


def _ground_truth(split: str) -> dict[str, set[int]]:
    lbl_dir = path_of("pest_dataset") / "labels" / split
    gt: dict[str, set[int]] = {}
    for f in sorted(lbl_dir.glob("*.txt")):
        classes = set()
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if not parts:
                continue
            try:
                classes.add(int(float(parts[0])))
            except ValueError:
                continue
        gt[f.stem] = classes
    return gt


def _images(split: str) -> list[Path]:
    d = path_of("pest_dataset") / "images" / split
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)


def _crop_of_class() -> dict[int, str]:
    """类别下标 -> 作物名（label_map 里的 crop 字段，口径与前端作物筛选一致）。"""
    data = json.loads(path_of("pest_label_map").read_text(encoding="utf-8"))
    out: dict[int, str] = {}
    for k, v in data["index_to_label"].items():
        crop = v.get("crop") or "多寄主"
        out[int(k)] = crop
    return out


def _predict(model, images: list[Path], *, imgsz: int, batch: int, device: str,
             chunk: int = 32) -> dict[str, list[tuple[int, float]]]:
    """图名 -> [(类别, 置信度)] 按置信度降序。低阈值提一次，后续在内存里扫阈值。

    这台机器物理内存紧张（见 40 号注释），沿用 chunk=64 / workers=0 / 每块清缓存。
    """
    import torch

    out: dict[str, list[tuple[int, float]]] = {}
    for start in range(0, len(images), chunk):
        part = images[start:start + chunk]
        results = model.predict(
            source=[str(p) for p in part], conf=MAP_CONF, iou=DEPLOY_IOU, imgsz=imgsz,
            batch=batch, device=device, workers=0, stream=True, verbose=False,
        )
        for r in results:
            boxes = getattr(r, "boxes", None)
            dets: list[tuple[int, float]] = []
            if boxes is not None and len(boxes):
                cls = boxes.cls.tolist()
                confs = boxes.conf.tolist()
                dets = [(int(cls[i]), float(confs[i])) for i in range(len(cls))]
                dets.sort(key=lambda d: d[1], reverse=True)
            out[Path(r.path).stem] = dets
            del r
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return out


def _score(gt: dict[str, set[int]], dets: dict[str, list[tuple[int, float]]], *,
           conf: float, topk: int, allowed: dict[str, set[int]] | None) -> dict:
    """按 conf 过滤后算整图 top-1 / top-5 / 无框率。

    `allowed` 不为 None 时模拟「已知作物」：候选类之外的框被丢弃（这就是场景先验）。
    """
    n = top1 = top5 = no_det = 0
    for stem, classes in gt.items():
        n += 1
        pool = dets.get(stem, [])
        if allowed is not None:
            cand = allowed.get(stem, set())
            pool = [d for d in pool if d[0] in cand]
        pool = [d for d in pool if d[1] >= conf]
        if not pool:
            no_det += 1
            continue
        if pool[0][0] in classes:
            top1 += 1
        seen: list[int] = []
        for c, _ in pool:
            if c not in seen:
                seen.append(c)
            if len(seen) >= topk:
                break
        if classes & set(seen):
            top5 += 1
    return {
        "num_images": n,
        "top1_accuracy": round(top1 / n, 4) if n else 0.0,
        "top5_accuracy": round(top5 / n, 4) if n else 0.0,
        "no_detection_rate": round(no_det / n, 4) if n else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="虫害识别 imgsz/conf/先验 三组扫描（只读）")
    ap.add_argument("--checkpoint", default=None, help="默认取交付权重 pest_best.pt")
    ap.add_argument("--split", default="test")
    ap.add_argument("--imgsz", nargs="+", type=int, default=[640, 960],
                    help="别一次跑 1280：3798 张 × 1280 会把显存/内存打满")
    ap.add_argument("--conf-grid", nargs="+", type=float,
                    default=[0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50])
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--device", default="0")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 张（调试用）")
    ap.add_argument("--dump-dets", action="store_true",
                    help="把每档 imgsz 的原始检测（类别+分数）一并写进报告，便于离线分层分析")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from ultralytics import YOLO

    ckpt = Path(args.checkpoint) if args.checkpoint else (path_of("checkpoints_dir") / "pest_best.pt")
    if not Path(ckpt).exists():
        raise SystemExit(f"权重不存在：{ckpt}")

    gt = _ground_truth(args.split)
    images = _images(args.split)
    if args.limit:
        images = images[: args.limit]
        gt = {k: v for k, v in gt.items() if k in {p.stem for p in images}}
    logger.info("权重 %s · split=%s · %d 张", ckpt, args.split, len(images))

    # ---- 已知作物先验：候选类 = 该图 GT 类所属作物的全部害虫
    idx2crop = _crop_of_class()
    crop2cls: dict[str, set[int]] = {}
    for idx, crop in idx2crop.items():
        crop2cls.setdefault(crop, set()).add(idx)
    allowed: dict[str, set[int]] = {}
    for stem, classes in gt.items():
        cand: set[int] = set()
        for c in classes:
            cand |= crop2cls.get(idx2crop.get(c, "多寄主"), set())
        allowed[stem] = cand or set(range(102))
    logger.info("已知作物先验：平均候选类 %.1f / 102",
                sum(len(v) for v in allowed.values()) / max(1, len(allowed)))

    model = YOLO(str(ckpt))
    rows: list[dict] = []
    dumped: dict[str, dict] = {}
    for imgsz in args.imgsz:
        logger.info("推理 imgsz=%d …", imgsz)
        dets = _predict(model, images, imgsz=imgsz, batch=args.batch, device=args.device)
        if args.dump_dets:
            dumped[str(imgsz)] = {k: [[c, round(s, 4)] for c, s in v] for k, v in dets.items()}
        for conf in args.conf_grid:
            base = _score(gt, dets, conf=conf, topk=args.topk, allowed=None)
            prior = _score(gt, dets, conf=conf, topk=args.topk, allowed=allowed)
            rows.append({"imgsz": imgsz, "conf": conf, "plain": base, "crop_prior": prior})
            logger.info("  imgsz=%4d conf=%.2f  top1=%.4f  top5=%.4f  no_det=%.4f"
                        "  | 作物先验 top1=%.4f",
                        imgsz, conf, base["top1_accuracy"], base["top5_accuracy"],
                        base["no_detection_rate"], prior["top1_accuracy"])
        del dets
        gc.collect()

    best_plain = max(rows, key=lambda r: r["plain"]["top1_accuracy"])
    best_prior = max(rows, key=lambda r: r["crop_prior"]["top1_accuracy"])
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checkpoint": str(ckpt),
        "split": args.split,
        "num_images": len(images),
        "map_conf": MAP_CONF,
        "iou": DEPLOY_IOU,
        "note": "只读扫描，不写交付物；crop_prior 用 GT 作物做 oracle 上界，代表「已知作物」场景约束的天花板。",
        "best_plain": best_plain,
        "best_crop_prior": best_prior,
        "rows": rows,
    }
    if args.dump_dets:
        payload["dets"] = dumped
    out = Path(args.out) if args.out else (path_of("outputs_dir") / "reports" / REPORT_NAME)
    ensure_dir(out.parent)
    write_json(out, payload)
    logger.info("最好 plain: imgsz=%d conf=%.2f top1=%.4f",
                best_plain["imgsz"], best_plain["conf"], best_plain["plain"]["top1_accuracy"])
    logger.info("最好 +作物先验: imgsz=%d conf=%.2f top1=%.4f",
                best_prior["imgsz"], best_prior["conf"],
                best_prior["crop_prior"]["top1_accuracy"])
    logger.info("写出 %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
