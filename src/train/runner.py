"""训练流程编排 —— 把配置、数据、模型、训练器串起来，供各训练脚本调用。

放在 src/ 而不是 scripts/ 里，是为了让训练脚本保持"只解析参数"的薄入口，
同时方便被其他脚本（如 1 epoch 试跑）复用。
"""

from __future__ import annotations

import json
from typing import Any

import torch

from src.common.config import load_training_config
from src.common.logging_utils import setup_logger
from src.common.paths import path_of, resolve
from src.data.dataset import build_dataloaders
from src.data.transforms import AgriAugment
from src.models.backbone import build_model, describe_device, resolve_device
from src.train.trainer import Trainer

logger = setup_logger("agri.runner")


def load_label_map() -> dict:
    import io

    p = path_of("label_map")
    if not p.exists():
        raise FileNotFoundError(f"缺少 {p}，请先运行：python scripts/03_build_label_map.py")
    return json.load(io.open(p, encoding="utf-8"))


def _apply_run_name(cfg: dict, run_name: str) -> None:
    """把配置里 output 段的默认运行名替换成实际的 run_name。

    配置文件里的路径是按主力模型命名的（best_resnet50.pth / train_resnet50.csv）。
    试跑（--probe）或训练 ViT 时若不改写，就会把正式训练的日志和权重覆盖掉。
    """
    token = "resnet50" if "resnet50" in str(cfg.get("model", {}).get("arch", "")) else "vit"
    if run_name == token:
        return
    out = cfg.get("output", {})
    for key, value in list(out.items()):
        if isinstance(value, str) and token in value:
            out[key] = value.replace(token, run_name)
    logger.info("输出路径已按运行名 `%s` 区分（避免覆盖 %s 的正式产物）", run_name, token)


def run_training(
    config_name: str,
    *,
    run_name: str | None = None,
    epochs: int | None = None,
) -> dict:
    """按 configs/<config_name>.yaml 跑一次完整训练，返回训练摘要。"""
    cfg = load_training_config(config_name)
    label_map = load_label_map()
    num_classes = int(label_map["num_classes"])
    cfg["model"]["num_classes"] = num_classes

    run_name = run_name or cfg["model"]["arch"]
    _apply_run_name(cfg, run_name)

    seed = int(cfg.get("runtime", {}).get("seed", 42))
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    device = resolve_device(cfg.get("runtime", {}).get("device", "auto"))
    logger.info("=" * 62)
    logger.info("配置 %s | 运行名 %s | 设备 %s", config_name, run_name, describe_device(device))
    if epochs is not None:
        cfg["train"]["epochs"] = int(epochs)
        logger.info("epoch 已被命令行覆盖为 %d", epochs)
    logger.info("数据集：%d 类 / %d 张，训练 %s",
                num_classes, label_map["num_images"], cfg["data"]["train_csv"])
    logger.info("=" * 62)

    aug = AgriAugment(cfg.get("augment", {}))
    loaders = build_dataloaders(
        cfg, aug.train_transform(), aug.eval_transform(),
        num_classes=num_classes, yaml_to_abs=resolve,
    )

    model = build_model(
        arch=cfg["model"]["arch"],
        num_classes=num_classes,
        pretrained=bool(cfg["model"].get("pretrained", True)),
        dropout=float(cfg["model"].get("dropout", 0.3)),
        gradient_checkpointing=bool(cfg["model"].get("gradient_checkpointing", False)),
    ).to(device)

    summary = Trainer(
        model, loaders, cfg, device=device, num_classes=num_classes, run_name=run_name,
    ).fit()

    logger.info("训练摘要：%s", json.dumps(
        {k: v for k, v in summary.items() if k != "history"}, ensure_ascii=False))
    return summary


def run_epoch_probe(config_name: str, *, run_name: str = "probe", epochs: int = 1) -> dict:
    """只跑极少量 epoch，用于在正式训练前实测吞吐、估算总时长与显存占用。"""
    import time

    torch.cuda.reset_peak_memory_stats() if torch.cuda.is_available() else None
    t0 = time.time()
    summary = run_training(config_name, run_name=run_name, epochs=epochs)
    elapsed = time.time() - t0

    per_epoch = summary["elapsed_seconds"] / max(1, summary["epochs_run"])
    planned = int(load_training_config(config_name)["train"]["epochs"])
    logger.info("=" * 62)
    logger.info("吞吐实测：%.1f 秒/epoch（含验证），按 %d epoch 估算总时长约 %.1f 分钟",
                per_epoch, planned, per_epoch * planned / 60)
    if torch.cuda.is_available():
        logger.info("峰值显存：%.2f GB / %.2f GB",
                    torch.cuda.max_memory_allocated() / 1024 ** 3,
                    torch.cuda.get_device_properties(0).total_memory / 1024 ** 3)
    logger.info("（试跑总耗时 %.1f 秒）", elapsed)
    logger.info("=" * 62)
    return summary
