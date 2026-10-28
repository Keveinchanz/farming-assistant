# 训练数据集

本文覆盖两条互不相干的训练线：**物候与收获期预测**（主要篇幅）与**虫害检测的补充数据**。
两者的数据集、许可与用途不同，表格不可混读。

## 一、收获期预测

「我的农田」的收获日预测（`scripts/24_train_harvest_model.py`）需要物候观测数据。

**本地记录不足以训练**：本地路（`scripts/24`）在全平台 `harvest_records` 累计 ≥ 30 条时才训练，
样本不足时收获预测走纯积温法（`method=gdd_fallback`），这是设计的等待状态。
更关键的是标签口径——库中存的是「收获当天」的特征快照，标签为全生育期总天数，
于是 `features["days_since_sow"] ≈ actual_days`，模型只能学成恒等映射；
线上却把输出当作「从观察日到成熟的剩余天数」使用，按此训练会使收获日系统性推晚。

**一个生长季需要多个观察截止日**才能学到该映射，本地记录无法提供，外部物候数据集可以。
因此外部数据是主路径，本地训练为补充。两条路径口径不同，`scripts/24` 遇到外部产物会拒绝覆盖，
除非显式指定 `--force`。

### 数据集清单

| 作物 | 数据集 | 链接 | 备注 |
|---|---|---|---|
| 水稻 | ChinaRiceCalendar | https://doi.org/10.7910/DVN/EUP8EY | **训练路径第 1 步（已落地）**：2503 县 × 2003–2022 × 早/中/晚稻的移栽、抽穗、成熟日，CC0 1.0 |
| 水稻/小麦/玉米 | ChinaCropPhen1km | https://doi.org/10.6084/m9.figshare.8313530.v5 | 1 km 格点物候，覆盖作物最广 |
| 水稻 | 长江中下游水稻物候（1981–2024） | https://doi.org/10.57760/sciencedb.25297 | 与演示区（湖北）最接近 |
| 小麦/玉米 | CY-Bench | https://doi.org/10.5281/zenodo.11502142 | **不采用**：`crop_calendar_*` 仅 `crop_name, adm_id, sos, eos` 四列，且官方标注 `sos/eos` 为 static（WorldCereal 多年平均，不随年份变化），无法给出逐年收获日 |
| 小麦 | IPK 小麦表型 | https://doi.org/10.5447/ipk/2019/11 | 基因型 × 表型，特征工程可参考 |
| 小麦 | 黄淮海冬小麦物候（1981–2021） | https://doi.org/10.57760/csciencedb.23011 | |
| 玉米 | 中国玉米物候（1985–2020） | https://cstr.cn/11738.11.NCDC.FIGSHARE.DB4056.2023 | |
| 玉米 | 东北玉米物候（1981–2024） | https://doi.org/10.57760/sciencedb.28709 | |
| 油菜 | 长江中下游油菜物候（1981–2024） | Nature 论文页 | 与水稻条目同源 |
| 油菜 | 江西冬油菜（1981–2022） | https://doi.org/10.57760/sciencedb.22236 | |
| 大豆 | 大豆生育动态观测 | https://escience.org.cn | 本项目暂不做大豆，仅记录 |
| 多作物 | GCPE | https://doi.org/10.20783/DIAS.643 | 全球作物物候估计 |
| 多作物 | SAGE | https://sage.nelson.wisc.edu/data-and-models/datasets/crop-calendar-dataset/netcdf-0-5-degree/ | 0.5° 格点播期 |
| 多作物 | 中国农业气象站（653 站） | http://image.data.cma.cn | **独立验证集**，不混入训练 |

### 训练路径

**第 1 步（已实现）**：ChinaRiceCalendar 水稻专项，四个脚本构成一条链，每步产物可单独检查。

| 脚本 | 做什么 | 产物 |
|---|---|---|
| `scripts/29_download_phenology_datasets.py` | 下载并校验（file id 与 sha256 硬编码，全量 CRC 校验） | `data/phenology/_raw/…xlsx` |
| `scripts/30_fetch_phenology_weather.py` | 按县质心（`data/admin_divisions.json`）拉取 2002–2023 逐日气温，一县一文件、存在即跳过 | `data/phenology/weather/<adcode>.json` |
| `scripts/31_build_harvest_dataset.py` | 将一季展开为多个观察截止日，11 个特征均由 `harvest_model.build_features()` 生成 | `data/phenology/harvest_train.csv` |
| `scripts/32_train_harvest_external.py` | 三档留出各训练一遍；以「留出湖北省」为上线闸门，通过且加 `--install` 才装入 `models/farm/` | `models/farm_staging/`、`outputs/reports/harvest_external_eval.json` |

