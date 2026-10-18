#!/usr/bin/env python
"""任务 2.8.3 —— 给半监督伪标注建「防污染」用的感知哈希索引。

## 这一步挡的是什么

要把识别集 `data/pest_cls_raw/extracted/classification/` 里那 5.6 万张
「有类别标签、没有框」的图拿去当训练样本，**必须先证明它们与检测 val/test 不重复** ——
否则伪标注出来的样本里混着验证集的图，指标会虚高，而那正是这次最不能出的错。

**只能按内容比对，不能按文件名。** 实测两套命名毫无对应关系：
检测集是 `IP000000003.jpg`（1146×848），识别集是 `00003.jpg`（330×247）——
名字对不上、尺寸也不一样，而 `IP000000003.jpg` 真正的同图是 `classification/train/0/00463.jpg`
（感知哈希距离 **0/64**）。**同一个 id 甚至不是同一张图。**

## 为什么用 pHash 而不是 MD5

同一张图在两套数据里被重存过（尺寸都变了），逐字节哈希必然对不上。
pHash 是先缩成 32×32 灰度、再取 DCT 低频与中位数比较，**重编码、改尺寸基本不影响它**，
而不同图之间距离通常在 20 以上 —— 阈值取 4 有充足裕量。

## 为什么要「向量化 + 分块」

5.6 万张查询 × 5317 张黑名单 = 3 亿次比较。用 numpy 把黑名单打包成 uint64 数组，
一次 XOR 再 `unpackbits` 求 popcount，单张图的比较是微秒级；剩下的瓶颈只有解码，
所以只对「解码」这一步开进程池。

## 用法

    python scripts/42_pest_phash_index.py                 # 全量建索引（约 10~20 分钟）
    python scripts/42_pest_phash_index.py --limit 2000    # 快速试跑
    python scripts/42_pest_phash_index.py --workers 1     # 更省资源
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.phash")

REPORT_NAME = "pest_phash_index.json"
POOL_NAME = "pest_pseudo_pool.json"
HASH_SIZE = 8            # 8×8 低频块 → 64 bit
HIGHRF = 4               # 先缩到 32×32 再做 DCT
DEFAULT_THRESHOLD = 4    # Hamming ≤ 4 视为「同一张图」
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def phash(path: str, hash_size: int = HASH_SIZE, highfreq: int = HIGHRF) -> int | None:
    """返回 64 位感知哈希（int）。读不了返回 None。

    与 `scripts/37_build_pest_aug.py` 里的 `_dhash` 是**两套不同**的东西，别混用：
    那个比的是相邻像素（dHash），适合「几乎逐像素相同」的重复背景；
    这里比的是 DCT 低频分量（pHash），对**缩放与重编码**更稳 —— 而这两套数据恰恰
    被重存过，尺寸都不一样，所以必须用 pHash。
    """
    import cv2
    import numpy as np

    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    size = hash_size * highfreq
    small = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(small))
    low = dct[:hash_size, :hash_size]
    # 去掉直流分量再取中位数：DC 反映整体亮度，留着会让阈值随曝光漂移
    med = float(np.median(low[1:, :]))
    bits = (low > med).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def _hash_one(path: str) -> tuple[str, int | None]:
    import cv2

    cv2.setNumThreads(1)          # 每个 worker 只用 1 个线程，别和训练抢
    return path, phash(path)


def _hash_many(paths: list[str], workers: int) -> dict[str, int]:
    out: dict[str, int] = {}
    done = 0
    t0 = time.perf_counter()
    if workers <= 1:
        it = map(_hash_one, paths)
        pool = None
    else:
        pool = ProcessPoolExecutor(max_workers=workers)
        it = pool.map(_hash_one, paths, chunksize=64)
    try:
        for p, h in it:
            done += 1
            if h is not None:
                out[p] = h
            if done % 5000 == 0:
                rate = done / max(1e-6, time.perf_counter() - t0)
                logger.info("  已算 %d/%d（%.0f 张/秒，预计还要 %.0f 分钟）",
                            done, len(paths), rate, (len(paths) - done) / max(1e-6, rate) / 60)
    finally:
        if pool is not None:
            pool.shutdown()
    return out


def _min_hamming(q: int, black: "object") -> int:
    """q 与黑名单里所有哈希的最小 Hamming 距离。向量化，单张微秒级。"""
    import numpy as np

    x = np.bitwise_xor(black, np.uint64(q))
    bits = np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1)
    return int(bits.sum(axis=1).min())


def _images(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXT)


def main() -> int:
    ap = argparse.ArgumentParser(description="半监督伪标注的防污染感知哈希索引")
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help="Hamming 距离小于等于它就算重复（默认 4）")
    ap.add_argument("--workers", type=int, default=2, help="解码进程数（默认 2，别和训练抢）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 张识别集图（试跑用）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import numpy as np

    ds = path_of("pest_dataset")
    cls_root = path_of("pest_cls_raw") / "extracted" / "classification"
    if not cls_root.exists():
        logger.error("找不到识别集：%s", cls_root)
        return 1

    # ---- 1a. 泄漏黑名单：检测集的 val 与 test（**碰都不能碰**）----
    leak_paths: list[str] = []
    for split in ("val", "test"):
        leak_paths += [str(p) for p in _images(ds / "images" / split)]
    logger.info("泄漏黑名单（检测 val/test）共 %d 张，开始算 pHash", len(leak_paths))
    leak_hash = _hash_many(leak_paths, args.workers)
    leak = np.array(sorted(leak_hash.values()), dtype=np.uint64)
    logger.info("泄漏黑名单哈希 %d 个", len(leak))

    # ---- 1b. 「已经有真框」名单：检测集 train ----
    # 识别集里混着检测集的图（实测是同一张照片的不同尺寸版本）。这些图**已经有真框**了，
    # 不该再走伪标注 —— 拿模型猜出来的框去替换/叠加真框，只会把训练集弄脏。
    # 所以它们既不是泄漏，也不进伪标注池，而是单独归一类，只做统计。
    train_paths = [str(p) for p in _images(ds / "images" / "train")]
    logger.info("检测 train（已有真框）共 %d 张，开始算 pHash", len(train_paths))
    train_hash = _hash_many(train_paths, args.workers)
    labeled = np.array(sorted(train_hash.values()), dtype=np.uint64)

    # ---- 2. 查询：识别集全部图 ----
    cls_paths = [str(p) for p in _images(cls_root)]
    if args.limit:
        cls_paths = cls_paths[: args.limit]
    logger.info("识别集待查 %d 张，开始算 pHash", len(cls_paths))
    cls_hash = _hash_many(cls_paths, args.workers)

    # ---- 3. 逐张比距离 ----
    logger.info("开始比对（阈值 Hamming ≤ %d）", args.threshold)
    leaked: list[dict] = []
    already: list[dict] = []
    clean: list[str] = []

    for i, (p, h) in enumerate(cls_hash.items()):
        d_leak = _min_hamming(h, leak)
        if d_leak <= args.threshold:
            leaked.append({"path": p, "distance": d_leak})
        else:
            d_lab = _min_hamming(h, labeled)
            if d_lab <= args.threshold:
                already.append({"path": p, "distance": d_lab})
            else:
                clean.append(p)
        if (i + 1) % 10000 == 0:
            logger.info("  已比对 %d/%d", i + 1, len(cls_hash))

    # 给前若干条命中配一个「撞上的是哪张检测图」，只为报告可读，不参与判定
    def _match_with(arr: np.ndarray, keys: list[str], h: int) -> str:
        x = np.bitwise_xor(arr, np.uint64(h))
        bits = np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)
        return keys[int(bits.argmin())] if len(keys) else ""

    leak_keys = sorted(leak_hash)
    leak_arr = np.array([leak_hash[k] for k in leak_keys], dtype=np.uint64)
    for item in leaked:
        item["matches"] = _match_with(leak_arr, leak_keys, cls_hash[item["path"]])
    train_keys = sorted(train_hash)
    train_arr = np.array([train_hash[k] for k in train_keys], dtype=np.uint64)
    # ② 的配对关系要**全量**存下来，不是只存前 200 条：它是后面验证伪标注框质量的
    # 留出集 —— 这些识别集图有对应的检测图，也就有真框，可以拿伪框去和真框比 IoU。
    for item in already:
        item["matches"] = _match_with(train_arr, train_keys, cls_hash[item["path"]])

    logger.info("")
    logger.info("=" * 78)
    logger.info("识别集共 %d 张，与检测集比完（Hamming ≤ %d）后分成三堆：", len(cls_hash), args.threshold)
    logger.info("  ① 与 val/test 重复 → **%d 张**（泄漏风险，永不使用）", len(leaked))
    logger.info("  ② 与检测 train 重复 → **%d 张**（已有真框，不必伪标注）", len(already))
    logger.info("  ③ 干净的可用池     → **%d 张**", len(clean))
    for name, grp in (("①", leaked), ("②", already)):
        if grp:
            ds_ = [b["distance"] for b in grp]
            logger.info("   %s 的距离分布：0（同一张图）%d 张，1~%d 共 %d 张",
                        name, sum(1 for d in ds_ if d == 0), args.threshold,
                        sum(1 for d in ds_ if 0 < d <= args.threshold))
    logger.info("=" * 78)
    logger.info("可用池按类别分布：")
    from collections import Counter
    cnt = Counter(Path(p).parent.name for p in clean)
    items = sorted(cnt.items(), key=lambda kv: kv[1])
    logger.info("  类数 %d；最少 5 类：%s", len(cnt), items[:5])
    logger.info("  最多 5 类：%s", items[-5:])

    out = Path(args.out) if args.out else (path_of("reports_dir") / REPORT_NAME)
    ensure_dir(out.parent)
    write_json(out, {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold_hamming": args.threshold,
        "hash_size": HASH_SIZE,
        "blacklist_leak": {"splits": ["val", "test"], "num_images": len(leak_hash)},
        "blacklist_labeled": {"splits": ["train"], "num_images": len(train_hash)},
        "classification": {
            "num_images": len(cls_hash),
            "leaked": len(leaked),
            "already_labeled": len(already),
            "clean": len(clean),
        },
        "leaked": leaked,
        "already_labeled": already,
    })
    write_json(path_of("reports_dir") / POOL_NAME, {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold_hamming": args.threshold,
        "clean_images": clean,
    })
    logger.info("索引报告：%s", out)
    logger.info("可用池清单：%s（%d 张）", path_of("reports_dir") / POOL_NAME, len(clean))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
