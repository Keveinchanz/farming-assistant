"""加权采样器 —— 抵消数据集严重的类间不均衡。

数据集实际情况：10165 张图里 7555 张集中在 10 个类别（每个 500~1122 张），
其余 261 个类别每个只有 10 张。若按原始分布训练，模型只要把样本全预测成那 10 个大类
就能拿到约 74% 的准确率，261 个小类几乎必然全错。

WeightedRandomSampler 让每个类别在每个 epoch 中被采到的期望次数大致相等，
配合 macro 指标一起看，才能反映真实能力。
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

import torch
from torch.utils.data import WeightedRandomSampler

from src.common.logging_utils import setup_logger

logger = setup_logger("agri.sampler")


def class_weights(labels: Sequence[int], num_classes: int) -> torch.Tensor:
    """每个类别的采样权重 ∝ 1/类频次；空类权重置 0（不会被采到）。"""
    counts = Counter(int(y) for y in labels)
    weights = torch.zeros(num_classes, dtype=torch.double)
    for cls in range(num_classes):
        n = counts.get(cls, 0)
        weights[cls] = 1.0 / n if n > 0 else 0.0
    return weights


def make_weighted_sampler(
    labels: Sequence[int],
    num_classes: int,
    *,
    num_samples: int | None = None,
    generator: torch.Generator | None = None,
) -> WeightedRandomSampler:
    """构造 WeightedRandomSampler。默认 num_samples 取训练集大小（每个 epoch 见到的样本数不变）。"""
    w = class_weights(labels, num_classes)
    sample_weights = w[torch.as_tensor([int(y) for y in labels], dtype=torch.long)]

    total_classes = int((w > 0).sum().item())
    effective = int((sample_weights > 0).sum().item())
    logger.info("加权采样：%d 个类别参与采样，训练样本 %d 条，每类期望约 %.1f 次/epoch",
                total_classes, effective, effective / max(total_classes, 1))

    return WeightedRandomSampler(
        weights=sample_weights,
        num_samples=num_samples or len(labels),
        replacement=True,
        generator=generator,
    )
