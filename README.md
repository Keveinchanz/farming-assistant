# 多方言 AI 农技与农田数字孪生平台

面向基层农户的农技服务平台。农户拍摄作物照片或用方言描述症状，平台输出病虫害诊断、防治方案与用药提醒，
并提供各省当季病虫害地图、知识图书库与惠农政策查询。

本项目完整实现《乡村农技多方言智能服务平台 Python 开发任务清单》的六大阶段 33 项任务。

在线演示：<https://comfortable-tuesday-remove-islamic.trycloudflare.com>
（由 Cloudflare 临时隧道提供，仅在演示实例运行期间有效。）

---

## 一、快速开始

```powershell
# 1. 激活环境（本项目使用 conda 环境 az）
conda activate az
cd /d "D:\code file\insert"

# 2. 安装依赖（首次）
python -m pip install -r requirements.txt

# 3. 一键启动（会先体检数据与模型，再拉起服务并打开浏览器）
.\start.ps1

# 也可以不经过 start.ps1 直接启动
python scripts\20_run_backend.py
```

启动后：

| 入口 | 地址 |
|---|---|
| 前端首页（中国地图） | http://127.0.0.1:8000/ |
| 接口文档（Swagger） | http://127.0.0.1:8000/docs |
| 服务与各能力状态 | http://127.0.0.1:8000/api/health |

> `.env` 不存在时，`start.ps1` 会从 `.env.example` 复制一份模板。

---

## 二、项目结构

```
insert\
├─ Sample\                     原始只读样本数据集（PDD271，271 类 / 10165 张）
├─ configs\                    静态配置：路径、阈值、作物词表、方言参数、训练超参
├─ data\                       阶段 1 产物（可由脚本重新生成）
│   ├─ processed\dataset\      归档图像，按「作物大类 / 病虫害名称」两级目录存放
│   ├─ splits\                 8:1:1 划分
│   ├─ label_map.json          271 类病害的全局标签表
│   ├─ province_pest_top3.json 各省 × 季节 TOP3（演示数据）
│   ├─ policy_list.json        惠农政策原文与摘要
│   ├─ pest\                   IP102 虫害检测集（YOLO 格式，102 类）
│   ├─ pest_label_map.json     102 类虫害中英文名，与上面的病害标签表相互独立
│   ├─ farm\farm.db            农田库：账号、田块、农事与收获记录
│   └─ farm_images\            诊断入档图片
├─ knowledge\                  农业词库、399 个 RAG 文档、知识卡片、ChromaDB 向量库
├─ models\                     训练权重、导出的 ONNX、模型元数据 registry.json
├─ outputs\                    训练日志、图表与评估报告
├─ src\                        可复用库：训练、推理、RAG、语音、农田引擎
├─ backend\app\                FastAPI 服务
├─ frontend\                   九个静态页面，以及本地打包的 ECharts / Three.js / 地图数据
├─ docs\                       论文口径与数据集说明
├─ scripts\                    薄入口脚本，只解析参数并调用 src/
├─ start.ps1                   一键启动
└─ .env.example                环境变量模板
```

依赖方向为 `scripts/` → `src/` ← `backend/`。`src/` 不引用 `backend/`，训练评估与线上推理共用
`src/infer/predictor.py`，两边的预处理与后处理保持一致。

`data/farm/farm.db` 是项目中唯一按用户划分、会被改写的数据，单独放在一个 SQLite 文件中，不纳入版本库。
清空 `data/farm/` 只影响账号与记录，其余功能不受影响。

---

## 三、脚本清单（对照任务编号）

