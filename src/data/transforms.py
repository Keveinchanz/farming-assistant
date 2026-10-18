"""任务 1.3 —— 构建数据增强 Pipeline：随机裁剪、旋转、亮度/对比度调整、高斯噪声。

对外暴露一个**可复用的数据增强类** AgriAugment，供训练脚本、评估脚本共用：

    aug = AgriAugment(cfg)                 # cfg 取自 configs/train_*.yaml 的 augment 段
    train_ds = ImageDataset(..., transform=aug.train_transform())
    val_ds   = ImageDataset(..., transform=aug.eval_transform())

两个实现（可通过 backend 参数选择）：
    · backend="torchvision"（默认，只依赖已装的 torchvision，最稳）
    · backend="albumentations"（可选，用 albumentations 2.x 的 Compose）
      依赖缺失或 API 不兼容时自动回退到 torchvision，不会让训练中断。

关键约束：
  · 验证/测试集**不做任何增强**，只做 Resize + CenterCrop + Normalize，
    否则评估指标会被人为扰动，无法反映真实性能。
  · 归一化参数统一取自 src/common/constants.py，与后端推理共用。
  · 高斯噪声实现在张量上（ToTensor 之后），而不是 PIL 上 —— PIL 没有高斯噪声算子。
"""

from __future__ import annotations

from typing import Any, Callable

import torch
from torch import nn

from src.common.constants import (
    DEFAULT_INPUT_SIZE,
    DEFAULT_RESIZE_SIZE,
    IMAGENET_MEAN,
    IMAGENET_STD,
)


class AddGaussianNoise(nn.Module):
    """在归一化前的 [0,1] 张量上加性高斯噪声，并 clamp 回合法范围。

    模拟田间拍摄的传感器噪声 / 低照度噪声，提升模型对实拍图的鲁棒性。
    """

    def __init__(self, sigma: float = 0.03, p: float = 0.3) -> None:
        super().__init__()
        if sigma < 0:
            raise ValueError("sigma 必须非负")
        self.sigma = float(sigma)
        self.p = float(p)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.sigma == 0 or not self.training or torch.rand(1).item() >= self.p:
            return x
        noise = torch.randn_like(x) * self.sigma
        return torch.clamp(x + noise, 0.0, 1.0)

    def extra_repr(self) -> str:
        return f"sigma={self.sigma}, p={self.p}"


