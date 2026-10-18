#!/usr/bin/env bash
# Codespaces 首次创建时执行：铺运行时资源并安装依赖。
#
# 资源铺平的方式与 deploy/hf/Dockerfile 的 COPY 一致，因此应用代码与配置无需修改。
# 依赖安装顺序也与该 Dockerfile 相同：先装 CPU 版 torch，再装其余依赖 ——
# 否则 ultralytics 会把 CUDA 版 torch 一起拉下来，体积多出 2 GB 且安装更慢。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[setup] 铺运行时资源"
# **必须合并复制，不能「目录存在就跳过」。**
# 仓库里本来就跟踪了 `data/pest_label_map.json`，所以全新 clone 之后 `data/` 是存在的；
# 早先按目录判断，结果其余文件（label_map.json、processed、pest/images…）全都没铺进去，
# 服务起来后报「缺少标签映射」。这里统一用 `cp -r src/. dst/` 合并，重复执行也安全。
for d in data knowledge models; do
  if [ -d "runtime_assets/$d" ]; then
    mkdir -p "$d"
    cp -r "runtime_assets/$d/." "$d/"
    echo "  runtime_assets/$d -> $d（合并）"
  fi
done
mkdir -p outputs/logs outputs/reports outputs/figures data/farm_images

# 铺完立刻校验关键文件，缺一个就明确报错，而不是让服务带着 degraded 起来
for f in data/label_map.json \
         models/onnx/inference_model.onnx \
         models/onnx/pest_yolo.onnx \
         knowledge/vectorstore/chroma.sqlite3; do
  if [ ! -e "$f" ]; then
    echo "[setup] **缺少 $f**，运行时资源不完整" >&2
    exit 1
  fi
done
echo "[setup] 关键文件齐全"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  .env 已从模板创建（未填写密钥）"
fi

echo "[setup] 安装 CPU 版 torch"
python -m pip install --upgrade pip
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

echo "[setup] 安装其余依赖"
python -m pip install -r requirements.txt
python -m pip install -r requirements-rag.txt

echo "[setup] 完成"
