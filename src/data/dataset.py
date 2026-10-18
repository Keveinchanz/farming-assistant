"""任务 2.1 —— 自定义 Dataset 类，对接数据集，支持批量加载、在线数据增强。

设计要点：
  · 读取 1.2 产出的 split CSV（path 为相对项目根的 POSIX 路径）
  · 在线做增强：__getitem__ 时才读图 + 变换，不预先把全部图片解码进内存
    （10165 张 256x256 全解码约 2GB，预载没必要且吃内存）
  · 图片打开后立即 convert("RGB")：数据集中存在灰度/带 Alpha 的图，统一三通道
  · 坏图不直接让训练崩：捕获异常后返回一张黑图并计数，训练结束可查看告警数
  · Windows 下多进程 DataLoader 必须 __main__ 保护，见 build_dataloaders 的说明
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

import torch
from torch.utils.data import DataLoader, Dataset

from src.common.io_utils import read_csv
from src.common.logging_utils import setup_logger
from src.common.paths import PROJECT_ROOT

logger = setup_logger("agri.dataset")


class AgriImageDataset(Dataset):
    """病虫害图像分类数据集。

    参数
        rows        : split CSV 的行列表（含 path/index/label_cn/crop 等字段）
        transform   : 图像变换（PIL -> Tensor）
        input_size  : 读图失败时的兜底黑图尺寸
        return_meta : True 时 __getitem__ 返回 (tensor, label, meta)，评估/错误分析用
    """

    def __init__(
        self,
        rows: Sequence[dict],
        transform: Callable | None = None,
        *,
        input_size: int = 224,
        return_meta: bool = False,
    ) -> None:
        if not rows:
            raise ValueError("数据集为空：split CSV 没有读到任何行")
        self.rows = list(rows)
        self.transform = transform
        self.input_size = input_size
        self.return_meta = return_meta
        self.bad_images = 0

        self.labels = [int(r["index"]) for r in self.rows]
        self.paths = [PROJECT_ROOT / r["path"] for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, i: int):
        from PIL import Image

        row = self.rows[i]
        path = self.paths[i]
        try:
            with Image.open(path) as im:
                img = im.convert("RGB")
        except Exception as e:  # noqa: BLE001
            self.bad_images += 1
            if self.bad_images <= 10:
                logger.warning("读图失败（第 %d 次）：%s（%s）", self.bad_images, path, e)
            img = Image.new("RGB", (self.input_size, self.input_size), (0, 0, 0))

        tensor = self.transform(img) if self.transform is not None else _to_tensor(img)
        label = int(row["index"])

        if self.return_meta:
            return tensor, label, {"path": row["path"], "label_cn": row["label_cn"],
                                   "crop": row["crop"], "dir_id": row["dir_id"]}
        return tensor, label

    # ---------------------------------------------------------------- 便捷构造

    @classmethod
    def from_csv(
        cls,
        csv_path: str | Path,
        transform: Callable | None = None,
        *,
        input_size: int = 224,
        return_meta: bool = False,
        limit: int | None = None,
    ) -> "AgriImageDataset":
        rows = read_csv(csv_path)
        if limit:
            rows = rows[:limit]
        logger.info("载入 %s：%d 条样本", Path(csv_path).name, len(rows))
        return cls(rows, transform, input_size=input_size, return_meta=return_meta)

    def class_counts(self, num_classes: int) -> list[int]:
        counts = [0] * num_classes
        for y in self.labels:
            counts[y] += 1
        return counts


def _to_tensor(img):
    from torchvision import transforms as T

    return T.ToTensor()(img)


# -------------------------------------------------------------------- DataLoader

def make_loader(
    dataset: Dataset,
    *,
    batch_size: int,
    shuffle: bool = False,
    sampler=None,
    num_workers: int = 4,
    pin_memory: bool = True,
    persistent_workers: bool = True,
    drop_last: bool = False,
) -> DataLoader:
    """统一构造 DataLoader。

    Windows 注意：num_workers>0 时 DataLoader 会 spawn 子进程，
    调用方脚本必须放在 `if __name__ == "__main__":` 之下，否则子进程会重复执行导入期代码，
    表现为进程数爆炸或直接卡死。若遇到 spawn 相关报错，把 num_workers 设为 0 即可。
    """
    use_workers = max(0, int(num_workers))
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle if sampler is None else False,   # sampler 与 shuffle 互斥
        sampler=sampler,
        num_workers=use_workers,
        pin_memory=bool(pin_memory) and torch.cuda.is_available(),
        persistent_workers=bool(persistent_workers) and use_workers > 0,
        drop_last=drop_last,
    )


def build_dataloaders(
    cfg: dict[str, Any],
    train_transform: Callable,
    eval_transform: Callable,
    *,
    num_classes: int,
    yaml_to_abs: Callable[[str], Path],
) -> dict[str, DataLoader]:
    """按训练配置一次性建出 train / val / test 三个 DataLoader。

    train 在配置 weighted_sampler=true 时启用加权采样（见 src/train/sampler.py），
    用于抵消 10 个大类占 7555/10165 张的严重不均衡。
    """
    from src.train.sampler import make_weighted_sampler

    data_cfg = cfg.get("data", {})
    train_cfg = cfg.get("train", {})
    eval_cfg = cfg.get("eval", {})

    input_size = int(cfg.get("model", {}).get("input_size", 224))
    workers = int(data_cfg.get("num_workers", 4))
    pin = bool(data_cfg.get("pin_memory", True))
    persist = bool(data_cfg.get("persistent_workers", True))

    train_ds = AgriImageDataset.from_csv(yaml_to_abs(data_cfg["train_csv"]), train_transform,
                                         input_size=input_size)
    val_ds = AgriImageDataset.from_csv(yaml_to_abs(data_cfg["val_csv"]), eval_transform,
                                       input_size=input_size)
    test_ds = AgriImageDataset.from_csv(yaml_to_abs(data_cfg["test_csv"]), eval_transform,
                                        input_size=input_size)

    sampler = None
    if data_cfg.get("weighted_sampler", True):
        counts = train_ds.class_counts(num_classes)
        sampler = make_weighted_sampler(train_ds.labels, num_classes)
        empty = [i for i, c in enumerate(counts) if c == 0]
        if empty:
            logger.warning("训练集中有 %d 个类别没有样本：%s", len(empty), empty[:20])

    return {
        "train": make_loader(
            train_ds,
            batch_size=int(train_cfg.get("batch_size", 32)),
            sampler=sampler,
            num_workers=workers, pin_memory=pin, persistent_workers=persist, drop_last=True,
        ),
        "val": make_loader(
            val_ds,
            batch_size=int(eval_cfg.get("batch_size", 64)),
            num_workers=workers, pin_memory=pin, persistent_workers=persist,
        ),
        "test": make_loader(
            test_ds,
            batch_size=int(eval_cfg.get("batch_size", 64)),
            num_workers=workers, pin_memory=pin, persistent_workers=persist,
        ),
    }
