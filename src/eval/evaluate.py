"""任务 2.5 —— 模型评估：测试集准确率、精确率、召回率，绘制混淆矩阵。

产出：
    outputs/reports/eval_<run>.json          完整评估报告（含 macro/weighted、每类、每作物）
    outputs/figures/confusion_matrix_<run>.png  混淆矩阵图

关于 271 类的混淆矩阵：
    直接画 271×271 的原始计数图会因为长尾而全黑；因此默认**按真实类别归一化**（每行和为 1），
    并用 40×40 英寸的画布，坐标刻度按 tick_step 抽样，避免中文标签糊成一团。
    另外报告里单独列出「最容易混淆的类别对」——比看图更快定位问题。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import torch

from src.common.io_utils import ensure_dir, write_json
from src.common.logging_utils import setup_logger, silence_matplotlib_cjk
from src.common.paths import path_of, resolve
from src.data.dataset import AgriImageDataset, make_loader
from src.data.transforms import eval_transform
from src.models.backbone import build_model, resolve_device
from src.train.metrics import accuracy, confusion_matrix, per_class_report, summarize, topk_accuracy

logger = setup_logger("agri.evaluate")


def load_checkpoint(path: Path, device: torch.device):
    ckpt = torch.load(path, map_location="cpu", weights_only=False)
    model = build_model(
        arch=ckpt.get("arch", "resnet50"),
        num_classes=int(ckpt["num_classes"]),
        pretrained=False,
    ).to(device).eval()
    model.load_state_dict(ckpt["model_state"])
    return model, ckpt


@torch.no_grad()
def collect_predictions(model, loader, device: torch.device) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """跑完整个 loader，收集 (logits, y_true, y_pred)。

    数据集在评估时开了 return_meta=True（每条形如 (tensor, label, meta)），
    因此这里按元素个数弹性解包，而不是硬写 `for x, y in loader`。
    """
    logits_all, y_all = [], []
    for batch in loader:
        x, y = batch[0], batch[1]
        x = x.to(device, non_blocking=True)
        with torch.amp.autocast("cuda", enabled=device.type == "cuda"):
            out = model(x)
        logits_all.append(out.float().cpu().numpy())
        y_all.append(y.numpy())
    logits = np.concatenate(logits_all, axis=0)
    y_true = np.concatenate(y_all, axis=0)
    return logits, y_true, logits.argmax(axis=1)


def plot_confusion_matrix(cm: np.ndarray, labels: list[str], out_path: Path, *,
                          normalize: bool = True, figsize=(40, 40), dpi: int = 120,
                          tick_step: int = 1) -> Path:
    import matplotlib.pyplot as plt

    silence_matplotlib_cjk()
    n = cm.shape[0]

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    im = ax.imshow(cm, interpolation="nearest", cmap="Blues",
                   vmin=0, vmax=1 if normalize else None)
    fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)

    ticks = list(range(0, n, max(1, tick_step)))
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    ax.set_xticklabels([labels[i] for i in ticks], rotation=90, fontsize=5)
    ax.set_yticklabels([labels[i] for i in ticks], fontsize=5)

    ax.set_xlabel("预测类别", fontsize=14)
    ax.set_ylabel("真实类别", fontsize=14)
    ax.set_title(f"混淆矩阵（{n} 类，{'按真实类别归一化' if normalize else '原始计数'}）", fontsize=18)

    fig.tight_layout()
    ensure_dir(out_path.parent)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def top_confusions(y_true: np.ndarray, y_pred: np.ndarray, labels: list[str],
                   top_n: int = 20) -> list[dict]:
    """最容易被混淆的类别对（排除预测正确的情况）。"""
    pairs = Counter(
        (int(t), int(p)) for t, p in zip(y_true, y_pred) if int(t) != int(p)
    )
    out = []
    for (t, p), count in pairs.most_common(top_n):
        out.append({
            "true": labels[t], "predicted": labels[p], "count": count,
        })
    return out


def confusing_classes(y_true: np.ndarray, y_pred: np.ndarray, labels: list[str],
                      top_n: int = 20) -> list[dict]:
    """召回率最低的类别 —— 模型最不认识的病害。"""
    per: dict[int, list[int]] = {}
    for t, p in zip(y_true, y_pred):
        rec = per.setdefault(int(t), [0, 0])
        rec[0] += 1
        rec[1] += int(int(t) == int(p))
    rows = [
        {"label_cn": labels[t], "support": tot, "recall": corr / tot}
        for t, (tot, corr) in per.items() if tot > 0
    ]
    rows.sort(key=lambda r: (r["recall"], -r["support"]))
    return rows[:top_n]


def evaluate(
    *,
    run_name: str = "resnet50",
    checkpoint: str | Path | None = None,
    config_name: str = "train_resnet50",
    split: str = "test",
    max_samples: int | None = None,
) -> dict:
    from src.common.config import load_training_config

    cfg = load_training_config(config_name)
    label_map = json.loads(path_of("label_map").read_text(encoding="utf-8"))
    num_classes = int(label_map["num_classes"])
    names = [label_map["index_to_label"][str(i)]["label_cn"] for i in range(num_classes)]

    ckpt_path = resolve(checkpoint) if checkpoint else (
        resolve(cfg["output"].get("checkpoint", f"models/checkpoints/best_{run_name}.pth"))
    )
    device = resolve_device(cfg.get("runtime", {}).get("device", "auto"))
    logger.info("评估 %s：权重 %s，划分 %s，设备 %s", run_name, ckpt_path.name, split, device)

    model, ckpt = load_checkpoint(ckpt_path, device)

    input_size = int(ckpt.get("input_size", cfg["model"].get("input_size", 224)))
    ds = AgriImageDataset.from_csv(
        resolve(cfg["data"][f"{split}_csv"]), eval_transform(input_size),
        input_size=input_size, return_meta=True, limit=max_samples,
    )
    loader = make_loader(
        ds, batch_size=int(cfg.get("eval", {}).get("batch_size", 64)),
        num_workers=int(cfg["data"].get("num_workers", 4)),
        pin_memory=bool(cfg["data"].get("pin_memory", True)),
        persistent_workers=bool(cfg["data"].get("persistent_workers", True)),
    )
    logger.info("测试集规模：%d 条", len(ds))

    logits, y_true, y_pred = collect_predictions(model, loader, device)

    metrics = per_class_report(y_true, y_pred, labels=names, num_classes=num_classes)
    top1 = accuracy(y_true, y_pred)
    top3 = topk_accuracy(logits, y_true, 3)
    top5 = topk_accuracy(logits, y_true, 5)

    cm_cfg = cfg.get("eval", {}).get("confusion_matrix", {}) or {}
    cm = confusion_matrix(y_true, y_pred, num_classes,
                          normalize=bool(cm_cfg.get("normalize", True)))
    fig_path = plot_confusion_matrix(
        cm, names,
        path_of("figures_dir") / f"confusion_matrix_{run_name}.png",
        normalize=bool(cm_cfg.get("normalize", True)),
        figsize=tuple(cm_cfg.get("figsize", [40, 40])),
        dpi=int(cm_cfg.get("dpi", 120)),
        tick_step=int(cm_cfg.get("tick_step", 1)),
    )

    report = {
        "run_name": run_name,
        "checkpoint": str(ckpt_path).replace("\\", "/"),
        "checkpoint_epoch": ckpt.get("epoch"),
        "split": split,
        "num_samples": int(len(y_true)),
        "num_classes": num_classes,
        "overall": {
            "accuracy": top1,
            "top3_accuracy": top3,
            "top5_accuracy": top5,
        },
        "macro": metrics["macro"],
        "weighted": metrics["weighted"],
        "per_class": metrics["per_class"],
        "per_crop": summarize(label_map, y_true, y_pred),
        "top_confusions": top_confusions(y_true, y_pred, names),
        "lowest_recall_classes": confusing_classes(y_true, y_pred, names),
        "confusion_matrix_figure": str(fig_path).replace("\\", "/"),
        "notes": (
            "数据集严重不均衡（10 个大类占 7555/10165 张），务必以 macro 指标为准；"
            "weighted 会被长尾大类拉高。"
        ),
    }
    write_json(path_of("reports_dir") / f"eval_{run_name}.json", report)
    _log(report)
    return report


def _log(r: dict) -> None:
    o = r["overall"]
    logger.info("=" * 62)
    logger.info("整体指标（%d 条样本）：Top-1 %.4f | Top-3 %.4f | Top-5 %.4f",
                r["num_samples"], o["accuracy"], o["top3_accuracy"], o["top5_accuracy"])
    logger.info("macro    P=%.4f R=%.4f F1=%.4f",
                r["macro"]["precision"], r["macro"]["recall"], r["macro"]["f1"])
    logger.info("weighted P=%.4f R=%.4f F1=%.4f",
                r["weighted"]["precision"], r["weighted"]["recall"], r["weighted"]["f1"])

    logger.info("按作物大类的准确率（Top 10）：")
    for crop, v in sorted(r["per_crop"].items(), key=lambda kv: -kv[1]["accuracy"])[:10]:
        logger.info("    %-8s %5.1f%%  (%d 条)", crop, v["accuracy"] * 100, v["total"])

    logger.info("最易混淆的类别对：")
    for c in r["top_confusions"][:8]:
        logger.info("    %s → 误判为 %s（%d 次）", c["true"], c["predicted"], c["count"])

    logger.info("召回率最低的类别（模型最不认识的病害）：")
    for c in r["lowest_recall_classes"][:8]:
        logger.info("    %-16s recall=%.2f  样本 %d", c["label_cn"], c["recall"], c["support"])

    logger.info("报告：outputs/reports/eval_%s.json", r["run_name"])
    logger.info("混淆矩阵：%s", r["confusion_matrix_figure"])
    logger.info("=" * 62)
