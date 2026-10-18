#!/usr/bin/env python
"""任务 2.8.1 —— 虫害「整图识别正确率」评测（mAP50 之外的产品口径）。

## 为什么要另立一个口径

18/35 号算的是 **macro mAP50**：对验证集里出现过的每一个类算 AP，再**等权平均**。
这份数据集长尾极重 —— 97 个可测类里 92 个在 val 上实例数不到 50，其中 48 个不到 10。
于是「有 48 个类只被看到了不到十次」这件事，会以同样的权重压在那个平均值上：
把头部类做到 0.9 也拉不动它，因为分母里同权的长尾才是大头。

但**产品实际在做的事不是 macro mAP50**。实测 val 1519 张里 **1414 张（93.1%）只有一个框**
（每图平均 1.16 个框）：用户拍一张照片，App 给**一个**主要结论。所以真正该问的是
「**这张图里最主要的那个结论，认对了没有**」—— 这就是本脚本的主口径：

    **整图 top-1 识别正确率** = 取模型最高置信度那个框的类别，落在该图标注的类别集合里 的比例。

它同样可以逐张复核（同一份 val/test、同一批权重、脚本可复算），不是换了个说法把数字做大。

## 四个口径

| 口径 | 定义 | 回答什么问题 |
|---|---|---|
| `top1_accuracy` | 最高置信度框的类别 ∈ 该图 GT 类别集合 | 主结论认对了吗 |
| `top5_accuracy` | 置信度最高的 5 个**不同类别**里含 GT 类 | 候选列表给对了吗（App 会展示候选） |
| `instance_recall_at_deploy` | 线上阈值（0.25）下，GT 实例被**同类且 IoU≥0.5** 的框命中的比例 | 到了线上还认得出多少 |
| `no_detection_rate` | 线上阈值下一张图都没框出来的比例 | 产品上「没识别到」的比例 |

## 两个阈值不能混用

mAP 是对 PR 曲线积分、与置信度阈值无关，所以 18/35 号用 ultralytics 默认 `conf=0.001`
（意思是「一个框都别漏」）。线上是 `conf=0.25`（`configs/default.yaml`）。
**同一批权重跑两遍**：`0.001` 求 top-1/top-5，`0.25` 求线上口径 —— 不在一个 pass 的结果上
二次过滤，因为 ultralytics 是先按 conf 过滤再做 NMS，两件事不等价。

## 用法

    python scripts/40_eval_pest_recognition.py                       # staging 里的候选 + 交付权重
    python scripts/40_eval_pest_recognition.py --checkpoints ep100_640_n
    python scripts/40_eval_pest_recognition.py --split test --imgsz 640
    python scripts/40_eval_pest_recognition.py --out outputs/reports/pest_recognition.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.recognition")

REPORT_NAME = "pest_recognition.json"
# 求 top-1/top-5 用的低阈值：与 18/35 号的 mAP 口径一致（「别漏框」）
MAP_CONF = 0.001
# 线上部署阈值，取自 configs/default.yaml 的 pest_confidence_threshold
DEPLOY_CONF = 0.25
DEPLOY_IOU = 0.7
MATCH_IOU = 0.5      # 判「这个实例被检到了」的 IoU 门槛，与 COCO 一致
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# ---------------------------------------------------------------- 真值

def _ground_truth(split: str) -> dict[str, set[int]]:
    """图名(stem) -> 该图标注里出现的类别集合。不引入任何外部映射，直接读标注文件。"""
    lbl_dir = path_of("pest_dataset") / "labels" / split
    gt: dict[str, set[int]] = {}
    for f in sorted(lbl_dir.glob("*.txt")):
        classes = set()
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                classes.add(int(float(line.split()[0])))
            except (ValueError, IndexError):
                continue
        gt[f.stem] = classes
    return gt


def _images(split: str) -> list[Path]:
    d = path_of("pest_dataset") / "images" / split
    return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)


# ---------------------------------------------------------------- IoU

def _iou(a: list[float], b: list[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, x2 - x1), max(0.0, y2 - y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _gt_boxes(split: str) -> dict[str, list[tuple[int, list[float]]]]:
    """图名 -> [(类别, [x1,y1,x2,y2] 归一化)]。求实例召回时要拿真框比 IoU。"""
    lbl_dir = path_of("pest_dataset") / "labels" / split
    out: dict[str, list[tuple[int, list[float]]]] = {}
    for f in sorted(lbl_dir.glob("*.txt")):
        boxes = []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                c = int(float(parts[0]))
                cx, cy, w, h = (float(v) for v in parts[1:5])
            except ValueError:
                continue
            boxes.append((c, [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]))
        out[f.stem] = boxes
    return out


# ---------------------------------------------------------------- 推理

def _predict_all(model, images: list[Path], *, conf: float, imgsz: int, batch: int,
                 device: str, workers: int, chunk: int = 64) -> dict[str, list[tuple[int, float, list[float]]]]:
    """逐图返回 [(类别, 置信度, [x1,y1,x2,y2] 像素)]，按置信度降序。

    **这台机器只有 15.4 GB 物理内存，而且平时已经被 VS Code + Defender 占到只剩 1～2 GB**，
    所以这里的默认值一律往「省内存」一侧压，宁慢勿占：

    - `chunk=64`：分块递交。把 1519/3798 张的完整列表一次性交给 `model.predict(source=[...])`，
      实测会一路涨到 7 GB 显存再索要 9.27 GB 而 OOM（32 张的小批次峰值只有 0.73 GB，
      说明问题出在「一次性递交整个列表」而不是单张的需求）。
    - `workers=0`：**不要 DataLoader 子进程**。每个 worker 都会再复制一份解释器与图像队列，
      在这台机器上是压垮内存的那一下。单进程取图慢一点，但不会把用户的其他程序挤掉。
    - `batch` 由调用方给（默认 4），配合 `--max-vram-gb` 的硬上限双保险。
    - 每块结束 `gc.collect()` + `empty_cache()`：别让 Python 的引用环和显存碎片攒到最后。
    """
    import gc

    import torch

    out: dict[str, list[tuple[int, float, list[float]]]] = {}
    for start in range(0, len(images), chunk):
        part = images[start:start + chunk]
        results = model.predict(
            source=[str(p) for p in part], conf=conf, iou=DEPLOY_IOU, imgsz=imgsz,
            batch=batch, device=device, workers=workers, stream=True, verbose=False,
        )
        for r in results:
            stem = Path(r.path).stem
            dets = []
            boxes = getattr(r, "boxes", None)
            if boxes is not None and len(boxes):
                xyxy = boxes.xyxy.tolist()
                cls = boxes.cls.tolist()
                confs = boxes.conf.tolist()
                for i in range(len(cls)):
                    dets.append((int(cls[i]), float(confs[i]), [float(v) for v in xyxy[i]]))
            dets.sort(key=lambda d: d[1], reverse=True)
            out[stem] = dets
            del r
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return out


# ---------------------------------------------------------------- 指标

def _evaluate(gt_cls: dict[str, set[int]], gt_box: dict[str, list[tuple[int, list[float]]]],
              dets_low: dict, dets_deploy: dict, image_sizes: dict[str, tuple[int, int]],
              topk: int) -> dict:
    n = len(gt_cls)
    top1_hit = top5_hit = 0
    no_det = 0
    # 逐类的 top-1 错认统计：谁最常被认错，是后面调模型的抓手
    errors: Counter[int] = Counter()
    miss_as: dict[int, Counter[int]] = {}

    for stem, classes in gt_cls.items():
        dets = dets_low.get(stem, [])
        if dets:
            if dets[0][0] in classes:
                top1_hit += 1
            else:
                truth = next(iter(classes)) if len(classes) == 1 else None
                if truth is not None:
                    errors[truth] += 1
                    miss_as.setdefault(truth, Counter())[dets[0][0]] += 1
            seen: list[int] = []
            for c, _, _ in dets:
                if c not in seen:
                    seen.append(c)
                if len(seen) >= topk:
                    break
            if classes & set(seen):
                top5_hit += 1
        if not dets_deploy.get(stem):
            no_det += 1

    # 实例召回：线上阈值下，GT 实例是否被「同类 + IoU≥0.5」的框命中
    total_gt = matched = 0
    for stem, boxes in gt_box.items():
        if not boxes:
            continue
        w, h = image_sizes.get(stem, (0, 0))
        if not w or not h:
            continue
        dets = dets_deploy.get(stem, [])
        pix = [(c, [b[0] * w, b[1] * h, b[2] * w, b[3] * h]) for c, b in boxes]
        used = [False] * len(dets)
        for gc_cls, gb in pix:
            total_gt += 1
            best, best_i = 0.0, -1
            # dets 的每项是 (类别, 置信度, 框)，别把置信度当成框传进 _iou
            for i, (dc, _conf, db) in enumerate(dets):
                if used[i] or dc != gc_cls:
                    continue
                v = _iou(gb, db)
                if v > best:
                    best, best_i = v, i
            if best >= MATCH_IOU:
                matched += 1
                used[best_i] = True

    idx2label = json.loads(path_of("pest_label_map").read_text(encoding="utf-8"))["index_to_label"]
    cls_instances: Counter[int] = Counter()
    for boxes in gt_box.values():
        for c, _ in boxes:
            cls_instances[c] += 1
    worst = []
    for cls, cnt in errors.most_common(10):
        entry = idx2label.get(str(cls), {})
        conf_as = miss_as.get(cls, Counter()).most_common(3)
        worst.append({
            "index": cls,
            "label_cn": entry.get("label_cn", f"<未知类别 {cls}>"),
            "gt_instances": cls_instances.get(cls, 0),
            "top1_errors": cnt,
            "error_rate": round(cnt / cls_instances[cls], 4) if cls_instances.get(cls) else None,
            "often_confused_with": [
                {"index": c, "label_cn": idx2label.get(str(c), {}).get("label_cn", f"<{c}>"), "n": k}
                for c, k in conf_as
            ],
        })

    return {
        "num_images": n,
        "topk": topk,
        "top1_accuracy": round(top1_hit / n, 4) if n else None,
        "top1_hits": top1_hit,
        f"top{topk}_accuracy": round(top5_hit / n, 4) if n else None,
        f"top{topk}_hits": top5_hit,
        "deploy_conf": DEPLOY_CONF,
        "no_detection_rate": round(no_det / n, 4) if n else None,
        "no_detection_images": no_det,
        "instance_recall_at_deploy": round(matched / total_gt, 4) if total_gt else None,
        "gt_instances_total": total_gt,
        "gt_instances_matched": matched,
        "worst_classes": worst,
    }


# ---------------------------------------------------------------- 入口

def _resolve(ckpt: str) -> Path:
    """`ep100_640_n` 之类的 tag 去 staging 找；其余当路径。"""
    p = Path(ckpt)
    if p.exists():
        return p
    staged = path_of("pest_models_staging") / ckpt / "best.pt"
    if staged.exists():
        return staged
    raise SystemExit(f"找不到权重：{ckpt}（既不是路径，也不是 staging 里的 tag）")


def _image_size_map(images: list[Path]) -> dict[str, tuple[int, int]]:
    """图名 -> (宽, 高)。逐张读、读完就放，不囤图。

    只需要尺寸，所以用 PIL 的懒加载只读文件头 —— cv2.imread 会**解码整张图**再丢掉，
    在这台只剩 1～2 GB 空闲内存的机器上没必要付这个代价。
    """
    from PIL import Image

    out = {}
    for p in images:
        try:
            with Image.open(p) as im:
                out[p.stem] = (im.width, im.height)
        except OSError:
            continue
    return out


def _limit_resources(*, max_vram_gb: float = 3.0, threads: int = 4) -> None:
    """给本进程上闸：显存硬上限 + 限制 CPU 线程。

    用户明确要求「别把机器占满」。这台机器 15.4 GB 物理内存、8 GB 显存，
    而它在被其他程序用着 —— 所以不是「尽量别 OOM」，而是**主动留出余量**：

    - `set_per_process_memory_fraction`：到 3 GB 就报错，而不是一路涨到把整块卡吃光。
      yolov8n 在 640 下推理只要 0.73 GB，3 GB 是个宽裕的上限。
    - `set_num_threads`：不限制的话 torch 会开满 20 个逻辑核，把用户的编辑器一起拖慢。
    """
    import torch

    torch.set_num_threads(threads)
    if max_vram_gb and torch.cuda.is_available():
        total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
        torch.cuda.set_per_process_memory_fraction(min(1.0, max_vram_gb / total), 0)
        logger.info("显存上限：本进程 %.1f GB / 卡上共 %.1f GB；CPU 线程上限 %d",
                    min(max_vram_gb, total), total, threads)


def main() -> int:
    ap = argparse.ArgumentParser(description="虫害整图识别正确率评测")
    ap.add_argument("--checkpoints", nargs="*", default=None,
                    help="staging 里的 tag 或权重路径；缺省自动发现 staging + 交付权重")
    ap.add_argument("--split", nargs="+", default=["val", "test"])
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="0", help="'0' 或用 'cpu'")
    # 默认值一律往省内存一侧压：这台机器只有 15.4 GB 物理内存，平时已经被编辑器
    # 和 Defender 占到只剩 1～2 GB。batch 小、worker 关掉，慢一点但不挤别人。
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--workers", type=int, default=0, help="默认 0：不要 DataLoader 子进程")
    ap.add_argument("--chunk", type=int, default=64, help="每块递交多少张图")
    ap.add_argument("--max-vram-gb", type=float, default=3.0,
                    help="本进程允许占用的显存上限（GB）；0 表示不限制")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    _limit_resources(max_vram_gb=args.max_vram_gb)

    if args.checkpoints:
        ckpts = [(c, _resolve(c)) for c in args.checkpoints]
    else:
        staging = path_of("pest_models_staging")
        ckpts = [(d.name, d / "best.pt") for d in sorted(staging.iterdir())
                 if (d / "best.pt").exists()] if staging.exists() else []
        delivered = path_of("checkpoints_dir") / "pest_best.pt"
        if delivered.exists():
            ckpts.append(("交付权重", delivered))
    if not ckpts:
        logger.error("没有可评的权重")
        return 1

    from ultralytics import YOLO

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "imgsz": args.imgsz,
        "map_conf": MAP_CONF,
        "deploy_conf": DEPLOY_CONF,
        "match_iou": MATCH_IOU,
        "note": "top1/top5 用 conf=0.001 求（与 mAP 口径一致）；deploy 两项用 conf=0.25 求（线上口径）。",
        "splits": {},
    }

    for split in args.split:
        images = _images(split)
        gt_cls = _ground_truth(split)
        gt_box = _gt_boxes(split)
        logger.info("")
        logger.info("=" * 96)
        logger.info("split=%s  图 %d 张（有标注的 %d 张）", split, len(images), len(gt_cls))
        logger.info("%-16s %10s %10s %12s %12s %10s", "checkpoint", "top-1", f"top-{args.topk}",
                    "实例召回", "无检出率", "命中/总数")
        logger.info("-" * 96)

        sizes = _image_size_map(images)
        per_ckpt = {}
        for tag, ckpt in ckpts:
            model = YOLO(str(ckpt))
            low = _predict_all(model, images, conf=MAP_CONF, imgsz=args.imgsz,
                               batch=args.batch, device=args.device, workers=args.workers,
                               chunk=args.chunk)
            deploy = _predict_all(model, images, conf=DEPLOY_CONF, imgsz=args.imgsz,
                                  batch=args.batch, device=args.device, workers=args.workers,
                                  chunk=args.chunk)
            del model
            res = _evaluate(gt_cls, gt_box, low, deploy, sizes, args.topk)
            res["checkpoint"] = str(ckpt)
            per_ckpt[tag] = res
            logger.info("%-16s %10.4f %10.4f %12s %12s %6d/%-6d", tag,
                        res["top1_accuracy"], res[f"top{args.topk}_accuracy"],
                        f"{res['instance_recall_at_deploy']:.4f}",
                        f"{res['no_detection_rate']:.4f}",
                        res["top1_hits"], res["num_images"])
        logger.info("=" * 96)
        report["splits"][split] = per_ckpt

    # 最常被认错的类：给下一轮调模型一个抓手
    logger.info("")
    for split, per_ckpt in report["splits"].items():
        for tag, res in per_ckpt.items():
            w = res["worst_classes"]
            if not w:
                continue
            logger.info("%s / %s 最常被认错的类（前 5）：", split, tag)
            for item in w[:5]:
                confused = "、".join(f"{c['label_cn']}×{c['n']}" for c in item["often_confused_with"])
                logger.info("    %-14s 错 %2d 次   常被认成：%s", item["label_cn"], item["top1_errors"], confused)

    out = Path(args.out) if args.out else (path_of("reports_dir") / REPORT_NAME)
    ensure_dir(out.parent)
    write_json(out, report)
    logger.info("")
    logger.info("已写入 %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
