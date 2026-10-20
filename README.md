# 多方言 AI 农技与农田数字孪生平台

面向基层农户的农技服务平台。农户拍摄作物照片或用方言描述症状，平台输出病虫害诊断、防治方案与用药提醒，
并提供各省当季病虫害地图、知识图书库与惠农政策查询。

在线演示：<https://comfortable-tuesday-remove-islamic.trycloudflare.com>
（由 Cloudflare 临时隧道提供，仅在演示实例运行期间有效。）

---

## 一、功能概览

| 模块 | 能做什么 |
|---|---|
| 3D 首页沙盘 | 以中国地图形式展示各省当季病虫害 TOP3，支持四季切换，点击可直接跳转到对应知识卡片 |
| 智能诊断 | 图片、方言语音、文字描述三路输入，融合后输出诊断结论、防治方案、用药提醒与知识库引用 |
| 知识图书库 | 399 张知识卡片，覆盖病虫害防治与作物养护，支持关键词检索和语音检索 |
| 惠农政策 | 收录农业农村部官网的惠农政策原文与摘要，可按类型、时间、关键词筛选 |
| 我的农田 | 田块管理、积温生长模拟、3D 长势、天气与农事提醒、收获期区间预测 |
| 方言语音 | 8 种方言的语音识别与合成，贯穿诊断、检索、记账等场景 |
| 账号体系 | 注册登录、资料与密码管理、账号注销，所有农田数据按用户隔离 |

---

## 二、功能详解

### 1. 首页 · 病虫害地图

![首页](docs/screenshots/home.jpg)

以三维中国地图为底，按省份显示当季高发的三种病虫害。地图支持春夏秋冬四季切换，
切换时省份底色、省名颜色与风险图例同步变化。悬停省份弹出卡片，点击 TOP3 中的任意一条，
直接跳转到知识图书库并展开该条详情。

底图的地理数据随前端一起打包，首屏不请求外部服务，打开即可看到完整地图。

### 2. 智能诊断

![智能诊断](docs/screenshots/diagnose.png)

诊断页提供三种输入方式，可以单独使用，也可以组合提交：

- **拍照或上传图片**：支持调用摄像头实时取景，也可以从相册或文件中选择
- **方言语音**：直接说出作物和症状，系统自动转写
- **文字描述**：输入补充信息

系统将三条线索合并后给出结论。报告包含主诊断结论与置信度、防治方案分节正文、用药提醒、
知识库引用来源，以及判断依据。若上传的照片中检测到害虫，虫害会成为本次的主结论，
防治方案与用药提醒同时切换到虫害对应的内容。

诊断结果可以一键记入「我的农田」，自动写入该田块的病虫害档案。

### 3. 知识图书库

![知识图书库](docs/screenshots/library.jpg)

共 399 张卡片，分为三类：

| 分类 | 数量 | 内容 |
|---|---|---|
| 病害防治知识 | 250 | 每种病害的病原类型、症状特征、防治要点 |
| 虫害防治知识 | 102 | 每种害虫的取食方式类群、形态特征、防治要点 |
| 农作物养护知识 | 47 | 按作物大类的栽培与养护要点 |

列表支持关键词检索和语音检索，方言识别结果会自动填入搜索框。点击卡片打开全屏详情，
按分节展示正文与配图。

### 4. 惠农政策

![惠农政策](docs/screenshots/policy.png)

收录 10 篇农业农村部官网的政策原文，自动提取摘要。页面支持按政策类型、发布时间和关键词筛选，
原文与摘要均在站内查看，不跳转外部链接。

### 5. 我的农田

![我的农田](docs/screenshots/farm.png)

「我的农田」把一次性诊断延伸为全周期管理，页面左侧是三维田块视图，右侧是信息面板。

进入田块后的 3D 长势：

![进入田块后的 3D 长势](docs/screenshots/farm3d.jpg)

- **田块管理**：新建、编辑、删除田块，记录作物、播种日期、位置
- **三维长势**：根据积温进度渲染植株形态，包括株高、叶片数、叶色、穗的形态，支持旋转与缩放
- **生长模拟**：按有效积温模型推算当前生育阶段，并给出该阶段的农事提示
- **收获预测**：输出收获日期的区间而不是单一日期，并说明区间是如何得出的
- **天气与提醒**：今日与未来两天的天气，以及当天需要注意的农事
- **语音记账**：说一句话即可记录农事，系统解析出田块、农事类型与数量，确认后才写入

