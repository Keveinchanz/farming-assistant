#!/usr/bin/env python
"""任务 2.8.6 —— 两阶段的第二阶段：虫体分类器（102 类）。

## 训练什么

输入是**虫体占满画面的小图**，不是整张田景照。两个来源混合：

| 来源 | 规模 | 作用 |
|---|---|---|
| `data/pest_crops/train`（45 号用检测**真框**裁的） | 约 1.6 万 | 域对齐：和线上「检测框裁出来喂进去」的形态一致 |
| `data/pest_cls_raw/.../classification/train`（IP102 分类集） | 约 4.5 万 | 多样性：检测集只有 1.6 万个框，分类集给了 4 倍以上的样本，长尾类靠它 |

分类集里**与检测 val/test 近重复的图已按 42 号感知哈希剔除**（`pest_phash_index.json`
的 `leaked`），否则分类器等于提前见过考题，评估数字不作数。

`crop` 样本用 1.5 倍采样权重：它只有 4.5 万里的 1.6 万，但和线上分布最像，
不加权会被分类集的「白底标本照」淹没 —— 那类图的背景远比田间照片干净。

## 内存

这台机器内存紧张，训练侧做了三件事：`num_workers` 默认 3（不是 8）、
`persistent_workers=False`（每 epoch 结束释放 worker 进程）、图像**全部预先裁好落盘**
（训练时只解码 256×256 的小图，不做大图在线裁剪）。

## 用法

    python scripts/46_train_pest_cls.py --epochs 8
    python scripts/46_train_pest_cls.py --arch resnet34 --epochs 6      # 快速对比
    python scripts/46_train_pest_cls.py --batch 48 --workers 2 --size 224
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.cls")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
NUM_CLASSES = 102


# ---------------------------------------------------------------- 数据

class _FolderByIntClass:
    """`root/split/<类号>/*.jpg` —— 按 int 目录名取标签，不走 ImageFolder（它按字符串排序，
    会把 10 排在 2 前面，类别号全错位）。"""

    def __init__(self, root: Path, split: str, transform, exclude: set[str] | None = None,
                 limit: int = 0):
        self.transform = transform
        self.items: list[tuple[Path, int]] = []
        split_dir = root / split
        if not split_dir.is_dir():
            return
        for d in sorted(split_dir.iterdir()):
            if not d.is_dir() or not d.name.isdigit():
                continue
            c = int(d.name)
            for p in sorted(d.iterdir()):
                if p.suffix.lower() not in IMAGE_EXT:
                    continue
                if exclude and str(p.resolve()) in exclude:
                    continue
                self.items.append((p, c))
        if limit:
            self.items = self.items[:limit]

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, i: int):
        p, c = self.items[i]
        from PIL import Image
        with Image.open(p) as im:
            im = im.convert("RGB")
            return self.transform(im), c


def _leaked_paths() -> set[str]:
    """42 号感知哈希算出的「与检测 val/test 近重复」的分类集图片，训练时必须避开。"""
    f = path_of("outputs_dir") / "reports" / "pest_phash_index.json"
    if not f.exists():
        logger.warning("没找到 %s，跳过泄漏过滤（分类器评估数字需自行打折）", f)
        return set()
    data = json.loads(f.read_text(encoding="utf-8"))
    return {str(Path(e["path"]).resolve()) for e in data.get("leaked", [])}


def _transforms(size: int):
    from torchvision import transforms as T
    mean = (0.485, 0.456, 0.406)
    std = (0.229, 0.224, 0.225)
    train_tf = T.Compose([
        T.RandomResizedCrop(size, scale=(0.55, 1.0), ratio=(0.8, 1.25)),
        T.RandomHorizontalFlip(),
        T.RandomRotation(12),
        T.ColorJitter(brightness=0.25, contrast=0.25, saturation=0.25, hue=0.05),
        T.ToTensor(),
        T.Normalize(mean, std),
        T.RandomErasing(p=0.15, scale=(0.02, 0.08)),
    ])
    eval_tf = T.Compose([
        T.Resize(int(size * 1.14)),
        T.CenterCrop(size),
        T.ToTensor(),
        T.Normalize(mean, std),
    ])
    return train_tf, eval_tf


def _build_model(arch: str, *, pretrained: bool, dropout: float = 0.2):
    import torch.nn as nn
    from torchvision import models

    # 预训练权重统一从仓库内的 models/pretrained 找（离线可用，不去碰用户 HOME 缓存）
    os.environ.setdefault("TORCH_HOME", str(path_of("pretrained_dir")))

    if arch == "resnet50":
        m = models.resnet50(weights=models.ResNet50_Weights.IMAGENET1K_V1 if pretrained else None)
        dim = m.fc.in_features
    elif arch == "resnet34":
        m = models.resnet34(weights=models.ResNet34_Weights.IMAGENET1K_V1 if pretrained else None)
        dim = m.fc.in_features
    elif arch == "resnet18":
        m = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1 if pretrained else None)
        dim = m.fc.in_features
    else:
        raise SystemExit(f"不支持的 arch：{arch}")
    m.fc = nn.Sequential(nn.Dropout(dropout), nn.Linear(dim, NUM_CLASSES))
    return m


# ---------------------------------------------------------------- 评估

def _evaluate(model, loader, device) -> dict:
    import torch
    model.eval()
    correct = total = 0
    top5 = 0
    with torch.no_grad():
        for x, y in loader:
            x, y = x.to(device, non_blocking=True), y.to(device, non_blocking=True)
            with torch.amp.autocast("cuda", enabled=device.type == "cuda"):
                logits = model(x)
            pred = logits.argmax(1)
            correct += (pred == y).sum().item()
            k = min(5, logits.size(1))
            top5 += logits.topk(k, dim=1).indices.eq(y[:, None]).any(1).sum().item()
            total += y.numel()
    return {
        "num": total,
        "top1": round(correct / total, 4) if total else 0.0,
        "top5": round(top5 / total, 4) if total else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="两阶段·虫体分类器训练")
    ap.add_argument("--arch", default="resnet50", choices=["resnet50", "resnet34", "resnet18"])
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch", type=int, default=48)
    ap.add_argument("--size", type=int, default=224)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--weight-decay", type=float, default=0.05)
    ap.add_argument("--workers", type=int, default=1,
                    help="默认 1：多 worker 会各复制一份解释器，这台机器内存吃紧")
    ap.add_argument("--crop-weight", type=float, default=1.5, help="真框裁剪样本的采样权重")
    ap.add_argument("--cls-limit", type=int, default=0, help="分类集只用前 N 张（调试）")
    ap.add_argument("--crop-limit", type=int, default=0, help="裁剪集只用前 N 张（调试）")
    ap.add_argument("--no-pretrained", action="store_true")
    ap.add_argument("--tag", default=None, help="输出名，默认 <arch>_<size>")
    args = ap.parse_args()

    import torch
    from torch.utils.data import ConcatDataset, DataLoader, WeightedRandomSampler

    torch.manual_seed(42)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tag = args.tag or f"{args.arch}_{args.size}"
    logger.info("训练 %s · batch=%d · workers=%d · device=%s",
                tag, args.batch, args.workers, device)

    train_tf, eval_tf = _transforms(args.size)
    leaked = _leaked_paths()
    crop_root = path_of("data_dir") / "pest_crops"
    cls_root = path_of("pest_cls_raw") / "extracted" / "classification"

    d_crop_tr = _FolderByIntClass(crop_root, "train", train_tf, limit=args.crop_limit)
    d_cls_tr = _FolderByIntClass(cls_root, "train", train_tf, exclude=leaked,
                                 limit=args.cls_limit)
    d_crop_va = _FolderByIntClass(crop_root, "val", eval_tf)
    d_cls_va = _FolderByIntClass(cls_root, "val", eval_tf, exclude=leaked)
    if len(d_crop_tr) == 0:
        raise SystemExit("没有裁剪样本，先跑 scripts/45_build_pest_crops.py")

    logger.info("训练集：真框裁剪 %d + 分类集 %d = %d（泄漏剔除 %d 条黑名单）",
                len(d_crop_tr), len(d_cls_tr), len(d_crop_tr) + len(d_cls_tr), len(leaked))
    logger.info("验证集：真框裁剪 %d（主指标）+ 分类集 %d（参考）", len(d_crop_va), len(d_cls_va))

    train_ds = ConcatDataset([d_crop_tr, d_cls_tr])
    weights = [args.crop_weight] * len(d_crop_tr) + [1.0] * len(d_cls_tr)
    n_samples = int(len(d_crop_tr) * args.crop_weight + len(d_cls_tr))
    sampler = WeightedRandomSampler(weights, num_samples=n_samples, replacement=True)
    # persistent_workers=False：每 epoch 收掉 worker，避免内存随轮次爬升
    train_ld = DataLoader(train_ds, batch_size=args.batch, sampler=sampler,
                          num_workers=args.workers, pin_memory=True, drop_last=True,
                          persistent_workers=False)
    crop_va_ld = DataLoader(d_crop_va, batch_size=args.batch, shuffle=False,
                            num_workers=max(1, args.workers - 1), pin_memory=True)
    cls_va_ld = DataLoader(d_cls_va, batch_size=args.batch, shuffle=False,
                           num_workers=max(1, args.workers - 1), pin_memory=True)

    model = _build_model(args.arch, pretrained=not args.no_pretrained).to(device)
    criterion = torch.nn.CrossEntropyLoss(label_smoothing=0.1)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    steps = max(1, len(train_ld))
    warmup = steps * 1  # 头 1 个 epoch 线性升温
    total_steps = steps * args.epochs

    def lr_at(step: int) -> float:
        if step < warmup:
            return args.lr * (step + 1) / warmup
        p = (step - warmup) / max(1, total_steps - warmup)
        return 0.5 * args.lr * (1 + math.cos(math.pi * p))

    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
    best = {"top1": -1.0}
    history = []
    out_dir = ensure_dir(path_of("models_dir") / "pest_cls")
    t0 = time.time()

    for epoch in range(1, args.epochs + 1):
        model.train()
        run_loss = 0.0
        seen = 0
        for i, (x, y) in enumerate(train_ld):
            step = (epoch - 1) * steps + i
            for g in opt.param_groups:
                g["lr"] = lr_at(step)
            x = x.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)
            opt.zero_grad(set_to_none=True)
            with torch.amp.autocast("cuda", enabled=device.type == "cuda"):
                loss = criterion(model(x), y)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            run_loss += loss.item() * y.numel()
            seen += y.numel()
            if (i + 1) % 100 == 0:
                logger.info("  ep%d %d/%d loss=%.4f lr=%.2e", epoch, i + 1, steps,
                            run_loss / max(1, seen), opt.param_groups[0]["lr"])

        crop_va = _evaluate(model, crop_va_ld, device)
        cls_va = _evaluate(model, cls_va_ld, device) if len(d_cls_va) else {"top1": 0.0, "top5": 0.0}
        row = {
            "epoch": epoch,
            "train_loss": round(run_loss / max(1, seen), 4),
            "crop_val_top1": crop_va["top1"],
            "crop_val_top5": crop_va["top5"],
            "cls_val_top1": cls_va["top1"],
            "minutes": round((time.time() - t0) / 60, 1),
        }
        history.append(row)
        logger.info("ep%d 完成 loss=%.4f | 真框裁剪 top1=%.4f top5=%.4f | 分类集 top1=%.4f | 累计 %.1f 分钟",
                    epoch, row["train_loss"], row["crop_val_top1"], row["crop_val_top5"],
                    row["cls_val_top1"], row["minutes"])

        if crop_va["top1"] > best["top1"]:
            best = {**row, "top1": crop_va["top1"], "epoch": epoch}
            ensure_dir(out_dir)
            torch.save({"arch": args.arch, "size": args.size, "num_classes": NUM_CLASSES,
                        "state_dict": model.state_dict(), "meta": row},
                       out_dir / f"{tag}.pt")
            logger.info("  ↑ 新最优，存 %s", out_dir / f"{tag}.pt")

        gc.collect()
        if device.type == "cuda":
            torch.cuda.empty_cache()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "arch": args.arch,
        "size": args.size,
        "epochs": args.epochs,
        "batch": args.batch,
        "lr": args.lr,
        "crop_weight": args.crop_weight,
        "train": {"crop": len(d_crop_tr), "classification": len(d_cls_tr),
                  "leaked_excluded": len(leaked)},
        "val": {"crop": len(d_crop_va), "classification": len(d_cls_va)},
        "best": best,
        "history": history,
        "note": "主指标是真框裁剪 val top1（与线上『检测框裁出来再分类』同域）；分类集 val 仅参考。",
        "checkpoint": str(out_dir / f"{tag}.pt"),
    }
    out_json = path_of("outputs_dir") / "reports" / f"pest_cls_{tag}.json"
    ensure_dir(out_json.parent)
    write_json(out_json, payload)
    logger.info("训练结束：最优 crop-val top1=%.4f（ep%d），报告 %s",
                best["top1"], best.get("epoch", -1), out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
