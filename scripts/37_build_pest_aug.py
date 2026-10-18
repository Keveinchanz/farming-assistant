#!/usr/bin/env python
"""任务 2.7.6 第二档 —— 把 IP102 分类图用 copy-paste 变成稀有类的检测样本。

## 为什么是 copy-paste，而不是给分类图「随便画个框」

分类图**没有框**。想把它们变成检测监督信号，只有两条路：

1. 假设「主体占满整图」→ 框 = 整图或中心裁切。**这是错的**：IP102 分类图里有大量
   虫体只占一角、背景是叶片的样本，整图当框会把叶片纹理也教成「这只虫」。
2. 用前景分割切出虫体，**贴到已知背景上**，框由粘贴位置直接算出来 —— 就是本脚本。

第 2 条路的好处是**框是构造出来的、不是猜出来的**：贴在哪、多大，标签就是什么，精确可验。

## 只补「稀有类」，而且这条规则是量出来的

目标类 = **三档（train+val+test）检测实例总数 < 20** 的类。这条规则自然覆盖那 5 个
零实例「死类」（59 葡萄根瘤蚜 / 60 葡萄缺节瘿螨 / 63 茶跗线螨 / 75 柑橘锈瘿螨 / 80 糠片蚧）——
**它们是本档存在的唯一理由**，因为只有「同物种、同体系」的 IP102 分类图能给它们监督信号。

## 类序定死，不猜

分类集目录名是数字（0..101 或 1..102，两种发布都有）。36 号把发现结果写进
`layout.json` 的 `name_range`，本脚本据此定 `index_base` —— 只有这两种可能，
所以推断是安全的；但**下标一旦错一位，102 类整体错位且不报错**，所以这里把它
打印出来并写进报告，让人有机会看一眼。

## 质量过滤（宁缺毋滥）

被接受的样本必须同时满足：前景在裁切框里的占比够、贴上去的框面积在合理区间、
**前景源与背景都不得与 val/test 重复**、单张背景贴的实例数不超上限。**被丢掉的都计数写进报告**，
不是悄悄跳过。

（为什么前景源也要挡：IP102 的**分类集与检测集同源**，检测集 val/test 的那批图很可能
也在分类集里 —— 拿它当前景源就等于把评估集的对象搬进训练集。背景那条旧版就有，
前景源这条是补的。）

## 用法

    python scripts/37_build_pest_aug.py --dry-run          # 只算目标类与配额，不生成任何图
    python scripts/37_build_pest_aug.py                    # 真生成
    python scripts/37_build_pest_aug.py --per-class 80 --max-per-bg 2
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2          # noqa: E402
import numpy as np  # noqa: E402

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.exp.aug")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
SPLITS = ("train", "val", "test")

# 目标类门槛：检测实例总数低于它就补
RARE_MAX_INSTANCES = 20

# ---- 质量过滤阈值（都可调，但默认值是有理由的，见报告里的说明）----
MIN_FG_COVERAGE = 0.35   # **前景像素占裁切框面积比** —— 计划里那条「前景占候选框比例 ≥ 阈值」
# 上面这条的分母是**裁切框**。整图占比另有下面这个**退化下限**，它不是计划里的门槛，
# 用途也不同：只挡「GrabCut 只捞到几个像素的杂点」这种明显失败的掩膜。
#
# ★ 初版把 MIN_FG_COVERAGE 也用在了整图占比上 —— 分母错了。IP102 分类图里虫体占
#   整图的中位数只有 0.09~0.23，拿 0.35 卡整图等于要求每张源图都是虫体特写；实测把
#   896 张源图砍到 93 张（十去其九）。计划要的门槛是裁切框那条，这里改回它的语义。
MIN_FG_COVERAGE_WHOLE = 0.02
MIN_BOX_FRAC = 0.005     # 贴上去的框至少占整图 0.5% 面积，再小就没法学
MAX_BOX_FRAC = 0.30      # 至多 30%，再大就不像「目标检测」而像「整图分类」
GRABCUT_SIDE = 256       # 分割前把图缩到这个边长：GrabCut 在原尺寸上太慢


# ---------------------------------------------------------------- 读检测标注

def _label_dir(split: str) -> Path:
    return path_of("pest_dataset") / "labels" / split


def count_detection_instances() -> dict[int, int]:
    """三档合计的逐类实例数 —— 就是 35 号报告里那张分箱表的同一个量。"""
    counts: Counter[int] = Counter()
    for split in SPLITS:
        d = _label_dir(split)
        if not d.exists():
            continue
        for f in d.glob("*.txt"):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    counts[int(float(line.split()[0]))] += 1
                except (ValueError, IndexError):
                    continue
    return dict(counts)


def _background_index() -> tuple[list[Path], set[str]]:
    """背景候选 = 检测集 train 里的图。同时算出 val/test 的感知哈希，用来挡重复背景。"""
    tr = path_of("pest_dataset") / "images" / "train"
    bgs = sorted(p for p in tr.glob("*") if p.suffix.lower() in IMAGE_EXT)

    banned: set[str] = set()
    for split in ("val", "test"):
        d = path_of("pest_dataset") / "images" / split
        if not d.exists():
            continue
        for p in d.glob("*"):
            if p.suffix.lower() in IMAGE_EXT:
                banned.add(_dhash(p))
    return bgs, banned


def _dhash(p: Path, size: int = 8) -> str:
    """差异哈希：缩放成灰度小图后比相邻像素。用来挡「背景其实就是验证集某张图」。"""
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return "unreadable"
    small = cv2.resize(img, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = small[:, 1:] > small[:, :-1]
    return hashlib.md5(bits.tobytes()).hexdigest()[:16]


def _labels_of(img_path: Path) -> list[tuple[int, float, float, float, float]]:
    """读某张 train 图的 YOLO 标注，(类号, cx, cy, w, h)。读一次，供多处复用。"""
    lbl = _label_dir("train") / f"{img_path.stem}.txt"
    out: list[tuple[int, float, float, float, float]] = []
    if not lbl.exists():
        return out
    for line in lbl.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 5:
            try:
                out.append((int(float(parts[0])), *(float(v) for v in parts[1:5])))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------- 前景分割

def extract_foreground(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """GrabCut 抠出前景，返回 (原图 BGR, 前景掩膜 uint8 0/255)。失败返回 None。

    在小图上跑 GrabCut（原图跑一张要 ~1 秒，几万张等不起），掩膜再放大回原尺寸。
    矩形先验取「中心 80%」：IP102 分类图基本都是主体居中。
    """
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = GRABCUT_SIDE / max(h, w)
    if scale < 1.0:
        small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = img
    sh, sw = small.shape[:2]

    mask = np.zeros((sh, sw), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    mx, my = int(sw * 0.10), int(sh * 0.10)
    rect = (mx, my, max(1, sw - 2 * mx), max(1, sh - 2 * my))
    try:
        cv2.grabCut(small, mask, rect, bgd, fgd, 3, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)

    # 只留最大连通域：扫过整图的杂点会被拼上去当第二只虫，直接污染标签
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n <= 1:
        return None
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    fg = np.where(lab == biggest, 255, 0).astype(np.uint8)

    if fg.shape != (h, w):
        fg = cv2.resize(fg, (w, h), interpolation=cv2.INTER_NEAREST)
    return img, fg


def _crop_to_fg(img: np.ndarray, fg: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """按掩膜外接框裁切，返回 (裁切图, 裁切掩膜)。"""
    ys, xs = np.where(fg > 0)
    if len(xs) < 16:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return img[y0:y1, x0:x1], fg[y0:y1, x0:x1]


# ---------------------------------------------------------------- 粘贴

def paste(canvas: np.ndarray, fg_img: np.ndarray, fg_mask: np.ndarray,
          boxes: list[tuple[float, float, float, float]], rng: random.Random,
          ) -> tuple[float, float, float, float] | None:
    """把前景贴到画布上，返回新的 YOLO 归一化框 (cx,cy,w,h)；失败返回 None。"""
    H, W = canvas.shape[:2]
    fh, fw = fg_img.shape[:2]

    # 目标缩放：让贴上去的框占整图面积落在 [MIN_BOX_FRAC, MAX_BOX_FRAC]
    target_frac = rng.uniform(MIN_BOX_FRAC * 1.5, min(MAX_BOX_FRAC, 0.12))
    s = float(np.sqrt(target_frac * H * W / max(fh * fw, 1)))
    nw, nh = max(8, int(fw * s)), max(8, int(fh * s))
    if nw >= W or nh >= H:
        return None

    fgi = cv2.resize(fg_img, (nw, nh), interpolation=cv2.INTER_AREA)
    mki = cv2.resize(fg_mask, (nw, nh), interpolation=cv2.INTER_NEAREST)

    # 贴的位置：随机试几次，尽量避开已有框（否则会把「别的虫的一部分」盖住，
    # 让原图那个框的标签变得不对 —— 那是在给数据集注噪）
    best = None
    for _ in range(12):
        px = rng.randint(0, W - nw)
        py = rng.randint(0, H - nh)
        cx, cy = (px + nw / 2) / W, (py + nh / 2) / H
        inter = sum(_iou_xywh((cx, cy, nw / W, nh / H), b) for b in boxes)
        if best is None or inter < best[0]:
            best = (inter, px, py)
        if inter == 0:
            break
    _, px, py = best

    roi = canvas[py:py + nh, px:px + nw]
    cv2.copyTo(fgi, mki, roi)

    return ((px + nw / 2) / W, (py + nh / 2) / H, nw / W, nh / H)


def _iou_xywh(a, b) -> float:
    ax1, ay1 = a[0] - a[2] / 2, a[1] - a[3] / 2
    ax2, ay2 = a[0] + a[2] / 2, a[1] + a[3] / 2
    bx1, by1 = b[0] - b[2] / 2, b[1] - b[3] / 2
    bx2, by2 = b[0] + b[2] / 2, b[1] + b[3] / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------- 分类集定位

def _class_dirs() -> tuple[Path, int, dict[int, Path]]:
    """从 36 号写的 layout.json 里取「逐类图片目录」与下标基。

    返回 (父目录, index_base, {检测类号: 分类图目录})。
    index_base 只可能是 0 或 1 —— 目录名要么 0..101 要么 1..102，
    这两种可能互斥，所以按 name_range 推断是安全的（但仍然打印出来让人过目）。
    """
    layout_path = path_of("pest_cls_raw") / "layout.json"
    if not layout_path.exists():
        raise SystemExit(
            f"找不到 {layout_path} —— 请先跑 scripts/36_download_ip102_cls.py 下载分类集。"
        )
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    cd = layout.get("class_dirs")
    if not cd:
        raise SystemExit("layout.json 里没有逐类图片目录，说明 36 号没能识别出布局 —— 先看它的报告。")

    parent = Path(cd["parent"])
    dirs = {int(d.name): d for d in parent.iterdir()
            if d.is_dir() and d.name.isdigit() and len(d.name) <= 3}
    lo, hi = min(dirs), max(dirs)
    base = lo
    if hi - lo + 1 != 102 or base not in (0, 1):
        raise SystemExit(
            f"分类集目录编号异常：{lo}..{hi}（共 {hi - lo + 1} 个）。"
            "期望恰好 102 个且从 0 或 1 开始 —— 不猜，请人工确认。"
        )
    logger.info("分类集目录：%s，编号 %d..%d → index_base=%d（检测类号 = 目录号 - %d）",
                parent, lo, hi, base, base)
    return parent, base, dirs


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.6 第二档：copy-paste 造稀有类增广样本")
    ap.add_argument("--per-class", type=int, default=60, help="每个稀有类最多用多少张分类图")
    ap.add_argument("--max-per-bg", type=int, default=2, help="同一张背景最多承载几个副本")
    ap.add_argument("--out-root", default=None,
                    help="增广产物根目录（缺省 data/pest_aug）。换一个目录就等于并存一份变体，"
                         "不会覆盖旧产物 —— 要对比两套阈值时用得上。")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="只算目标类与配额，不生成图片")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    # 1) 找出目标类
    det = count_detection_instances()
    total_classes = len(json.loads(path_of("pest_label_map").read_text(encoding="utf-8"))["index_to_label"]) \
        if path_of("pest_label_map").exists() else 102
    rare = sorted([c for c in range(total_classes) if det.get(c, 0) < RARE_MAX_INSTANCES])
    zero = [c for c in rare if det.get(c, 0) == 0]

    logger.info("=" * 78)
    logger.info("检测实例总数 < %d 的类：%d / %d 个", RARE_MAX_INSTANCES, len(rare), total_classes)
    logger.info("其中**零实例**的类 %d 个：%s", len(zero), zero)
    logger.info("（零实例类是本档存在的唯一理由 —— 别的外部数据集给不了它们同体系监督）")
    logger.info("=" * 78)

    if args.dry_run:
        logger.info("--dry-run：不生成任何文件。")
        for c in rare:
            logger.info("  类 %3d：现有实例 %2d", c, det.get(c, 0))
        return 0

    parent, base, dirs = _class_dirs()
    bgs, banned_hashes = _background_index()
    if not bgs:
        logger.error("没有背景图可用：%s", path_of("pest_dataset") / "images" / "train")
        return 1
    logger.info("背景候选 %d 张（已排除与 val/test 重复的 %d 个哈希）", len(bgs), len(banned_hashes))

    # 必须 resolve 成绝对路径：yaml 里 train 是**目录列表**，而 ultralytics 会把非绝对的
    # 条目按 `path` 键再拼一次 —— 留相对路径就会变成 .../v2/data/pest_aug/v2/images/train，
    # 静默找不到图（或不报错但训了 0 张）。默认值 path_of() 本来就是绝对的，只有 --out-root
    # 给相对路径时才会踩到，所以在这里统一 resolve。
    out_root = ensure_dir(Path(args.out_root).resolve() if args.out_root
                          else path_of("pest_aug_dataset"))
    img_out = ensure_dir(out_root / "images" / "train")
    lbl_out = ensure_dir(out_root / "labels" / "train")

    stats: dict[str, dict] = {}
    rejected: Counter[str] = Counter()
    used_bg: Counter[str] = Counter()
    accepted_total = 0

    for cls in rare:
        cls_dir = dirs.get(cls + base)
        if cls_dir is None:
            logger.warning("类 %d：分类集里没有对应目录（目录号 %d），跳过", cls, cls + base)
            continue
        imgs = sorted(p for p in cls_dir.iterdir() if p.suffix.lower() in IMAGE_EXT)
        rng.shuffle(imgs)
        imgs = imgs[: args.per_class]

        made = 0
        for src in imgs:
            # 前景源先过重复检查再谈抠图 —— 顺序反了会白跑一遍 GrabCut。
            # 分类集与检测集同源，检测集 val/test 的图很可能也在分类集里；
            # 用它当前景源等于把评估集的对象搬进训练集。背景那条在这里也有一份。
            if _dhash(src) in banned_hashes:
                rejected["前景源与 val/test 重复"] += 1
                continue
            got = extract_foreground(src)
            if got is None:
                rejected["分割失败"] += 1
                continue
            fimg, fmask = got
            cov = float((fmask > 0).mean())
            if cov < MIN_FG_COVERAGE_WHOLE:
                rejected["前景掩膜退化为杂点"] += 1
                continue
            cropped = _crop_to_fg(fimg, fmask)
            if cropped is None:
                rejected["前景太小"] += 1
                continue
            cimg, cmask = cropped
            if float((cmask > 0).mean()) < MIN_FG_COVERAGE:
                rejected["裁切后占比过低"] += 1
                continue

            # 轮着挑背景，避免同一张背景被反复用
            placed = False
            for _ in range(6):
                bg = bgs[rng.randrange(len(bgs))]
                if used_bg[bg.name] >= args.max_per_bg:
                    continue
                canvas = cv2.imread(str(bg), cv2.IMREAD_COLOR)
                if canvas is None:
                    continue
                if _dhash(bg) in banned_hashes:
                    rejected["背景与 val/test 重复"] += 1
                    continue
                existing = _labels_of(bg)
                box = paste(canvas, cimg, cmask, [e[1:] for e in existing], rng)
                if box is None:
                    continue
                stem = f"aug_{cls:03d}_{made:04d}"
                if not cv2.imwrite(str(img_out / f"{stem}.jpg"), canvas):
                    rejected["写盘失败"] += 1
                    continue
                lines = [f"{cls} {box[0]:.6f} {box[1]:.6f} {box[2]:.6f} {box[3]:.6f}\n"]
                # 原图已有的框**一个都不能丢**：这张图既有原标注、又新贴了一个目标，
                # 少写一行就等于把原来那只虫标成了背景。
                for ec, *eb in existing:
                    lines.append(f"{ec} {eb[0]:.6f} {eb[1]:.6f} {eb[2]:.6f} {eb[3]:.6f}\n")
                (lbl_out / f"{stem}.txt").write_text("".join(lines), encoding="utf-8")
                used_bg[bg.name] += 1
                made += 1
                accepted_total += 1
                placed = True
                break
            if not placed:
                rejected["找不到可用背景"] += 1

        stats[str(cls)] = {"existing_instances": det.get(cls, 0),
                           "classification_images": len(imgs), "generated": made}
        logger.info("类 %3d：现有实例 %2d → 生成 %3d 张", cls, det.get(cls, 0), made)

    # 2) 写训练入口 yaml —— **绝不碰 data/pest/data.yaml**
    import yaml

    src_yaml = yaml.safe_load(path_of("pest_yaml").read_text(encoding="utf-8"))
    orig_images = path_of("pest_dataset") / "images" / "train"
    aug_images = out_root / "images" / "train"
    aug_yaml = {
        "path": str(out_root),
        # train 写成目录列表：原来的 train 一张不动，增广样本作为第二个目录挂上去
        "train": [str(orig_images), str(aug_images)],
        "val": str(path_of("pest_dataset") / "images" / "val"),
        "test": str(path_of("pest_dataset") / "images" / "test"),
        "nc": src_yaml["nc"],
        # names 逐字照抄原 yaml，保证两边永远是同一套类名
        "names": src_yaml["names"],
    }
    # yaml 跟产物同目录（默认 out_root=data/pest_aug → 仍就是 data/pest_aug/data_aug.yaml）
    ypath = out_root / "data_aug.yaml"
    ensure_dir(ypath.parent)
    ypath.write_text(yaml.safe_dump(aug_yaml, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logger.info("训练入口已写：%s（train 为目录列表，原 train 未被修改）", ypath)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": args.seed, "per_class_cap": args.per_class, "max_per_bg": args.max_per_bg,
        "rare_max_instances": RARE_MAX_INSTANCES,
        "thresholds": {"min_fg_coverage_crop": MIN_FG_COVERAGE,
                       "min_fg_coverage_whole_degenerate_floor": MIN_FG_COVERAGE_WHOLE,
                       "min_box_frac": MIN_BOX_FRAC,
                       "max_box_frac": MAX_BOX_FRAC, "grabcut_side": GRABCUT_SIDE},
        "num_rare_classes": len(rare), "num_zero_instance_classes": len(zero),
        "zero_instance_classes": zero,
        "accepted_total": accepted_total,
        "rejected": dict(rejected),
        "per_class": stats,
        "aug_yaml": str(ypath),
        "note": "只往 train 里加；val/test 仍指原始划分，所以增广的效果能在原划分上直接对比。"
                "被拒绝的样本全部计数在 rejected 里，没有静默丢弃。",
    }
    dest = path_of("reports_dir") / "pest_aug_report.json"
    ensure_dir(dest.parent)
    write_json(dest, report)

    logger.info("=" * 78)
    logger.info("生成 %d 张增广图；拒绝明细：%s", accepted_total, dict(rejected))
    logger.info("报告：%s", dest)
    logger.info("=" * 78)
    logger.info("下一步：python scripts/34_train_pest_experiments.py --tag ep100_640_n_aug "
                "--data %s --epochs 100", ypath)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