先在小范围验证链路：

```powershell
python scripts/29_download_phenology_datasets.py
python scripts/30_fetch_phenology_weather.py --provinces 湖北省 湖南省 江西省 安徽省 江苏省 --wait-quota
python scripts/31_build_harvest_dataset.py --provinces 湖北省 湖南省 江西省 安徽省 江苏省
python scripts/32_train_harvest_external.py          # 先不加 --install
```

### 天气拉取的额度约束

这一步耗时较长，原因不在脚本本身，而在 Open-Meteo 免费额度的节奏：一次请求 22 年（8035 天）× 3 个变量，
按调用权重计算消耗很高，每小时只能发出数十次。触发上限时服务端返回：

```
HTTP 429（Hourly API request limit exceeded. Please try again in the next hour.）
```

该窗口以**小时**计，同一窗口内重试无意义。`scripts/30` 对三类失败分别处理：

| 失败类型 | 等待策略 | 原因 |
|---|---|---|
| 普通网络失败 | 4 / 8 / 12 秒递增 | 偶发丢包 |
| 429（分钟窗口） | 至少 65 秒 | 等 30 秒无法跨过一分钟窗口，只会在窗口边缘连续失败 |
| 429（小时窗口） | 默认干净退出；加 `--wait-quota` 则睡到下一整点自动续 | 窗口内重试无效 |

一县一文件、存在即跳过，中断无代价，重跑自动续传。四五百个县需跨多个整点窗口完成，属预期行为。

**不以合成天气替代缺失数据**：拉取失败即留缺，缺数据的季在第 31 步被丢弃并计数。
用气候平均或邻近县插值补出的天气会让模型在假数据上训练，且在报告中无法察觉。

### 评测结果

数据集覆盖 8 省 65 县，共 9569 行、1260 组。以已安装权重实跑复评：

| 划分 | 样本 | 模型 MAE | 积温基线 MAE | 区间覆盖率 |
|---|---|---|---|---|
| 按组（同分布，上限参考） | 1921 行 | 5.73 天 | 80.79 天 | 0.794（原始） |
| 按时间（训练 ≤ 2016） | 2749 行 | 5.58 天 | 102.18 天 | 0.795（原始） |
| **留出湖北省（上线闸门）** | **1005 行** | **11.81 天** | **26.25 天** | **0.553 → 0.714（保形校准后）** |

四项闸门的判定：MAE 比积温基线低 14.4 天（要求至少低 1.0 天）；区间覆盖率经跨省保形校准
（w = 3.96）后为 0.714，落在 0.70–0.90 区间（名义 0.80）；留出集 1005 行、134 组，
样本量与组数均满足要求。闸门通过，模型已安装。

首次在 21 个县上验证链路时，留出湖北一档的覆盖率为 0.414，未达阈值。原因有两方面：
分位区间在训练省份上标定，换到样本外省份后真实散布更大而区间偏窄；训练省份过少
（当时仅 1 个省在训）放大了这一偏差。扩至 8 省后覆盖率回到合格区间。

逐省轮转（留一省，其余省训练）的 7 折结果：

| 留出省份 | 黑龙江 | 湖北 | 安徽 | 江苏 | 江西 | 河南 | 浙江 |
|---|---|---|---|---|---|---|---|
| MAE（天） | 15.31 | 11.81 | 9.87 | 9.44 | 6.79 | 6.52 | 6.47 |

七折均值 9.46 天。湖南仅 28 行，轮转时跳过。完整结果见 `outputs/reports/harvest_lopo.json`。

**重训不可完全复现**：`reg:quantileerror` 的分位草图按线程归约，同一数据重训的 MAE 会抖动约 ±0.13 天。
对外数字一律取自已安装权重的复评（`tools/diag/eval_installed.py` 只加载权重、不训练）。

### 起点口径：移栽日而非播种日

数据集提供**移栽日**，而应用的 `field.sow_date` 语义为**播种**。该差异不以「减去育秧天数」抹平，
而是如实记入 `provenance`（`season_start_basis="transplant"`）与评测报告。这也是闸门必须采用
「留出湖北省」这一档的原因。

### 尚未开展的路径

- **第 2 步**：IPK 与油菜数据集补小麦、油菜。本次仅做水稻，`CROP_CODES` 被单元测试锁定，
  新增作物需同步修改配置与断言。`ChinaCropPhen1km` 为 1 km GeoTIFF（约 9.4 GiB，需 GDAL/rasterio），
  Windows + Python 3.11 环境安装困难，本次不采用。
