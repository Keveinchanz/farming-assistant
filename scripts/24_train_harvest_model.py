#!/usr/bin/env python
"""任务 7.3.4 —— 训练收获日预测的三分位模型（XGBoost）。

**触发条件：全平台 harvest_records 累计 ≥ 30 条**（清单 7.3.4 定死的阈值）。
不到 30 条时本脚本不会硬训 —— 它会说明当前有多少条、还差多少，
然后退出 0（这不是错误，是设计的等待状态）。此时收获预测走纯积温法
（`method=gdd_fallback`），功能完整可用，只是区间更粗。

产出：
    models/farm/harvest_xgb_q10.json
    models/farm/harvest_xgb_q50.json
    models/farm/harvest_xgb_q90.json

## ⚠ 这条本地路径造不出正确的标签（重要）

`predict_harvest` 把模型输出当作**「从观察日到成熟还有几天」**用 ——
它与 `_gdd_interval`（返回的确实是剩余天数）放进同一个列表取 min/max
（见 src/farm/harvest_model.py 的 candidates）。

但本脚本存的标签是**全生育期总天数**：后端存的是
`actual_days = days_between(sow_date, harvest_date)`，而特征是在**标记收获那一天**
算的快照，于是 `features["days_since_sow"] ≈ actual_days` ——
模型只会学成一个恒等映射。真按现在这样训出模型，**线上会把收获日系统性推晚**。

「一季多个观察截止日」才可能学到那张映射表。本地库做不到这一点（它只有收获当天的
单点快照）；做得到的是外部物候数据集那条离线管线：
`scripts/29 → 30 → 31 → 32_train_harvest_external.py`。

因此本脚本加了一道保护：若 `models/farm/provenance.json` 标明产物来自外部数据集，
默认**拒绝覆盖**（除非显式 `--force`）。要彻底修好本地路径得在生长季里周期性快照特征
（新表 / 新记录），那超出当前范围。

**隐私（7.14）**：训练只吃「数值特征 + 作物名」，不带 user_id、田块名、经纬度。
这一点在 src/farm/repo.py 的 list_harvest_features() 里就切干净了 ——
SQL 根本不选那些列。本脚本额外做一次断言，防止将来有人改了那条 SQL 却没人发现。

用法：
    python scripts/24_train_harvest_model.py
    python scripts/24_train_harvest_model.py --min-samples 30
    python scripts/24_train_harvest_model.py --force      # 样本不够也硬训（仅调试用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger  # noqa: E402
from src.farm import db, harvest_model  # noqa: E402

logger = setup_logger("agri.farm.train")

# 训练样本里**不允许**出现的字段（7.14 的隐私要求）
FORBIDDEN_KEYS = ("user_id", "field_id", "name", "province", "city", "lng", "lat", "ip")


def main() -> int:
    parser = argparse.ArgumentParser(description="训练收获日预测模型")
    parser.add_argument("--min-samples", type=int, default=harvest_model.TRAIN_MIN_SAMPLES,
                        help=f"训练触发阈值（默认 {harvest_model.TRAIN_MIN_SAMPLES}）")
    parser.add_argument("--force", action="store_true",
                        help="样本不足也强行训练（仅供调试，产出的模型不可信）")
    args = parser.parse_args()

    # 不覆盖外部数据集训出来的模型。本脚本的标签口径是错的（见头部说明），
    # 拿它去顶掉一条过了闸门的模型，是把好模型换成坏模型 —— 那不是「更新」。
    provenance = harvest_model.models_dir() / "provenance.json"
    if provenance.exists() and not args.force:
        try:
            import json

            meta = json.loads(provenance.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        if meta.get("origin") == "external_phenology":
            logger.warning("models/farm/ 里的模型来自外部物候数据集（%s）。", provenance.name)
            logger.warning("  数据集：%s", meta.get("dataset", "（未记录）"))
            logger.warning("  装于：%s；留出方式：%s", meta.get("installed_at", "?"), meta.get("holdout", "?"))
            logger.warning("本脚本的标签口径是「全生育期天数」，与线上消费的「剩余天数」不一致"
                           "（见脚本头部），拿它覆盖会把好模型换成坏的。")
            logger.warning("确实要覆盖请显式加 --force。")
            return 0

    with db.connect() as conn:
        from src.farm import repo

        total = repo.count_harvests(conn)
        logger.info("全平台收获记录：%d 条（阈值 %d）", total, args.min_samples)

        samples = repo.list_harvest_features(conn)
        for item in samples:
            leaked = [k for k in FORBIDDEN_KEYS if k in item]
            if leaked:
                # 直接失败而不是警告：一旦身份字段进了训练集，模型文件里就带着它们，
                # 而模型文件是要被拷来拷去的。
                logger.error("训练样本里出现了身份字段 %s —— 7.14 禁止，已中止", leaked)
                return 1

        if total < args.min_samples and not args.force:
            logger.info("样本不足，暂不训练。这不是错误：收获预测会自动走纯积温法"
                        "（method=gdd_fallback），功能不受影响。")
            logger.info("还差 %d 条收获记录。在农田页点「我收了」即可积累。",
                        args.min_samples - total)
            return 0

        report = harvest_model.train_from_records(conn, min_samples=1 if args.force else args.min_samples)

    if not report.get("trained"):
        logger.error("训练未完成：%s", report.get("reason"))
        return 1

    logger.info("训练完成：%d 条样本，%d 个特征", report["samples"], len(report["features"]))
    for name, path in report["models"].items():
        logger.info("  %-4s -> %s", name, path)
    logger.info("服务无需重启：下次请求会自动加载新模型（训练后已清缓存）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
