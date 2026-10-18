#!/usr/bin/env python
"""任务 2.7.6 第三档 —— 把 Pest24 的零物体图按比例混进训练集（**只组装，不训练**）。

## 负样本是对着 precision 去的

现在 val/test 的 mean precision 是 0.4785 / 0.5166 —— 用户感知到的「置信度低」里，
有相当一部分其实是**误检**（把叶片纹理、光斑、别的虫报成了目标）。纯背景图正是
对准这个的：它们不含任何目标，模型在它们身上报框就是错的，梯度会把它压下去。

**代价要预先说清**：混太多背景会压 recall（该报的不敢报了）。所以比例锁在
原始 train 规模的 10%~20%（原本 13659 张 → 1366~2732 张），并且：

    预期方向 = mean precision ↑，recall 持平或略 ↓
    **若 recall 明显掉，说明比例过大** —— 报告里把这条写死，免得事后各说各话。

## 为什么要离线缩图，而不是让 dataloader 每轮缩

Pest24 是 2095×1944 的极小目标数据集。直接丢进训练目录的话，每个 epoch 都要
把它 resize 到 640（还要参与 mosaic 拼接），既慢又浪费；而且**大图会主导 BN 的
统计量**，那是在悄悄改变整批数据的归一化行为。所以这里一次性缩到 640 级再落盘。

## 只组装 yaml，不训练

训练仍走 34 号（`--data` 指过来）。这样「混了多少、混了哪些」和「训出来怎么样」
是两件事，可以分别复现。`data/pest/data.yaml` **一个字节都不改**。

## 用法

    python scripts/39_build_pest_mixed.py --dry-run          # 只算配额与统计
    python scripts/39_build_pest_mixed.py --ratio 0.15
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.exp.mixed")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
SPLITS = ("train", "val", "test")

# 计划里锁死的比例区间
MIN_RATIO, MAX_RATIO = 0.10, 0.20


def _instances(split: str) -> Counter:
    c: Counter = Counter()
    d = path_of("pest_dataset") / "labels" / split
    if not d.exists():
        return c
    for f in d.glob("*.txt"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if parts:
                try:
                    c[int(float(parts[0]))] += 1
                except ValueError:
                    continue
    return c


def _dhash(p: Path, size: int = 8) -> str:
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return "unreadable"
    small = cv2.resize(img, (size + 1, size), interpolation=cv2.INTER_AREA)
    return hashlib.md5((small[:, 1:] > small[:, :-1]).tobytes()).hexdigest()[:16]


def _banned_hashes() -> set[str]:
    """val/test 的感知哈希 —— 负样本绝不能和评估集重复，否则就是在评估集上训。"""
    out = set()
    for split in ("val", "test"):
        d = path_of("pest_dataset") / "images" / split
        if d.exists():
            for p in d.glob("*"):
                if p.suffix.lower() in IMAGE_EXT:
                    out.add(_dhash(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.6 第三档：组装 Pest24 负样本混训入口")
    ap.add_argument("--ratio", type=float, default=0.15, help=f"负样本占比（{MIN_RATIO}~{MAX_RATIO}）")
    ap.add_argument("--max-side", type=int, default=640, help="离线缩图后的最长边")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not (MIN_RATIO <= args.ratio <= MAX_RATIO):
        logger.error("ratio=%.3f 越界。计划锁死 %s~%s —— 超出这个区间 recall 会被拖下去，"
                     "要试别的比例请显式改这两个常量并说明理由。", args.ratio, MIN_RATIO, MAX_RATIO)
        return 1

    manifest_path = path_of("pest_dataset") / "manifest.json"
    n_train = json.loads(manifest_path.read_text(encoding="utf-8"))["num_images"]["train"]
    target = int(round(n_train * args.ratio))
    logger.info("原始 train %d 张 → 负样本配额 %d 张（比例 %.3f）", n_train, target, args.ratio)

    # 混入前统计（作为基线打印，混完再打印一次对比）
    before = {s: _instances(s) for s in SPLITS}
    total_inst = sum(sum(c.values()) for c in before.values())
    logger.info("混入前：train 实例 %d，val 实例 %d，test 实例 %d，合计 %d",
                sum(before["train"].values()), sum(before["val"].values()),
                sum(before["test"].values()), total_inst)

    src = path_of("pest24_dataset") / "images" / "all"
    if not src.exists():
        logger.error("找不到 Pest24 负样本目录：%s\n请先跑 scripts/38_download_pest24.py。", src)
        return 1
    pool = sorted(p for p in src.glob("*") if p.suffix.lower() in IMAGE_EXT)
    if not pool:
        logger.error("负样本目录是空的：%s", src)
        return 1

    rng = random.Random(args.seed)
    rng.shuffle(pool)

    banned = _banned_hashes()
    chosen, skipped_dup, seen = [], 0, set()
    for p in pool:
        if len(chosen) >= target:
            break
        h = _dhash(p)
        if h in banned or h in seen:
            skipped_dup += 1
            continue
        seen.add(h)
        chosen.append(p)
    logger.info("从 %d 张里选出 %d 张（跳过与 val/test 或彼此重复的 %d 张）",
                len(pool), len(chosen), skipped_dup)

    if args.dry_run:
        logger.info("--dry-run：不生成任何文件。若真跑，将把上面这些图缩到最长边 %d 并落盘。",
                    args.max_side)
        return 0

    out = ensure_dir(path_of("pest_mixed_yaml")).parent
    img_out = ensure_dir(out / "images" / "train")
    lbl_out = ensure_dir(out / "labels" / "train")

    resized = failed = 0
    for p in chosen:
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            failed += 1
            continue
        h, w = img.shape[:2]
        s = args.max_side / max(h, w)
        if s < 1.0:
            img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                             interpolation=cv2.INTER_AREA)
        name = f"neg_{p.stem}.jpg"
        if not cv2.imwrite(str(img_out / name), img, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            failed += 1
            continue
        # 空 label = 这张图上没有目标。这就是负样本的全部内容。
        (lbl_out / f"neg_{p.stem}.txt").write_text("", encoding="utf-8")
        resized += 1

    # --- 组装 yaml ---
    import yaml

    src_yaml = yaml.safe_load(path_of("pest_yaml").read_text(encoding="utf-8"))
    mixed = {
        "path": str(out),
        "train": [str(path_of("pest_dataset") / "images" / "train"), str(img_out)],
        "val": str(path_of("pest_dataset") / "images" / "val"),
        "test": str(path_of("pest_dataset") / "images" / "test"),
        "nc": src_yaml["nc"],
        "names": src_yaml["names"],
    }
    ypath = Path(path_of("pest_mixed_yaml"))
    ypath.write_text(yaml.safe_dump(mixed, allow_unicode=True, sort_keys=False), encoding="utf-8")

    after = {s: _instances(s) for s in SPLITS}
    total_inst_after = sum(sum(c.values()) for c in after.values())

    # 各类实例数必须**一个都没变** —— 负样本不带标签，这是它的定义。
    changed = [k for k in set(before["train"]) | set(after["train"])
               if before["train"].get(k, 0) != after["train"].get(k, 0)]

    rare = sorted(after["train"].items(), key=lambda kv: kv[1])[:10]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed, "ratio": args.ratio, "max_side": args.max_side,
        "base_train_images": n_train, "target_negatives": target,
        "negatives_used": resized, "skipped_duplicate": skipped_dup, "resize_failed": failed,
        "train_images_after": n_train + resized,
        "background_fraction_of_train": round(resized / (n_train + resized), 4),
        "instances_before": {s: sum(before[s].values()) for s in SPLITS},
        "instances_after": {s: sum(after[s].values()) for s in SPLITS},
        "per_class_instances_unchanged": not changed,
        "changed_classes": changed[:20],
        "rarest_train_classes": dict(rare),
        "mixed_yaml": str(ypath),
        "expected_direction": "mean precision ↑，recall 持平或略 ↓；"
                              "**若 recall 明显下降 → 负样本比例过大**，回到 39 号调低 --ratio。",
        "note": "负样本只加图、不加框：各类实例数必须逐类不变，报告里的 "
                "per_class_instances_unchanged 就是在验这一条。",
    }
    dest = path_of("reports_dir") / "pest_mixed_report.json"
    ensure_dir(dest.parent)
    write_json(dest, report)

    logger.info("=" * 78)
    logger.info("落盘负样本 %d 张（缩到最长边 %d）→ %s", resized, args.max_side, img_out)
    logger.info("训练图数 %d → %d（背景占比 %.1f%%）", n_train, n_train + resized,
                100 * resized / (n_train + resized))
    logger.info("实例数：%d → %d", total_inst, total_inst_after)
    if changed:
        logger.error("**有 %d 个类的实例数变了**，这不该发生 —— 负样本不该带标签：%s",
                     len(changed), changed[:10])
        return 1
    logger.info("逐类实例数不变 ✓（负样本不带框）")
    logger.info("最稀有的 train 类（前 10）：%s", rare)
    logger.info("训练入口：%s", ypath)
    logger.info("=" * 78)
    logger.info("下一步：python scripts/34_train_pest_experiments.py --tag mix_neg "
                "--data %s --epochs 100", ypath)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
