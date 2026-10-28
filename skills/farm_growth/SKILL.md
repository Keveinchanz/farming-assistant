---
name: farm-growth
description: 农田生长、收获预测与三维植株参数。由田块、播种日期与逐日气温计算积温、生育阶段、收获日期区间和可直接渲染的三维形态参数。适用于长势评估、收获期预测和三维农田展示。
---

# 农田生长、收获预测与三维参数

## 能力范围

- 按有效积温模型计算当前生育阶段、进度与剩余积温
- 输出可直接交给 `Farm3D.setModel()` 的三维植株参数
- 预测收获日期区间（XGBoost 模型，样本不足时回退积温法）
- 给出收割建议与推荐收割窗口

本技能不依赖登录，不读取 SQLite。气温可直接注入，也可提供经纬度供技能联网获取 Open-Meteo 数据；
两者均缺失时返回完整结构并标记降级，不虚构收获日期。

## 输入

| 字段 | 说明 |
|---|---|
| `field` | 田块：`crop`、`sow_date` 必填；`id`、`name`、`area_mu`、`province`、`city`、`county`、`lng`、`lat` 可选 |
| `today` | 计算基准日，默认今天 |
| `history` | 逐日气温：`date`、`tmax`、`tmin`；`tmean` 缺省时自动补齐 |
| `forecast` | 未来天气，结构同上，可选 |
| `management` | `{days, note, sources}`，施肥等管理措施对成熟期的影响 |
| `sample_count` | 收获样本数；传入后不再查询数据库 |
| `include` | 返回块：`growth / model / harvest / advice / weather`，默认全部返回 |

## 输出

- `growth`：`gdd_cum`、`stage`、`progress`、`matured`、`confidence`、`source_note` 等
- `model`：三维参数，含 `height`、`leafCount`、`leafColor`、`leafDroop`、`stemColor`、
  `hasEar`、`earSize`、`earColor`、`earDroop`、`dryLeafRatio`、`hasBlackLayer`、`milkLineGone`
- `harvest`：`earliest / most_likely / latest`、`method`（`xgboost` 或 `gdd_fallback`）、
  `basis`、`sample_count`
- `harvest_advice` / `harvest_window`：收割建议与推荐窗口
- `degraded`：`weather / history / harvest_model`
- `weather`：历史与预报的来源、覆盖天数、截至日期

## 三维渲染

`model` 与前端农田页使用同一结构，可直接渲染：

```js
const view = Farm3D.create(canvas, { areaMu: () => 1 });
view.setModel(result.model);
```

## HTTP

```bash
curl -X POST http://127.0.0.1:8000/api/skills/farm-growth \
  -H "Content-Type: application/json" \
  -d '{
    "field": {"crop":"水稻","sow_date":"2026-05-01","area_mu":1.2,
              "province":"湖北省","city":"武汉市","lng":114.3,"lat":30.58},
    "today": "2026-10-22",
    "history": [{"date":"2026-08-01","tmax":32,"tmin":24},
                {"date":"2026-08-02","tmax":33,"tmin":25}]
  }'
```

## CLI

```bash
python -m skills run farm-growth --input payload.json --pretty
python -m skills run farm-growth --input - --pretty
```

## 降级语义

- 无 `history` 且无经纬度：`degraded.history = true`，积温为 0，不虚构收获日期。
- 天气接口失败：保留已注入或已缓存的气温，`source_note` 如实说明。
- 收获样本不足：`harvest.method = "gdd_fallback"`，`basis` 注明采用积温法。
- 不支持的作物：返回 400，并列出可选作物。
