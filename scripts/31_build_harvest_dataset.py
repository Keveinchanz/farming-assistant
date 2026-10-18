#!/usr/bin/env python
"""阶段 7 / 7.3.4（外部数据）—— 把物候日历 + 天气拼成训练样本表。

输入：scripts/29 解压出的县表（xlsx）、scripts/30 拉的一县一个天气文件。
输出：
    data/phenology/rice_calendar.csv    长表，**一季一行**（县 × 年 × 稻型）
    data/phenology/harvest_train.csv    训练样本，**一个观察截止日一行**

## 标签的口径

    y = mature_doy - cutoff_doy          # 从观察日到成熟的**剩余**天数

这一条必须和线上一致：`predict_harvest` 把三个分位模型的输出与 `_gdd_interval`
（返回的确实是剩余天数）放进同一个列表取 min/max（harvest_model.py 的 candidates）。
口径错了模型不会报错，只会把收获日系统性推早或推晚 —— 详见
`src/farm/harvest_external.py` 与 `scripts/24_train_harvest_model.py` 的头部说明。

## 起点用的是移栽日，不是播种日

数据集给的是移栽日。本脚本把它当作作物起点，并在元数据里写死
`season_start_basis="transplant"`。**不许**凭空减一个「育秧天数」把它折算成播种日 ——
那是编数据。这个口径差是真实的域偏移，也正是闸门必须用「留出湖北」那一档的原因。

## 县 → 质心的 join 率

低于 95% 直接报错退出。缺质心的县拿不到天气，对应的季会被整片丢掉；
丢掉的如果是「某个省的绝大多数县」，训练集就会悄悄偏向剩下的省，
而报告上只看得到「行数少了点」。宁可在这一步炸掉。

## 用法

    python scripts/31_build_harvest_dataset.py                       # 全量
    python scripts/31_build_harvest_dataset.py --provinces 湖北省      # 最小闭环
    python scripts/31_build_harvest_dataset.py --provinces 湖北省 湖南省 江西省
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import read_json, write_csv, write_json        # noqa: E402
from src.common.logging_utils import setup_logger                       # noqa: E402
from src.common.paths import path_of                                    # noqa: E402
from src.farm import harvest_external                                   # noqa: E402

logger = setup_logger("agri.phenology.dataset")

# 县 → 质心的最低 join 率。见模块头部：低于这个值不是「数据少一点」，
# 而是训练集的省份构成被悄悄改了。
MIN_JOIN_RATE = 0.95

CALENDAR_COLUMNS = (
    "adcode", "province", "city", "county", "year", "rice_type",
    "start_doy", "mature_doy", "season_days", "has_weather",
)


def weather_by_year(adcode: str) -> dict[int, list[dict]] | None:
    """读一个县的天气缓存并按年分组。**没有缓存返回 None**（不是空 dict）。"""
    path = path_of("phenology_weather") / f"{adcode}.json"
    if not path.exists():
        return None
    rows = (read_json(path) or {}).get("rows") or []
    if not rows:
        return None
    out: dict[int, list[dict]] = {}
    for r in rows:
        day = str(r.get("date") or "")
        if len(day) < 4:
            continue
        try:
            year = int(day[:4])
        except ValueError:
            continue
        out.setdefault(year, []).append(r)
    return out or None


def county_centroid_index() -> dict[str, str]:
    """{adcode: 省名}。只用来判有没有质心，坐标本身在 30 号脚本里已经用掉了。"""
    data = read_json(path_of("admin_divisions"))
    out: dict[str, str] = {}
    for prov in data.get("provinces") or []:
        for city in prov.get("cities") or []:
            for county in city.get("counties") or []:
                code = str(county.get("adcode") or "")
                center = county.get("center") or []
                if len(code) == 6 and len(center) == 2:
                    out[code] = prov.get("name") or ""
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="拼装收获日模型的训练样本表")
    ap.add_argument("--provinces", nargs="*", default=None, help="只保留这些省的季（最小闭环用）")
    ap.add_argument("--years", nargs="*", type=int, default=None, help="只保留这些年份")
    args = ap.parse_args()

    raw_dir = path_of("phenology_raw")
    extract = raw_dir / "_extract"
    found = [p for p in extract.rglob("*.xlsx")] if extract.exists() else []
    if not found:
        logger.error("找不到县表 —— 先跑 python scripts/29_download_phenology_datasets.py")
        raise SystemExit(1)

    seasons, cal_stats = harvest_external.read_calendar(found[0])
    if args.provinces:
        wanted = set(args.provinces)
        seasons = [s for s in seasons if s["province"] in wanted]
    if args.years:
        wanted_years = set(args.years)
        seasons = [s for s in seasons if s["year"] in wanted_years]
    if not seasons:
        logger.error("筛选后一季都不剩 —— 检查 --provinces / --years 是否写对（要用省名全称）。")
        raise SystemExit(1)

    # ---- 县 → 质心 join 率
    centroids = county_centroid_index()
    used = {s["adcode"] for s in seasons}
    joined = {c for c in used if c in centroids}
    rate = len(joined) / len(used)
    logger.info("县表：%d 季 / %d 个县（原始 %d 季，丢弃顺序不符 %d 条、越界 %d 条）",
                len(seasons), len(used), cal_stats["kept"],
                cal_stats["no_order"], cal_stats["outside"])
    logger.info("县 → 质心 join 率：%d/%d = %.1f%%", len(joined), len(used), rate * 100)
    if rate < MIN_JOIN_RATE:
        missing = sorted(used - joined)[:20]
        logger.error("join 率低于 %.0f%%，**不生成数据集**。", MIN_JOIN_RATE * 100)
        logger.error("  缺质心的县共 %d 个，例如：%s", len(used - joined), missing)
        logger.error("  多半是 scripts/27_build_admin_divisions.py 的行政区划与数据集版本不同。")
        raise SystemExit(1)

    # ---- 逐县拼样本
    weather_cache: dict[str, dict[int, list[dict]] | None] = {}
    rows: list[dict] = []
    calendar_rows: list[dict] = []
    row_provinces: list[str] = []          # 与 rows 一一对应，只为报告统计（不进训练表）
    reasons: Counter = Counter()

    for season in seasons:
        adcode = season["adcode"]
        if adcode not in weather_cache:
            weather_cache[adcode] = weather_by_year(adcode)
        by_year = weather_cache[adcode]

        start_doy = int(round(season["transplant"]))
        mature_doy = int(round(season["maturity"]))
        calendar_rows.append({
            "adcode": adcode,
            "province": season["province"],
            "city": season["city"],
            "county": season["county"],
            "year": season["year"],
            "rice_type": season["rice_type"],
            "start_doy": start_doy,
            "mature_doy": mature_doy,
            "season_days": mature_doy - start_doy,
            "has_weather": bool(by_year),
        })

        if not by_year:
            reasons["该县没有天气缓存"] += 1
            continue
        daily = by_year.get(season["year"]) or []
        if not daily:
            reasons["该县该年没有天气"] += 1
            continue

        built = 0
        for cutoff in harvest_external.season_cutoffs(season["transplant"], season["maturity"]):
            row = harvest_external.build_sample(
                adcode=adcode,
                year=season["year"], rice_type=season["rice_type"],
                start_doy=season["transplant"], mature_doy=season["maturity"],
                cutoff_doy=cutoff, daily=daily,
            )
            if row is None:
                reasons["天气覆盖不足 / 积温为空 / 已完熟"] += 1
                continue
            rows.append(row)
            # 训练行本身不带省名（见 EXTRA_COLUMNS 的注释），所以省份统计在这里
            # 顺带记下来 —— 报告里要给人看「各省多少行」，而 region 不进训练表。
            row_provinces.append(season["province"])
            built += 1
        if built == 0:
            reasons["整季一个截止日都不可用"] += 1

    if not rows:
        logger.error("一季都没拼出样本 —— 检查天气缓存是否与县表匹配。")
        logger.error("  各类丢弃原因：%s", dict(reasons))
        raise SystemExit(1)

    # ---- 落盘
    write_csv(path_of("phenology_calendar"), calendar_rows, CALENDAR_COLUMNS)
    train_rows = [harvest_external.row_to_csv(r) for r in rows]
    write_csv(path_of("phenology_train"), train_rows, harvest_external.CSV_COLUMNS)

    groups = {r["group"] for r in rows}
    provinces = Counter(row_provinces)
    years = [r["year"] for r in rows]
    feat_path = path_of("phenology_train")
    write_json(path_of("phenology_raw") / "dataset_meta.json", {
        "seasons": len(seasons),
        "counties": len(used),
        "centroid_join_rate": round(rate, 4),
        "rows": len(rows),
        "groups": len(groups),
        "year_range": [min(years), max(years)],
        "labels": "y = mature_doy - cutoff_doy（剩余天数）",
        "season_start_basis": "transplant",
        "features_from": "harvest_model.build_features（训练与推理共用同一份）",
        "provinces": dict(provinces.most_common()),
        "dropped": dict(reasons),
        "note": ("起点是移栽日而非播种日，存在系统性口径差；"
                 "上线闸门以「留出湖北省」这一档为准。"),
    })

    logger.info("=" * 70)
    logger.info("长表：%s（%d 季）", path_of("phenology_calendar").name, len(calendar_rows))
    logger.info("训练表：%s", feat_path)
    logger.info("  样本 %d 行 / %d 组（一组 = 一季，切分时整组同侧）", len(rows), len(groups))
    logger.info("  文件 %.1f MB", feat_path.stat().st_size / 1024 / 1024)
    logger.info("  年份 %d–%d；覆盖 %d 个省", min(years), max(years), len(provinces))
    logger.info("  各省行数：%s", dict(provinces.most_common(8)))
    if reasons:
        logger.info("丢弃统计：%s", dict(reasons))
    logger.info("=" * 70)
    logger.info("下一步：python scripts/32_train_harvest_external.py   # 先不加 --install")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
