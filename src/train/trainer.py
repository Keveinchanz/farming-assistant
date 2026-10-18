"""任务 2.3 / 2.4 —— 训练策略与训练执行。

策略（对应 configs/train_resnet50.yaml 的 train 段）：
    损失    : CrossEntropyLoss + label_smoothing=0.1
              —— 小样本 + 不均衡，软标签能抑制过自信，是性价比最高的一招
    优化器  : AdamW，weight_decay=0.05，骨干/分类头分组学习率（见 src/models/backbone.py）
    调度    : 线性 warmup（默认 3 epoch）+ 余弦退火
              —— 分类头随机初始化，直接上大学习率会震荡；warmup 让它先稳定下来
    精度    : torch.amp 混合精度 + GradScaler，8GB 显存下速度接近翻倍
    早停    : 监控 val_acc，patience=7
    记录    : 每 epoch 写 CSV；tensorboard 有装就写，没装跳过

产出（任务 2.4 交付物）：
    models/checkpoints/best_*.pth     验证集最优权重
    models/checkpoints/last_*.pth     最后一个 epoch 的权重
    outputs/logs/train_*.csv          逐 epoch 的 loss / 准确率
    models/registry.json              模型元数据（类别数 / 输入尺寸 / 指标）
"""

from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

from src.common.io_utils import ensure_dir
from src.common.logging_utils import setup_logger
from src.common.paths import resolve
from src.models.backbone import param_groups
from src.train.early_stop import EarlyStopping
from src.train.metrics import accuracy

logger = setup_logger("agri.trainer")


def build_scheduler(optimizer, *, epochs: int, warmup_epochs: int, base_lrs: list[float]):
    """线性 warmup + 余弦退火。按每个参数组的基准学习率分别缩放，保证分组 lr 比例不被破坏。"""
    warmup = max(0, int(warmup_epochs))

    def lr_lambda(epoch: int) -> float:
        if warmup and epoch < warmup:
            return float(epoch + 1) / float(warmup)
        progress = (epoch - warmup) / max(1, epochs - warmup)
        return 0.5 * (1.0 + math.cos(math.pi * min(1.0, progress)))

    return torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda), base_lrs


