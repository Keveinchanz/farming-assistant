# 农学依据与文献口径

「我的农田」的温度基点、积温算法与特征表均有文献来源。本文记录各参数的出处，
便于从 `configs/crop_stages.yaml` 与 `src/farm/harvest_model.py` 回溯依据。

> 本项目为工程实现，不发表论文。下列文献提供**口径来源**（在何种条件下测得何种关系），
> 非逐字抄录。引用时请核对原文献的版本与页码。

---

## 1. 积温四段式

**栾青等《基于线性生长假设的作物积温模型稳定性比较》**
→ `src/farm/gdd.py` 的 `daily_gdd()`
→ `configs/crop_stages.yaml` 中各作物的 `Tbase / Topt / Tu`

本项目采用 Ae4 模型。其要点是日均温超过最适温度后积温回落：

```
Tmean < Tbase          → 0
Tbase ≤ Tmean ≤ Topt   → Tmean - Tbase
Topt < Tmean ≤ Tu      → (Tu - Tmean) × (Topt - Tbase) / (Tu - Topt)
Tmean > Tu             → 0
```

若简化为 `Tmean - Tbase`，盛夏会被计为超额积温，导致生育期整体提前、收获日随之提前。
武汉 7 至 8 月有相当多日日均温高于 30 ℃，误差不在小数级别。第三段的物理含义为高温胁迫：
温度超过上限后作物生长不再加快。

**对实现的要求**：`Topt` 与 `Tu` 必须逐作物设定。油菜属冷凉作物（本项目取 25 / 35），
沿用水稻的 30 / 42 会使其生育期明显提前。该点亦见于 `configs/crop_stages.yaml` 注释。

## 2. 积温法的精度边界

**牛博文等《基于有效积温的加工番茄生育期模拟模型》**
→ `src/farm/growth.py` 的 `compute_growth()`、`src/farm/harvest_model.py` 的纯积温法分支

该文验证了仅依赖积温所能达到的精度：短期准确、跨季偏差显著。这也是本项目在预测跨度较大时
主动标注「可靠性有限、仅作参考」的依据（见 `harvest_model.predict_harvest()` 中
`likely_days > 60` 分支）。

纯积温法因此定位为**兜底**而非主方法：模型不可用时保证功能可用，输出为区间而非具体日期。

## 3. 水稻 GDD 与机器学习

**Liu et al. "Rice Growth Modeling Based on GDD and AI"**
→ `src/farm/harvest_model.py` 的整体思路（积温特征加分位数回归）

该文确认「积温量 + 气象统计量」这组特征对生育期预测有效。`FEATURE_NAMES` 中的
`gdd_cum / gdd_7d / gdd_remain / tmax_mean_30d / tmin_mean_30d / precip_sum_30d /
hot_days_30d / cold_days_30d` 即由此而来。

## 4. XGBoost 的 11 个特征

**"A scalable machine learning approach for predicting wheat growth stages"（2025）**
→ `src/farm/harvest_model.py` 的 `FEATURE_NAMES`（11 个，顺序即训练列顺序）

该文提供特征集，以及「以分位数回归给出区间而非点估计」的做法。本项目取 q10 / q50 / q90 三条分位线，
对应「最可能何时收获」与「可能提前或推迟多久」两个问题，而非给出单一日期。

特征表由训练与推理共用同一份常量（`build_features()` 为唯一实现），口径取自文献，
避免临时增删字段造成的、数月后才暴露的漂移。

## 5. 远期方向

**"Physics-guided DL for crop phenology"（2025）**
→ 本阶段未实现，仅作方向记录

该方向将物理约束（积温单调、低温不倒退）显式编入网络结构，而非依赖数据学习。
本项目现采用「积温先行、模型修正」的两段式，属同一思路的工程化简化。
在本平台落地的前提是收获记录积累到足够规模，当前 `harvest_records` 仅数十条。

---

## 与实现的对应关系

| 文献口径 | 落在哪 |
|---|---|
| 积温四段式（含高温回落） | `src/farm/gdd.py::daily_gdd` |
| 温度基点逐作物 | `configs/crop_stages.yaml` |
| 预报不可用时的气候基准值 | `src/farm/growth.py::_normals_weather`（5 年同日分组的均值 ± 标准差） |
| 11 个特征 | `src/farm/harvest_model.py::FEATURE_NAMES` |
| q10 / q50 / q90 三分位 | `harvest_model.QUANTILES`、`models/farm/harvest_xgb_q*.json` |
| 跨度较大时标注可靠性有限 | `harvest_model.predict_harvest()` 的 `likely_days > 60` 分支 |
| 区间宽度按距收获天数收窄 | `harvest_model._half_width_limit()` |
