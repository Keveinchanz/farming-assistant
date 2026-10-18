"""任务 2.5 的指标计算部分 —— 准确率、精确率、召回率、混淆矩阵。

为什么必须同时看 macro 和 weighted：
    本数据集 7555/10165 张集中在 10 个大类。一个「把一切都预测成那 10 类」的退化模型
    能拿到 ~74% 的 weighted accuracy，看着不错，但 261 个小类全错。
    macro 指标对每个类等权，会立刻暴露这个问题。两者一起看才不会被长尾骗过去。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


def accuracy(y_true: Sequence[int], y_pred: Sequence[int]) -> float:
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    return float((y_true == y_pred).mean()) if y_true.size else 0.0


def topk_accuracy(logits: np.ndarray, y_true: Sequence[int], k: int = 5) -> float:
    """Top-k 准确率。271 类细分类任务里 top-3/top-5 比 top-1 更有参考价值。"""
    logits = np.asarray(logits)
    y_true = np.asarray(y_true)
    if logits.size == 0:
        return 0.0
    k = min(k, logits.shape[1])
    topk = np.argpartition(-logits, k - 1, axis=1)[:, :k]
    hit = (topk == y_true[:, None]).any(axis=1)
    return float(hit.mean())


def per_class_report(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    labels: Sequence[str] | None = None,
    num_classes: int | None = None,
) -> dict:
    """每类的 precision / recall / f1 / support，以及 macro、weighted 汇总。"""
    from sklearn.metrics import classification_report

    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    if num_classes is None:
        num_classes = int(max(y_true.max(), y_pred.max())) + 1
    target_names = list(labels) if labels is not None else [str(i) for i in range(num_classes)]

    report = classification_report(
        y_true, y_pred,
        labels=list(range(num_classes)),
        target_names=target_names,
        output_dict=True,
        zero_division=0,
    )
    macro = report.get("macro avg", {})
    weighted = report.get("weighted avg", {})
    return {
        "macro": {
            "precision": float(macro.get("precision", 0.0)),
            "recall": float(macro.get("recall", 0.0)),
            "f1": float(macro.get("f1-score", 0.0)),
        },
        "weighted": {
            "precision": float(weighted.get("precision", 0.0)),
            "recall": float(weighted.get("recall", 0.0)),
            "f1": float(weighted.get("f1-score", 0.0)),
        },
        "per_class": {
            name: {
                "precision": float(v["precision"]),
                "recall": float(v["recall"]),
                "f1": float(v["f1-score"]),
                "support": int(v["support"]),
            }
            for name, v in report.items()
            if name in target_names
        },
        "accuracy": accuracy(y_true, y_pred),
    }


def confusion_matrix(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    num_classes: int,
    *,
    normalize: bool = True,
) -> np.ndarray:
    """混淆矩阵。normalize=True 时按真实类别归一化（每行和为 1），
    否则长尾大类的绝对数量会把 271×271 的图压成一片漆黑。
    """
    from sklearn.metrics import confusion_matrix as sk_cm

    cm = sk_cm(np.asarray(y_true), np.asarray(y_pred), labels=list(range(num_classes)))
    if normalize:
        with np.errstate(invalid="ignore", divide="ignore"):
            cm = cm.astype(np.float64) / cm.sum(axis=1, keepdims=True)
        cm = np.nan_to_num(cm)
    return cm


def summarize(label_map: dict, y_true, y_pred) -> dict:
    """按作物大类汇总，用于回答"模型在哪些作物上更靠谱"。"""
    idx2label = label_map["index_to_label"]
    by_crop: dict[str, dict[str, int]] = {}
    for t, p in zip(y_true, y_pred):
        crop = idx2label[str(int(t))]["crop"]
        rec = by_crop.setdefault(crop, {"total": 0, "correct": 0})
        rec["total"] += 1
        rec["correct"] += int(int(t) == int(p))
    return {
        crop: {"total": v["total"], "correct": v["correct"],
               "accuracy": v["correct"] / v["total"] if v["total"] else 0.0}
        for crop, v in sorted(by_crop.items())
    }
