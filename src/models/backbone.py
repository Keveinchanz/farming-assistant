"""任务 2.2 —— 搭建 ResNet50 / ViT-B/16 模型，加载 ImageNet 预训练权重，替换分类头。

统一入口：
    model = build_model("resnet50", num_classes=271)
    model = build_model("vit_base_patch16_224", num_classes=271)

预训练权重缓存：
    默认下到 C 盘用户目录，本项目改为落到 models/pretrained/ 下（通过 TORCH_HOME / HF_HOME），
    这样权重跟着项目走，重装环境不用重下；离线环境下也能直接命中本地缓存。

分类头替换：
    ResNet50  ->  fc 换成 Dropout + Linear(2048, num_classes)
    ViT-B/16  ->  head 换成 Dropout + Linear(768, num_classes)
    优化器对「新头」和「预训练骨干」用不同学习率（见 trainer.py）。
"""

from __future__ import annotations

import os
from pathlib import Path

import torch
from torch import nn

from src.common.constants import SUPPORTED_ARCHS
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.backbone")


def configure_weight_cache() -> Path:
    """把预训练权重缓存指到 models/pretrained/，避免污染 C 盘用户目录。"""
    cache = path_of("pretrained_dir")
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TORCH_HOME", str(cache))
    os.environ.setdefault("HF_HOME", str(cache / "hf"))
    # timm 的 ViT 权重走 HuggingFace Hub，国内直连 huggingface.co 会超时，
    # 因此同样默认走镜像；境外可用 .env 的 HF_ENDPOINT 覆盖回官方站。
    from src.common.config import env as _env

    os.environ.setdefault("HF_ENDPOINT", str(_env("HF_ENDPOINT", "https://hf-mirror.com")))
    return cache


def is_vit(arch: str) -> bool:
    return arch.startswith("vit")


def build_model(
    arch: str = "resnet50",
    num_classes: int = 271,
    *,
    pretrained: bool = True,
    dropout: float = 0.3,
    gradient_checkpointing: bool = False,
) -> nn.Module:
    """按名称构建模型并替换分类头。"""
    if arch not in SUPPORTED_ARCHS:
        raise ValueError(f"不支持的主干：{arch}；可选 {SUPPORTED_ARCHS}")

    configure_weight_cache()

    if arch == "resnet50":
        model = _build_resnet50(num_classes, pretrained=pretrained, dropout=dropout)
    elif arch == "vit_base_patch16_224":
        model = _build_vit(num_classes, pretrained=pretrained, dropout=dropout)
        if gradient_checkpointing and hasattr(model, "set_grad_checkpointing"):
            model.set_grad_checkpointing(True)
            logger.info("ViT 已开启梯度检查点（用时间换显存，8GB 显存必需）")
    else:  # pragma: no cover - 上面的白名单已拦截
        raise ValueError(arch)

    n_params = sum(p.numel() for p in model.parameters())
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("已构建 %s：类别数 %d，预训练 %s，参数 %.2fM（可训练 %.2fM）",
                arch, num_classes, pretrained, n_params / 1e6, n_train / 1e6)
    return model


def _build_resnet50(num_classes: int, *, pretrained: bool, dropout: float) -> nn.Module:
    from torchvision import models

    weights = models.ResNet50_Weights.IMAGENET1K_V2 if pretrained else None
    model = models.resnet50(weights=weights)
    in_features = model.fc.in_features
    model.fc = nn.Sequential(nn.Dropout(p=dropout), nn.Linear(in_features, num_classes))
    return model


def _build_vit(num_classes: int, *, pretrained: bool, dropout: float) -> nn.Module:
    try:
        import timm
    except ImportError as e:  # pragma: no cover
        raise ImportError("ViT 需要 timm：python -m pip install timm") from e

    model = timm.create_model(
        "vit_base_patch16_224",
        pretrained=pretrained,
        num_classes=num_classes,
        drop_rate=dropout,
    )
    return model


def param_groups(model: nn.Module, *, lr_backbone: float, lr_head: float,
                 weight_decay: float) -> list[dict]:
    """按「预训练骨干 / 新初始化的分类头」分组，给不同学习率。

    分类头是随机初始化的，需要更大的学习率才能快速收敛；
    骨干已经预训练好，用大学习率会把学到的特征打散（灾难性遗忘）。
    """
    head_names = ("fc.", "head.", "classifier.")
    head_params, backbone_params = [], []
    for name, p in model.named_parameters():
        if not p.requires_grad:
            continue
        (head_params if name.startswith(head_names) else backbone_params).append(p)

    logger.info("参数分组：骨干 %d 个张量（lr=%g），分类头 %d 个张量（lr=%g）",
                len(backbone_params), lr_backbone, len(head_params), lr_head)
    return [
        {"params": backbone_params, "lr": lr_backbone, "weight_decay": weight_decay},
        {"params": head_params, "lr": lr_head, "weight_decay": weight_decay},
    ]


def resolve_device(setting: str = "auto") -> torch.device:
    if setting == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(setting)


def describe_device(device: torch.device) -> str:
    if device.type == "cuda":
        idx = device.index or 0
        name = torch.cuda.get_device_name(idx)
        total = torch.cuda.get_device_properties(idx).total_memory / 1024 ** 3
        return f"cuda:{idx} {name}（显存 {total:.1f} GB）"
    return str(device)