class Trainer:
    """通用的分类训练器（ResNet50 / ViT 共用）。"""

    def __init__(
        self,
        model: nn.Module,
        loaders: dict[str, Any],
        cfg: dict,
        *,
        device: torch.device,
        num_classes: int,
        run_name: str = "resnet50",
    ) -> None:
        self.model = model
        self.loaders = loaders
        self.cfg = cfg
        self.device = device
        self.num_classes = num_classes
        self.run_name = run_name

        t = cfg.get("train", {})
        o = cfg.get("output", {})

        self.epochs = int(t.get("epochs", 40))
        self.amp = bool(t.get("amp", True)) and device.type == "cuda"
        self.grad_clip = float(t.get("grad_clip_norm", 5.0))
        self.label_smoothing = float(t.get("label_smoothing", 0.1))
        self.grad_accum = max(1, int(t.get("grad_accum_steps", 1)))

        self.ckpt_best = resolve(o.get("checkpoint", f"models/checkpoints/best_{run_name}.pth"))
        self.ckpt_last = resolve(o.get("last_checkpoint", f"models/checkpoints/last_{run_name}.pth"))
        self.log_csv = resolve(o.get("log_csv", f"outputs/logs/train_{run_name}.csv"))
        self.registry = resolve(o.get("registry", "models/registry.json"))
        self.tb_dir = resolve(o.get("tensorboard_dir", f"outputs/logs/tensorboard_{run_name}"))

        groups = param_groups(
            model,
            lr_backbone=float(t.get("lr_backbone", 1e-4)),
            lr_head=float(t.get("lr_head", 1e-3)),
            weight_decay=float(t.get("weight_decay", 0.05)),
        )
        base_lrs = [g["lr"] for g in groups]
        self.optimizer = torch.optim.AdamW(groups)
        self.scheduler, self.base_lrs = build_scheduler(
            self.optimizer, epochs=self.epochs,
            warmup_epochs=int(t.get("warmup_epochs", 3)), base_lrs=base_lrs,
        )

        self.criterion = nn.CrossEntropyLoss(label_smoothing=self.label_smoothing)
        self.scaler = torch.amp.GradScaler("cuda", enabled=self.amp)

        es = t.get("early_stop", {}) or {}
        self.early = EarlyStopping(
            patience=int(es.get("patience", 7)),
            mode=str(es.get("mode", "max")),
        ) if es.get("enabled", True) else None
        self.monitor = str(es.get("monitor", "val_acc"))

        self.history: list[dict] = []
        self.best_metric = float("-inf") if self.monitor.endswith("acc") else float("inf")
        self.best_epoch = -1

    # ---------------------------------------------------------------- 训练

    def fit(self) -> dict:
        logger.info("开始训练 %s：%d epoch，batch=%d，AMP=%s，梯度累积=%d",
                    self.run_name, self.epochs,
                    self.cfg["train"].get("batch_size"), self.amp, self.grad_accum)
        if self.early:
            logger.info("早停：监控 %s，patience=%d", self.monitor, self.early.patience)

        writer = self._make_writer()
        t_start = time.time()

        for epoch in range(1, self.epochs + 1):
            lr_now = [round(g["lr"], 6) for g in self.optimizer.param_groups]

            tr_loss, tr_acc, tr_time = self._train_epoch(epoch)
            va_loss, va_acc = self._validate(self.loaders["val"])
            self.scheduler.step()

            record = {
                "epoch": epoch,
                "lr_backbone": lr_now[0],
                "lr_head": lr_now[-1],
                "train_loss": round(tr_loss, 5),
                "train_acc": round(tr_acc, 5),
                "val_loss": round(va_loss, 5),
                "val_acc": round(va_acc, 5),
                "epoch_seconds": round(tr_time, 1),
            }
            self.history.append(record)
            self._append_csv(record)

            if writer:
                writer.add_scalars("loss", {"train": tr_loss, "val": va_loss}, epoch)
                writer.add_scalars("acc", {"train": tr_acc, "val": va_acc}, epoch)
                writer.add_scalar("lr/head", lr_now[-1], epoch)

            logger.info(
                "epoch %3d/%d | train loss %.4f acc %.4f | val loss %.4f acc %.4f | lr %.2e | %.1fs",
                epoch, self.epochs, tr_loss, tr_acc, va_loss, va_acc, lr_now[-1], tr_time,
            )

            metric = va_acc if self.monitor == "val_acc" else va_loss
            higher_is_better = self.monitor.endswith("acc")
            improved = (
                metric > self.best_metric + 1e-4 if higher_is_better
                else metric < self.best_metric - 1e-4
            )
            if improved:
                self.best_metric, self.best_epoch = metric, epoch
                self._save(self.ckpt_best, epoch, record)
                logger.info("    ↳ 验证集最佳已更新（%.4f），权重保存至 %s", metric, self.ckpt_best.name)

            if self.early and self.early.step(metric, epoch):
                self.best_metric = metric

            self._save(self.ckpt_last, epoch, record)

            if self.early and self.early.should_stop:
                logger.info("在第 %d 个 epoch 提前结束训练", epoch)
                break

        elapsed = time.time() - t_start
        if writer:
            writer.close()

        summary = {
            "run_name": self.run_name,
            "epochs_run": len(self.history),
            "epochs_planned": self.epochs,
            "best_epoch": self.best_epoch,
            "best_val_acc": max((h["val_acc"] for h in self.history), default=0.0),
            "final_train_acc": self.history[-1]["train_acc"] if self.history else 0.0,
            "elapsed_seconds": round(elapsed, 1),
            "history": self.history,
        }
        self._write_registry(summary)
        logger.info("训练结束：共 %d 个 epoch，用时 %.1f 分钟，最佳 val_acc=%.4f（epoch %d）",
                    len(self.history), elapsed / 60, summary["best_val_acc"], self.best_epoch)
        return summary

    def _train_epoch(self, epoch: int) -> tuple[float, float, float]:
        self.model.train()
        loader = self.loaders["train"]
        total_loss, total_correct, total_n = 0.0, 0, 0
        t0 = time.time()

        self.optimizer.zero_grad(set_to_none=True)
        for step, (x, y) in enumerate(loader, start=1):
            x = x.to(self.device, non_blocking=True)
            y = y.to(self.device, non_blocking=True)

            with torch.amp.autocast("cuda", enabled=self.amp):
                logits = self.model(x)
                loss = self.criterion(logits, y)
                loss_scaled = loss / self.grad_accum

            self.scaler.scale(loss_scaled).backward()

            if step % self.grad_accum == 0 or step == len(loader):
                if self.grad_clip > 0:
                    self.scaler.unscale_(self.optimizer)
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_clip)
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad(set_to_none=True)

            bs = y.size(0)
            total_loss += loss.item() * bs
            total_correct += (logits.detach().argmax(1) == y).sum().item()
            total_n += bs

            if step % 50 == 0 or step == len(loader):
                logger.info("    [%s] epoch %d step %d/%d  loss %.4f  lr_head %.2e",
                            self.run_name, epoch, step, len(loader),
                            total_loss / max(total_n, 1), self.optimizer.param_groups[-1]["lr"])

        return total_loss / max(total_n, 1), total_correct / max(total_n, 1), time.time() - t0

    @torch.no_grad()
    def _validate(self, loader) -> tuple[float, float]:
        self.model.eval()
        total_loss, total_correct, total_n = 0.0, 0, 0
        for x, y in loader:
            x = x.to(self.device, non_blocking=True)
            y = y.to(self.device, non_blocking=True)
            with torch.amp.autocast("cuda", enabled=self.amp):
                logits = self.model(x)
                loss = self.criterion(logits, y)
            bs = y.size(0)
            total_loss += loss.item() * bs
            total_correct += (logits.argmax(1) == y).sum().item()
            total_n += bs
        return total_loss / max(total_n, 1), total_correct / max(total_n, 1)

    # ---------------------------------------------------------------- 落盘

    def _save(self, path: Path, epoch: int, record: dict) -> None:
        ensure_dir(path.parent)
        torch.save(
            {
                "arch": self.cfg["model"]["arch"],
                "num_classes": self.num_classes,
                "input_size": int(self.cfg["model"].get("input_size", 224)),
                "epoch": epoch,
                "model_state": self.model.state_dict(),
                "optimizer_state": self.optimizer.state_dict(),
                "metrics": record,
            },
            path,
        )

    def _append_csv(self, record: dict) -> None:
        ensure_dir(self.log_csv.parent)
        new_file = not self.log_csv.exists()
        with self.log_csv.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(record))
            if new_file:
                w.writeheader()
            w.writerow(record)

    def _make_writer(self):
        try:
            from torch.utils.tensorboard import SummaryWriter
        except Exception:  # noqa: BLE001
            logger.info("未安装 tensorboard，跳过 TensorBoard 记录（CSV 日志照常写入）")
            return None
        ensure_dir(self.tb_dir)
        logger.info("TensorBoard 日志：%s（查看：tensorboard --logdir %s）", self.tb_dir, self.tb_dir)
        return SummaryWriter(str(self.tb_dir))

    def _write_registry(self, summary: dict) -> None:
        """把模型元数据写进 models/registry.json —— 后端启动时会读它做一致性自检。"""
        ensure_dir(self.registry.parent)
        data = {}
        if self.registry.exists():
            try:
                data = json.loads(self.registry.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("registry.json 解析失败，将重建：%s", self.registry)
        data[self.run_name] = {
            "arch": self.cfg["model"]["arch"],
            "num_classes": self.num_classes,
            "input_size": int(self.cfg["model"].get("input_size", 224)),
            "checkpoint": str(self.ckpt_best).replace("\\", "/"),
            "onnx": data.get(self.run_name, {}).get("onnx"),
            "best_epoch": summary["best_epoch"],
            "best_val_acc": summary["best_val_acc"],
            "epochs_run": summary["epochs_run"],
            "elapsed_seconds": summary["elapsed_seconds"],
        }
        self.registry.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("模型元数据已更新：%s", self.registry)
