#!/usr/bin/env python
"""任务 2.7.2 —— 训练 IP102 虫害检测模型（YOLOv8n）。

复现口径照 PestYOLO 的 train.py：yolov8n / imgsz 640 / batch 16 / 30 epochs。
本脚本与它的差别只在「让这台机器跑得完」，逐条都写明了理由（见 _TRAIN_KWARGS）。

产出：
    outputs/runs/pest_ip102/weights/best.pt     训练过程完整产物（含逐轮 checkpoint，可续训）
    models/checkpoints/pest_best.pt             验证集最优权重（供导出 ONNX）
    outputs/logs/train_pest_yolo.csv            逐 epoch 指标
    models/registry.json                        新增 pest_yolov8n 键（resnet50 键不动）

用法（PowerShell）：
    conda activate az
    cd /d "D:\\code file\\insert"

    python scripts/17_train_pest_yolo.py --probe     # 先用 5% 数据跑 2 轮，实测吞吐并推算全量耗时
    python scripts/17_train_pest_yolo.py             # 完整 30 轮（实测约 1 小时）
    python scripts/17_train_pest_yolo.py --resume    # 中断后续训
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir  # noqa: E402
from src.common.logging_utils import setup_logger  # noqa: E402
from src.common.paths import path_of  # noqa: E402

logger = setup_logger("agri.pest.train")

RUN_NAME = "pest_yolov8n"
RUN_DIR_NAME = "pest_ip102"


def _quiet_telemetry() -> None:
    """关掉 ultralytics 的匿名使用数据上报。

    与项目其余部分「运行时不依赖外部服务」的取向一致：这是个离线演示平台，
    没有理由把训练行为同步给第三方。
    """
    try:
        from ultralytics import settings

        settings.update({"sync": False})
        logger.info("已关闭 ultralytics 遥测（settings.sync=False）")
    except Exception as e:  # noqa: BLE001
        logger.warning("关闭遥测失败（不影响训练）：%s", e)


def _preflight() -> None:
    """开跑前把「这台机器撑不撑得住」说清楚，而不是等 OOM 了再猜。"""
    import shutil as sh

    import torch

    if not torch.cuda.is_available():
        logger.warning("CUDA 不可用，将退回 CPU 训练 —— 30 轮会慢到不可接受，请先确认 torch 是 cu 版本")
        return
    name = torch.cuda.get_device_name(0)
    vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
    free_disk = sh.disk_usage(str(path_of("pest_dataset").anchor)).free / 1024 ** 3
    logger.info("GPU：%s（显存 %.1f GB）｜可用磁盘 %.0f GB", name, vram, free_disk)
    if vram < 6:
        logger.warning("显存不足 6 GB，建议把 --batch 降到 8 并加 --accumulate 2")


def _train_kwargs(args, run_dir: Path) -> dict:
    """训练参数。与 PestYOLO train.py 的差别逐条注明原因。"""
    return dict(
        data=str(path_of("pest_yaml")),
        epochs=args.epochs,
        batch=args.batch,
        imgsz=args.imgsz,
        project=str(run_dir.parent),
        name=run_dir.name,
        exist_ok=True,
        seed=args.seed,

        # ---- 与 PestYOLO 原版一致的部分 ----
        save=True,
        save_period=1,        # 每轮落一个 checkpoint：中断了能接着跑，不用从头再来 3 小时

        # ---- 为这台机器加的 ----
        # 15178 张 640×640 全缓存进内存约需 18 GB，而本机物理内存只有 15 GB，
        # 开 cache 必 OOM。这条不是优化，是能不能跑起来的开关。
        cache=False,
        # 默认 8 个 worker 在只剩 1~2 GB 空闲内存的机器上会把内存吃穿，压到 4。
        workers=args.workers,
        device=args.device,
        amp=True,             # 混合精度：显存约省一半，8 GB 上基本是必需的
        plots=True,           # 训练曲线，答辩材料要用
        verbose=True,
    )


def _copy_outputs(run_dir: Path) -> dict:
    """把权重与日志搬到项目既有的约定位置。"""
    w = run_dir / "weights"
    if not (w / "best.pt").exists():
        raise FileNotFoundError(f"训练没产出 best.pt，请检查 {w}")

    ckpt_dir = ensure_dir(path_of("checkpoints_dir"))
    best = ckpt_dir / "pest_best.pt"
    last = ckpt_dir / "pest_last.pt"
    shutil.copy2(w / "best.pt", best)
    if (w / "last.pt").exists():
        shutil.copy2(w / "last.pt", last)

    log_csv = None
    if (run_dir / "results.csv").exists():
        log_dir = ensure_dir(path_of("logs_dir"))
        log_csv = log_dir / "train_pest_yolo.csv"
        shutil.copy2(run_dir / "results.csv", log_csv)

    logger.info("权重：%s（%.2f MB）", best, best.stat().st_size / 1024 / 1024)
    if log_csv:
        logger.info("逐轮指标：%s", log_csv)
    return {"checkpoint": best, "last_checkpoint": last, "log_csv": log_csv}


def _epochs_run(log_csv: Path | None) -> int:
    """已完成的轮数 = results.csv 的数据行数（表头不算，csv 模块自动跳过）。

    这里原来写的是 `len(rows) - 1`，**是个少算一轮的 off-by-one**：30 轮跑完时
    文件是 31 行（表头 + 30 行），DictReader 正好给出 30 行、没有空行幻影
    （实测文件以 CRLF 结尾，不解出全 None 行）。当时多减的那 1 是为了防
    「空文件」，但空文件下 DictReader 本来就给 0 行，用不着减。
    症状是 registry 里 `epochs_run: 29`、控制台打「训练结束：29 轮」，与
    `outputs/logs/train_pest_yolo.csv` 的 30 行自相矛盾。
    """
    if not log_csv or not log_csv.exists():
        return 0
    import csv

    with log_csv.open("r", encoding="utf-8-sig", newline="") as f:
        return len(list(csv.DictReader(f)))


def _epoch_times(log_csv: Path | None) -> list[float]:
    """从 ultralytics 的 results.csv 读逐轮总耗时（秒，含该轮验证）。"""
    if not log_csv or not log_csv.exists():
        return []
    import csv

    with log_csv.open("r", encoding="utf-8-sig", newline="") as f:
        return [float(r["time"]) for r in csv.DictReader(f) if r.get("time")]


def _write_registry(entry_data: dict) -> None:
    """写 models/registry.json 的 pest_yolov8n 键。

    只读-改-写这一个键，resnet50 那条一个字段都不动 —— registry_entry(run_name)
    是按名取的，加新键天然向后兼容。
    """
    reg = path_of("model_registry")
    data = {}
    if reg.exists():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("registry.json 解析失败，将重建：%s", reg)
    data[RUN_NAME] = entry_data
    ensure_dir(reg.parent)
    reg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("模型元数据已更新：%s（键 %s）", reg, RUN_NAME)


def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.2 训练 IP102 虫害检测模型（YOLOv8n）")
    ap.add_argument("--epochs", type=int, default=30, help="训练轮数（PestYOLO 原配置为 30）")
    ap.add_argument("--batch", type=int, default=16, help="批大小（PestYOLO 原配置为 16）")
    ap.add_argument("--imgsz", type=int, default=640, help="输入尺寸（PestYOLO 原配置为 640）")
    ap.add_argument("--workers", type=int, default=4, help="DataLoader 进程数（本机内存吃紧，默认压到 4）")
    ap.add_argument("--device", default="0", help="0=第一块 GPU，cpu=CPU")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--probe", action="store_true", help="跑 2 轮、仅用 5% 数据，实测吞吐与显存并推算全量耗时")
    ap.add_argument("--resume", action="store_true", help="从上次中断处续训")
    ap.add_argument("--force", action="store_true", help="已有训练产物也重新训练")
    args = ap.parse_args()

    _quiet_telemetry()
    _preflight()

    try:
        from ultralytics import YOLO
    except ImportError:
        logger.error("未安装 ultralytics。请先运行：python -m pip install \"ultralytics==8.4.*\"")
        return 1

    run_dir = path_of("outputs_dir") / "runs" / RUN_DIR_NAME
    ckpt = path_of("checkpoints_dir") / "pest_best.pt"

    if ckpt.exists() and not args.force and not args.resume and not args.probe:
        logger.warning("检测到已有权重 %s，跳过训练（要重训请加 --force，要续训请加 --resume）", ckpt)
        return 0

    # ---------------------------------------------------------------- 续训
    if args.resume:
        last = run_dir / "weights" / "last.pt"
        if not last.exists():
            logger.error("找不到可续训的 %s，请先完整跑一次训练", last)
            return 1
        logger.info("从 %s 续训", last)
        YOLO(str(last)).train(resume=True)
        moved = _copy_outputs(run_dir)
        _write_registry({
            "arch": "yolov8n", "task": "detect", "num_classes": 102, "input_size": args.imgsz,
            "checkpoint": str(moved["checkpoint"]), "run_dir": str(run_dir),
            "epochs_run": _epochs_run(moved["log_csv"]), "resumed": True,
        })
        return 0

    # ---------------------------------------------------------------- 试跑
    if args.probe:
        import math
        import time

        import torch

        logger.info("试跑：2 轮 + 5%% 数据，实测吞吐后再推算全量耗时")

        probe_dir = path_of("outputs_dir") / "runs" / "_probe"
        kw = _train_kwargs(args, run_dir)
        # 跑 **2 轮**而不是 1 轮：第 1 轮含只付一次的开销（CUDA kernel 首次编译、
        # dataloader worker 预热、optimizer 构建、扫描并写 labels/*.cache），
        # 拿它去乘 30 会把一次性开销复制 30 遍 —— 本脚本初版正是这么算出
        # 「48.1 小时」的，而实测全量约 1 小时。第 2 轮才是可复现的稳态。
        kw.update(epochs=2, fraction=0.05, project=str(probe_dir),
                  name=RUN_DIR_NAME, exist_ok=True, plots=False, save_period=-1)
        torch.cuda.reset_peak_memory_stats()
        YOLO("yolov8n.pt").train(**kw)
        peak_gb = torch.cuda.max_memory_allocated() / 1024 ** 3 if torch.cuda.is_available() else 0.0

        times = _epoch_times(probe_dir / RUN_DIR_NAME / "results.csv")
        if len(times) < 2:
            logger.warning("试跑没拿到两轮计时（%s），无法推算全量耗时", times)
            return 0
        t_first, t_steady = times[0], times[-1]

        # 验证段耗时单独实测：它按整份 val 跑满（本机 1519 张），与 fraction 无关
        # —— 实测 fraction=0.05 时训练集缩到 683 张，验证仍是 1519 张。
        # 所以它是每轮的固定成本，不能跟着训练集一起按比例缩放。
        probe_weights = probe_dir / RUN_DIR_NAME / "weights" / "last.pt"
        t0 = time.perf_counter()
        YOLO(str(probe_weights)).val(
            data=str(path_of("pest_yaml")), imgsz=args.imgsz, batch=args.batch,
            device=args.device, verbose=False, plots=False,
        )
        t_val = time.perf_counter() - t0

        n_train = json.loads(
            (path_of("pest_dataset") / "manifest.json").read_text(encoding="utf-8")
        )["num_images"]["train"]
        n_probe = math.ceil(n_train * 0.05 / args.batch)
        n_full = math.ceil(n_train / args.batch)
        t_train_steady = max(t_steady - t_val, 1e-6)   # 扣掉验证，剩下的才是训练段
        est_epoch = t_train_steady / n_probe * n_full + t_val
        est_total = est_epoch * args.epochs

        logger.info("=" * 62)
        logger.info("试跑实测（%d 张全量数据集，试跑用其中 5%%）", n_train)
        logger.info("  第 1 轮 %6.1f 秒（含一次性开销，不参与外推）", t_first)
        logger.info("  稳态轮 %6.1f 秒 = 训练 %6.1f + 验证 %6.1f", t_steady, t_train_steady, t_val)
        logger.info("  迭代数 %d（试跑）→ %d（全量），%.1f 倍", n_probe, n_full, n_full / n_probe)
        if peak_gb:
            logger.info("  显存峰值 %.2f GB", peak_gb)
        logger.info("推算全量 %d 轮约需 %.1f 小时（%.0f 秒/轮）",
                    args.epochs, est_total / 3600, est_epoch)
        logger.info("=" * 62)
        return 0

    # ---------------------------------------------------------------- 正式训练
    import time

    logger.info("=" * 62)
    logger.info("开始训练：yolov8n / imgsz=%d / batch=%d / %d epochs / workers=%d",
                args.imgsz, args.batch, args.epochs, args.workers)
    logger.info("数据集：%s", path_of("pest_yaml"))
    logger.info("=" * 62)

    t0 = time.perf_counter()
    model = YOLO("yolov8n.pt")
    model.train(**_train_kwargs(args, run_dir))
    elapsed = time.perf_counter() - t0

    moved = _copy_outputs(run_dir)
    epochs_run = _epochs_run(moved["log_csv"])

    # 最优 mAP 从逐轮指标里取，与分类链路记 best_val_acc 的位置对应
    best_map50 = None
    if moved["log_csv"] and moved["log_csv"].exists():
        import csv

        with moved["log_csv"].open("r", encoding="utf-8-sig", newline="") as f:
            vals = [float(r["metrics/mAP50(B)"]) for r in csv.DictReader(f)
                    if r.get("metrics/mAP50(B)")]
        if vals:
            best_map50 = max(vals)

    _write_registry({
        "arch": "yolov8n", "task": "detect", "num_classes": 102, "input_size": args.imgsz,
        "checkpoint": str(moved["checkpoint"]), "run_dir": str(run_dir),
        "best_mAP50": best_map50, "epochs_run": epochs_run,
        "elapsed_seconds": round(elapsed, 1),
    })

    logger.info("=" * 62)
    logger.info("训练结束：%d 轮，用时 %.1f 分钟（%.1f 小时）", epochs_run, elapsed / 60, elapsed / 3600)
    if best_map50 is not None:
        logger.info("最佳 mAP50 = %.4f", best_map50)
    logger.info("下一步：python scripts/18_evaluate_pest_yolo.py")
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
