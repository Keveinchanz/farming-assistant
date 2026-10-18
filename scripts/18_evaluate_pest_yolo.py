#!/usr/bin/env python
"""任务 2.7.4 —— 虫害检测模型评估：val / test 上的 mAP 与逐类 AP。

产出：
    outputs/reports/eval_pest_yolo.json      mAP50 / mAP50-95 / 逐类 AP / 速度
    outputs/figures/pest_confusion_matrix.png
    outputs/figures/pest_pr_curve.png

与 scripts/12_evaluate.py 是**并列关系**：那个脚本量的是分类
（accuracy / precision / recall / 混淆矩阵），这个量的是检测（mAP / IoU 曲线）。
指标定义不同，报告结构也不同，所以不硬套同一个 schema。

## 顺带做的对照实验

把 PestYOLO 仓库自带的 `best.pt`（作者在 Colab 上训的）用**同一份 val** 跑一遍，
与自训模型并列成表。这一条有两个作用：

  1. 它是「复现成功与否」的客观证据 —— 自训模型若与官方权重差得离谱，
     说明训练管线有问题（数据、类序、超参）；差得不多就说明复现到位。
  2. 它顺带验证了数据集的**类序**与官方权重互通。若两边类序错位，
     官方权重的 mAP 会塌到接近 0 —— 这是个很强的信号。

## 用法

    python scripts/18_evaluate_pest_yolo.py
    python scripts/18_evaluate_pest_yolo.py --split test        # 只在 test 上评
    python scripts/18_evaluate_pest_yolo.py --skip-baseline     # 跳过官方权重对照
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger       # noqa: E402
from src.common.paths import path_of                     # noqa: E402

logger = setup_logger("agri.pest.eval")

# PestYOLO 仓库自带的权重。**实测（2026-10-07）四条全部拿不到**，列在这里是为了
# 让失败有一次可复现的记录、而不是一句「拉不到」：
#   直连 raw.githubusercontent.com → DNS 解析失败（域名在本机不可达）
#   三个代理（含 main / master 两个分支）→ 一律 404
# 全部失败时脚本不报错，只是跳过对照并在报告里写下原因（见 fetch_baseline）。
# 想补上对照可人工放置 models/pretrained/pestyolo_ip102_best.pt，脚本会命中本地缓存。
BASELINE_URLS = [
    "https://raw.githubusercontent.com/jianxi-Erin/PestYOLO/main/IP102_YOLOv8/best.pt",
    "https://gh-proxy.com/https://raw.githubusercontent.com/jianxi-Erin/PestYOLO/main/IP102_YOLOv8/best.pt",
    "https://gh-proxy.com/raw.githubusercontent.com/jianxi-Erin/PestYOLO/master/IP102_YOLOv8/best.pt",
    "https://ghproxy.net/https://raw.githubusercontent.com/jianxi-Erin/PestYOLO/main/IP102_YOLOv8/best.pt",
]


# ------------------------------------------------------------------ 取值

def _instance_counts(split: str) -> dict[int, int]:
    """从 YOLO 标注文件里数出每个类别的实例数（class_id -> 实例数）。

    刻意**不读** `metrics.box.nt_per_class`：ultralytics 8.4 里这个属性已经没了
    （`metrics.box` 现在是个 `Metric` 实例，`dir()` 里没有它，取一次就是 AttributeError），
    而它自家 `DetMetrics.summary()` 仍在引用 `nt_per_class` / `nt_per_image` / `all_ap`，
    所以「换个官方 API 拿同一份数字」这条退路也是断的。
    索性自己数：标注文件每行是 `class_id cx cy w h`，第一列就是类别，数行即可。
    好处是这份数字与 ultralytics 的版本彻底解耦 —— 换个版本照样能算。
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
    """把 ultralytics 的 DetMetrics 拆成朴素的 dict。

    不直接 json.dumps(metrics) —— 那玩意儿塞满了 numpy 数组和路径对象，
    写出来是一坨读不了的东西，而且换一个 ultralytics 版本字段就变。
    """
    box = metrics.box
    idx2label = label_map["index_to_label"]
    instances = instances or {}

    per_class = []
    # ap_class_index 是**只有出现在验证集里的类别**，不是 0..101 全部。
    # 直接 zip(maps, ap_class_index) 才是对的；用 range(102) 会整体错位。
    for c, ap in zip(box.ap_class_index, box.maps):
        entry = idx2label.get(str(int(c)), {})
        per_class.append({
            "index": int(c),
            "label_cn": entry.get("label_cn", f"<未知类别 {c}>"),
            "label_en": entry.get("label_en", ""),
            "crop": entry.get("crop", ""),
            "pest_type": entry.get("pest_type", ""),
            "ap50_95": round(float(ap), 4),
            # 实例数由 _instance_counts 从标注文件数出来，不是从 ultralytics 取的
            "instances": int(instances.get(int(c), 0)),
        })
    per_class.sort(key=lambda x: x["ap50_95"])

    # 用 getattr 而不是直接点出来：`speed` 是验证器在实例上挂的，不是 DetMetrics 的类属性，
    # 8.4 的 dir() 里根本没有它。哪天真没了也不该让整份评估报告作废。
    speed = {k: round(float(v), 2) for k, v in (getattr(metrics, "speed", None) or {}).items()}

    return {
        "split": split,
        # 名字里不带 split：同一份代码要在 val 和 test 上都跑，
        # 叫 num_classes_in_val 的话，test 报告里就会出现「val 中有 96 类」这种错话。
        "num_classes_present": int(len(box.ap_class_index)),
        "map50": round(float(box.map50), 4),
        "map50_95": round(float(box.map), 4),
        "map75": round(float(box.map75), 4),
        "mean_precision": round(float(box.mp), 4),
        "mean_recall": round(float(box.mr), 4),
        "speed_ms": speed,
        "per_class": per_class,
    }