- **第 3 步**：本地 `harvest_records` 微调。需先在生长季内周期性快照特征（新表或新记录），
  否则标签口径始终有误。
- **独立验证**：653 站气象数据用于外部验证，不与训练集混用。混用会使评估虚高，换地块后即消失。

## 二、隐私

**训练只提取数值特征，不含 `user_id`、田块名与位置。**

该约束在取数阶段即生效：`src/farm/repo.py::list_harvest_features()` 的 SQL 仅
`SELECT crop, sow_date, harvest_date, actual_days, gdd_at_harvest, features_json`，
不查询 `user_id` 与 `field_id`；`scripts/24_train_harvest_model.py` 另有一道断言，
样本中出现身份字段即中止训练，而非仅告警。

模型文件会被复制与分发，身份字段一旦进入训练集，将以某种形式保留在模型中，而后续无人会再检查该文件。

## 三、虫害检测的补充数据

虫害检测（`scripts/16`–`19`，IP102 / YOLOv8n）包含 18976 张检测图、22284 个实例。
精度受限有四个原因，其中仅两个与数据量有关：训练未收敛（30 轮时 mAP50 仍单调上升）与长尾
（5 个类在三档划分中均为零）。

**所有数据补充方案都受同一条硬约束限制**：

> 没有公开检测数据集使用 IP102 的 102 类物种体系。Pest24 为 24 类、AgriPest 14 类、
> MPD2018 16 类、AgroPest-12 为目科级。硬并入 102 类会引入标签噪声。

因此两条补充路径均遵循「宁缺毋滥」，且不改变 102 类体系。

| 数据集 | 来源 / 机构 | 许可 | 规模 | 用途 | 是否进入训练 |
|---|---|---|---|---|---|
| **IP102 分类集** | Figshare `IP102.zip`（原作者官方镜像） | 学术免费 | 2.94 GB / 75226 个成员 | 唯一同体系补充源。`scripts/37` 做 copy-paste 增广，仅补三档合计实例数 < 20 的 10 个稀有类 | **否**。已生成 584 张（`data/pest_aug/v2/`），仅进 train，尚未训练 |
| **Pest24** | 中科院合肥智能机械研究所 | 研究/非商业 | 25378 张 / 24 类 | 仅将 XML 中零 object 的图作为纯负样本 | **否**，见下 |
| Zenodo · COCO 重排版 | `zenodo.org/records/15546285` | CC BY 4.0 | 798 MB | **不采用**：无原始切图语义，copy-paste 需要原图 | 否 |
| Kaggle · `ip102-pest-dataset` | Kaggle | 见数据集页 | 约 3.0 GB | IP102 分类集备用镜像 | 否 |

### 镜像可用性（2026-10-11 实测，`scripts/36 --probe`）

| 镜像 | 结果 |
|---|---|
| Figshare `ndownloader.figshare.com/files/62860060` | HTTP 200，2.94 GB，带 ETag。已下载并校验：75226 个成员全量 CRC 通过，sha256 `9a7cd114…a3f` |
| Kaggle `api/v1/datasets/download/...` | HTTP 200，2.94 GB，带 32 位 MD5 `d8b997dc…106c` |
| Zenodo | 本机 DNS 解析失败 |

Figshare 直链稳定且响应带摘要，可钉住 sha256。网盘临时链则只能证明「每次下载的是同一份」，
不代表来源未变，该区分记录在 `data/pest_cls_raw/checksums.json` 的 `mode` 字段。

### Pest24 未进入训练的原因

`scripts/38 --probe` 实测 `aisys.iim.ac.cn:80` 连接超时（域名解析成功、TCP 握手超时，即服务不可达）。
按计划约定，探不到即整体停止并如实报告，不绕过、不合成替代数据。

如需补充，需人工在浏览器中申请下载，然后执行：

```powershell
python scripts/38_download_pest24.py --archive <下载到的档案>
python scripts/39_build_pest_mixed.py --ratio 0.15
```

**负样本的取舍规则**：仅 XML 中零 object 的图可作为负样本。两条理由方向相反但结论一致——
有可映射到 IP102 的物体时，作为负样本等于压制召回；物体均不可映射时，Pest24 整份为虫害图，
「类名表未覆盖」不等于「图上无虫」，作为负样本同样是假负样本。该规则不依赖类名映射表。

### 类序核对：102 类逐条对齐

