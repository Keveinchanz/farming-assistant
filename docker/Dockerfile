# ============================================================================
# 乡村农技多方言智能服务平台 —— 服务端镜像（任务 6.3 可选部署方式）
#
# 说明：
#   · 镜像内只跑「推理服务」，不包含训练。训练请在本机 GPU 环境执行，
#     把 models/onnx/inference_model.onnx 与 data/、knowledge/ 一起挂载进来即可。
#   · torch 单独装 CPU 版（约 200MB），避免把 2GB+ 的 CUDA 版拖进镜像。
#     后端推理走 onnxruntime CPU，实测单图 20~30ms，无需 GPU。
#
# 构建与运行：
#     docker build -f docker/Dockerfile -t rural-agri-platform .
#     docker run --rm -p 8000:8000 --env-file .env rural-agri-platform
#
# 或直接用 compose（见 docker/docker-compose.yml）
# ============================================================================

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# libgomp1 是 onnxruntime / torch CPU 版的运行期依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# 先装 CPU 版 torch（体积最大的一层，单独放前面便于缓存）
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

# 再装其余依赖
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 拷贝项目代码与运行期需要的产物
COPY src/ ./src/
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY start.ps1 .env.example README.md ./

# 数据与模型通过挂载卷提供（体积大且经常更新，不适合烤进镜像）
#   /app/data        阶段 1 产物
#   /app/knowledge   知识库与词库
#   /app/models      训练好的权重与 ONNX
#   /app/Sample      原始数据集（仅供 /media 展示知识卡片配图）
RUN mkdir -p data knowledge models Sample outputs/logs outputs/reports outputs/figures

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 不自动开浏览器；容器里监听 0.0.0.0 才能被宿主访问
CMD ["python", "scripts/20_run_backend.py", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
