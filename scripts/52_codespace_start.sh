#!/usr/bin/env bash
# Codespaces 每次启动时执行：在后台拉起后端，并保证幂等。
#
# 用 setsid + 重定向把进程从当前 shell 会话里摘出来 —— postStartCommand 返回后，
# 前台子进程会被回收，直接 `python ... &` 有可能一起被带走。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if curl -fsS --max-time 3 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
  echo "[start] 服务已在运行，跳过"
  exit 0
fi

mkdir -p outputs/logs
setsid python scripts/20_run_backend.py --host 0.0.0.0 --port 8000 --no-browser \
  < /dev/null > outputs/logs/codespace_server.log 2>&1 &

echo "[start] 已在后台启动（pid $!）"
echo "[start] 日志：outputs/logs/codespace_server.log"
echo "[start] 端口 8000 就绪后，在「端口」面板把它设为 Public 即可获得外部地址"
