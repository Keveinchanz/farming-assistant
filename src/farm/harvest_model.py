"""阶段 7 / 7.3.4 —— 收获日预测（XGBoost 三分位 + 纯积温法）。

两条路，**同一条降级链**：
    全平台 harvest_records ≥ 30 条且模型已训练 → XGBoost 三分位（method=xgboost）
    否则                                     → 纯积温法（method=gdd_fallback）
清单把这两条写成「第一版 / 第二版」，但代码里它们是并存的：模型是**可选增强**，
没有它功能照样完整可用，只是区间更粗。演示时永远是 gdd_fallback ——
因为一个全新部署的平台上不可能已经攒够 30 条收获记录，这是设计，不是没做完。

区间是**两个来源取并集**再收窄（7.3.4）：
    来源一（有模型才有）：q10 / q50 / q90
    来源二（总有）：按剩余积温 ÷ 日均积温估算，并用 ±1.5×标准差体现快慢
    收窄：按「距收获还有多少天」限制半宽 —— 离得远允许宽，离得近必须窄。
        一条「3 天后收获，误差 ±15 天」的预测是废话，收窄这一步就是防这个。

**点预测取 q50，不取并集中点**（改动见下方 `_combination_note`）。

隐私（7.14）：训练只吃 features_json 里的**数值**，那条在 repo.list_harvest_features()
的 SQL 里就已经切掉了 user_id / 田块名 / 位置。
"""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.farm.stages import crop_config
from src.farm.timeutil import today_str

logger = setup_logger("agri.farm.harvest")

# 7.3.4 的 11 个特征，**顺序即训练时的列顺序**，不要重排
FEATURE_NAMES = (
    "gdd_cum", "gdd_7d", "gdd_remain",
    "tmax_mean_30d", "tmin_mean_30d", "precip_sum_30d", "hot_days_30d", "cold_days_30d",
    "sow_doy", "crop_type", "days_since_sow",
)

# 作物 → 整数编码。训练与推理必须用同一张表，所以只在这里定义一次。
CROP_CODES = {"水稻": 0, "玉米": 1, "油菜": 2}

QUANTILES = (10, 50, 90)          # 7.3.4 的 q10 / q50 / q90
TRAIN_MIN_SAMPLES = 30            # 训练触发阈值（全平台累计）

# 高温日 / 低温日的判定线。写死在这里并同时在训练与推理使用 ——
# 如果两处各写一套，模型学到的特征分布会和线上算出来的对不上，
# 表现为「训练时看着挺好、上线后预测离谱」。
HOT_TMAX = 35.0
COLD_TMIN = 0.0

# 区间宽度控制表（7.3.4）的四个档：距收获天数 → 允许的最大半宽（天）
WIDTH_BUCKETS = (60, 30, 7)          # >60 / 30~60 / 7~30 / <7
WIDTH_LIMITS = (15, 10, 5, 2)

# 跨省保形校准的落盘文件名，与三个分位模型同目录。
# 三个分位模型是各自独立训练的，它们给的区间在**换一个没见过的省**时会偏窄
# （实测留出湖北：名义 80% 的区间只盖住 55%）。校准量 w 由「留一省」的残差定出，
# 用的时候把区间两边各放宽 w 天 —— 这是保形预测的标准做法，不是事后调参：
# w 只用训练省之间互相留出来算，留出省一次都没参与。
CALIBRATION_NAME = "harvest_calibration.json"


def _half_width_limit(days_away: int) -> int:
    """按距收获的天数取允许的最大半宽（7.3.4 的宽度控制表）。

    写成逐档判断而不是「阈值表 + 比较」的循环：清单的四档在边界上是
    「>60」「30~60」「<30」「<7」混着开的，用同一个比较符（`>` 或 `>=`）
    必然有一处落到邻档 —— 实测 30 天会掉进「<30 天」那一档。
    """
    if days_away > WIDTH_BUCKETS[0]:
        return WIDTH_LIMITS[0]
    if days_away >= WIDTH_BUCKETS[1]:
        return WIDTH_LIMITS[1]
    if days_away >= WIDTH_BUCKETS[2]:
        return WIDTH_LIMITS[2]
    return WIDTH_LIMITS[3]