| 脚本 | 任务 | 说明 |
|---|---|---|
| `00_download_datasets.py` | 1.1 | 数据集体检；IDADP 下载器（默认跳过） |
| `01_organize_data.py` | 1.1 | 整理为「作物大类 / 病虫害名称」二级目录 |
| `02_split_dataset.py` | 1.2 | 8:1:1 逐类分层划分 |
| `03_build_label_map.py` | 1.4 | 生成 `label_map.json` |
| `04_gen_province_pest.py` | 1.5 | 各省 × 季节 TOP3（演示数据，见第五节） |
| `05_fetch_policies.py` | 1.6 | 抓取 10 条官方政策原文并提取摘要 |
| `06_test_pipeline.py` | 1.3 / 2.1 / 2.2 | 自检：数据增强、Dataset、模型前向 |
| `07_build_library.py` | 4.5 | 生成知识图书库与 RAG 切分源 |
| `08_build_rag_index.py` | 4.4 | 构建 ChromaDB 向量索引 |
| `09_test_voice_llm.py` | 3.1–3.4 | 自检：词库、方言、语音、大模型 |
| `10_train_resnet50.py` | 2.3 / 2.4 | 训练 ResNet50，`--probe` 可先实测吞吐 |
| `11_train_vit.py` | 2.2 | 训练 ViT-B/16（可选） |
| `12_evaluate.py` | 2.5 | 测试集评估与混淆矩阵 |
| `13_export_onnx.py` | 2.6 | 导出 ONNX，含数值对齐校验 |
| `16_download_ip102.py` | 2.7.1 | 下载并整理 IP102 虫害检测数据集 |
| `17_train_pest_yolo.py` | 2.7.3 | 训练 YOLOv8n 检测，`--probe` / `--resume` 可用 |
| `18_evaluate_pest_yolo.py` | 2.7.4 | 虫害检测评估 |
| `19_export_pest_onnx.py` | 2.7.5 | 导出检测 ONNX，做四道对齐校验 |
| `20_run_backend.py` | 6.3 | 启动后端服务 |
| `21_smoke_test_api.py` | 6.1 / 6.2 | 接口冒烟测试（需服务已启动） |
| `22_verify_artifacts.py` | 6.2 | 交付物核对（不需要启动服务） |
| `23_init_farm_db.py` | 7.0.10 | 建农田库并执行迁移，幂等 |
| `24_train_harvest_model.py` | 7.3.4 | 训练收获日预测模型 |
| `25_warm_farm_cache.py` | 7.2.4 | 预热天气缓存 |
| `26_fetch_pesticide_registry.py` | 7.15 | 采集农药登记信息，建立「防治对象 → 登记农药」索引 |
| `27_build_admin_divisions.py` | 7.11 | 构建省、市、县三级行政区划与中心经纬度 |
| `28_build_china_geo.py` | 首页 | 生成 3D 沙盘底图数据（高程、河流、湖泊） |
| `29_download_phenology_datasets.py` | 7.3.4 | 下载并校验 ChinaRiceCalendar 物候日历 |
| `30_fetch_phenology_weather.py` | 7.3.4 | 按县质心拉取逐日气温，一县一文件 |
| `31_build_harvest_dataset.py` | 7.3.4 | 把一季作物摊成多个观察截止日，拼出训练样本表 |
| `32_train_harvest_external.py` | 7.3.4 | 三档留出训练与上线闸门 |
| `33_make_dev_cert.py` | 7.0.2 | 生成本地调试用的自签 https 证书 |

RAG 依赖约 1–2 GB（含中文向量模型），单独安装：

```powershell
python -m pip install -r requirements-rag.txt
python scripts\08_build_rag_index.py
```

索引覆盖全部 399 个文档，共 612 个片段。重建是增量的：脚本按片段内容计算 id，只补缺失的部分，
图书库新增虫害卡片后重跑一次即可，不会重算已有的旧卡片。

---

## 四、接口一览

统一响应包为 `{"code": 0, "msg": "ok", "data": {...}}`，`code != 0` 时 `msg` 为中文错误说明。

| 任务 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 4.2 | POST | `/api/diagnose/image` | 上传图片，ONNX 推理，返回 Top-5 候选 |
| 4.3 | POST | `/api/diagnose/multimodal` | 图片、语音、文字三路融合诊断 |
| 3.3 | POST | `/api/diagnose/ask` | 针对诊断结果追问 |
| 4.5 | GET | `/api/library` | 知识卡片列表（分类、检索、分页） |
| 4.5 | GET | `/api/library/{id}` | 卡片详情（分节正文与配图） |
| 4.6 | GET | `/api/map/pest_top3` | 各省 TOP3 病虫害，按季节筛选 |
| 4.7 | GET | `/api/policy/list` | 2026 年政策列表，按类型、时间、关键词筛选 |
| 3.4 | GET | `/api/dialect/list` | 八大方言列表 |
| 3.1 | POST | `/api/voice/tts` | 文本转方言语音 |
| 3.1 | POST | `/api/voice/asr` | 语音转文本 |
| — | GET | `/api/health` | 各能力可用状态 |
| — | POST | `/api/admin/reload` | 清缓存重载模型与数据，仅管理员（id=1）可用 |

