"""早停机制（任务 2.3 训练策略的一部分）。

271 类、多数类仅 10 张样本，过拟合来得很快：验证集准确率往往在几十个 epoch 后就停滞甚至下降。
早停按 val_acc 监控，连续 patience 个 epoch 没有提升就停止，并回滚到最佳权重。
"""

from __future__ import annotations

from src.common.logging_utils import setup_logger

logger = setup_logger("agri.earlystop")


class EarlyStopping:
    """按监控指标早停。

    参数
        patience : 连续多少个 epoch 没提升就停
        mode     : "max"（如准确率）或 "min"（如损失）
        min_delta: 小于该幅度的变化视为没有提升
    """

    def __init__(self, patience: int = 7, mode: str = "max", min_delta: float = 1e-4) -> None:
        if mode not in {"max", "min"}:
            raise ValueError("mode 只能是 max 或 min")
        self.patience = max(1, int(patience))
        self.mode = mode
        self.min_delta = float(min_delta)

        self.best = float("-inf") if mode == "max" else float("inf")
        self.best_epoch = -1
        self.counter = 0
        self.should_stop = False

    def _improved(self, value: float) -> bool:
        if self.mode == "max":
            return value > self.best + self.min_delta
        return value < self.best - self.min_delta

    def step(self, value: float, epoch: int) -> bool:
        """传入本周期的监控值，返回「本次是否刷新了最佳」。"""
        if self._improved(value):
            self.best = value
            self.best_epoch = epoch
            self.counter = 0
            return True

        self.counter += 1
        if self.counter >= self.patience:
            self.should_stop = True
            logger.info("早停触发：连续 %d 个 epoch 未提升（最佳 %.4f @ epoch %d）",
                        self.patience, self.best, self.best_epoch)
        return False

    def state_dict(self) -> dict:
        return {
            "patience": self.patience, "mode": self.mode, "min_delta": self.min_delta,
            "best": self.best, "best_epoch": self.best_epoch, "counter": self.counter,
            "should_stop": self.should_stop,
        }