# ---------------------------------------------------------------- 特征

def build_features(field: dict, growth: dict, temps: list[dict], *, today: str | None = None) -> dict:
    """按 7.3.4 构造 11 个特征。

    **训练与推理共用这一个函数**。「收获时存下的特征」与「线上算的特征」
    如果由两段代码生成，两边会悄悄飘开，而模型不会报错，只会给出越来越离谱的日期。
    """
    today = today or today_str()
    window = temps[-30:]
    gdds = [r.get("gdd_daily", 0.0) for r in temps]

    # 缺测的日子**跳过，不补 0**：补 0 会把「没测到」说成「那天 0℃」，
    # hot_days 会少算、tmin_mean 会被拉低，而这些特征是要喂给模型的。
    # v1~v3 期间写入的旧行没有 tmax/tmin/precip（迁移 v4 才加列），也必须容忍。
    highs = [float(r["tmax"]) for r in window if r.get("tmax") is not None]
    lows = [float(r["tmin"]) for r in window if r.get("tmin") is not None]
    precips = [float(r["precip"]) for r in window if r.get("precip") is not None]

    def mean(values, default=0.0):
        return round(statistics.fmean(values), 3) if values else default

    crop_code = CROP_CODES.get(field["crop"])
    if crop_code is None:
        raise ValueError(f"未知作物，无法构造特征：{field['crop']}")

    return {
        "gdd_cum": round(float(growth.get("gdd_cum", 0.0)), 2),
        "gdd_7d": round(sum(gdds[-7:]), 2),
        "gdd_remain": round(float(growth.get("gdd_remain", 0.0)), 2),
        "tmax_mean_30d": mean(highs),
        "tmin_mean_30d": mean(lows),
        "precip_sum_30d": round(sum(precips), 2),
        "hot_days_30d": sum(1 for v in highs if v >= HOT_TMAX),
        "cold_days_30d": sum(1 for v in lows if v <= COLD_TMIN),
        "sow_doy": date.fromisoformat(field["sow_date"]).timetuple().tm_yday,
        "crop_type": crop_code,
        "days_since_sow": int(growth.get("days_since_sow", 0)),
    }


def feature_vector(features: dict) -> list[float]:
    """按 FEATURE_NAMES 的固定顺序取成向量 —— 顺序错了模型不会报错，只会算错。"""
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


# ---------------------------------------------------------------- 模型

def models_dir():
    return path_of("farm_models")


def model_path(q: int):
    return models_dir() / f"harvest_xgb_q{q}.json"


# 三分位模型的超参。**只留这一份** —— scripts/32_train_harvest_external.py 用外部数据集
# 训练时也调 fit_quantile_models()，不复制第二份。两份超参不会报错，
# 只会让「用同一套模型」这句话在某次改动之后悄悄不成立。
XGB_PARAMS = {
    "objective": "reg:quantileerror",
    "n_estimators": 300,
    "learning_rate": 0.05,
    "max_depth": 4,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "random_state": 42,
}


def fit_quantile_models(X, y, out_dir):
    """在 out_dir 下训练并存 q10/q50/q90 三个模型，返回 {分位: 路径}。

    从 train_from_records 里抽出来是为了让外部数据集那条路（scripts/32）
    复用同一份超参与同一套存盘约定。**行为与抽取前逐字一致**。
    """
    from xgboost import XGBRegressor

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = {}
    for q in QUANTILES:
        model = XGBRegressor(quantile_alpha=q / 100.0, **XGB_PARAMS)
        model.fit(X, y)
        target = out_dir / f"harvest_xgb_q{q}.json"
        model.save_model(str(target))
        paths[q] = str(target)
    return paths