我的农田模块（会话走 HttpOnly Cookie，前端不接触 token）：

| 任务 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 7.0.3 | POST | `/api/auth/register` | 注册（自动登录，并预置三条示例田） |
| 7.0.3 | POST | `/api/auth/login` | 登录（同 IP 5 次/分钟、同账号 10 次/小时限流） |
| 7.0.3 | POST | `/api/auth/logout` | 退出登录 |
| 7.0.3 | GET | `/api/auth/status` | 当前登录状态 |
| 7.0.3 | GET | `/api/auth/me` | 我的资料 |
| 7.0.3 | PATCH | `/api/auth/me` | 修改昵称与头像 |
| 7.0.3 | POST | `/api/auth/change_password` | 修改密码，改后所有会话失效 |
| 7.0.3 | DELETE | `/api/auth/me` | 注销账号，需再次验证密码 |
| 7.10 | GET | `/api/farm/fields` | 田块列表 |
| 7.10 | POST | `/api/farm/fields` | 新建田块 |
| 7.10 | PATCH | `/api/farm/fields/{id}` | 修改田块 |
| 7.10 | DELETE | `/api/farm/fields/{id}` | 删除田块及其记录 |
| 7.10 | GET | `/api/farm/logs` | 农事记录，倒序 |
| 7.10 | POST | `/api/farm/logs` | 记一笔农事 |
| 7.10 | GET | `/api/farm/today` | 一次返回天气、生长、收获、提醒、历史与叙述 |
| 7.10 | GET | `/api/farm/growth/{id}` | 积温与生育阶段 |
| 7.10 | GET | `/api/farm/model/{id}` | 3D 植株参数（12 项） |
| 7.10 | POST | `/api/farm/attach_diagnosis` | 诊断入档 |
| 7.10 | POST | `/api/farm/voice_parse` | 解析一句话，不落库 |
| 7.10 | POST | `/api/farm/voice_reparse` | 修改文字后重新解析 |
| 7.10 | POST | `/api/farm/harvest_mark` | 记录一次收获，同时保存训练用的数值特征 |
| — | POST | `/api/farm/reminder_snooze` | 某条提醒暂缓两天 |

`reminder_snooze` 不在任务清单 7.10 的接口表内。7.7 的交互要求「改天」暂缓两天，而暂缓状态需要持久化，
因此补充该接口。

鉴权边界：`/api/farm/*` 全部需要登录；`/api/diagnose/*`、`/api/library`、`/api/map/*`、`/api/policy/*`、
`/api/health` 公开。跨用户访问统一返回 404。

`/api/library?category=` 的取值为 `pest`（病害 250 张）、`insect`（虫害 102 张）、`care`（养护 47 张），
留空返回全部 399 条。`pest` 这个键存放的是病害，沿用旧名以保持首页地图的深链，虫害在 `insect` 下。
虫害卡片额外返回 `pest_type` 与 `latin`，病害卡片返回 `pathogen_type`，两套字段互不填充。

服务启动时会预热知识库，打开图书库页面时无需等待 JSON 解析。启动后访问 `/docs` 可直接在浏览器中试跑。

---

## 五、数据的来源与性质

| 数据 | 来源 | 性质 |
|---|---|---|
| 图像分类数据集 | PDD271 公开样本（`Sample/`） | 真实：271 类 / 10165 张 / 256×256 |
| 虫害检测数据集 | IP102 的 YOLO 格式转存（`phtrungagu321o/Yolov5_IP102`） | 真实：102 类 / 18976 张带框标注（train 15178 / test 3798，val 1519 张从 train 分层抽出） |
| 惠农政策 | 农业农村部官网 10 个链接 | 真实：抓取原文并提取摘要，可回溯 |
| 各省 TOP3 病虫害 | 各省作物分布与病原季节性规律推导 | 演示数据：官方只有全国维度趋势，无逐省逐季明细，文件内标注 `demo_data: true` |
| 知识卡片正文 | 按病原类型、取食方式类群或作物类别套用的规则模板 | 演示内容：每张卡片带 `content_source` 字段，前端显示内容来源 |
| 模型指标 | 本项目在测试集上的实测结果 | 真实，见第六节 |

