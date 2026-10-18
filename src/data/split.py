"""任务 1.2 —— 按 8:1:1 划分训练/验证/测试集，生成路径标签对照表。

为什么不用 sklearn 的 train_test_split(stratify=...)：
    本数据集 261 个类各只有 10 张图。分层抽样按比例算，10*0.1 = 1 张，
    但 sklearn 在取整/随机边界上无法保证每个类都被抽到 —— 一旦某个类在
    测试集里是 0 张，该类就永远不会被评估，评测结论会失真。
因此改为【逐类循环 + 固定种子】：
    n_val  = max(1, round(n * 0.1))
    n_test = max(1, round(n * 0.1))
    n_train = n - n_val - n_test
10 张的类 -> 8/1/1，强制每类至少 1 张进验证集与测试集，271 类全覆盖。

长尾大样本类（500~1122 张）按 10% 取验证/测试会拖慢每轮评估，
用 VAL_CAP / TEST_CAP 封顶（默认各 50 张），训练集仍保留其余全部样本。

产出：data/splits/{train,val,test}.csv
     列：path(相对项目根, 正斜杠), index(0..270), dir_id, label_cn, crop, is_healthy
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from src.common.io_utils import list_images, write_csv
from src.common.logging_utils import setup_logger
from src.common.paths import PROJECT_ROOT, path_of

logger = setup_logger("agri.split")

SEED = 42
VAL_RATIO = 0.1
TEST_RATIO = 0.1
VAL_CAP = 50      # 长尾大类的验证集上限，避免每轮评估过慢
TEST_CAP = 50     # 长尾大类的测试集上限
FIELDS = ["path", "index", "dir_id", "label_cn", "crop", "is_healthy"]


@dataclass
class SplitResult:
    train: list[dict]
    val: list[dict]
    test: list[dict]


def _rel(path: Path) -> str:
    """相对项目根的 POSIX 风格路径，保证 CSV 在换平台后仍可用。"""
    return path.relative_to(PROJECT_ROOT).as_posix()


def _allocate(n: int, test_ratio: float, val_ratio: float, test_cap: int, val_cap: int) -> tuple[int, int]:
    """给单个类别分配验证/测试集大小，始终保证训练集至少 1 张、每类都进 val 与 test。

    n=1 -> (0, 0)：只有一张时全部留给训练，不参与验证/测试
    n=2 -> (1, 0)
    n=3 -> (1, 1)
    n>=4 -> 按比例取整，各自 max(1, ...)，再受 *_cap 封顶，且不侵占训练集
    """
    if n <= 1:
        return 0, 0
    if n == 2:
        return 1, 0
    if n == 3:
        return 1, 1

    n_test = min(max(1, round(n * test_ratio)), test_cap, n - 2)
    n_val = min(max(1, round(n * val_ratio)), val_cap, n - n_test - 1)
    return n_test, n_val


def load_classes() -> list[dict]:
    """从 1.1 产出的 manifest.json 读类别清单，按目录号升序编号 index=0..N-1。

    index 必须是连续整数（不用原始目录号 100..370）：271 类若出现空档，
    分类头维度与实际标签对不上，训练直接崩。
    """
    manifest_path = path_of("processed_dataset").parent / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"未找到 {manifest_path}，请先运行：python scripts/01_organize_data.py"
        )
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    classes = sorted(manifest["classes"], key=lambda c: int(c["dir_id"]))
    for idx, c in enumerate(classes):
        c["index"] = idx
    return classes


def split_dataset(
    *,
    val_ratio: float = VAL_RATIO,
    test_ratio: float = TEST_RATIO,
    val_cap: int = VAL_CAP,
    test_cap: int = TEST_CAP,
    seed: int = SEED,
) -> SplitResult:
    classes = load_classes()
    logger.info("载入 %d 个类别，开始逐类分层划分（seed=%d）", len(classes), seed)

    train: list[dict] = []
    val: list[dict] = []
    test: list[dict] = []

    for c in classes:
        out_dir = PROJECT_ROOT / c["out_dir"]
        images = list_images(out_dir, recursive=False)
        if not images:
            logger.warning("类别 %s 没有图片，已跳过：%s", c["label_cn"], out_dir)
            continue

        images = sorted(images)                      # 先排序，保证跨平台可复现
        rng = random.Random(seed + c["index"])       # 每类独立种子，互不干扰
        rng.shuffle(images)

        n = len(images)
        n_test, n_val = _allocate(n, test_ratio, val_ratio, test_cap, val_cap)

        buckets = [
            (test, images[:n_test]),
            (val, images[n_test:n_test + n_val]),
            (train, images[n_test + n_val:]),
        ]
        for bucket, files in buckets:
            for p in files:
                bucket.append(
                    {
                        "path": _rel(p),
                        "index": c["index"],
                        "dir_id": c["dir_id"],
                        "label_cn": c["label_cn"],
                        "crop": c["crop"],
                        "is_healthy": int(bool(c["is_healthy"])),
                    }
                )

    result = SplitResult(train, val, test)
    _write(result)
    _report(result, len(classes))
    return result


def _write(result: SplitResult) -> None:
    splits = path_of("splits_dir")
    write_csv(splits / "train.csv", result.train, FIELDS)
    write_csv(splits / "val.csv", result.val, FIELDS)
    write_csv(splits / "test.csv", result.test, FIELDS)
    logger.info("已写出：train.csv / val.csv / test.csv -> %s", splits)


def _report(result: SplitResult, n_classes: int) -> None:
    total = len(result.train) + len(result.val) + len(result.test)
    logger.info("划分结果：train=%d  val=%d  test=%d  合计=%d",
                len(result.train), len(result.val), len(result.test), total)

    for name, rows in (("train", result.train), ("val", result.val), ("test", result.test)):
        covered = {r["index"] for r in rows}
        missing = sorted(set(range(n_classes)) - covered)
        if missing:
            logger.warning("%s 集缺少 %d 个类别（index: %s...）", name, len(missing), missing[:10])
        else:
            logger.info("%s 集覆盖全部 %d 个类别 ✓", name, n_classes)


if __name__ == "__main__":
    split_dataset()
