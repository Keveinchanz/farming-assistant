#!/usr/bin/env python
"""任务 2.7.6 —— 虫害检测对比实验：评估候选并出对比表（**不覆盖交付物**）。

## 为什么不能直接用 18 号

18 号的报告名与图名是写死的（val 时 `suffix=""` → 直接写 `outputs/reports/eval_pest_yolo.json`
与 `outputs/figures/pest_pr_curve.png`）。那两处正是 `scripts/22_verify_artifacts.py:264-281`
校验的**交付物** —— 拿候选模型跑一次 18，交付物就被候选的数字顶掉了，而且门禁还是绿的
（22 只查「文件在不在、字段全不全」，不比对内容）。所以这里只**照抄** 18 的取值函数
（`_instance_counts` / `_metrics_to_dict`），落盘一律换新名字。

## 这份报告要回答什么

1. 「同一份数据、不同训练配置」到底差多少 —— `deltas_vs_baseline`；
2. **长尾是不是主因** —— `binned` 按实例数分箱的 AP。若低实例箱的平均 AP 贴近 0、
   高实例箱明显高，那么「精度低」的主因就是长尾，而不是「模型不行」。

**分箱的类集是 0..101 全部**，不是只有出现在验证集里的那些：一个类在 val 里零实例时
根本不会出现在 `ap_class_index` 里，若只按它遍历，这些「死类」就被悄悄跳过了 ——
而它们恰恰是长尾最严重的那一端。所以这里用标注文件的实例数对齐全部 102 类，
没有 AP 的类记进 `n_unmeasurable`，**不当 0 算进均值**（那是两回事：测不了 ≠ 测得 0）。

## 分辨率要一起评

960 训出来的模型按 640 上线（门禁要求 ONNX 静态 [1,3,640,640]）会打折，只评自己那个
分辨率又会高估它的实际价值。所以 `--imgsz` 接受多个值，**每个模型在每个分辨率下都评一遍** ——
val 只有 1519 张，多评两遍的成本可以忽略，换来的是一张诚实的表。

## 用法

    python scripts/35_eval_pest_experiments.py                      # 自动发现 staging 里所有候选
    python scripts/35_eval_pest_experiments.py --candidates ep100_640_n --imgsz 640 960
    python scripts/35_eval_pest_experiments.py --split test
    python scripts/35_eval_pest_experiments.py --snapshot-deliverables   # 实验前记录交付物指纹
    python scripts/35_eval_pest_experiments.py --verify-deliverables     # 实验后比对，逐条打印「未变」
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.exp.eval")

REPORT_NAME = "pest_experiments.json"
FINGERPRINT_NAME = "pest_deliverables.sha256.json"
EVAL_RUN_ROOT = "pest_exp_eval"

# 按实例数分箱。上界用 10**9 兜住「没有上界」这一档。
BINS: list[tuple[int, int, str]] = [
    (0, 0, "0"),
    (1, 9, "1-9"),
    (10, 49, "10-49"),
    (50, 199, "50-199"),
    (200, 999, "200-999"),
    (1000, 10 ** 9, "1000+"),
]


# ---------------------------------------------------------------- 照 18 抄的取值

def _instance_counts(split: str) -> dict[int, int]:
    """从 YOLO 标注文件里数出每个类别的实例数（class_id -> 实例数）。

    与 18 号同一实现：刻意不读 `metrics.box.nt_per_class`（ultralytics 8.4 里已经没了），
    自己数行反而与 ultralytics 的版本彻底解耦。
    """
    lbl_dir = path_of("pest_dataset") / "labels" / split
    counts: dict[int, int] = {}
    if not lbl_dir.exists():
        return counts
    for f in lbl_dir.glob("*.txt"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                cls = int(float(line.split()[0]))
            except (ValueError, IndexError):
                continue
            counts[cls] = counts.get(cls, 0) + 1
    return counts


def _metrics_to_dict(metrics, label_map: dict, split: str,
                     instances: dict[int, int] | None = None) -> dict:
    """把 ultralytics 的 DetMetrics 拆成朴素的 dict（与 18 号同口径）。"""
    box = metrics.box
    idx2label = label_map["index_to_label"]
    instances = instances or {}

    per_class = []
    # ap_class_index 是**只有出现在该划分里的类别**，不是 0..101 全部。
    for c, ap in zip(box.ap_class_index, box.maps):
        entry = idx2label.get(str(int(c)), {})
        per_class.append({
            "index": int(c),
            "label_cn": entry.get("label_cn", f"<未知类别 {c}>"),
            "label_en": entry.get("label_en", ""),
            "crop": entry.get("crop", ""),
            "pest_type": entry.get("pest_type", ""),
            "ap50_95": round(float(ap), 4),
            "instances": int(instances.get(int(c), 0)),
        })
    per_class.sort(key=lambda x: x["ap50_95"])

    speed = {k: round(float(v), 2) for k, v in (getattr(metrics, "speed", None) or {}).items()}

    return {
        "split": split,
        "num_classes_present": int(len(box.ap_class_index)),
        "map50": round(float(box.map50), 4),
        "map50_95": round(float(box.map), 4),
        "map75": round(float(box.map75), 4),
        "mean_precision": round(float(box.mp), 4),
        "mean_recall": round(float(box.mr), 4),
        "speed_ms": speed,
        "per_class": per_class,
    }


# ---------------------------------------------------------------- 分箱

def _binned(report: dict, label_map: dict, instances: dict[int, int]) -> dict:
    """按实例数分箱的 AP。

    遍历的是 **0..101 全部类**（`idx2label` 的键），不是 `per_class` ——
    零实例类不会出现在 per_class 里，而它们正是长尾最严重的那一端，不能跳过。
    """
    # 类别 -> AP（只含该划分里出现过的）
    ap_of = {c["index"]: c["ap50_95"] for c in report["per_class"]}
    total = len(label_map["index_to_label"])

    out: dict[str, dict] = {}
    for lo, hi, name in BINS:
        cls = [i for i in range(total) if lo <= instances.get(i, 0) <= hi]
        aps = [ap_of[i] for i in cls if i in ap_of]
        out[name] = {
            "n_classes": len(cls),
            # 「没测到」与「测出来是 0」必须分开：前者是连验证样本都没有，
            # 后者是样本有但一个都没检对。混在一起会把后者说得比实际更糟。
            "n_unmeasurable": len([i for i in cls if i not in ap_of]),
            "n_measured": len(aps),
            "n_dead": len([a for a in aps if a == 0.0]),
            "mean_ap50_95": round(statistics.fmean(aps), 4) if aps else None,
            "median_ap50_95": round(statistics.median(aps), 4) if aps else None,
        }
    return out


# ---------------------------------------------------------------- 交付物指纹

def _deliverable_paths() -> list[Path]:
    """整条实验链里必须逐字节不变的文件（与计划第一节的表格一一对应）。"""
    figs = path_of("figures_dir")
    rep = path_of("reports_dir")
    return [
        path_of("checkpoints_dir") / "pest_best.pt",
        path_of("checkpoints_dir") / "pest_last.pt",
        path_of("model_registry"),
        path_of("logs_dir") / "train_pest_yolo.csv",
        rep / "eval_pest_yolo.json",
        rep / "eval_pest_yolo_test.json",
        figs / "pest_pr_curve.png",
        figs / "pest_confusion_matrix.png",
        figs / "pest_pr_curve_test.png",
        figs / "pest_confusion_matrix_test.png",
        path_of("pest_onnx"),
    ]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint() -> dict:
    out = {}
    for p in _deliverable_paths():
        rel = str(p.relative_to(path_of("pest_dataset").anchor))
        out[rel] = {"sha256": _sha256(p), "bytes": p.stat().st_size} if p.exists() else {"missing": True}
    return out


def snapshot_deliverables() -> int:
    """实验前记录交付物指纹。"""
    fp = _fingerprint()
    dest = path_of("reports_dir") / FINGERPRINT_NAME
    ensure_dir(dest.parent)
    write_json(dest, {"taken_at": datetime.now().isoformat(timespec="seconds"), "files": fp})
    logger.info("交付物指纹已记录：%s（%d 个文件）", dest, len(fp))
    for k, v in fp.items():
        logger.info("  %s  %s", "缺失" if v.get("missing") else v["sha256"][:16], k)
    return 0


def verify_deliverables() -> int:
    """实验后比对指纹，逐条打印「未变 / 已变 / 新出现 / 已消失」。"""
    dest = path_of("reports_dir") / FINGERPRINT_NAME
    if not dest.exists():
        logger.error("没有基线指纹，请先跑：python scripts/35_eval_pest_experiments.py --snapshot-deliverables")
        return 1
    old = json.loads(dest.read_text(encoding="utf-8"))["files"]
    new = _fingerprint()

    changed = []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k, {}), new.get(k, {})
        if o.get("missing") and n.get("missing"):
            state = "两边都缺（不是本次造成的）"
        elif o.get("missing"):
            state = "新出现"
            changed.append(k)
        elif n.get("missing"):
            state = "已消失"
            changed.append(k)
        elif o["sha256"] != n["sha256"]:
            state = "**已变**"
            changed.append(k)
        else:
            state = "未变"
        logger.info("  [%s] %s", state, k)

    if changed:
        logger.error("交付物被改动了 %d 处：%s", len(changed), "、".join(changed))
        return 1
    logger.info("全部交付物逐字节未变（指纹取自 %s）", dest)
    return 0


# ---------------------------------------------------------------- 评估

def _discover_candidates() -> list[str]:
    staging = path_of("pest_models_staging")
    if not staging.exists():
        return []
    return sorted(d.name for d in staging.iterdir() if (d / "best.pt").exists())


def _val_once(ckpt: Path, split: str, imgsz: int, batch: int, device: str,
              run_name: str, workers: int = 2) -> tuple[dict, Path]:
    from ultralytics import YOLO

    project = path_of("outputs_dir") / "runs" / EVAL_RUN_ROOT
    model = YOLO(str(ckpt))
    metrics = model.val(
        data=str(path_of("pest_yaml")), split=split, imgsz=imgsz,
        batch=batch, device=device, plots=True, verbose=False,
        # workers 默认调低到 2：这台机器只有 15.4 GB 物理内存，而评估经常要和训练
        # 抢内存。实测用 ultralytics 默认的 8 个 worker 时，一个 worker 在 cv2.imdecode
        # 里连 2.2 MB 都分配不出来（`cv2.OutOfMemoryError`），跟着训练那个进程的
        # DataLoader worker 被别人杀掉、整个训练崩在第 29 轮。评估本身只有 1519/3798 张，
        # 2 个 worker 完全够，慢不了多少。
        workers=workers,
        project=str(project), name=run_name, exist_ok=True,
    )
    return metrics, project / run_name


def _copy_figs(run_dir: Path, tag: str, imgsz: int) -> list[str]:
    """把 PR 曲线 / 混淆矩阵复制成**带 tag 的新名字**。

    源文件名带 `Box` 前缀是 ultralytics 检测分支的叫法（18 号在这上面栽过一次：
    写死 `PR_curve.png`，于是每次都静默走「没生成，跳过」）。两个名字都试。
    """
    figs = ensure_dir(path_of("figures_dir"))
    saved = []
    for src_names, suffix in ((("BoxPR_curve.png", "PR_curve.png"), "pr"),
                              (("confusion_matrix.png",), "cm")):
        src = next((run_dir / n for n in src_names if (run_dir / n).exists()), None)
        if src is None:
            continue
        dst = figs / f"pest_exp_{tag}_{imgsz}_{suffix}.png"
        shutil.copyfile(src, dst)
        saved.append(dst.name)
    return saved


def _fmt(v, nd=4) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def _print_table(models: list[dict], imgsz: int, split: str) -> None:
    logger.info("")
    logger.info("=" * 100)
    logger.info("对比表（%s，imgsz=%d）", split, imgsz)
    logger.info("%-16s %8s %10s %8s %8s %7s %11s %11s", "tag", "mAP50", "mAP50-95", "meanP", "meanR",
                "覆盖类", "箱0-9 AP", "箱1000+ AP")
    logger.info("-" * 100)
    for m in models:
        r = m["runs"].get(str(imgsz))
        if not r:
            continue
        b = r["binned"]
        low = [b["0"]["mean_ap50_95"], b["1-9"]["mean_ap50_95"], b["10-49"]["mean_ap50_95"]]
        low = [v for v in low if v is not None]
        low_ap = round(statistics.fmean(low), 4) if low else None
        logger.info("%-16s %8.4f %10.4f %8.4f %8.4f %7d %11s %11s",
                    m["tag"], r["map50"], r["map50_95"], r["mean_precision"], r["mean_recall"],
                    r["num_classes_present"], _fmt(low_ap), _fmt(b["1000+"]["mean_ap50_95"]))
    logger.info("=" * 100)


def _print_bins(baseline: dict, imgsz: int) -> None:
    r = baseline["runs"].get(str(imgsz))
    if not r:
        return
    logger.info("")
    logger.info("按实例数分箱（基线 %s，imgsz=%d）—— 「精度低是不是长尾造成的」看这张表：",
                baseline["tag"], imgsz)
    logger.info("%-9s %8s %14s %8s %10s %10s", "实例数箱", "类数", "其中测不了的", "测得数", "AP=0 的类", "平均 AP50-95")
    logger.info("-" * 70)
    for _, _, name in BINS:
        b = r["binned"][name]
        logger.info("%-9s %8d %14d %8d %10d %10s", name, b["n_classes"], b["n_unmeasurable"],
                    b["n_measured"], b["n_dead"], _fmt(b["mean_ap50_95"]))
    logger.info("-" * 70)
    logger.info("「测不了的」= 该划分里一个实例都没有 → 根本进不了 AP 统计。它们不是「测出来 0」，")
    logger.info("是「无从测起」；这一类越往左越多，就是长尾的直接证据。")


def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.6 虫害检测对比实验评估（不覆盖交付物）")
    ap.add_argument("--candidates", nargs="*", default=None, help="tag 列表；缺省则自动发现 staging 下的候选")
    ap.add_argument("--baseline", default=None, help="默认 models/checkpoints/pest_best.pt（只读，不写）")
    ap.add_argument("--split", default="val", choices=["val", "test", "train"])
    ap.add_argument("--imgsz", type=int, nargs="+", default=[640], help="可给多个，每个模型在每个分辨率下各评一遍")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="0")
    ap.add_argument("--workers", type=int, default=2,
                    help="评估的 DataLoader worker 数。默认 2（不是 ultralytics 的 8）："
                         "本机 15.4 GB 内存，评估常与训练并行，8 个 worker 会 OOM")
    ap.add_argument("--snapshot-deliverables", action="store_true", help="只记录交付物指纹后退出")
    ap.add_argument("--verify-deliverables", action="store_true", help="只比对交付物指纹后退出")
    args = ap.parse_args()

    if args.snapshot_deliverables:
        return snapshot_deliverables()
    if args.verify_deliverables:
        return verify_deliverables()

    from src.infer.pest_detector import load_pest_label_map

    label_map = load_pest_label_map()
    baseline_ckpt = Path(args.baseline) if args.baseline else (path_of("checkpoints_dir") / "pest_best.pt")
    if not baseline_ckpt.exists():
        logger.error("基线权重不存在：%s", baseline_ckpt)
        return 1

    tags = args.candidates if args.candidates else _discover_candidates()
    staging = path_of("pest_models_staging")
    todo: list[tuple[str, Path]] = [("baseline", baseline_ckpt)]
    for t in tags:
        p = staging / t / "best.pt"
        if not p.exists():
            logger.error("候选权重不存在：%s（先跑 scripts/34_train_pest_experiments.py --tag %s）", p, t)
            return 1
        todo.append((t, p))
    if len(todo) == 1:
        logger.warning("只评了基线 —— staging 里还没有候选（%s）", staging)

    instances = _instance_counts(args.split)
    logger.info("评估 %d 个模型 × %d 个分辨率，split=%s（划分恒为原始 data/pest，候选之间才可比）",
                len(todo), len(args.imgsz), args.split)

    models: list[dict] = []
    for tag, ckpt in todo:
        meta_path = staging / tag / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        entry = {
            "tag": tag,
            "checkpoint": str(ckpt),
            "is_baseline": tag == "baseline",
            "arch": meta.get("arch", ""),
            "train_meta": meta,
            "runs": {},
        }
        for imgsz in args.imgsz:
            run_name = f"{tag}_{imgsz}"
            logger.info("评估 %s @ imgsz=%d …", tag, imgsz)
            metrics, run_dir = _val_once(ckpt, args.split, imgsz, args.batch, args.device,
                                         run_name, args.workers)
            rep = _metrics_to_dict(metrics, label_map, args.split, instances)
            rep["imgsz"] = imgsz
            rep["binned"] = _binned(rep, label_map, instances)
            entry["runs"][str(imgsz)] = rep
            entry.setdefault("figures", []).extend(_copy_figs(run_dir, tag, imgsz))
            logger.info("  mAP50=%.4f  mAP50-95=%.4f  meanP=%.4f  meanR=%.4f",
                        rep["map50"], rep["map50_95"], rep["mean_precision"], rep["mean_recall"])
        models.append(entry)

    baseline = models[0]
    deltas = []
    for m in models[1:]:
        for imgsz in args.imgsz:
            b, c = baseline["runs"].get(str(imgsz)), m["runs"].get(str(imgsz))
            if not b or not c:
                continue
            deltas.append({
                "tag": m["tag"],
                "imgsz": imgsz,
                "delta_map50": round(c["map50"] - b["map50"], 4),
                "delta_map50_95": round(c["map50_95"] - b["map50_95"], 4),
                "delta_mean_precision": round(c["mean_precision"] - b["mean_precision"], 4),
                "delta_mean_recall": round(c["mean_recall"] - b["mean_recall"], 4),
                # 「变好了没有」由人拍板，这里只给事实与一条中性提示
                "improved": bool(c["map50_95"] > b["map50_95"]),
            })

    for imgsz in args.imgsz:
        _print_table(models, imgsz, args.split)
    _print_bins(baseline, args.imgsz[0])

    if deltas:
        logger.info("")
        logger.info("相对基线的变化：")
        for d in deltas:
            logger.info("  %-16s @%-5d ΔmAP50=%+.4f  ΔmAP50-95=%+.4f  ΔP=%+.4f  ΔR=%+.4f  → %s",
                        d["tag"], d["imgsz"], d["delta_map50"], d["delta_map50_95"],
                        d["delta_mean_precision"], d["delta_mean_recall"],
                        "改善" if d["improved"] else "**未改善**")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "split": args.split,
        "imgsz_list": list(args.imgsz),
        "dataset_yaml": str(path_of("pest_yaml")),
        "note": "候选权重来自 models/pest_staging/，基线只读；本文件与 eval_pest_yolo*.json 是两回事，"
                "后者是交付物、只由 scripts/18 写。",
        "models": models,
        "deltas_vs_baseline": deltas,
        # 结论由人填。这里只给一句中性事实，**不写「有所提升」这种模糊话**。
        "conclusion": {
            "filled_by": "human",
            "no_improvement": (bool(deltas) and not any(d["improved"] for d in deltas)) or None,
            "text": "",
        },
    }
    dest = path_of("reports_dir") / REPORT_NAME
    ensure_dir(dest.parent)
    write_json(dest, report)
    logger.info("")
    logger.info("对比报告：%s", dest)
    logger.info("（交付物 eval_pest_yolo*.json 未被触碰；用 --verify-deliverables 可自证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
