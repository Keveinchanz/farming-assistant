# 阶段 7 论文引用

「我的农田」这一块的功能不是凭感觉设计的 —— 下面每篇对应一个具体实现，
写在这里是为了让**参数的来源可追溯**：看到 `configs/crop_stages.yaml` 里的
温度基点或 `src/farm/harvest_model.py` 的特征表时，能顺着找到依据。

> 关于引用格式的说明：本项目是工程实现，不发表论文。下表列的是**口径来源**
> （谁在什么条件下测出这些关系），不是逐字抄录。引用时请核对原文献的版本与页码。

---

## 1. 积温四段式

**栾青等《基于线性生长假设的作物积温模型稳定性比较》**
→ `src/farm/gdd.py` 的 `daily_gdd()`
→ `configs/crop_stages.yaml` 里每个作物的 `Tbase / Topt / Tu`

用途：本项目用的 Ae4 模型。它的关键一点是**日均温超过最适温度后积温回落**：

```
Tmean < Tbase          → 0
Tbase ≤ Tmean ≤ Topt   → Tmean - Tbase
Topt < Tmean ≤ Tu      → (Tu - Tmean) × (Topt - Tbase) / (Tu - Topt)
Tmean > Tu             → 0
```

为什么不用最简单的「Tmean - Tbase」：那样会把盛夏算成超额积温，
生育期整体提前、收获日跟着提前，而武汉 7~8 月的日均温有相当多天在 30℃ 以上，
误差不是小数点级别。第三段的物理含义是高温胁迫 —— 作物不会因为更热就长得更快。

**对实现的影响**：`Topt`/`Tu` 必须逐作物给。油菜是冷凉作物（本项目取 25/35），
抄水稻的 30/42 会让它的生育期整体提前一大截 —— 这一点在
`configs/crop_stages.yaml` 的注释里也写了。

## 2. 积温法的精度

**牛博文等《基于有效积温的加工番茄生育期模拟模型》**
→ `src/farm/growth.py` 的 `compute_growth()`、`src/farm/harvest_model.py` 的纯积温法分支

用途：验证「只靠积温能到什么精度」。结论是**短期准、跨季不准** —— 这也是本项目
在预测跨度大时主动加一句「可靠性有限、仅作参考」的依据（见
`harvest_model.predict_harvest()` 里 `likely_days > 60` 那段）。

所以本项目把纯积温法定位成**兜底**而不是主方法：模型没训练好时它保证功能可用，
但它给的是区间而不是具体某一天。

## 3. 水稻 GDD + 机器学习

**Liu et al. "Rice Growth Modeling Based on GDD and AI"**
→ `src/farm/harvest_model.py` 的整体思路（积温特征 + 分位数回归）

用途：确认「积温量 + 气象统计量」这组特征对生育期预测是有效的 ——
`FEATURE_NAMES` 里的 `gdd_cum / gdd_7d / gdd_remain / tmax_mean_30d /
tmin_mean_30d / precip_sum_30d / hot_days_30d / cold_days_30d` 就来自这个思路。

## 4. XGBoost 的 11 个特征

**"A scalable machine learning approach for predicting wheat growth stages"（2025）**
→ `src/farm/harvest_model.py` 的 `FEATURE_NAMES`（11 个，顺序即训练列顺序）

用途：特征集与「用分位数回归给出区间而不是点估计」的做法。
本项目取 q10 / q50 / q90 三条分位线，正是因为它要回答的是
「最可能什么时候收」+「可能提前或推迟多久」，而不是一个假装精确的日期。

**为什么不自己发明特征**：特征表是训练与推理共用的同一份常量
（`build_features()` 唯一实现），从论文口径来能避免「我随手加一个字段」
这种只在几个月后才会暴露的漂移。

## 5. 远期方向

**"Physics-guided DL for crop phenology"（2025）**
→ 本阶段**未实现**，仅作为方向记录

用途：把物理约束（积温单调、低温不倒退）显式编进网络结构，而不是靠数据学。
本项目现在用的是「积温算好、模型只做修正」的两段式，本质上是同一条思路的
工程化简化版。要在本平台落地，前提是收获记录攒够 ——
`harvest_records` 现在是几十条量级，先积累再谈。

---

## 与实现的对应关系一览

| 论文口径 | 落在哪 |
|---|---|
| 积温四段式（含高温回落） | `src/farm/gdd.py::daily_gdd` |
| 温度基点逐作物 | `configs/crop_stages.yaml` |
| 预报不可用时的气候基准值 | `src/farm/growth.py::_normals_weather`（用 5 年同日分组的均值±标准差） |
| 11 个特征 | `src/farm/harvest_model.py::FEATURE_NAMES` |
| q10/q50/q90 三分位 | `harvest_model.QUANTILES`、`models/farm/harvest_xgb_q*.json` |
| 跨度大就加一句「可靠性有限、仅作参考」 | `harvest_model.predict_harvest()` 的 `likely_days > 60` 分支 |
| 区间宽度按距收获天数收窄 | `harvest_model._half_width_limit()`（清单 7.3.4 的宽度控制表） |