知识图书库共 399 张卡片：病害 250 张，虫害 102 张，养护 47 张。三者的正文模板分属三套口径，
统一由 `content_source` 字段标注为演示内容。虫害卡片的文字不是官方植保手册原文，
用药描述需结合当地植保部门指导核对。

虫害卡片配图取自 IP102 数据集样图，通过 `/media-pest/` 挂载。其中 5 个类别在三个划分中均无实例，
卡片不显示封面；另有 2 类（灰象甲、橘瘿蚊）缺少可靠的拉丁学名，卡片留空。前端支持无封面展示。

替换演示数据不影响代码：把 `data/province_pest_top3.json` 换成真实的逐省数据后，接口与前端无需改动。

---

## 六、模型训练与评估结果

### 病害分类（PDD271 / ResNet50）

训练配置：ResNet50，224×224，batch 32，AMP，AdamW（骨干 1e-4、分类头 1e-3），
余弦退火加 3 epoch warmup，label smoothing 0.1，WeightedRandomSampler，早停 patience 7。

实测结果（RTX 5060 Laptop 8GB，测试集 761 条）：

| 指标 | 数值 |
|---|---|
| 训练轮次 | 22 epoch 触发早停（最佳 epoch 15），约 10.7 分钟 |
| 峰值显存 | 1.74 GB / 7.96 GB |
| Top-1 准确率 | 0.8975 |
| Top-3 准确率 | 0.9606 |
| Top-5 准确率 | 0.9777 |
| macro P / R / F1 | 0.6686 / 0.7411 / 0.6917 |
| weighted P / R / F1 | 0.8563 / 0.8975 / 0.8719 |

应以 macro 指标为准。本数据集严重不均衡，10 个大类占 7555/10165 张，261 个小类各 10 张，
weighted 指标会被长尾大类拉高约 20 个百分点。训练时启用 WeightedRandomSampler 也是同一原因：
不加权时模型只要全部预测成那 10 个大类就能得到 74% 的准确率。

已知局限：多数类别在测试集中只有 1 张样本（10 张的类按 8:1:1 划分只能分到 1 张），
单类 precision 与 recall 非 0 即 1，波动很大，应按作物大类或 Top-3 命中率解读。
完整报告见 `outputs/reports/eval_resnet50.json`。

### 虫害目标检测（IP102 / YOLOv8n）

训练配置按 PestYOLO 的 `train.py` 复现：yolov8n，640×640，batch 16，30 epoch，AMP。
该链路与病害分类使用两套独立的标签空间：此处为 102 类目标检测，输出边界框；
病害为 271 类单标签分类，输出概率。两者互不换算。

实测结果（RTX 5060 Laptop 8GB）：

| 指标 | 数值 |
|---|---|
| 训练轮次 | 30 epoch 跑满，约 49.5 分钟 |
| 峰值显存 | 2.53 GB / 7.96 GB |
| 逐轮 mAP50 最佳 | 0.5062（第 30 轮，仍在上升） |
| val 评估 | mAP50 0.5025 / mAP50-95 0.3214 / P 0.4785 / R 0.5459 |
| test 评估 | mAP50 0.4982 / mAP50-95 0.3118 / P 0.5166 / R 0.5015 |
| val 覆盖类别 | 97 / 102（test 96 / 102） |
| 导出 ONNX | 12.98 MB，opset 17，静态 640×640，单文件 |

训练日志中的 0.5062 是第 30 轮，评估报告中的 0.5025 来自 `best.pt`。ultralytics 按 mAP50-95 挑选
`best.pt`，该指标的最高点在第 29 轮（0.3209），与 mAP50 的最高点不在同一轮。

30 轮尚未进入平台期：mAP50 从第 1 轮 0.0542 上升到第 30 轮 0.5062，第 10 轮为 0.4028，第 20 轮为 0.4799。

