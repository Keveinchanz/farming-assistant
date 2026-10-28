#!/usr/bin/env python
"""阶段 7 / 7.3.4（外部数据）—— 用公开物候数据集训练收获日模型，并过一道上线闸门。

读 `data/phenology/harvest_train.csv`（由 scripts/31 生成），按三档留出各训一遍、
各评一遍，最后拿**最严的那一档**（留出湖北省）当上线闸门。

## 三档留出为什么都要看

    group     按「季」的 GroupKFold。同分布，给的是**能力上限**的参考。
    time      train ≤ 2016 / val ≥ 2017。看的是「换了年份还准不准」。
    province  **留出湖北省**（train = 其余省）。本项目演示区，`crop_stages.yaml` 里
              水稻就是按武汉标定的。这一档同时吃下三类偏移：气候、品种、以及
              「数据集给移栽日、应用记播种日」的口径差。**闸门以它为准。**

`group` 那一档分数一定最高，只看它会得到一个过于乐观的结论。

## 基线与模型必须走同一个函数

两者都用真正的 `predict_harvest`，只把 `_load_models` 的落点分别指向候选模型和
「什么都没有」的目录 —— 于是基线自动退回纯积温法。**不另写一套评价逻辑**：
如果评价用的组合方式与线上不同，闸门量到的就不是线上的东西。

## 报告里还有一个诊断量：只用 q50 的 MAE

`most_likely` 是「积温两端点 ∪ 三个分位」并集的**中点**。积温区间又宽又偏时，
一个很准的模型会被稀释掉 —— 这是**架构效应**，不是数据或特征的问题。
所以同时报 q50-only 的 MAE。若出现「q50 明显更好，但部署口径过不了闸门」，
那就是一个真结论：瓶颈在 `predict_harvest` 的组合方式。**本次不改它**，只如实写进报告。

## 用法

    python scripts/32_train_harvest_external.py              # 只训练 + 评估 + 打报告
    python scripts/32_train_harvest_external.py --install    # 过闸门才装进 models/farm/

**没加 `--install` 时绝不碰 `models/farm/`**：产物一律落在 `models/farm_staging/`，
而 `models_available()` 只认 `models/farm/harvest_xgb_q*.json` 三个确切路径，
所以暂存区里的东西线上不会加载。

未过闸门时**退出码仍然是 0**（沿用 24 号脚本「等待/未达标不是错误」的哲学），
但会把每一条没过的理由用中文打出来。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import read_csv, write_json                     # noqa: E402
from src.common.logging_utils import setup_logger                        # noqa: E402
from src.common.paths import path_of                                     # noqa: E402
from src.farm import harvest_external as hx                              # noqa: E402
from src.farm import harvest_model                                       # noqa: E402
from src.farm.timeutil import today_str                                  # noqa: E402

logger = setup_logger("agri.phenology.train")

# 训练样本里**不允许**出现的字段（7.14 的隐私要求）。外部物候数据本身不含个人信息，
# 但这条断言照抄 scripts/24，免得将来有人往 CSV 里加了一列身份字段却没人发现。
FORBIDDEN_KEYS = ("user_id", "field_id", "name", "province", "city", "lng", "lat", "ip")

# 写进 models/farm/provenance.json 的来源标记。24 号脚本看到它就会拒绝覆盖 ——
# 那条本地路径的标签口径是错的（见其头部说明），不该把这里的产物顶掉。
ORIGIN = "external_phenology"


def load_rows(path: Path) -> list[dict]:
    raw = read_csv(path)
    if not raw:
        logger.error("训练表是空的：%s", path)
        raise SystemExit(1)
    leaked = sorted({k for k in FORBIDDEN_KEYS if k in raw[0]})
    if leaked:
        logger.error("训练表里出现了身份字段 %s —— 7.14 禁止，已中止", leaked)
        raise SystemExit(1)
    return [hx.row_from_csv(r) for r in raw]


def mae(errors: list[int]) -> float:
    return sum(abs(e) for e in errors) / len(errors) if errors else float("nan")


def lopo_calibration(train_rows: list[dict], staging: Path, *,
                     level: float = 0.80, min_rows: int = 200) -> dict:
    """用「留一省」的残差定出区间该放宽多少天（跨省保形校准）。

    ## 为什么需要它

    三个分位模型是各自独立训练的，没有联合约束；换到一个**没见过的省**上，
    名义 80% 的区间会明显偏窄（实测留出湖北只盖住 55%）。这不是模型坏了，
    而是分位数回归在分布偏移下本来就不保证覆盖率。保形预测给的就是这个修法：
    量出一个常数 w，把区间两边各放宽 w 天。

    ## 为什么 w 可以算在训练集上

    留出的那个省是**训练省之间**互相留出，留出省（湖北）一次都没参与。
    所以把这个 w 用到湖北上仍然是盲测，闸门量到的不是调出来的数。

    返回 {w_days, level, scores, by_province}；样本太少的省跳过 —— 只有 28 行的省
    留出来的是噪声，把它算进分位数只会把 w 抬歪。
    """
    codes = [hx.province_code(r["group"].split("|")[0]) for r in train_rows]
    X = [harvest_model.feature_vector(r) for r in train_rows]
    y = [float(r["y"]) for r in train_rows]
    uniq = sorted(set(codes))

    scores: list[float] = []
    by_prov: dict[str, dict] = {}
    for code in uniq:
        te = [i for i, c in enumerate(codes) if c == code]
        tr = [i for i, c in enumerate(codes) if c != code]
        if len(te) < min_rows or len(tr) < min_rows:
            by_prov[code] = {"rows": len(te), "skipped": True}
            continue
        pred = harvest_model.predict_quantiles_at(
            [X[i] for i in tr], [y[i] for i in tr], [X[i] for i in te])
        at = [y[i] for i in te]
        prov_scores = [max(p10 - a, a - p90) for p10, p90, a in zip(pred[10], pred[90], at)]
        prov_err = [p - a for p, a in zip(pred[50], at)]
        scores.extend(prov_scores)
        by_prov[code] = {
            "rows": len(te),
            "mae": round(sum(abs(e) for e in prov_err) / len(prov_err), 3),
            "raw_coverage": round(sum(1 for s in prov_scores if s <= 0) / len(prov_scores), 4),
        }
        logger.info("  留一省 %s：%d 行，MAE %.2f，原始覆盖 %.3f",
                    code, len(te), by_prov[code]["mae"], by_prov[code]["raw_coverage"])

    if not scores:
        return {"w_days": 0.0, "level": level, "scores": 0, "by_province": by_prov,
                "note": "可留出的省不足（每个都要 ≥ %d 行），不做校准" % min_rows}

    ordered = sorted(scores)
    idx = min(int(round(level * len(ordered))), len(ordered) - 1)
    w = float(ordered[idx])

    payload = {
        "w_days": round(w, 3),
        "level": level,
        "scores": len(scores),
        "basis": "leave-one-province-out（只用训练省，留出省未参与）",
        "by_province": by_prov,
    }
    staging.mkdir(parents=True, exist_ok=True)
    (staging / harvest_model.CALIBRATION_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("跨省校准：留一省 %d 个分数，%g%% 分位 → w = %.1f 天", len(scores), level * 100, w)
    return payload


def bias(errors: list[int]) -> float:
    return sum(errors) / len(errors) if errors else float("nan")


def predict_all(rows: list[dict], models_dir_fn) -> list[dict]:
    """把 `_load_models` 的落点临时指向某处，逐行跑**真正的** predict_harvest。

    基线不另写一套评价逻辑，只是把落点指向一个必定不存在的目录 ——
    `models_available()` 于是为 False，`predict_harvest` 自动退回纯积温法。
    两边走的是同一条代码路径，闸门量到的才是线上会有的东西。
    """
    saved = harvest_model.models_dir
    try:
        harvest_model.models_dir = models_dir_fn
        harvest_model.clear_model_cache()
        return [
            harvest_model.predict_harvest(*hx.row_to_inputs(r), today=r["cutoff_date"])
            for r in rows
        ]
    finally:
        harvest_model.models_dir = saved
        harvest_model.clear_model_cache()


def features_of(row: dict) -> dict:
    """这一行的 11 个特征 —— 走的是训练与推理共用的那个 build_features。"""
    field, growth, temps = hx.row_to_inputs(row)
    return harvest_model.build_features(field, growth, temps, today=row["cutoff_date"])


def days_of(pred: dict, today: str) -> int | None:
    if pred.get("most_likely") is None:
        return None
    return (date.fromisoformat(pred["most_likely"]) - date.fromisoformat(today)).days


def covered(pred: dict, today: str, actual_days: int) -> bool:
    if pred.get("earliest") is None:
        return False
    target = date.fromisoformat(today) + timedelta(days=actual_days)
    return date.fromisoformat(pred["earliest"]) <= target <= date.fromisoformat(pred["latest"])


def evaluate(train_rows: list[dict], val_rows: list[dict], staging: Path, *,
             calibrate: bool = False) -> dict:
    """训练 + 校准 + 评估。返回指标字典（含闸门用的那几项）。

    `calibrate` 只在闸门那一档打开：留一省要按省逐个重训，代价是三次训练的量级，
    而 group/time 两档只是参考，不需要为它们再付一遍。
    """
    X = [harvest_model.feature_vector(r) for r in train_rows]
    y = [float(r["y"]) for r in train_rows]
    hx_fit = harvest_model.fit_quantile_models(X, y, staging)

    # 校准要在 predict_all **之前**算好并落盘：predict_harvest 读的就是这个文件，
    # 先算后预测，线上那条路径才会真的按校准后的区间走，闸门量的才是线上的东西。
    if calibrate:
        calib = lopo_calibration(train_rows, staging)
    else:
        stale = staging / harvest_model.CALIBRATION_NAME
        if stale.exists():
            stale.unlink()          # 上一轮留下的校准文件不能套到这一档上
        calib = {"w_days": 0.0, "note": "本档不做校准"}
    w = float(calib.get("w_days") or 0.0)

    dead = staging / "__baseline__"          # 故意不存在的目录 = 纯积温法
    pred_model = predict_all(val_rows, lambda: staging)
    pred_base = predict_all(val_rows, lambda: dead)

    # 直接用三个分位模型算 q50-only 与原始 q10–q90（诊断用，不走 predict_harvest）
    saved = harvest_model.models_dir
    try:
        harvest_model.models_dir = lambda: staging
        harvest_model.clear_model_cache()
        loaded = harvest_model._load_models()
        if loaded is None:
            raise RuntimeError(f"候选模型留在暂存目录但加载不出来：{hx_fit}")
        Xv = [harvest_model.feature_vector(features_of(r)) for r in val_rows]
        q10 = [float(v) for v in loaded[10].predict(Xv)]
        q50 = [float(v) for v in loaded[50].predict(Xv)]
        q90 = [float(v) for v in loaded[90].predict(Xv)]
    finally:
        harvest_model.models_dir = saved
        harvest_model.clear_model_cache()

    actual = [float(r["y"]) for r in val_rows]
    err_model = [days_of(p, r["cutoff_date"]) - r["y"] for p, r in zip(pred_model, val_rows)]
    err_base = [days_of(p, r["cutoff_date"]) - r["y"] for p, r in zip(pred_base, val_rows)]
    err_q50 = [v - a for v, a in zip(q50, actual)]

    covered_raw = sum(1 for lo, hi, a in zip(q10, q90, actual) if lo <= a <= hi)
    covered_cal = sum(1 for lo, hi, a in zip(q10, q90, actual) if lo - w <= a <= hi + w)
    covered_final = sum(1 for r, p in zip(val_rows, pred_model) if covered(p, r["cutoff_date"], r["y"]))

    return {
        "rows": len(val_rows),
        "groups": len({r["group"] for r in val_rows}),
        "model_mae": round(mae(err_model), 3),
        "q50_mae": round(mae(err_q50), 3),
        "baseline_mae": round(mae(err_base), 3),
        "model_bias": round(bias(err_model), 3),
        "baseline_bias": round(bias(err_base), 3),
        "raw_coverage": round(covered_raw / len(val_rows), 4),
        "calibrated_coverage": round(covered_cal / len(val_rows), 4),
        "calibration_w": round(w, 3),
        "final_coverage": round(covered_final / len(val_rows), 4),
        "staging": {f"q{q}": p for q, p in hx_fit.items()},
    }


def install(staging: Path, report: dict, *, rows: int, groups: int) -> None:
    """把候选模型装进 models/farm/ 并留一份 provenance。"""
    target = path_of("farm_models")
    target.mkdir(parents=True, exist_ok=True)
    for q in harvest_model.QUANTILES:
        shutil.copyfile(staging / f"harvest_xgb_q{q}.json", target / f"harvest_xgb_q{q}.json")
    # 校准文件必须与模型一起装 —— 只装模型会退化成未校准的窄区间，
    # 而闸门是通过校准后的区间才过的，漏了这个文件等于装了半个东西。
    calib = staging / harvest_model.CALIBRATION_NAME
    if calib.exists():
        shutil.copyfile(calib, target / harvest_model.CALIBRATION_NAME)

    write_json(target / "provenance.json", {
        "origin": ORIGIN,
        "dataset": "ChinaRiceCalendar（县级年度水稻物候日历）",
        "doi": "10.7910/DVN/EUP8EY",
        "license": "CC0 1.0",
        "season_start_basis": "transplant",
        "label": "y = mature_doy - cutoff_doy（剩余天数）",
        "features": list(harvest_model.FEATURE_NAMES),
        "train_rows": rows,
        "train_groups": groups,
        "holdout": "留出湖北省（train = 其余省）",
        "metrics": report,
        "installed_at": today_str(),
        "warning": ("起点是移栽日而非播种日；农户填播种日时存在系统性偏移，"
                    "部署口径的 MAE 与偏差见 metrics。"),
    })
    harvest_model.clear_model_cache()
    logger.info("已安装到 %s（并写了 provenance.json）", target)


def main() -> int:
    ap = argparse.ArgumentParser(description="用外部物候数据集训练收获日模型")
    ap.add_argument("--install", action="store_true",
                    help="过闸门后把模型装进 models/farm/（默认只落暂存目录）")
    ap.add_argument("--csv", default=None, help="训练表路径（默认 data/phenology/harvest_train.csv）")
    ap.add_argument("--max-eval", type=int, default=0, help="每档最多评估多少行（0 = 全评）")
    args = ap.parse_args()

    csv_path = Path(args.csv) if args.csv else path_of("phenology_train")
    if not csv_path.exists():
        logger.error("找不到训练表：%s", csv_path)
        logger.error("先跑 python scripts/31_build_harvest_dataset.py")
        return 1

    rows = load_rows(csv_path)
    logger.info("训练表：%s", csv_path)
    logger.info("  样本 %d 行 / %d 组", len(rows), len({r["group"] for r in rows}))

    staging_root = path_of("farm_models_staging")
    report: dict[str, dict] = {}
    for mode in ("group", "time", "province"):
        train_rows, val_rows, meta = hx.split_by_group(rows, mode=mode)
        if not train_rows or not val_rows:
            logger.warning("[%s] 切不出两侧（train %d / val %d）—— 跳过", mode, len(train_rows), len(val_rows))
            continue
        if not meta["groups_disjoint"]:
            logger.error("[%s] 训练集与验证集有共同的季 —— 这会泄漏，已中止", mode)
            return 1
        if args.max_eval and len(val_rows) > args.max_eval:
            val_rows = val_rows[: args.max_eval]

        logger.info("=" * 70)
        logger.info("[%s] %s", mode, meta)
        metrics = evaluate(train_rows, val_rows, staging_root / mode,
                           calibrate=(mode == "province"))
        report[mode] = {**meta, **metrics}
        logger.info("[%s] 模型 MAE %.2f 天 | 仅 q50 %.2f 天 | 基线 MAE %.2f 天",
                    mode, metrics["model_mae"], metrics["q50_mae"], metrics["baseline_mae"])
        logger.info("[%s] 偏差 model %+.2f / baseline %+.2f 天；覆盖率 原始 %.3f → 校准后 %.3f（w=%.1f）；"
                    "最终区间 %.3f",
                    mode, metrics["model_bias"], metrics["baseline_bias"],
                    metrics["raw_coverage"], metrics["calibrated_coverage"],
                    metrics["calibration_w"], metrics["final_coverage"])

    gate_src = report.get("province")
    if not gate_src:
        logger.error("没有「留出湖北省」这一档的评估结果 —— 数据里可能压根没有省外的季。")
        logger.error("  先用 --provinces 多拉几个省重跑 scripts/31，或检查 31 的省份筛选。")
        return 1

    verdict = hx.gate_check(
        model_mae=gate_src["model_mae"], baseline_mae=gate_src["baseline_mae"],
        coverage=gate_src["calibrated_coverage"], val_rows=gate_src["rows"],
        val_groups=gate_src["groups"],
    )

    logger.info("=" * 70)
    logger.info("上线闸门（以「留出湖北省」为准）：%s", "通过" if verdict["passed"] else "**未通过**")
    for name, chk in verdict["checks"].items():
        logger.info("  [%s] %s —— %s", "OK" if chk["ok"] else "不达标", name, chk["detail"])

    # 架构效应的诊断：q50 明显更好而部署口径没赢，说明瓶颈在组合方式而不是数据/特征
    if gate_src["q50_mae"] + 1.0 < gate_src["model_mae"]:
        logger.warning("诊断：仅用 q50 的 MAE(%.2f) 明显低于部署口径(%.2f) —— "
                       "积温区间把准的模型稀释掉了。这是 predict_harvest 的组合方式问题，"
                       "不是数据或特征的问题；本次范围内**不改**。",
                       gate_src["q50_mae"], gate_src["model_mae"])

    out = path_of("reports_dir") / "harvest_external_eval.json"
    write_json(out, {
        "generated_at": today_str(),
        "csv": str(csv_path),
        "samples": len(rows),
        "groups": len({r["group"] for r in rows}),
        "splits": report,
        "gate": {"basis": "province（留出湖北省）", **verdict},
        "installed": False,
    })
    logger.info("报告：%s", out)

    if not verdict["passed"]:
        logger.info("未过闸门 → **不安装**。产物留在 %s，可继续调数据/特征。", staging_root)
        logger.info("（这次运行是成功的：闸门就是用来挡住不够好的模型的。）")
        return 0

    if not args.install:
        logger.info("闸门通过。要装进 models/farm/ 请加 --install 重跑。")
        return 0

    # 传的是**训练侧**的行数与组数：这两项是在说明「这个模型吃了多少数据」，
    # 以前误传了留出集的（1005 行 / 134 组），读 provenance 的人会以为模型只用了一千行训练。
    install(staging_root / "province", report["province"],
            rows=gate_src["train_rows"], groups=gate_src["train_groups"])
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["installed"] = True
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("下一步：重跑 scripts/21_smoke_test_api.py 与农田页探针做回归。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