class AgriAugment:
    """农业病虫害图像的数据增强工厂。"""

    def __init__(self, cfg: dict[str, Any] | None = None, *, backend: str = "torchvision") -> None:
        cfg = cfg or {}
        self.cfg = cfg
        self.backend = backend

        rrc = cfg.get("random_resized_crop", {}) or {}
        self.input_size = int(rrc.get("size", DEFAULT_INPUT_SIZE))
        self.resize_size = int(cfg.get("resize_size", DEFAULT_RESIZE_SIZE))
        self.scale = tuple(rrc.get("scale", [0.7, 1.0]))
        self.ratio = tuple(rrc.get("ratio", [0.8, 1.25]))
        self.hflip = float(cfg.get("horizontal_flip", 0.5))
        self.rotation = float(cfg.get("rotation_degrees", 15))

        cj = cfg.get("color_jitter", {}) or {}
        self.color_jitter = {
            "brightness": float(cj.get("brightness", 0.25)),
            "contrast": float(cj.get("contrast", 0.25)),
            "saturation": float(cj.get("saturation", 0.25)),
            "hue": float(cj.get("hue", 0.05)),
        }

        gn = cfg.get("gaussian_noise", {}) or {}
        self.noise_enabled = bool(gn.get("enabled", True))
        self.noise_sigma = float(gn.get("sigma", 0.03))
        self.noise_p = float(gn.get("p", 0.3))

    # ------------------------------------------------------------ 训练/评估管线

    def train_transform(self) -> Callable:
        if self.backend == "albumentations":
            fn = self._albumentations_compose(train=True)
            if fn is not None:
                return fn
        return self._torchvision_compose(train=True)

    def eval_transform(self) -> Callable:
        if self.backend == "albumentations":
            fn = self._albumentations_compose(train=False)
            if fn is not None:
                return fn
        return self._torchvision_compose(train=False)

    # ------------------------------------------------------------ torchvision

    def _torchvision_compose(self, *, train: bool) -> Callable:
        from torchvision import transforms as T

        if train:
            ops: list[Callable] = [
                T.RandomResizedCrop(self.input_size, scale=self.scale, ratio=self.ratio),
                T.RandomHorizontalFlip(p=self.hflip),
                T.RandomRotation(degrees=self.rotation, fill=0),
                T.ColorJitter(**self.color_jitter),
                T.ToTensor(),
            ]
            if self.noise_enabled:
                ops.append(AddGaussianNoise(self.noise_sigma, self.noise_p))
            ops.append(T.Normalize(IMAGENET_MEAN, IMAGENET_STD))
            return T.Compose(ops)

        return T.Compose(
            [
                T.Resize(self.resize_size),
                T.CenterCrop(self.input_size),
                T.ToTensor(),
                T.Normalize(IMAGENET_MEAN, IMAGENET_STD),
            ]
        )

    # ------------------------------------------------------------ albumentations（可选）

    def _albumentations_compose(self, *, train: bool) -> Callable | None:
        """albumentations 2.x 版本间 API 变动较多，这里做全量兜底：任何异常都回退 torchvision。"""
        try:
            import albumentations as A
            from albumentations.pytorch import ToTensorV2
        except Exception:  # noqa: BLE001
            return None

        try:
            if train:
                ops = [
                    A.RandomResizedCrop(size=(self.input_size, self.input_size),
                                        scale=self.scale, ratio=self.ratio),
                    A.HorizontalFlip(p=self.hflip),
                    A.Rotate(limit=self.rotation),
                    A.ColorJitter(**self.color_jitter),
                ]
                if self.noise_enabled:
                    # albumentations 的 GaussNoise 作用在 0~255 的 uint8 图上
                    ops.append(A.GaussNoise(std_range=(self.noise_sigma / 4, self.noise_sigma),
                                            p=self.noise_p))
                ops += [A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD), ToTensorV2()]
            else:
                ops = [
                    A.Resize(self.resize_size, self.resize_size),
                    A.CenterCrop(self.input_size, self.input_size),
                    A.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD),
                    ToTensorV2(),
                ]
            return _AlbumentationsAdapter(A.Compose(ops))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ 预览（自检用）

    def describe(self) -> list[str]:
        lines = [
            f"后端: {self.backend}",
            f"输入尺寸: {self.input_size}（评估时先 Resize {self.resize_size} 再 CenterCrop）",
            f"随机裁剪: scale={self.scale} ratio={self.ratio}",
            f"随机水平翻转: p={self.hflip}",
            f"随机旋转: ±{self.rotation}°",
            f"颜色抖动: {self.color_jitter}",
        ]
        if self.noise_enabled:
            lines.append(f"高斯噪声: sigma={self.noise_sigma} p={self.noise_p}")
        else:
            lines.append("高斯噪声: 关闭")
        return lines


class _AlbumentationsAdapter:
    """把 albumentations 的 {'image': ndarray} 接口适配成 PIL -> Tensor 的接口。"""

    def __init__(self, compose: Any) -> None:
        self.compose = compose

    def __call__(self, img):  # PIL.Image -> Tensor
        import numpy as np

        arr = np.asarray(img.convert("RGB"))
        return self.compose(image=arr)["image"]


def eval_transform(input_size: int = DEFAULT_INPUT_SIZE) -> Callable:
    """推理/评估统一预处理：Resize -> CenterCrop -> ToTensor -> Normalize。

    后端推理（src/infer/predictor.py）与测试集评估必须走这一个函数，
    否则预处理不一致会导致"训练指标很好、线上结果离谱"，且不会报任何错。
    """
    return AgriAugment(
        {"random_resized_crop": {"size": int(input_size)},
         "resize_size": max(int(input_size) + 32, int(input_size * 8 / 7))},
        backend="torchvision",
    ).eval_transform()