最弱的五类（val，只列有实例的类别）：苜蓿斑蚜 0.000（1 个实例）、康氏粉蚧 0.000（4）、
吹绵蚧 0.000（24）、芒果切叶象甲 0.000（5）、麦二叉蚜 0.009（4）。弱项集中在蚜虫与蚧类：
虫体小，在整叶照片中只有几十个像素，且长尾类样本少。

ONNX 导出做了四道对齐校验：

| 校验 | 结果 |
|---|---|
| A 预处理：自写 letterbox 与 ultralytics 对比，逐像素 | 20 张图 × auto∈{False,True} 全等 |
| B 导出保真：同一张量 torch 前向与 ONNX 前向对比 | 最大绝对误差 2.17e-03 |
| C 后处理：自写 numpy 与 `non_max_suppression` 对比 | 类别 id 全等、IoU 1.0000（仅 1 张图 1 个框） |
| D 端到端：自写全链路与 `YOLO.predict()` 对比 | 20 张图 20 个框、最差 IoU 0.9982（4 张无检出） |

C 的样本量小，D 覆盖 20 张图、跨 4 个尺度，并允许无检出情形参与统计，是主要的兜底依据。
完整报告见 `outputs/reports/eval_pest_yolo.json` 与 `eval_pest_yolo_test.json`。

---

## 七、验证方式

```powershell
# 1. 单元测试（不需要启动服务，140 项）
python tests\test_units.py

# 2. 交付物核对（不需要启动服务，131 项）
python scripts\22_verify_artifacts.py

# 3. 启动服务
python scripts\20_run_backend.py --no-browser

# 4. 接口冒烟测试（另开一个终端；断言条数随虫害模型是否就位浮动）
python scripts\21_smoke_test_api.py
```

我的农田模块的准备步骤均为幂等操作，也可以跳过，服务启动时会自动建库：

```powershell
python scripts\23_init_farm_db.py       # 建库 + 迁移（首次）
python scripts\25_warm_farm_cache.py    # 预热天气缓存（可选，演示前跑一次更流畅）
# 收获模型要等全平台累计 ≥30 条「我收了」之后才有意义：
python scripts\24_train_harvest_model.py
```

用公开数据集训练收获模型（可选，详见 `docs/datasets.md`）：

```powershell
python scripts\29_download_phenology_datasets.py                       # 下载 + 校验（含 sha256 钉值）
python scripts\30_fetch_phenology_weather.py --wait-quota              # 逐县拉气温，可断点续传；见下
python scripts\31_build_harvest_dataset.py                             # 拼训练样本（一季多个观察截止日）
python scripts\32_train_harvest_external.py                            # 先不加 --install，只看报告
```

`32` 号脚本按三档留出各训练一遍，以「留出湖北省」作为上线闸门：模型 MAE 至少比纯积温法低 1 天，
分位覆盖率落在 0.70–0.90，验证集不少于 500 行、50 组。通过并加 `--install` 才把模型放进 `models/farm/`；
未通过时只写入 `models/farm_staging/`，并逐条打印未通过的原因，退出码仍为 0。

首次实跑（仅 21 个县）未通过闸门，覆盖率 0.414，低于 0.70–0.90 的要求；MAE 21.08 天已低于基线
26.25 天。缺口来自训练样本只覆盖一个省造成的域偏移，完整数字见 `docs/datasets.md`。

虫害检测链路共四步，首次全量约 1.5 小时：

```powershell
python scripts\16_download_ip102.py      # 下载 + 整理（约 1 分钟，778 MB）
python scripts\17_train_pest_yolo.py     # 训练 30 轮（--probe 可先实测吞吐）
python scripts\18_evaluate_pest_yolo.py  # 评估（val）
python scripts\18_evaluate_pest_yolo.py --split test   # 评估（test，另存一份报告）
python scripts\19_export_pest_onnx.py    # 导出 ONNX（四道对齐校验全过才算成功）
```

单元测试覆盖那些容易静默出错的逻辑：作物名的最长前缀匹配（大白菜、猕猴桃、甘薯-紫薯-马铃薯）、
训练与推理共用同一份归一化常量、评估管线不含随机增强、8:1:1 划分不会掏空小样本类的训练集、
模拟实现与真实实现返回同一套字段、术语抽取不做改写。另有一类约束「同一能力只有一份实现」：
全项目的 `createScriptProcessor` 计数必须为 1 且位于 `recorder.js`；媒体采集只允许出现在
`recorder.js` 与 `camera.js` 两份能力原语中。

