#!/usr/bin/env python
"""任务 2.8.4 —— 用「类无关定位器」给识别集里那 5.7 万张无框图生成伪框。

## 为什么是「定位器 + 目录名」而不是「让 102 类模型自己标」

两条路的差别不在工程，而在**长尾**：

| | 做法 | 类别从哪来 | 对稀有类有用吗 |
|---|---|---|---|
| 让检测模型标自己 | 102 类模型跑一遍，高置信的留下 | **模型预测**（长尾类它本来就没把握） | **没有** —— 只保留模型已有把握的样本，最需要补的类恰恰补不进来 |
| 定位器 + 目录名（本脚本） | 单类模型只回答「虫在哪」 | **识别集的目录名**（现成的真值） | **有** —— 稀有类在识别集里有的是图，缺的只是框 |

定位「这儿有只虫」比分辨「是哪只虫」容易得多，所以 `--single-cls` 训出来的单类模型
框得比 102 类模型准；而类别我们本来就知道（`classification/<split>/<类号>/xxx.jpg`）。
这正是这次能补上长尾的原因。

## 两道闸

**1. 质量闸（`validate` 子命令）** —— 上量之前必须先证明框是准的。
识别集里有 12,145 张图**同时也在检测 train 里**（42 号按感知哈希配出来的），
它们有真框 ⇒ 天然的留出集。在这些图上比「伪框 vs 真框」的 IoU：

    IoU≥0.5 命中率 ≥ 0.85 → 放行；0.6~0.85 → 只上高置信子集；< 0.6 → **整档放弃**

之所以可信：两套数据是同一张照片的不同尺寸版本（感知哈希距离 0~4，画面几乎逐像素对应），
所以归一化坐标可以直接比。

**2. 泄漏闸（在 42 号里已经做完）** —— 与检测 val/test 近重复的 5,944 张**永不进入**本脚本，
可用池是 42 号输出的 `pest_pseudo_pool.json`，本脚本不自己筛。

## 用法

    # 先验质量（约 1 分钟）
    python scripts/43_build_pest_pseudo.py validate --locator models\\pest_staging\\agnostic_s640\\best.pt --limit 800

    # 通过后再上量（约 20~40 分钟）
    python scripts/43_build_pest_pseudo.py build --locator models\\pest_staging\\agnostic_s640\\best.pt
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.pseudo")

INDEX_NAME = "pest_phash_index.json"
POOL_NAME = "pest_pseudo_pool.json"
REPORT_NAME = "pest_pseudo_report.json"
MATCH_IOU = 0.5
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _pseudo_root() -> Path:
    """伪标注数据集的落点：`data/pest_pseudo`。

    刻意**不往 `configs/default.yaml` 的 paths 段里加键** —— 那个文件是交付包的一部分，
    而这是一次实验的新产物。从 `pest_dataset`（`data/pest`）的父目录推出来即可，
    既有键一个不动。
    """
    return path_of("pest_dataset").parent / "pest_pseudo"


# ---------------------------------------------------------------- 索引 / 工具

def _load_json(p: Path) -> dict:
    if not p.exists():
        raise SystemExit(f"找不到 {p} —— 请先跑 scripts/42_pest_phash_index.py")
    return json.loads(p.read_text(encoding="utf-8"))


def _det_label_path(image_path: str) -> Path | None:
    """检测集图 -> 它的 YOLO 标注文件。`…/images/<split>/x.jpg` ⇒ `…/labels/<split>/x.txt`。"""
    p = Path(image_path)
    parts = list(p.parts)
    if "images" not in parts:
        return None
    i = parts.index("images")
    parts[i] = "labels"
    cand = Path(*parts).with_suffix(".txt")
    return cand if cand.exists() else None


def _read_yolo(path: Path) -> list[tuple[int, list[float]]]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            out.append((int(float(parts[0])), [float(v) for v in parts[1:5]]))
        except ValueError:
            continue
    return out


def _iou_xywh(a: list[float], b: list[float]) -> float:
    """两个归一化 (cx,cy,w,h) 的 IoU。"""
    ax1, ay1, ax2, ay2 = a[0] - a[2] / 2, a[1] - a[3] / 2, a[0] + a[2] / 2, a[1] + a[3] / 2
    bx1, by1, bx2, by2 = b[0] - b[2] / 2, b[1] - b[3] / 2, b[0] + b[2] / 2, b[1] + b[3] / 2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    ub = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    return inter / (ua + ub - inter) if (ua + ub - inter) > 0 else 0.0


def _limit_resources(max_vram_gb: float, threads: int) -> None:
    import torch

    torch.set_num_threads(threads)
    if max_vram_gb and torch.cuda.is_available():
        total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
        torch.cuda.set_per_process_memory_fraction(min(1.0, max_vram_gb / total), 0)
        logger.info("显存上限 %.1f GB / 卡上 %.1f GB；CPU 线程上限 %d",
                    min(max_vram_gb, total), total, threads)


def _predict_top1(model, paths: list[str], *, conf: float, imgsz: int, batch: int,
                  device: str, chunk: int) -> dict[str, list[float] | None]:
    """逐图返回**最高置信度那个框**的归一化 (cx,cy,w,h)；没有框返回 None。

    只留一个框是有依据的：检测集里 93% 的图只有一只虫（val 1519 张里 1414 张单框），
    多框反倒是伪标注噪声的主要来源。
    """
    import gc

    import torch

    out: dict[str, list[float] | None] = {}
    for start in range(0, len(paths), chunk):
        part = paths[start:start + chunk]
        for r in model.predict(source=part, conf=conf, iou=0.7, imgsz=imgsz, batch=batch,
                               device=device, workers=0, stream=True, verbose=False):
            stem = Path(r.path).stem
            boxes = getattr(r, "boxes", None)
            best = None
            if boxes is not None and len(boxes):
                cf = boxes.conf.tolist()
                i = int(max(range(len(cf)), key=lambda k: cf[k]))
                xywhn = boxes.xywhn.tolist()[i]
                best = [float(v) for v in xywhn]
            out[stem] = best
            del r
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    return out


def _box_ok(b: list[float]) -> str | None:
    """框的形态过滤：返回 None 表示通过，否则返回拒绝原因。"""
    cx, cy, w, h = b
    area = w * h
    if not (0.01 <= area <= 0.90):
        return "面积越界"
    if w < 0.05 or h < 0.05:
        return "边长过小"
    if cx - w / 2 <= 0.002 or cy - h / 2 <= 0.002 or cx + w / 2 >= 0.998 or cy + h / 2 >= 0.998:
        return "贴边（多半是残缺目标）"
    return None


# ---------------------------------------------------------------- 质量闸

def cmd_validate(args) -> int:
    index = _load_json(path_of("reports_dir") / INDEX_NAME)
    if args.source == "leaked":
        # **默认用它，因为它才是诚实的代理。**
        # `already_labeled`（与检测 train 近重复的 12,145 张）看着更顺手，但那批图正是
        # 模型**训练过**的 —— 在上面量 IoU 量到的是「记住了没有」，不是「框得准不准」。
        # 实测同样一个模型：在 train 那批上命中率 0.9983，一看就知道是虚的。
        # `leaked`（与检测 val/test 近重复的 5,944 张）没参与梯度更新，才代表真实泛化。
        # （仍有一点点偏差：best.pt 是按 val 指标挑的，但那只影响类别排序，不影响框。）
        items = [x for x in index["leaked"] if x.get("matches")]
        logger.info("留出集取 leaked（对应检测 val/test，未参与训练）")
    else:
        items = [x for x in index["already_labeled"] if x.get("matches")]
        logger.warning("留出集取 already_labeled —— 这些图**模型训练过**，命中率会虚高，只作参考")
    if args.limit:
        items = items[: args.limit]
    if not items:
        logger.error("索引里没有可用的配对，无法验证")
        return 1

    from ultralytics import YOLO
    paths = [x["path"] for x in items]
    logger.info("用 %d 张「已有真框」的识别图当留出集，比伪框与真框的 IoU", len(paths))
    pred = _predict_top1(YOLO(str(args.locator)), paths, conf=args.conf, imgsz=args.imgsz,
                         batch=args.batch, device=args.device, chunk=args.chunk)

    hit = miss = no_box = class_mismatch = 0
    ious: list[float] = []
    for x in items:
        b = pred.get(Path(x["path"]).stem)
        if b is None:
            no_box += 1
            continue
        lp = _det_label_path(x["matches"])
        if lp is None:
            continue
        real = _read_yolo(lp)
        if not real:
            continue
        # 目录名给的类号 vs 检测标注里的类号：不一致说明配对是「同图不同类」，
        # 那 IoU 比较就没意义了，单独计数而不是混进命中率
        cls_dir = int(Path(x["path"]).parent.name)
        if cls_dir not in {c for c, _ in real}:
            class_mismatch += 1
        best = max((_iou_xywh(b, rb) for _, rb in real), default=0.0)
        ious.append(best)
        if best >= MATCH_IOU:
            hit += 1
        else:
            miss += 1

    n = hit + miss
    rate = hit / n if n else 0.0
    logger.info("")
    logger.info("=" * 78)
    logger.info("伪标注定位质量（留出集 %d 张，conf=%.2f）：", len(paths), args.conf)
    logger.info("  没有出框      ：%d 张", no_box)
    logger.info("  类号与真值不符：%d 张（配对是同图不同类，已混入下方统计）", class_mismatch)
    logger.info("  有框的 %d 张里，IoU≥%.1f 命中 **%d**，未命中 %d", n, MATCH_IOU, hit, miss)
    logger.info("  → **命中率 %.4f**", rate)
    if ious:
        ious.sort()
        logger.info("  IoU 中位数 %.4f，四分之一分位 %.4f",
                    ious[len(ious) // 2], ious[len(ious) // 4])
    verdict = ("放行（≥0.85）" if rate >= 0.85 else
               "只上高置信子集（0.60~0.85）" if rate >= 0.60 else
               "**整档放弃（<0.60）**")
    logger.info("  结论：%s", verdict)
    logger.info("=" * 78)

    write_json(path_of("reports_dir") / "pest_pseudo_quality.json", {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "locator": str(args.locator), "conf": args.conf, "split_size": len(paths),
        "no_box": no_box, "class_mismatch": class_mismatch,
        "measured": n, "hit_iou50": hit, "hit_rate": round(rate, 4),
        "iou_median": round(ious[len(ious) // 2], 4) if ious else None,
        "verdict": verdict,
    })
    return 0 if rate >= 0.60 else 1


# ---------------------------------------------------------------- 生成

def cmd_build(args) -> int:
    pool = _load_json(path_of("reports_dir") / POOL_NAME)["clean_images"]
    if args.limit:
        pool = pool[: args.limit]
    logger.info("可用池 %d 张，开始用定位器出框", len(pool))

    from ultralytics import YOLO
    pred = _predict_top1(YOLO(str(args.locator)), pool, conf=args.conf, imgsz=args.imgsz,
                         batch=args.batch, device=args.device, chunk=args.chunk)

    out_root = _pseudo_root()
    img_dir = _raw_img_dir(out_root)
    lbl_dir = _raw_lbl_dir(out_root)
    ensure_dir(img_dir)
    ensure_dir(lbl_dir)

    kept: list[tuple[str, int, list[float]]] = []
    reasons: Counter[str] = Counter()
    per_class: Counter[int] = Counter()
    linked = copied = 0

    for p in pool:
        cls = int(Path(p).parent.name)
        b = pred.get(Path(p).stem)
        if b is None:
            reasons["定位器没出框"] += 1
            continue
        bad = _box_ok(b)
        if bad:
            reasons[bad] += 1
            continue
        kept.append((p, cls, b))

    logger.info("出框 %d 张，过滤掉 %d 张", len(kept), sum(reasons.values()))
    for k, v in reasons.most_common():
        logger.info("    %-22s %d", k, v)

    for p, cls, b in kept:
        stem = f"c{cls:03d}_{Path(p).stem}"
        dst_img = img_dir / f"{stem}{Path(p).suffix.lower()}"
        try:
            # 硬链接而不是复制：57k 张图有 6 GB，同盘硬链接几乎不占空间，
            # ultralytics 读起来和普通文件一样。跨盘或文件系统不支持时退回复制。
            if not dst_img.exists():
                os.link(p, dst_img)
                linked += 1
        except OSError:
            shutil.copyfile(p, dst_img)
            copied += 1
        (lbl_dir / f"{stem}.txt").write_text(
            f"{cls} {b[0]:.6f} {b[1]:.6f} {b[2]:.6f} {b[3]:.6f}\n", encoding="utf-8")
        per_class[cls] += 1

    yaml_path = _write_yaml(out_root)
    logger.info("")
    logger.info("=" * 78)
    logger.info("伪标注集：%d 张（硬链接 %d，复制 %d）", len(kept), linked, copied)
    logger.info("  落点：%s", out_root)
    logger.info("  yaml：%s", yaml_path)
    logger.info("  覆盖类数：%d / 102", len(per_class))
    if per_class:
        items = sorted(per_class.items(), key=lambda kv: kv[1])
        logger.info("  最少 5 类：%s", items[:5])
        logger.info("  最多 5 类：%s", items[-5:])
    logger.info("=" * 78)

    write_json(path_of("reports_dir") / REPORT_NAME, {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "locator": str(args.locator), "conf": args.conf, "imgsz": args.imgsz,
        "pool_size": len(pool), "kept": len(kept),
        "rejected": dict(reasons),
        "hardlinked": linked, "copied": copied,
        "data_yaml": str(yaml_path),
        "classes_covered": len(per_class),
        "per_class": {str(k): v for k, v in sorted(per_class.items())},
    })
    return 0


def _raw_img_dir(out_root: Path) -> Path:
    return out_root / "images" / "train"


def _raw_lbl_dir(out_root: Path) -> Path:
    return out_root / "labels" / "train"


def _write_yaml(out_root: Path) -> Path:
    """train 写成**目录列表**：真 train + 伪标注 train。

    `path` 必须是绝对路径 —— 相对路径会被 ultralytics 再拼一次，然后**静默找不到**图片
    （不是报错，是训练集悄悄少一半）。这条教训来自 `scripts/37_build_pest_aug.py`。
    """
    import yaml

    src_yaml = path_of("pest_yaml")
    src = yaml.safe_load(src_yaml.read_text(encoding="utf-8"))
    names = src["names"]
    root = out_root.resolve()
    dst = root / "data_pseudo.yaml"
    payload = {
        "path": str(root),
        "train": [str(path_of("pest_dataset") / "images" / "train"),
                  str(root / "images" / "train")],
        "val": str(path_of("pest_dataset") / "images" / "val"),
        "test": str(path_of("pest_dataset") / "images" / "test"),
        "nc": int(src.get("nc", len(names))),
        "names": names,
    }
    dst.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return dst


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="用类无关定位器生成伪标注")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("validate", "用「已有真框」的留出集验伪框质量"),
                            ("build", "对可用池生成伪标注数据集")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--locator", required=True, help="单类定位器权重（--single-cls 训出来的）")
        s.add_argument("--conf", type=float, default=0.5)
        s.add_argument("--imgsz", type=int, default=640)
        s.add_argument("--batch", type=int, default=8)
        s.add_argument("--device", default="0")
        s.add_argument("--chunk", type=int, default=64)
        s.add_argument("--max-vram-gb", type=float, default=3.0)
        s.add_argument("--threads", type=int, default=4)
        s.add_argument("--limit", type=int, default=None)
        s.add_argument("--source", choices=("leaked", "already_labeled"), default="leaked",
                       help="validate 专用：留出集取自哪一堆。默认 leaked（未参与训练，诚实）")

    args = ap.parse_args()
    _limit_resources(args.max_vram_gb, args.threads)
    return cmd_validate(args) if args.cmd == "validate" else cmd_build(args)


if __name__ == "__main__":
    raise SystemExit(main())