def _worst_five(report: dict) -> str:
    bad = [c for c in report["per_class"] if c["instances"] > 0][:5]
    if not bad:
        return "（该划分里没有可用类别）"
    return "；".join(f"{c['label_cn']} {c['ap50_95']:.3f}（{c['instances']} 个实例）" for c in bad)


# ------------------------------------------------------------------ 对照权重

def fetch_baseline(dest: Path) -> tuple[Path | None, list[str]]:
    """拉 PestYOLO 自带的 best.pt，返回 `(权重路径或 None, 失败原因列表)`。

    失败**不阻断主流程** —— 对照只是加分项，自训模型的结果该照常出。
    但失败原因要带回给调用方写进报告：`baseline_official: null` 什么都不说，
    看报告的人无从判断是「没跑对照」还是「跑了但拉不到权重」。

    ## 本机的实测结论（2026-10-07）：这条路走不通

    四个源全部失败，而且是**两种不同的失败**，说明不是代理选错了：
      - 直连 raw.githubusercontent.com：DNS 解析失败（getaddrinfo 11004），
        域名在本机不可达；
      - 三个代理（gh-proxy.com 的两种路径写法 + ghproxy.net）：清一色 404，
        连 `main` 和 `master` 两个分支都试了。
    404 指向的是「这个仓库/路径取不到」，而不是「代理不通」。所以官方的
    IP102 权重在本机拿不到，**自训 vs 官方的并列对照做不了**，报告里如实留 null。
    想补上这个对照，需要人工从能访问 GitHub 的机器上取
    `IP102_YOLOv8/best.pt`，放进 models/pretrained/pestyolo_ip102_best.pt，
    再重跑本脚本（第一步就会命中本地缓存）。
    """
    if dest.exists() and dest.stat().st_size > 1_000_000:
        logger.info("官方权重已在本地：%s（%.1f MB）", dest, dest.stat().st_size / 1024 ** 2)
        return dest, []

    import requests

    ensure_dir(dest.parent)
    errors: list[str] = []
    for url in BASELINE_URLS:
        try:
            logger.info("下载官方权重：%s", url)
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            size = dest.stat().st_size
            if size < 1_000_000:
                raise RuntimeError(f"下到的文件只有 {size} 字节，不像权重")
            logger.info("已下载：%.1f MB", size / 1024 ** 2)
            return dest, []
        except Exception as e:  # noqa: BLE001
            msg = f"{url} -> {type(e).__name__}: {e}"
            logger.warning("这条源不可用（%s），换下一条", e)
            errors.append(msg)
            dest.unlink(missing_ok=True)
    logger.warning("官方权重全部源都拉不到，跳过对照（原因已写进评估报告）")
    return None, errors


# ------------------------------------------------------------------ 主流程

def run_val(weights: Path, *, split: str, imgsz: int, batch: int, device: str,
            project: Path, name: str) -> tuple[dict, Path]:
    from ultralytics import YOLO

    model = YOLO(str(weights))
    metrics = model.val(
        data=str(path_of("pest_yaml")), split=split, imgsz=imgsz,
        batch=batch, device=device, plots=True, verbose=False,
        project=str(project), name=name, exist_ok=True,
    )
    return metrics, project / name