新注册的账号会自动获得三块覆盖不同作物与生长阶段的示例田，便于直接体验完整功能。

### 6. 方言语音交互

支持普通话、粤语、四川话、河南话、东北话、湖南话、陕西话、台湾话共 8 种。

语音识别采用方言大模型，一个模型覆盖多种方言，无需用户手动切换。语音合成按方言选择对应的发音人，
诊断结论、政策摘要均可方言播报。语音输入在识别后仅做农业术语匹配，不改写原意。

### 7. 账号与数据隔离

用户名加密码注册登录，密码经 PBKDF2 加盐哈希存储。会话使用 HttpOnly Cookie，前端不接触令牌，
有效期 7 天。所有田块、农事记录与诊断档案按用户隔离，跨用户访问统一返回 404。
个人中心支持修改昵称、修改密码与注销账号。

---

## 三、快速开始

环境要求：Python 3.11（本项目验证版本），Windows、macOS、Linux 均可。
仓库已包含推理模型、知识库与前端资源，无需另外下载模型。

以下命令均在项目根目录执行。

### 方式一：使用 conda

```powershell
# 首次使用先创建环境
conda create -n az python=3.11 -y
conda activate az

# 把仓库中的运行时资源铺到程序读取的位置，只需执行一次
python scripts\53_prepare_runtime.py

# 安装依赖；requirements-rag.txt 是知识库检索所需，约 1 至 2 GB
python -m pip install -r requirements.txt
python -m pip install -r requirements-rag.txt

# 启动
.\start.ps1
```

### 方式二：不使用 conda

```powershell
# 先确认 Python 版本；若不是 3.11，用 py -3.11 指定解释器
python --version

python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 若提示「无法加载文件，因为在此系统上禁止运行脚本」，先放开当前会话的执行策略
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

python -m pip install --upgrade pip

# 只运行、不训练时先装 CPU 版 torch，避免 pip 拉取约 2 GB 的 CUDA 版本
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

python -m pip install -r requirements.txt
python -m pip install -r requirements-rag.txt

python scripts\53_prepare_runtime.py
.\start.ps1
```

macOS 或 Linux 下，激活命令为 `source .venv/bin/activate`，启动命令换成
`python scripts/20_run_backend.py`。

### 方式三：环境已就绪

```powershell
python scripts\20_run_backend.py
```

启动后：

| 入口 | 地址 |
|---|---|
| 前端首页 | http://127.0.0.1:8000/ |
| 我的农田 | http://127.0.0.1:8000/farm.html |
| 接口文档 | http://127.0.0.1:8000/docs |
| 服务状态 | http://127.0.0.1:8000/api/health |

演示账号：`user`，密码 `nihao123`。每个部署实例在数据库初始化时都会自动创建该账号，
登录后可直接查看三块示例田；页面本身仍然要求登录或注册，普通用户注册不受影响。

首次启动会自动创建农田数据库。`start.ps1` 会依次检查解释器、依赖与数据产物并打印每一步结果；
若提示「数据集划分缺失」之类的信息，那是训练期产物，不影响页面功能。

`.env` 不存在时，`start.ps1` 会从 `.env.example` 复制一份模板。

---

## 四、使用指南

### 诊断一次病虫害

1. 打开「智能诊断」，上传一张作物照片，或按住麦克风用方言描述症状
2. 补充一句文字描述，例如「叶片上有褐色斑点，边缘发黄」
3. 提交后查看结果：主结论、置信度、防治方案、用药提醒与知识库引用
4. 需要留档时，点击「记入我的农田」并选择田块

若照片中有害虫，页面会在「先做这一步」之后显示虫害区块，列出检测框与对应的防治建议。

### 新建一块田

1. 注册或登录后进入「我的农田」
2. 点击田块区域的「新建田块」，填写作物、播种日期与位置
3. 保存后左侧三维视图与右侧信息面板会同步刷新
4. 日常可以用语音记账：「昨天给水稻施了 20 斤尿素」，解析后确认即可

### 查询一条政策

1. 打开「惠农政策」
2. 按类型或时间筛选，或在搜索框中输入关键词
3. 点击条目查看原文与摘要

### 检索一条病害知识

1. 打开「知识图书库」
2. 在搜索框中输入病害名或作物名，也可以点击麦克风用方言检索
3. 点击卡片查看分节正文、病原类型与配图

---

## 五、接口一览