IP102 分类集的 `classes.txt` 每行为「1 起的序号 + 名称」，权威表来自 16 号脚本的 `PESTYOLO_CLASSES`。
脚本剥除序号前缀后逐条比对，结果 102/102 行与权威表逐字相同。剥前缀不等于放宽比对：
同时要求序号与行号自洽（第 i 行序号必须为 i 或 i+1），序号与行序错位的文件仍会被中止。
该守卫必要——整表错一位不会报错，只会让稀有类拿到其它虫的监督信号，且报告无异常。

解压后结构为 `extracted/classification/{train,val,test}/<0..101>/`，目录从 0 起，与检测集下标同基。

### copy-paste 增广的产出

`scripts/37` 的结果（`outputs/reports/pest_aug_report.json`）：初版 61 张，修正后 584 张。

差异来自一处分母错误：计划要求「前景占**候选框**比例 ≥ 0.35」，初版将同一阈值用于
「前景占**整图**比例」。IP102 分类图中虫体占整图的中位数为 0.09–0.23，按 0.35 过滤整图
等于要求每张源图均为虫体特写，实测将 896 张源图筛至 93 张。裁切框阈值未变（仍为 0.35），
另加 0.02 的整图退化下限，仅拦截掩膜面积过小的样本（修正后仍拦下 166 张）。

逐类产出（零实例类加粗）：

| 类 | 检测集原有实例 | 生成张数 |
|---|---|---|
| 52 | 5 | 92 |
| **59** | **0** | **121** |
| **60** | **0** | **53** |
| 61 | 2 | 29 |
| **63** | **0** | **37** |
| 72 | 13 | 27 |
| **75** | **0** | **62** |
| **80** | **0** | **19** |
| 91 | 8 | 105 |
| 98 | 13 | 39 |

### 指标上基本无法体现

增广仅进 train，val/test 保持原始划分，以便效果对比。代价是：

| 类 | 三档合计 | val+test 实例数 | 能否测到 AP |
|---|---|---|---|
| 59 / 60 / 63 / 75 / 80 | 0 | **0** | **无法测量**（零分母） |
| 61 | 2 | 1 | 名义可测，实际为二值噪声 |
| 52 / 72 / 91 / 98 | 5~13 | 2~5 | 名义可测，分母过小 |

该路径的正当性不在于提升指标，而在于这 5 个类当前得不到任何监督信号，且没有其它公开数据集
提供同物种、同体系的监督。报告中不会将「未涨分」表述为其它结论。

### 未完成的检查：产物外观

`data/pest_aug/v2/` 的 584 张图已验证结构不变量：584 图对应 584 标签、每张含目标类框、
坐标不越界、无 NaN、彼此无重复、与 val/test 无重复、新贴框面积落在 0.0075–0.1193 的目标区间。
**未验证贴入内容的外观是否合理**——本机图片渲染在当次会话不可用。当前约束「贴入叶子当虫」的
只有两道占比门槛与「取 GrabCut 最大连通域」，属已知的残余风险。

### 红线：逐字节不变的文件

`data/pest/data.yaml`、`data/pest_label_map.json`、`models/checkpoints/pest_best.pt`、
`models/registry.json`、`outputs/reports/eval_pest_yolo*.json`、`models/onnx/pest_yolo.onnx`。

候选模型一律落 `models/pest_staging/`，线上不加载，与 `models/farm_staging/` 相同。
自证手段为 `scripts/35_eval_pest_experiments.py --verify-deliverables`，它对上述文件计算 sha256 并逐条比对；
22 号门禁仅检查文件大小与 ONNX 形状，不检查内容。

## 四、与现有数据的关系

| 数据 | 用途 | 是否进入训练集 |
|---|---|---|
| `data/processed/`（271 类病害，10165 张） | 病害图像识别 | 否（分类任务，非物候） |
| `data/pest/`（IP102，102 类虫害） | 虫害检测 | 否（既有检测集） |
| `data/pest_cls_raw/`（IP102 分类集，75222 张） | 稀有类 copy-paste 增广 | 间接：仅通过 `scripts/37` 三重过滤的样本以增广图形式进 train |
| `data/pest24/`（Pest24 零物体图） | 纯负样本 | 未获取 |
| `data/farm/farm.db` 的 `harvest_records` | 收获日预测 | 是，仅取数值特征 |
| `data/farm/farm.db` 的 `daily_temp` | 积温计算 | 间接（特征由其算出） |
| 上述 15 个外部数据集 | 提升模型精度 | 是（不含个人信息） |

## 五、当前能否训练

运行 `python scripts/24_train_harvest_model.py` 会先打印当前样本数。不足 30 条时不会强制训练，
会提示差额并以退出码 0 结束（属等待状态，非错误）。

如需验证链路，可用 `--force` 强制训练，但产出的模型不可信，不得写入任何报告。
