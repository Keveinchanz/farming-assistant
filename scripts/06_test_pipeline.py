#!/usr/bin/env python
"""任务 1.3 / 2.1 / 2.2 自检 —— 数据增强 Pipeline、Dataset、模型前向能否跑通。

做的事：
    1. 按 configs/train_resnet50.yaml 的 augment 段构建增强管线（torchvision + albumentations 两种后端）
    2. 从 data/splits/train.csv 取若干样本，抽一个 batch，打印张量形状与统计量
    3. 构建 ResNet50（加载 ImageNet 预训练），前向一个 batch，打印输出形状与显存占用
    4. 把增强前后的图拼成一张对比图存到 outputs/figures/augment_preview.png，肉眼确认增强合理

用法：
    python scripts/06_test_pipeline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402
from PIL import Image  # noqa: E402

from src.common.config import load_training_config  # noqa: E402
from src.common.io_utils import ensure_dir, read_csv  # noqa: E402
from src.common.logging_utils import setup_logger, silence_matplotlib_cjk  # noqa: E402
from src.common.paths import PROJECT_ROOT, path_of  # noqa: E402
from src.data.dataset import AgriImageDataset, make_loader  # noqa: E402
from src.data.transforms import AgriAugment  # noqa: E402
from src.models.backbone import build_model, describe_device, resolve_device  # noqa: E402

logger = setup_logger("agri.check")


def preview_augment(aug: AgriAugment, rows: list[dict], out_path: Path, n: int = 6) -> Path:
    """把原图与增强后的图并排画出来，用于肉眼确认增强没有破坏语义。"""
    import matplotlib.pyplot as plt

    silence_matplotlib_cjk()
    tf = aug.train_transform()

    fig, axes = plt.subplots(2, n, figsize=(2.2 * n, 4.6))
    for i in range(n):
        row = rows[i % len(rows)]
        src = PROJECT_ROOT / row["path"]
        with Image.open(src) as im:
            img = im.convert("RGB")

        axes[0, i].imshow(img)
        axes[0, i].set_title(f"原图\n{row['label_cn'][:8]}", fontsize=8)
        axes[0, i].axis("off")

        t = tf(img).clamp(0, 1).permute(1, 2, 0).numpy()   # 归一化后再 clamp 仅供预览
        axes[1, i].imshow(t)
        axes[1, i].set_title("增强后", fontsize=8)
        axes[1, i].axis("off")

    fig.suptitle("数据增强效果预览（任务 1.3）", fontsize=13)
    fig.tight_layout()
    ensure_dir(out_path.parent)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


def main() -> int:
    cfg = load_training_config("train_resnet50")
    aug_cfg = cfg.get("augment", {})
    input_size = int(cfg["model"]["input_size"])

    logger.info("=" * 62)
    for backend in ("torchvision", "albumentations"):
        aug = AgriAugment(aug_cfg, backend=backend)
        tf = aug.train_transform()
        kind = type(tf).__name__
        logger.info("增强后端 %s -> %s", backend, kind)
        for line in aug.describe():
            logger.info("    %s", line)
    logger.info("=" * 62)

    # ---- Dataset ----
    train_csv = PROJECT_ROOT / cfg["data"]["train_csv"]
    rows = read_csv(train_csv)
    aug = AgriAugment(aug_cfg, backend="torchvision")
    ds = AgriImageDataset(rows[:64], aug.train_transform(), input_size=input_size)
    logger.info("Dataset 就绪：%d 条样本（取自 %s）", len(ds), train_csv.name)

    loader = make_loader(ds, batch_size=8, num_workers=0, shuffle=True)
    x, y = next(iter(loader))
    logger.info("一个 batch：images=%s labels=%s dtype=%s",
                tuple(x.shape), tuple(y.shape), x.dtype)
    logger.info("张量统计：min=%.3f max=%.3f mean=%.3f std=%.3f",
                x.min().item(), x.max().item(), x.mean().item(), x.std().item())
    logger.info("标签示例：%s", y.tolist())

    # ---- 模型前向 ----
    device = resolve_device(cfg.get("runtime", {}).get("device", "auto"))
    logger.info("设备：%s", describe_device(device))

    model = build_model(
        arch=cfg["model"]["arch"],
        num_classes=271,
        pretrained=cfg["model"]["pretrained"],
        dropout=cfg["model"].get("dropout", 0.3),
    ).to(device).eval()

    with torch.no_grad():
        out = model(x.to(device))
    logger.info("前向输出：%s（应为 [8, 271]）", tuple(out.shape))

    if device.type == "cuda":
        logger.info("显存占用：%.2f GB / %.2f GB",
                    torch.cuda.memory_allocated() / 1024 ** 3,
                    torch.cuda.get_device_properties(0).total_memory / 1024 ** 3)

    # ---- 增强预览图 ----
    fig_path = preview_augment(aug, rows, path_of("figures_dir") / "augment_preview.png")
    logger.info("增强预览图：%s", fig_path)

    logger.info("=" * 62)
    logger.info("自检通过：增强 / Dataset / 模型前向 全部正常")
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