统一响应包为 `{"code": 0, "msg": "ok", "data": {...}}`，`code != 0` 时 `msg` 为中文错误说明。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/diagnose/image` | 上传图片，返回 Top-5 候选 |
| POST | `/api/diagnose/multimodal` | 图片、语音、文字三路融合诊断 |
| POST | `/api/diagnose/ask` | 针对诊断结果追问 |
| GET | `/api/library` | 知识卡片列表（分类、检索、分页） |
| GET | `/api/library/{id}` | 卡片详情 |
| GET | `/api/map/pest_top3` | 各省 TOP3 病虫害，按季节筛选 |
| GET | `/api/policy/list` | 政策列表，按类型、时间、关键词筛选 |
| GET | `/api/dialect/list` | 方言列表 |
| POST | `/api/voice/tts` | 文本转方言语音 |
| POST | `/api/voice/asr` | 语音转文本 |
| GET | `/api/health` | 各能力可用状态 |
| POST | `/api/admin/reload` | 清缓存重载模型与数据，仅管理员可用 |

「我的农田」相关接口需要登录：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（自动登录并预置示例田） |
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 退出登录 |
| GET | `/api/auth/status` | 当前登录状态 |
| GET / PATCH | `/api/auth/me` | 我的资料 |
| POST | `/api/auth/change_password` | 修改密码 |
| DELETE | `/api/auth/me` | 注销账号 |
| GET / POST | `/api/farm/fields` | 田块列表 / 新建田块 |
| PATCH / DELETE | `/api/farm/fields/{id}` | 修改 / 删除田块 |
| GET / POST | `/api/farm/logs` | 农事记录 |
| GET | `/api/farm/today` | 天气、生长、收获、提醒、历史与叙述 |
| GET | `/api/farm/growth/{id}` | 积温与生育阶段 |
| GET | `/api/farm/model/{id}` | 3D 植株参数 |
| POST | `/api/farm/attach_diagnosis` | 诊断入档 |
| POST | `/api/farm/voice_parse` | 解析一句话，不落库 |
| POST | `/api/farm/voice_reparse` | 修改文字后重新解析 |
| POST | `/api/farm/harvest_mark` | 记录一次收获 |
| POST | `/api/farm/reminder_snooze` | 某条提醒暂缓两天 |

`/api/library?category=` 取 `pest`（病害 250 张）、`insect`（虫害 102 张）、`care`（养护 47 张），
留空返回全部 399 条。`pest` 这个键存放的是病害，沿用旧名以保持首页的深链。

---

## 六、模型效果

### 病害分类

| 指标 | 数值 |
|---|---|
| Top-1 准确率 | 0.8975 |
| Top-3 准确率 | 0.9606 |
| Top-5 准确率 | 0.9777 |
| macro P / R / F1 | 0.6686 / 0.7411 / 0.6917 |
| weighted P / R / F1 | 0.8563 / 0.8975 / 0.8719 |

指标在 761 条测试样本上测得。数据集类别分布不均衡，10 个大类占 7555/10165 张，
261 个小类各 10 张，因此 weighted 指标高于 macro 指标，评估模型时应以 macro 与 Top-3 为准。

### 虫害检测

| 指标 | 验证集 | 测试集 |
|---|---|---|
| mAP50 | 0.6309 | 0.6102 |
| mAP50-95 | 0.4087 | 0.3820 |
| 精确率 | 0.5985 | 0.5431 |
| 召回率 | 0.6132 | 0.6297 |
| 覆盖类别 | 97 / 102 | 96 / 102 |

在 1519 张验证图上，整图识别正确率（取最高置信度的检测框，判断其类别是否命中该图真值）
为 0.7564，Top-5 候选命中率为 0.8381。

导出 ONNX 时执行了四道对齐校验，逐像素预处理一致、导出最大绝对误差 1.53e-03、
后处理类别一致、端到端 20 张图最差 IoU 0.9981。

---

## 七、部署

### GitHub Codespaces

仓库已包含 `.devcontainer` 配置。创建 Codespace 后会自动安装依赖并在后台启动服务，
首次约 8 至 15 分钟。服务在 8000 端口，转发该端口即可访问。

### Docker

```bash
docker build -f docker/Dockerfile -t tianyu-zhinong .
docker run --rm -p 8000:8000 --env-file .env tianyu-zhinong
```

镜像以 `python:3.11-slim` 为基础，安装 CPU 版 torch 与运行依赖，推理走 ONNX Runtime，不需要 GPU。
`deploy/hf/Dockerfile` 是适配 Hugging Face Spaces 的版本。

---

## 八、目录结构与技术栈

```
insert\
├─ frontend\          九个静态页面与本地打包的 ECharts / Three.js / 地图数据
├─ backend\app\       FastAPI 服务
├─ src\               可复用库：推理、RAG、语音、农田引擎、内容生成
├─ configs\           路径、阈值、作物词表、方言参数
├─ data\              归档图像、划分、标签表、虫害检测集、农田库
├─ knowledge\         农业词库、知识卡片、RAG 文档与向量库
├─ models\            推理模型与模型元数据
├─ scripts\           启动、构建、训练、导出脚本
├─ tests\             单元测试
├─ docs\              数据集说明与论文口径
└─ .devcontainer\     Codespaces 配置
```

依赖方向为 `scripts/` → `src/` ← `backend/`。`src/` 不引用 `backend/`，训练与线上推理共用同一份实现。

技术栈：FastAPI、ONNX Runtime、ChromaDB、YOLOv8、ResNet50、Three.js、ECharts。

---

## 九、关键脚本

| 脚本 | 用途 |
|---|---|
| `20_run_backend.py` | 启动后端服务 |
| `23_init_farm_db.py` | 初始化农田数据库（服务启动时也会自动执行） |
| `07_build_library.py` | 重建知识卡片与 RAG 切分源 |
| `08_build_rag_index.py` | 重建向量索引 |
| `10_train_resnet50.py` | 训练病害分类模型 |
| `13_export_onnx.py` | 导出病害分类 ONNX |
| `16_download_ip102.py` | 下载虫害检测数据集 |
| `17_train_pest_yolo.py` | 训练虫害检测模型 |
| `18_evaluate_pest_yolo.py` | 评估虫害检测 |
| `19_export_pest_onnx.py` | 导出虫害检测 ONNX |

---

## 十、常见问题

**问：注册后为什么自动有三块田？**
这是注册时创建的示例田，覆盖水稻分蘖期、玉米抽穗期与油菜成熟期，用于直接体验三维长势与积温功能。
属于演示数据，可以改名或删除。

**问：收获预测为什么显示的是积温法？**
收获模型需要全平台累计 30 条以上的收获记录才具备训练条件。记录不足时使用积温法推算区间，
这是设计状态。记录足够后运行 `scripts\24_train_harvest_model.py`，服务会自动切换到模型预测。

**问：访问别人的田块为什么返回 404？**
跨用户访问统一返回 404 而非 403，避免暴露「该记录存在但不属于当前用户」。删除他人田块同理，
对方数据不受影响。

**问：页面上出现没有内容的空白框？**
这是带有 `hidden` 属性但被作者样式覆盖的元素。`base.css` 中已有全局的
`[hidden] { display: none !important }` 兜底。若需要显示元素，必须显式设置 `hidden = false`。

**问：`/api/health` 显示虫害模型未就位，但诊断页能出检测框？**
`main.py` 在组装检测器时读取的属性名与实际实现不一致，会抛出 `AttributeError` 并被捕获，
导致状态显示不可用。现由冒烟测试核对状态与模型文件是否一致。若再次出现，查看
`pest_detector` 的 `error` 字段，其中包含原始异常。

---

## 十一、数据与许可

- 病害分类数据集：PDD271，https://github.com/liuxindazz/PDD271
- 虫害检测数据集：IP102，https://github.com/xpwu95/IP102
- 检测模型训练超参参考：PestYOLO，https://github.com/jianxi-Erin/PestYOLO
- 农业病虫害图库：IDADP，http://www.icgroupcas.cn/website_bchtk/index.html
- 政策原文：农业农村部官网，共 10 篇，清单见 `data/policy_list.json`

病虫害训练使用的 `ultralytics` 采用 AGPL-3.0，其传染性覆盖网络服务。本项目将其限制在
`scripts/17~19` 三个脚本中，服务进程不引用它，线上只加载导出的 ONNX。如需闭源商用，
请先评估该许可的影响，或改用非 AGPL 的检测框架重新训练。

IP102 与 PestYOLO 均限定研究用途，相关数据与权重仅供学习与研究。

诊断结果与防治建议仅供参考，不构成用药指令。具体农药品种、剂型与用量，
请以当地植保部门指导、农药登记标签和最新禁限用名录为准。