def predict_quantiles_at(X, y, X_new) -> dict[int, list[float]]:
    """在 (X, y) 上训三个分位模型，返回它们在 X_new 上的预测。

    只为「留一省校准」提供：那条路要按省反复重训，但**不落盘**、不参与上线。
    超参仍然只取 XGB_PARAMS —— 超参若在两处各写一套，模型不会报错，
    只会悄悄变成另一个模型，这正是本函数存在的理由（不要在图省事时直接 new 一个
    XGBRegressor 盖上自己的参数）。
    """
    from xgboost import XGBRegressor

    out: dict[int, list[float]] = {}
    for q in QUANTILES:
        model = XGBRegressor(quantile_alpha=q / 100.0, **XGB_PARAMS)
        model.fit(X, y)
        out[q] = [float(v) for v in model.predict(X_new)]
    return out


def models_available() -> bool:
    """三个分位模型是否都在盘上（缺一个就当没有 —— 半个模型给不出区间）。"""
    return all(model_path(q).exists() for q in QUANTILES)


@lru_cache(maxsize=1)
def _load_models():
    """加载三个分位模型。任何一步失败都返回 None（调用方退回纯积温法）。

    这里**不抛异常**：模型文件坏了不该让收获预测整个不可用，
    「降级到积温法 + 如实说明」才是正确反应。
    """
    if not models_available():
        return None
    try:
        from xgboost import XGBRegressor
    except Exception as e:  # noqa: BLE001
        logger.warning("未安装 xgboost，收获预测走纯积温法：%s", e)
        return None

    try:
        models = {}
        for q in QUANTILES:
            model = XGBRegressor()
            model.load_model(str(model_path(q)))
            models[q] = model
        return models
    except Exception as e:  # noqa: BLE001
        logger.warning("收获预测模型加载失败，走纯积温法：%s", e)
        return None


def clear_model_cache() -> None:
    _load_models.cache_clear()
    _load_calibration.cache_clear()


