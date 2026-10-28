# 田头农技台 —— Hugging Face Spaces 运行镜像
#
# 与 docker/Dockerfile 的区别只有三点：
#   1. 资源**烤进镜像**（`runtime_assets/`），不再靠挂载卷 —— Spaces 没有免费持久卷；
#   2. **构建期预下载** text2vec 嵌入模型 —— 否则冷启动第一次检索要等它下 400 MB；
#   3. 端口沿用 8000，由 Space 的 `app_port: 8000`（见 README.md 顶部）声明。
#
# 镜像里**不含**训练用的东西：.pt 权重、IP102 数据集、pest_cls_raw、phenology 全部不进。
# 实测 `runtime_assets/` 只有 175 MB，最大单文件 92 MB。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    HF_HOME=/app/models/pretrained/hf

WORKDIR /app

# libgomp1 是 onnxruntime / torch CPU 版的运行期依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# 先装 CPU 版 torch（最大的一层，单独放前面便于缓存）
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

COPY requirements.txt requirements-rag.txt ./
RUN pip install -r requirements.txt && pip install -r requirements-rag.txt

# 代码
COPY src/ ./src/
COPY backend/ ./backend/
COPY skills/ ./skills/
COPY frontend/ ./frontend/
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY README.md .env.example ./

# 运行期资源（175 MB）
COPY runtime_assets/data ./data
COPY runtime_assets/knowledge ./knowledge
COPY runtime_assets/models ./models

# 构建期把嵌入模型下进镜像。**失败不致命**：`src/rag/embeddings.py` 首次检索时还会再试，
# 只是冷启动会慢一分钟。这里先试 hf-mirror（国内可达），再回退到官方源。
RUN mkdir -p /app/models/pretrained/hf /app/outputs/logs /app/outputs/reports \
             /app/outputs/figures /app/data/farm_images \
    && (HF_ENDPOINT=https://hf-mirror.com python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('shibing624/text2vec-base-chinese', \
cache_folder='/app/models/pretrained/hf/sentence_transformers')" \
        || python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('shibing624/text2vec-base-chinese', \
cache_folder='/app/models/pretrained/hf/sentence_transformers')")

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 不自动开浏览器；容器里监听 0.0.0.0 才能被 Space 的网关访问
CMD ["python", "scripts/20_run_backend.py", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
