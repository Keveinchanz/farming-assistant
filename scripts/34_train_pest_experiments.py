#!/usr/bin/env python
"""任务 2.7.6 —— 虫害检测对比实验：训练候选模型（**不碰任何交付物**）。

## 为什么要有这个脚本，而不是给 17 加个开关

17 号是**产出交付物**的那条路：训完 `_copy_outputs()` 会把 best.pt 拷成
`models/checkpoints/pest_best.pt`、把逐轮指标拷成 `outputs/logs/train_pest_yolo.csv`，
`_write_registry()` 还会往 `models/registry.json` 写 `pest_yolov8n` 键。这三处全是
`scripts/22_verify_artifacts.py` 直接校验的交付物（`:249-291`）。

要做「同一份数据、不同训练配置」的对比实验，就不能让候选模型有**任何**覆盖它们的
可能。给 17 加 `--staging` 开关也解决不了问题 —— 只要那条写入路径还在同一个文件里，
参数敲错一次就污染交付物。所以这里**另起一个脚本**：它从物理上不具备那些代码路径，
候选权重一律落 `models/pest_staging/<tag>/`（线上不会加载，见 configs/default.yaml）。

本脚本里需要复用的几个短函数（`_quiet_telemetry` / `_preflight` / `_epochs_run` /
`_epoch_times` / `_train_kwargs`）是**照 17 抄的副本**，不是 import —— 项目本身就有
这个惯例（16 与 29 各自定义自己的 `download()`），宁可重复也不抽公共模块：一旦抽出去，
改 17 就会牵动这里，而这两条路的风险等级完全不同。

## 产物落点

    outputs/runs/pest_exp/<tag>/            训练全过程产物（曲线、逐轮 checkpoint）
    models/pest_staging/<tag>/best.pt       候选权重（供 35 号评估）
    models/pest_staging/<tag>/meta.json     这次训练的全部入参 + 峰值显存 + 最佳 mAP50

**绝不写**：models/checkpoints/、models/registry.json、outputs/logs/train_pest_yolo.csv。

## 用法

    python scripts/34_train_pest_experiments.py --tag ep100_640_n --epochs 100
    python scripts/34_train_pest_experiments.py --tag ep100_960_n --epochs 100 --imgsz 960 --batch 8
    python scripts/34_train_pest_experiments.py --tag probe_960 --imgsz 960 --batch 8 --probe
    python scripts/34_train_pest_experiments.py --tag mix_neg --data data/pest_mixed/data_mixed.yaml

**换 imgsz / batch / 模型尺寸之前先加 `--probe`**：它跑 2 轮 + 5% 数据实测吞吐与显存，
再外推全量耗时。拿等比例缩放去猜显存，在 8 GB 的卡上会直接 OOM。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir  # noqa: E402
from src.common.logging_utils import setup_logger  # noqa: E402
from src.common.paths import path_of  # noqa: E402

logger = setup_logger("agri.pest.exp.train")

# 实验的训练 run 落在这里，与 17 的 outputs/runs/pest_ip102/ 物理隔离
RUN_ROOT_NAME = "pest_exp"


# ---------------------------------------------------------------- 照 17 抄的公共件

def _quiet_telemetry() -> None:
    """关掉 ultralytics 的匿名使用数据上报（与 17 同口径）。"""
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
        logger.warning("CUDA 不可用，将退回 CPU 训练 —— 100 轮会慢到不可接受，请先确认 torch 是 cu 版本")
        return
    name = torch.cuda.get_device_name(0)
    vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
    free_disk = sh.disk_usage(str(path_of("pest_dataset").anchor)).free / 1024 ** 3
    logger.info("GPU：%s（显存 %.1f GB）｜可用磁盘 %.0f GB", name, vram, free_disk)
    if vram < 6:
        logger.warning("显存不足 6 GB，建议把 --batch 降到 8")


def _epochs_run(log_csv: Path | None) -> int:
    """已完成的轮数 = results.csv 的数据行数（表头不算）。"""
    if not log_csv or not log_csv.exists():
        return 0
    import csv

    with log_csv.open("r", encoding="utf-8-sig", newline="") as f:
        return len(list(csv.DictReader(f)))


def _epoch_times(log_csv: Path | None) -> list[float]:
    """从 ultralytics 的 results.csv 读**逐轮**耗时（秒，含该轮验证）。

    ⚠ 这一列是**累计值**，不是单轮值 —— 实测 `outputs/logs/train_pest_yolo.csv`
    的 epoch 1/2/3 是 125.8 / 221.715 / 317.355，差值 95.9 才是稳态轮耗时；
    第 30 行 2925.88 是 30 轮的**总和**。所以要取相邻差分，不能直接把原值当轮耗时。
    （照 17 抄的那个版本直接返回原值，于是 `times[-1]` 是「前两轮之和」，
    外推时会**翻倍** —— 见 `_probe` 里的说明。）

    ⚠⚠ **续跑会把计时器归零。** 实测 `ep100_640_n`：ep28 = 3078.66，
    ep29 = 143.768 —— 全是累计值，却在 resume 处**掉头重来**（新进程从 0 开始计）。
    所以差分里会出现一个**负值**，直接求和会得到「全程 247.9 秒」这种荒谬结论
    （真实是 ~3327 秒）。碰到负数就说明计时器在此处重启了：该轮的耗时取**重启后的
    原值**（含一次性的模型/优化器加载开销，偏大但不会为负）。
    """
    if not log_csv or not log_csv.exists():
        return []
    import csv

    with log_csv.open("r", encoding="utf-8-sig", newline="") as f:
        cum = [float(r["time"]) for r in csv.DictReader(f) if r.get("time")]
    if not cum:
        return []
    out = [cum[0]]
    for a, b in zip(cum, cum[1:]):
        out.append(b - a if b >= a else b)   # b < a ⇒ 续跑把计时器归零了
    return out


def _best_map50(log_csv: Path | None) -> float:
    """逐轮 mAP50 的最大值。列名是 ultralytics 原样的 'metrics/mAP50(B)'。"""
    if not log_csv or not log_csv.exists():
        return 0.0
    import csv

    with log_csv.open("r", encoding="utf-8-sig", newline="") as f:
        return max((float(r.get("metrics/mAP50(B)") or 0) for r in csv.DictReader(f)), default=0.0)


def _train_kwargs(args, run_dir: Path) -> dict:
    """训练参数。与 17 的差别只有两点：data 与 weights 可传（17 那份写死了）。

    另有两个**可选**增广开关（--close-mosaic / --copy-paste）：**不传就不写进 kwargs**，
    于是走 ultralytics 的默认值，行为与加这两个参数之前逐字一致 —— 老实验仍然可复现。
    这也是为什么它们不是在 dict 里写死 None：ultralytics 对 `close_mosaic=None` 的处理
    与「不传」并不等价，写死会悄悄改变已有实验的语义。
    """
    kw = dict(
        data=str(args.data),
        epochs=args.epochs,
        batch=args.batch,
        imgsz=args.imgsz,
        project=str(run_dir.parent),
        name=run_dir.name,
        exist_ok=True,
        seed=args.seed,

        # ---- 与 17（即 PestYOLO 原版）逐字一致的部分 ----
        save=True,
        save_period=1,        # 每轮落一个 checkpoint：100 轮的实验中断了能接着跑

        # ---- 为这台机器加的 ----
        # 15 GB 物理内存装不下 15178 张 640×640 的图像缓存，开 cache 必 OOM。
        cache=False,
        workers=args.workers,
        device=args.device,
        amp=True,             # 混合精度：显存约省一半，8 GB 上基本是必需的
        plots=True,
        verbose=True,
    )
    if getattr(args, "close_mosaic", None) is not None:
        kw["close_mosaic"] = args.close_mosaic
    if getattr(args, "copy_paste", None) is not None:
        kw["copy_paste"] = args.copy_paste
    if getattr(args, "mixup", None) is not None:
        kw["mixup"] = args.mixup
    # single_cls：把 102 类当**一个类**训。这不是「偷懒」——用它训出来的模型
    # 只会回答「这儿有只虫」，而这恰恰是半监督伪标注需要的那个能力：
    # 定位「是不是虫」比分辨「是哪只虫」容易得多。类别由识别集的目录名直接给出，
    # 所以定位器不需要会分类，只需要框得准。
    if getattr(args, "single_cls", None):
        kw["single_cls"] = True
    return kw


# ---------------------------------------------------------------- 路径与安全检查

def _staging_dir(tag: str) -> Path:
    """候选权重的暂存目录，并**当场断言**它不可能碰到交付物。"""
    if not tag or "/" in tag or "\\" in tag or tag in {".", ".."}:
        raise SystemExit(f"--tag 非法：{tag!r}（不能为空，也不能含路径分隔符）")

    staging = path_of("pest_models_staging")
    ckpt_dir = path_of("checkpoints_dir")

    # 这两条断言看着多余，但它们挡的是「以后有人改 configs/default.yaml 把
    # pest_models_staging 指到 models/checkpoints 上」这种改错 —— 那时候 34 号
    # 会安静地开始覆盖交付物，而所有测试都还是绿的。
    if staging == ckpt_dir or ckpt_dir in staging.parents or staging in ckpt_dir.parents:
        raise SystemExit(f"暂存目录与交付物目录重叠，拒绝运行：{staging} vs {ckpt_dir}")

    return staging / tag


def _write_meta(staging: Path, payload: dict) -> None:
    meta = staging / "meta.json"
    meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("实验元数据：%s", meta)


# ---------------------------------------------------------------- 试跑

def _probe(args, run_dir: Path) -> int:
    """2 轮 + 5% 数据，实测吞吐与显存后外推全量耗时（与 17:221-277 同思路）。"""
    import math

    import torch
    from ultralytics import YOLO

    logger.info("试跑：2 轮 + 5%% 数据，实测吞吐后再推算全量耗时")
    probe_dir = path_of("outputs_dir") / "runs" / RUN_ROOT_NAME
    kw = _train_kwargs(args, run_dir)
    # 跑 **2 轮**而不是 1 轮：第 1 轮含只付一次的开销（CUDA kernel 首次编译、
    # dataloader worker 预热、扫描并写 labels/*.cache），拿它外推会把一次性开销
    # 复制 100 遍 —— 17 号的初版正是这么算出「48.1 小时」的。
    #
    # 试跑落 `<tag>_probe` 而不是 `<tag>`：否则它会和正式训练的 results.csv /
    # weights/ 挤在同一个目录里，正式训练一开跑就把试跑的痕迹盖掉。
    #
    # **val=False**：关掉试跑每轮自带的那次验证。开着的话每轮增量 = 训练(5% 数据)
    # + 验证(整份 1519 张)，而验证比 5% 的训练还久 —— 实测第 2 轮总增量 13.2 秒，
    # 比一次独立 val()（15~31 秒）还小，一减就成负数，分解直接失去意义。
    # 关掉之后每轮增量就是纯训练耗时，验证另行单测，两边都是干净的。
    kw.update(epochs=2, fraction=0.05, project=str(probe_dir),
              name=f"{run_dir.name}_probe", exist_ok=True, plots=False,
              save_period=-1, val=False)
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    YOLO(args.weights).train(**kw)
    peak_gb = torch.cuda.max_memory_allocated() / 1024 ** 3 if torch.cuda.is_available() else 0.0

    times = _epoch_times(probe_dir / f"{run_dir.name}_probe" / "results.csv")
    if len(times) < 2:
        logger.warning("试跑没拿到两轮计时，无法推算全量耗时")
        return 0
    t_steady = times[-1]

    # 验证段按整份 val 跑满（1519 张），与 fraction 无关，所以是每轮的固定成本，
    # 不能跟着训练集一起按比例缩放。
    #
    # 这里用 `args.batch` 而不是训练循环内部那个更大的 batch：训练中途验证用的是
    # 2 倍 batch（1519/32=48 个 batch），比这快。所以算出来的验证成本偏**高**，
    # 外推因此偏保守 —— 宁可报多不报少。
    probe_weights = probe_dir / f"{run_dir.name}_probe" / "weights" / "last.pt"
    t0 = time.perf_counter()
    YOLO(str(probe_weights)).val(
        data=str(args.data), imgsz=args.imgsz, batch=args.batch,
        device=args.device, verbose=False, plots=False,
    )
    t_val = time.perf_counter() - t0

    n_train = json.loads(
        (path_of("pest_dataset") / "manifest.json").read_text(encoding="utf-8")
    )["num_images"]["train"]
    n_probe = math.ceil(n_train * 0.05 / args.batch)
    n_full = math.ceil(n_train / args.batch)
    t_train_steady = max(t_steady - t_val, 1e-6)

    logger.info("=" * 62)
    logger.info("试跑实测（%d 张全量数据集，试跑用其中 5%%）", n_train)
    logger.info("  逐轮增量 %s 秒（取最后一轮 %.1f 为稳态）",
                [round(t, 1) for t in times], t_steady)
    logger.info("  稳态轮 %6.1f 秒 = 训练 %6.1f + 验证 %6.1f", t_steady, t_train_steady, t_val)
    if peak_gb:
        logger.info("  显存峰值 %.2f GB", peak_gb)
    est = (t_train_steady / n_probe * n_full + t_val) * args.epochs
    logger.info("  推算全量 %d 轮约需 %.1f 小时（%.0f 秒/轮）", args.epochs, est / 3600, est / args.epochs)
    logger.info("  ⚠ 这是 5%% 数据的短测外推；若本机已跑过同配置的完整训练，"
                "直接用它的 results.csv 末行除以轮数更准。")
    logger.info("=" * 62)
    return 0


# ---------------------------------------------------------------- 正式训练

def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.6 虫害检测对比实验训练（不碰交付物）")
    ap.add_argument("--tag", required=True, help="候选名，如 ep100_640_n（决定落点目录名）")
    ap.add_argument("--weights", default="yolov8n.pt", help="起点权重（yolov8n.pt / yolov8s.pt / 绝对路径）")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--data", default=None, help="data.yaml；默认 data/pest/data.yaml")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--device", default="0", help="0=第一块 GPU，cpu=CPU")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--close-mosaic", type=int, default=None,
                    help="最后 N 轮关掉马赛克增广。**缺省不设置**，即用 ultralytics 的默认值 10 ——"
                         "这样默认行为与加这个参数之前逐字一致，老实验仍可复现。"
                         "要改就显式传，例如 --close-mosaic 20")
    ap.add_argument("--copy-paste", type=float, default=None,
                    help="copy-paste 增广概率。同样**缺省不设置**（走 ultralytics 默认）")
    ap.add_argument("--mixup", type=float, default=None,
                    help="mixup 概率。同样**缺省不设置**（ultralytics 默认 0.0）")
    ap.add_argument("--single-cls", action="store_true", default=None,
                    help="把所有类别当一类训（只学「哪儿有虫」，用于半监督伪标注的定位器）")
    ap.add_argument("--probe", action="store_true", help="2 轮 + 5%% 数据实测吞吐与显存")
    ap.add_argument("--resume", action="store_true",
                    help="从 <run_dir>/weights/last.pt 接着训（中断后续跑；会沿用 checkpoint 里"
                         "存的那套训练参数，命令行新传的 --epochs/--batch/--workers 不生效）")
    ap.add_argument("--force", action="store_true", help="已训过同样的 tag 也重训")
    args = ap.parse_args()

    args.data = Path(args.data) if args.data else path_of("pest_yaml")
    if not Path(args.data).exists():
        logger.error("data.yaml 不存在：%s", args.data)
        return 1
    if not (path_of("pest_dataset") / "manifest.json").exists():
        logger.error("找不到 %s，请先跑 scripts/16_download_ip102.py", path_of("pest_dataset") / "manifest.json")
        return 1

    staging = _staging_dir(args.tag)
    run_dir = path_of("outputs_dir") / "runs" / RUN_ROOT_NAME / args.tag

    _quiet_telemetry()
    _preflight()

    try:
        from ultralytics import YOLO  # noqa: F401
    except ImportError:
        logger.error("未安装 ultralytics。请先运行：python -m pip install \"ultralytics==8.4.*\"")
        return 1

    if args.probe:
        return _probe(args, run_dir)

    # 续跑点：每轮都落 checkpoint（save_period=1），所以中断最多只丢一轮。
    # ultralytics 的 resume 会从 checkpoint 里恢复优化器动量、轮次计数与**增强调度位置**
    # （mosaic 什么时候关就靠它），所以接着训等价于没断过 —— 比从 best.pt 重新 fine-tune
    # 干净得多，后者会把轮次归零、把 mosaic 重新打开。
    resume_ckpt: Path | None = None
    if args.resume:
        resume_ckpt = run_dir / "weights" / "last.pt"
        if not resume_ckpt.exists():
            logger.error("--resume 的续跑点不存在：%s", resume_ckpt)
            logger.error("没有 last.pt 说明这个 tag 还没跑过（或 checkpoint 被删了）—— 请从头训。")
            return 1
    elif (staging / "best.pt").exists() and not args.force:
        logger.warning("已有候选权重 %s，跳过（要重训请加 --force）", staging / "best.pt")
        return 0

    logger.info("=" * 62)
    if resume_ckpt is not None:
        logger.info("**续跑**实验训练：%s（从 %s 接着训）", args.tag, resume_ckpt)
        logger.info("⚠ 续跑沿用 checkpoint 里存的训练参数，命令行新传的 --epochs/--batch/--workers 不生效")
    else:
        logger.info("开始实验训练：%s / %s / imgsz=%d / batch=%d / %d epochs",
                    args.tag, Path(args.weights).stem, args.imgsz, args.batch, args.epochs)
    logger.info("数据集：%s", args.data)
    logger.info("候选权重将落：%s（**不碰 models/checkpoints 与 registry.json**）", staging)
    logger.info("=" * 62)

    import torch

    t0 = time.perf_counter()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    if resume_ckpt is not None:
        YOLO(str(resume_ckpt)).train(resume=True)
    else:
        YOLO(args.weights).train(**_train_kwargs(args, run_dir))
    elapsed = time.perf_counter() - t0
    peak_gb = torch.cuda.max_memory_allocated() / 1024 ** 3 if torch.cuda.is_available() else 0.0

    w = run_dir / "weights"
    if not (w / "best.pt").exists():
        logger.error("训练没产出 best.pt，请检查 %s", w)
        return 1

    ensure_dir(staging)
    shutil.copy2(w / "best.pt", staging / "best.pt")
    if (w / "last.pt").exists():
        shutil.copy2(w / "last.pt", staging / "last.pt")
    if (run_dir / "results.csv").exists():
        shutil.copy2(run_dir / "results.csv", staging / "results.csv")

    log_csv = staging / "results.csv"
    _write_meta(staging, {
        "tag": args.tag,
        "arch": Path(args.weights).stem,
        "weights": str(resume_ckpt) if resume_ckpt is not None else str(args.weights),
        "resumed": resume_ckpt is not None,
        "resumed_from": str(resume_ckpt) if resume_ckpt is not None else None,
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "seed": args.seed,
        "data_yaml": str(args.data),
        "run_dir": str(run_dir),
        "epochs_run": _epochs_run(log_csv),
        "best_mAP50": round(_best_map50(log_csv), 5),
        # elapsed_seconds 只覆盖**这一段**（续跑时就是后半段）；跨段的总时长看这个。
        # ultralytics 的 time 列在 resume 后会接着累计，所以末值就是全程秒数。
        "elapsed_seconds": round(elapsed, 1),
        "total_seconds_from_log": round(sum(_epoch_times(log_csv)), 1),
        "peak_vram_gb": round(peak_gb, 2),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })

    logger.info("=" * 62)
    logger.info("实验训练结束：%s（%d 轮，%.1f 分钟，最佳 mAP50=%.4f）",
                args.tag, _epochs_run(log_csv), elapsed / 60, _best_map50(log_csv))
    logger.info("下一步：python scripts/35_eval_pest_experiments.py --candidates %s", args.tag)
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