冒烟测试覆盖健康检查、方言列表、地图数据与季节切换、政策列表与筛选、图书库列表与检索、
问答接口、真实图片识别、多模态报告、虫害检测的三种状态（未启用、未检出、有检出），
以及虫害结果不得混入防幻觉的 candidates 闭集。虫害检测部分会分别用病灶图和 IP102 验证集原图
（`data/pest/images/val/IP016000663.jpg`）各跑一次，以覆盖「有内容」时的字段完整性。

浏览器端手工验收要点：

- 首页：地图打开即显示，首屏不发起 `/api` 与 `china.json` 请求；悬停省份时卡片跟随光标、
  移开即消失且不闪烁，点击后卡片停住。
- 首页：点击 TOP3 的第 2 条能跳转到对应卡片详情；四季切换时省份底色与按钮强调色变化，
  风险图例的五档颜色四季保持一致。
- 首页：首屏静止，不自动旋转或摆动；拖动与滚轮仍可改变视角，松手后停在原处。
- 诊断：三个输入区支持拖拽、拍摄与上传，录音有计时；提交后输入区隐藏，结果区出现。
- 诊断：有虫图片的主结论为虫害，标题写「诊断结果（虫害）」，检测框落在虫体上并与列表编号对应；
  无虫图片的主结论仍为病害，区块显示「未检测到虫害」。
- 诊断：摄像头权限被拒或没有可用设备时给出原因，并回落到文件选择器；关闭取景层后摄像头指示灯熄灭。
- 图书库：打开即显示卡片；分类计数为全部 399、病害 250、虫害 102、养护 47；
  卡片详情以全屏弹窗展示，ESC、遮罩与关闭按钮均可关闭。
- 图书库：病害卡片显示作物与病害字段，虫害卡片显示取食方式类群与拉丁学名，两类字段不混排；
  卡片封面统一为 4:3，无实例的类别显示灰色占位块。
- 图书库：搜索与语音检索可用，识别文本自动填入搜索框并返回结果。
- 政策：10 条政策与发布单位正常显示，页面无外链跳转。
- 导航：顶栏铺满整屏，五个入口为纯文字；窗口从 2560px 缩到 360px 时入口完整可见，
  900px 以下折成两行，页面不出现横向滚动条。
- 农田：注册后自动登录，三条示例田已预置；切换田块时 3D 形态、株高、叶数、积温与阶段同步变化。
- 农田：右侧七张卡片齐全；「干完了」「改天」「我收了」分别写入历史、暂缓提醒与收获记录；
  语音记账解析后可修改，未确认不入库。
- 农田：未登录访问 `/farm.html` 跳转登录页，登录后返回农田页。
- 账号：注册、登录、修改昵称、修改密码、注销全流程可用。

---

## 八、环境与依赖

- Python 3.11（conda 环境 `az`）
- PyTorch 2.14.0+cu130（由 conda 提供，`requirements.txt` 不锁定，以免覆盖 CUDA 版本）
- GPU：RTX 5060 Laptop 8GB；CPU 也可运行，训练较慢
- 关键依赖：torchvision、timm、albumentations、fastapi、uvicorn、onnx、onnxruntime、onnxscript、
  pandas、scikit-learn、requests
- 仅训练虫害检测时需要 `ultralytics` 与导出用的 `onnxslim`。服务进程不依赖它们，
  线上通过 `onnxruntime` 加载导出的 ONNX。

几个技术选择：

- ONNX 推理使用 CPU 版 `onnxruntime`。本机 torch 为 cu130，`onnxruntime-gpu` 通常滞后于 CUDA 13，
  单图 CPU 推理实测分类约 20–30 ms，满足使用需要。
- `ultralytics` 不进入服务进程。它是 AGPL-3.0，运行时依赖 torch，本项目只在 `scripts/17~19`
  三个脚本中使用。安装前的环境快照保存在 `outputs/reports/pip_freeze_before_pest.txt`。
  安装后建议运行一次 `22_verify_artifacts.py`，确认既有 ONNX 链路正常。
  ultralytics 默认同步匿名使用数据，本项目已通过 `settings.update({"sync": False})` 关闭。