@lru_cache(maxsize=1)
def _load_calibration() -> float:
    """读跨省校准量 w（天）。没有文件就是 0.0 —— 区间按原始 q10/q90，不擅自加宽。

    读不到就当 0 而不是报错：校准文件是**附加物**，它坏了不该让收获预测整个不可用，
    只是退回「区间偏窄」这个已知的旧行为。
    """
    path = models_dir() / CALIBRATION_NAME
    if not path.exists():
        return 0.0
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        w = float(blob.get("w_days") or 0.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("跨省校准文件读取失败，按不校准处理：%s", e)
        return 0.0
    return max(w, 0.0)


def train_from_records(conn, *, min_samples: int = TRAIN_MIN_SAMPLES) -> dict:
    """训练三个分位模型（7.3.4）。样本不够就如实返回「没训练」。

    只在 scripts/24_train_harvest_model.py 里调用 —— 服务进程不训练模型。

    ⚠ **这条本地路径造不出正确的标签**（见 scripts/24 的头部说明）：
    收获记录里存的是**收获当天**那一刻的特征快照，而标签 `actual_days` 是全生育期
    总天数，于是 `features["days_since_sow"] ≈ actual_days` —— 模型只会学成一个
    恒等映射，而线上把它的输出当「**剩余**天数」用（与 `_gdd_interval` 同列取 min/max），
    结果是收获日被系统性推晚。真正可用的标签来自「一个生长季里多个观察截止日」，
    在 scripts/32_train_harvest_external.py（外部物候数据集）。这里保留原样只是
    为了不破坏既有行为，并已加「不覆盖外部模型」的保护。
    """
    from src.farm import repo

    total = repo.count_harvests(conn)
    if total < min_samples:
        return {
            "trained": False, "samples": total, "needed": min_samples,
            "reason": f"全平台累计 {total} 条收获记录，不足 {min_samples} 条，暂不训练",
        }

    try:
        import xgboost  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return {"trained": False, "samples": total, "needed": min_samples,
                "reason": f"未安装 xgboost：{e}"}

    samples = repo.list_harvest_features(conn)
    rows = [s for s in samples if s.get("features") and s.get("actual_days")]
    if len(rows) < min_samples:
        return {"trained": False, "samples": len(rows), "needed": min_samples,
                "reason": f"带完整特征的样本只有 {len(rows)} 条，不足以训练"}

    X = [feature_vector(s["features"]) for s in rows]
    y = [float(s["actual_days"]) for s in rows]

    paths = fit_quantile_models(X, y, models_dir())
    report = {
        "trained": True, "samples": len(rows), "features": list(FEATURE_NAMES),
        "models": {f"q{q}": paths[q] for q in QUANTILES},
    }

    # 训练完把缓存清掉，服务进程下次请求就会加载新模型（否则要重启才生效）
    clear_model_cache()
    logger.info("收获预测模型已训练：%d 条样本，特征 %d 个", len(rows), len(FEATURE_NAMES))
    return report


# ---------------------------------------------------------------- 预测

def _gdd_interval(gdd_cum: float, maturity: float, temps: list[dict]) -> tuple[int, int] | None:
    """来源二：纯积温法给出的 (最早, 最晚) 天数。

    用最近 **14 天**的日均有效积温作为"接下来每天大约涨多少"的估计 ——
    取 30 天会把半个月前的盛夏高温一起平均进来，在入秋后明显高估增速
    （种在秋天的水稻会被算出"12 月就能收"）。14 天对降温趋势跟得更紧。
    ±1.5 个标准差体现快慢两种情形。

    7.3.4 里那两行公式写作
        days_high = remaining / (均温 + 1.5×std)
        days_low  = remaining / (均温 - 1.5×std)
    但按含义：**分母更大 → 天数更少 → 更早**。所以「days_high」其实是"最早"，
    这里按含义命名，免得下一个人被变量名带沟里。
    """
    recent = [r.get("gdd_daily", 0.0) for r in temps[-14:]]
    if not recent:
        return None

    mean = statistics.fmean(recent)
    std = statistics.stdev(recent) if len(recent) >= 2 else 0.0
    remaining = max(maturity - gdd_cum, 0.0)

    slow = mean - 1.5 * std
    # 慢到接近 0 或为负（持续低温）时日期会发散到无穷：给个下限，
    # 否则会算出「预计 2000 天后收获」这种数字。下限取日均 0.2 度。
    slow = max(slow, 0.2)
    if mean <= 0:
        return None

    earliest = int(round(remaining / (mean + 1.5 * std)))
    latest = int(round(remaining / slow))
    return max(earliest, 0), max(latest, earliest + 1)


def predict_harvest(
    field: dict, growth: dict, temps: list[dict], *,
    today: str | None = None, management: dict | None = None,
    sample_count: int | None = None,
) -> dict:
    """给出 HarvestData 的形状（不含 field_id/crop 之外的东西由调用方补）。

    已完熟（progress ≥ 1）时不再往前预测，而是标记 matured / overdue ——
    示例田的油菜就属于这种：它已经熟透，还给一个"未来某天收获"的区间就是错的。

    `management` 是 src/farm/management.py 算出来的施肥影响（{days, level, note, ...}），
    缺省 None 表示调用方没算 —— 那就当没有影响，与"未记录施肥"同义。

    `sample_count` 传了就不查数据库：技能层是无状态纯函数路径，不应因为一次
    计算去打开 SQLite。现有 HTTP 调用方不传，行为保持不变。
    """
    today = today or today_str()
    today_d = date.fromisoformat(today)
    crop = field["crop"]
    maturity = float(crop_config(crop)["maturity_gdd"])
    gdd_cum = float(growth.get("gdd_cum", 0.0))

    # 施肥记录的调整量：正 = 推迟、负 = 提前。四舍五入到整天 ——
    # 收获日是个日期，「推后 2.4 天」在日历上没有落点，写出来只会让人以为精度很高。
    shift = int(round(float((management or {}).get("days") or 0.0)))
    mgmt_note = str((management or {}).get("note") or "")

    if sample_count is None:
        sample_count = 0
        try:
            # 只为了在前端显示「XGBoost 已积累 12/30 条记录」，取不到也不影响预测
            from src.farm.db import connect

            with connect() as conn:
                from src.farm import repo
                sample_count = repo.count_harvests(conn)
        except Exception:  # noqa: BLE001
            sample_count = 0

    base = {
        "field_id": field["id"], "crop": crop,
        "sample_count": sample_count, "sample_needed": TRAIN_MIN_SAMPLES,
        "maturity_shift": float((management or {}).get("days") or 0.0),
        "management_note": mgmt_note,
        "management_sources": list((management or {}).get("sources") or []),
    }

    # 已完熟：不再预测未来日期
    if growth.get("matured"):
        # 已经熟了的田没有"日期"可挪，所以施肥只体现在文案里，不去伪造一个偏移过的日期。
        # 这句话以前写作「积温已达成熟阈值，无需再预测收获日 —— 建议尽快收割」，
        # 破折号前后是两个意思、又被连成一句，读着别扭。改成一句话说完。
        basis = "积温已累计到成熟阈值，作物已经完熟，可以安排收割了"
        if shift > 0:
            basis += "（施肥记录偏多，往后这块地的成熟可能略晚于模型推算）"
        return {
            **base,
            "earliest": None, "most_likely": None, "latest": None, "window_days": 0,
            "method": "xgboost" if _load_models() else "gdd_fallback",
            "matured": True, "overdue": True,
            "basis": basis,
        }

    remaining = max(maturity - gdd_cum, 0.0)

    gdd_iv = _gdd_interval(gdd_cum, maturity, temps)
    if gdd_iv is None:
        return {
            **base,
            "earliest": None, "most_likely": None, "latest": None, "window_days": 0,
            "method": "gdd_fallback", "matured": False, "overdue": False,
            "basis": "气温数据不足，无法估算收获日",
        }

    g_earliest, g_latest = gdd_iv

    models = _load_models()
    method = "gdd_fallback"
    basis = ("积温法（XGBoost 已积累 %d/%d 条记录）"
             % (sample_count, TRAIN_MIN_SAMPLES))

    # 点预测取 **q50**，区间取 **[q10, q90]**；只有没有模型时才退回积温法的两端点取中点。
    #
    # 以前这里是「积温两端点 ∪ 三个分位」取 min/max，再用中点当 most_likely。
    # 那样做的问题在外部数据集的留出省上量得很清楚（scripts/32 的报告里一直有这条诊断）：
    # 积温法在跨省时偏差 +21.7 天，模型 q50 的偏差是 −8.7 天，两者一取并集，
    # 一个 5.8 天量级的准模型被一个又宽又偏的区间稀释成 21.1 天。
    # 并集口径不是数据或特征的问题，是组合方式的问题。
    #
    # 改成 q50 之后，积温法并没有被丢掉：它仍是**没有模型时的唯一来源**，
    # 也是 `gdd_remain` 这类特征的计算基础。丢掉的是一个已被证伪的合成方式。
    likely_days: int | None = None
    calib_w = 0          # 跨省校准量；没有模型时为 0，区间完全由分位/积温决定

    if models:
        try:
            features = build_features(field, growth, temps, today=today)
            vec = [feature_vector(features)]
            raw = [max(int(round(models[q].predict(vec)[0])), 0) for q in QUANTILES]
            # 三个分位是**三个独立训练**的模型，各自最小化自己的分位损失，
            # 没有联合约束，所以「q10 比 q90 大」这种情况是会出现的（分位交叉）。
            # 排一次序，代价为零；不排就会给出反着的区间。
            q_lo, q_mid, q_hi = sorted(raw)
            # 跨省保形校准：两边各放宽 w 天。w 由「留一省」的残差定出（见 CALIBRATION_NAME），
            # 没有校准文件时 w=0，行为与不校准时逐字一致。
            calib_w = int(round(_load_calibration()))
            earliest_days = max(q_lo - calib_w, 0)
            latest_days = q_hi + calib_w
            likely_days = q_mid
            method = "xgboost"
            basis = "XGBoost 三分位回归（q10/q50/q90）"
            if calib_w:
                basis += "；区间已按跨省校准放宽 %d 天" % calib_w
        except Exception as e:  # noqa: BLE001
            # 推理失败就退回积温法，并把原因说出来 —— 不要让用户以为这是模型给的
            logger.warning("XGBoost 推理失败，改用积温法：%s", e)
            basis = f"积温法（XGBoost 推理失败：{e}）"

    if likely_days is None:
        # 没有模型（或推理失败）：积温法两端点取中点当点预测，与改动前逐字一致。
        earliest_days, latest_days = g_earliest, g_latest
        likely_days = (earliest_days + latest_days) // 2

    # 施肥记录的调整放在这里、也就是**所有候选都取完之后**：三种方法（积温法、
    # XGBoost 三分位）本身都不吃这块信息，与其分别去改它们，不如统一在最后挪一次，
    # 保证三者的相对位置不变、区间宽度也不被撑开。往前的调整不能把日期挪到过去，
    # 所以下界卡在 0。
    if shift:
        earliest_days = max(earliest_days + shift, 0)
        latest_days = max(latest_days + shift, 0)
        likely_days = max(likely_days + shift, 0)
        basis += "；已按记录的施肥情况调整"

    # 宽度控制：限制半宽上限，同时保证至少 ±1 天 ——
    # 「精确到某一天」是假承诺，区间宽度为 0 等于在说谎。
    #
    # 上限要**加上跨省校准量 w**：宽度表管的是「模型自己那段分位区间能有多宽」，
    # 而 w 是另外量出来的、有据可依的放宽。若让 15/10/5/2 把 w 也一并裁掉，
    # 呈现给用户的区间就只有闸门量到的那个覆盖率的一半（实测 0.71 掉到 0.50），
    # 等于闸门验的和页面显示的不是同一个东西。
    limit = _half_width_limit(likely_days) + calib_w
    half = min(max(likely_days - earliest_days, latest_days - likely_days, 1), limit)
    earliest_days = max(likely_days - half, 0)
    latest_days = likely_days + half

    # 这里以前会按半宽给一个「高 / 中 / 低」的置信度，现已删除：
    # 区间本来就是「积温两端点 ∪ 三个分位」取并集再按宽度收窄的产物，
    # 一个笼统的高低标签并不能说明这个日期准不准，反而像在给一个假精度。
    # 真正需要用户知道的两件事，写在下面 basis 的两条限定语里。

    # 低温期提醒：日均有效积温很低时，纯积温法会把收获日推到很远的将来 ——
    # 数学上没错（每天只涨一点点，自然要很多天），但作物在低温下会停止生长甚至冻死，
    # 所以那个日期只能当粗参考。示例田的水稻就是这种情况：9 月播种，算出来是明年 2 月。
    # 与其让它看起来像个准数，不如把原因写在依据里。
    recent = [r.get("gdd_daily", 0.0) for r in temps[-14:]]
    mean_gdd = statistics.fmean(recent) if recent else 0.0
    if mean_gdd < 5.0:
        basis += (f"；注意最近日均有效积温只有 {mean_gdd:.1f} 度，"
                  "作物可能因低温停止生长，这个日期只能作粗参考")
    elif likely_days > 60:
        # 跨度一大，纯积温外推就开始失真：它假设「接下来每一天的积温都和最近两周差不多」，
        # 而这个假设在跨季节时必然不成立（入秋后每天都在降温）。
        basis += "；预测跨度超过 60 天，纯积温外推的可靠性有限，仅作参考"

    return {
        **base,
        "earliest": (today_d + timedelta(days=earliest_days)).isoformat(),
        "most_likely": (today_d + timedelta(days=likely_days)).isoformat(),
        "latest": (today_d + timedelta(days=latest_days)).isoformat(),
        "window_days": half,
        "method": method,
        "matured": False,
        "overdue": False,
        "basis": basis,
    }
