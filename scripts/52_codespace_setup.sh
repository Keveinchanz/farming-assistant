#!/usr/bin/env bash
# Codespaces 首次创建时执行：铺运行时资源并安装依赖。
#
# 资源铺平的方式与 deploy/hf/Dockerfile 的 COPY 一致，因此应用代码与配置无需修改。
# 依赖安装顺序也与该 Dockerfile 相同：先装 CPU 版 torch，再装其余依赖 ——
# 否则 ultralytics 会把 CUDA 版 torch 一起拉下来，体积多出 2 GB 且安装更慢。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[setup] 铺运行时资源"
for d in data knowledge models; do
  if [ ! -e "$d" ]; then
    cp -r "runtime_assets/$d" "$d"
    echo "  runtime_assets/$d -> $d"
  else
    echo "  $d 已存在，跳过"
  fi
done
mkdir -p outputs/logs outputs/reports outputs/figures data/farm_images
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