def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.4 虫害检测评估")
    ap.add_argument("--checkpoint", default=None, help="默认 models/checkpoints/pest_best.pt")
    ap.add_argument("--split", default="val", choices=["val", "test", "train"])
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="0")
    ap.add_argument("--skip-baseline", action="store_true", help="不跑官方权重的对照")
    args = ap.parse_args()

    from src.infer.pest_detector import load_pest_label_map

    label_map = load_pest_label_map()
    ckpt = Path(args.checkpoint) if args.checkpoint else (path_of("checkpoints_dir") / "pest_best.pt")
    if not ckpt.exists():
        logger.error("权重不存在：%s\n请先训练：python scripts/17_train_pest_yolo.py", ckpt)
        return 1

    reports_dir = path_of("reports_dir")
    figures_dir = path_of("figures_dir")
    run_root = path_of("outputs_dir") / "runs" / "_eval"
    ensure_dir(reports_dir)
    ensure_dir(figures_dir)

    # 文件名带 split 后缀，但 **val 保持无后缀**：
    #   训练日志里的 mAP50 就是 val 口径，两者要能直接对着看；
    #   scripts/22_verify_artifacts.py 读的也是这个无后缀的名字。
    # test 另存一份带后缀的，免得跑完 test 就把 val 的报告覆盖掉。
    suffix = "" if args.split == "val" else f"_{args.split}"

    # ---- 自训模型 ----
    logger.info("评估自训模型：%s（split=%s）", ckpt.name, args.split)
    metrics, run_dir = run_val(ckpt, split=args.split, imgsz=args.imgsz, batch=args.batch,
                               device=args.device, project=run_root, name=f"ours{suffix}")
    report = _metrics_to_dict(metrics, label_map, args.split, _instance_counts(args.split))

    # 曲线图复制到统一位置（ultralytics 把图落在自己的 run 目录里）。
    # 检测任务的曲线文件名带 `Box` 前缀（BoxPR_curve.png），不是分类那套的
    # `PR_curve.png` —— 这里原先写的是后者，于是每次都静默走 "没生成，跳过" 分支，
    # outputs/figures/pest_pr_curve.png 从来没被产出过。两个名字都试一遍，
    # 免得再被某个次版本的改名坑一次。
    for src_names, dst_name in ((("BoxPR_curve.png", "PR_curve.png"), "pest_pr_curve.png"),
                                (("confusion_matrix.png",), "pest_confusion_matrix.png")):
        src = next((run_dir / n for n in src_names if (run_dir / n).exists()), None)
        dst = figures_dir / dst_name.replace(".png", f"{suffix}.png")
        if src is not None:
            shutil.copyfile(src, dst)
            logger.info("曲线已保存：%s", dst)
        else:
            logger.warning("ultralytics 没生成 %s 中的任何一个，跳过", " / ".join(src_names))

    # ---- 官方权重对照 ----
    baseline: dict | None = None
    baseline_note: str | None = None
    if args.skip_baseline:
        baseline_note = "命令行传了 --skip-baseline，本次刻意不跑对照。"
    else:
        weights_dir = path_of("pretrained_dir")
        official, errors = fetch_baseline(weights_dir / "pestyolo_ip102_best.pt")
        if official is None:
            # 不写 `baseline_official: null` 就走人 —— 那个 null 什么信息都不承载。
            # 把「试了哪几个源、各自怎么失败的」原样记下来，看报告的人才能判断
            # 这是「本机网络到不了 GitHub」还是「代码把路径写错了」。
            baseline_note = (
                f"官方权重不可达，未做对照。已试 {len(errors)} 个源，全部失败：" + "；".join(errors)
            )
        if official is not None:
            logger.info("评估官方权重做对照（同一份 %s）…", args.split)
            try:
                bm, _ = run_val(official, split=args.split, imgsz=args.imgsz, batch=args.batch,
                                device=args.device, project=run_root, name=f"official{suffix}")
                baseline = _metrics_to_dict(bm, label_map, args.split, _instance_counts(args.split))
            except Exception as e:  # noqa: BLE001
                # 对照实验失败不该让主结果作废，如实记下原因即可
                logger.warning("官方权重评估失败，对照跳过：%s", e)
                baseline = {"error": str(e)}

    # ---- 落盘 ----
    out = {
        "task": "2.7.4 虫害检测评估",
        "checkpoint": str(ckpt),
        "split": args.split,
        "imgsz": args.imgsz,
        "ours": report,
        "baseline_official": baseline,
    }
    if baseline_note:
        out["baseline_note"] = baseline_note
    if baseline and "map50" in baseline:
        out["comparison"] = {
            "ours_map50": report["map50"],
            "official_map50": baseline["map50"],
            "delta_map50": round(report["map50"] - baseline["map50"], 4),
            "ours_map50_95": report["map50_95"],
            "official_map50_95": baseline["map50_95"],
            "delta_map50_95": round(report["map50_95"] - baseline["map50_95"], 4),
            "note": f"两边用的是同一份 {args.split} 划分、同一套阈值，差异只来自权重本身。"
                    "若官方权重的 mAP 塌到接近 0，多半是类序错位而不是训练不行。",
        }

    out_path = reports_dir / f"eval_pest_yolo{suffix}.json"
    write_json(out_path, out)

    # ---- 控制台小结 ----
    logger.info("=" * 68)
    logger.info("自训模型（%s）：mAP50 = %.4f，mAP50-95 = %.4f，P = %.4f，R = %.4f",
                args.split, report["map50"], report["map50_95"],
                report["mean_precision"], report["mean_recall"])
    logger.info("%s 中出现 %d / %d 个类别", args.split, report["num_classes_present"],
                label_map["num_classes"])
    logger.info("最弱的五类：%s", _worst_five(report))
    if baseline and "map50" in baseline:
        c = out["comparison"]
        logger.info("官方权重对照：mAP50 = %.4f（自训 %+.4f），mAP50-95 = %.4f（自训 %+.4f）",
                    baseline["map50"], c["delta_map50"],
                    baseline["map50_95"], c["delta_map50_95"])
    elif baseline_note:
        logger.warning("未做官方权重对照 —— %s", baseline_note)
    logger.info("报告已保存：%s", out_path)
    logger.info("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