- 录音不使用 `MediaRecorder`：它默认输出 webm/opus，两家的识别接口均不接收。前端改用 Web Audio API
  采集原始 PCM，重采样至 16 kHz 单声道后封装为 WAV 再上传。讯飞的方言大模型需要裸 PCM
  （`encoding=raw`），由后端按 RIFF chunk 定位 `data` 段并剥离文件头，不依赖固定的 44 字节偏移。

---

## 九、常见问题

**问：注册后为什么自动有三块田？**
这是注册时创建的示例田，名字中带「示例」，用于让新账号进入「我的农田」后能看到完整的 3D 长势、
天气与积温。三块田覆盖三个作物与三个生长阶段（分蘖期水稻、抽穗期玉米、成熟期油菜），
属于演示数据，可以改名或删除。

**问：收获预测为什么使用积温法而不是 XGBoost？**
收获模型的触发条件是全平台 `harvest_records` 累计不少于 30 条。新部署的平台通常达不到该数量，
因此演示时 `method=gdd_fallback` 是设计状态。记录足够后运行
`python scripts\24_train_harvest_model.py`，训练完成会清空模型缓存，服务无需重启即可切换。

**问：访问其他用户的田块返回 404，是否是接口问题？**
这是有意设计。跨用户访问统一返回 404 而非 403，因为 403 会暴露「该记录存在但不属于当前用户」。
删除同理：删除他人的田块返回 404，对方的数据不受影响。

**问：页面上出现没有内容的空白框？**
这是带有 `hidden` 属性但仍被渲染的元素。`hidden` 依赖浏览器默认样式中的 `[hidden] { display: none }`，
作者样式中的 `display` 会覆盖它，例如 `.notice { display: flex }`。`base.css` 中已加入
`[hidden] { display: none !important }` 作为兜底。相应地，需要显示元素时必须显式设置
`hidden = false`，只写 `innerHTML` 不足以让元素出现。

**问：`/api/health` 显示虫害检测模型未就位，但诊断页能出检测框？**
这是曾经出现过的缺陷：`main.py` 在组装 `pest_detector` 时读取了 `confidence_threshold` 属性，
而 `OnnxPestDetector` 上的属性名为 `conf_threshold`，导致该段代码抛出 `AttributeError`，
health 接口据此报告 `available: false`。现由冒烟测试核对 health 的可用性与 ONNX 文件是否在盘上一致，
单元测试则比对 detector 的返回键与后端 schema 的字段声明。若修改过属性名，可查看 `pest_detector`
的 `error` 字段，其中包含原始异常。

---

## 十、数据来源与许可

- PDD271 植物病害数据集：https://github.com/liuxindazz/PDD271 （公开样本部分）
- IP102 虫害检测数据集（YOLO 格式转存）：https://github.com/phtrungagu321o/Yolov5_IP102
  （原始数据集见 https://github.com/xpwu95/IP102 ）
- PestYOLO：https://github.com/jianxi-Erin/PestYOLO （仅参照其训练超参，未使用其代码）
- IDADP 农业病虫害研究图库：http://www.icgroupcas.cn/website_bchtk/index.html
  （需注册下载，`scripts/00_download_datasets.py` 支持传入直链）
- 全国农作物病虫害发生趋势预报：全国农业技术推广服务中心
- 惠农政策原文：共 10 篇，均来自农业农村部官网，逐篇抓取原文后自动提取摘要，清单见
  `data/policy_list.json`

许可提示：

- 训练与导出使用的 `ultralytics` 采用 AGPL-3.0，其传染性覆盖网络服务。本项目将其限制在
  `scripts/17~19` 三个脚本中，服务进程不引用它，线上只加载导出的 ONNX。
  如需闭源商用，请先评估该许可的影响，或改用非 AGPL 的检测训练框架重新训练。
- PestYOLO 仓库未附 LICENSE，README 注明仅供学习和研究使用。本项目只借鉴其训练超参。
- IP102 数据集同样限定研究用途。以上数据与权重仅供学习与研究，不建议直接用于生产环境。

免责声明：平台的诊断结果与防治建议仅供参考，不构成用药指令。具体农药品种、剂型与用量，
请以当地植保部门指导、农药登记标签和最新禁限用名录为准。
