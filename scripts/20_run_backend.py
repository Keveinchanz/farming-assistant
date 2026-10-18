#!/usr/bin/env python
"""任务 6.3 —— 启动后端服务（uvicorn）。

用法（PowerShell）：
    conda activate az
    cd /d "D:\\code file\\insert"
    python scripts/20_run_backend.py                 # 启动服务并自动打开浏览器
    python scripts/20_run_backend.py --no-browser    # 不打开浏览器
    python scripts/20_run_backend.py --port 8080
    python scripts/20_run_backend.py --reload        # 改代码自动重载（开发用）

    python scripts/20_run_backend.py --https --port 8443 --no-browser
        # 走自签 https，让**手机**也能调摄像头（见下）

启动后：
    前端首页    http://127.0.0.1:8000/
    接口文档    http://127.0.0.1:8000/docs
    服务状态    http://127.0.0.1:8000/api/health

关于 --https：
    浏览器的媒体采集接口只在**安全上下文**下存在 —— https，或 http 的
    localhost / 127.0.0.1。桌面用 127.0.0.1 本来就满足，不需要 https；
    但手机访问 http://<局域网IP>:8000 时 navigator.mediaDevices 是 undefined，
    诊断页的「调用摄像头拍摄」会回落到文件选择器。要手机也能取景就得 https。
    证书先跑 scripts/33_make_dev_cert.py 生成到 certs/（已在 .gitignore 里）。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.config import env                # noqa: E402
from src.common.logging_utils import setup_logger  # noqa: E402
from src.common.paths import PROJECT_ROOT         # noqa: E402

logger = setup_logger("agri.run")


def main() -> int:
    parser = argparse.ArgumentParser(description="启动乡村农技多方言智能服务平台后端")
    parser.add_argument("--host", default=str(env("HOST", "127.0.0.1")))
    parser.add_argument("--port", type=int, default=int(env("PORT", 8000, cast="int") or 8000))
    parser.add_argument("--reload", action="store_true", help="代码改动自动重载（开发模式）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--https", action="store_true",
        help="用 certs/dev.crt|key 起 https（手机调摄像头需要，先跑 scripts/33_make_dev_cert.py）",
    )
    args = parser.parse_args()

    import uvicorn

    # https 是「要了才给」：证书不存在就退回 http 照常启动，不能让服务起不来。
    ssl_kwargs: dict[str, str] = {}
    scheme = "http"
    if args.https:
        cert_file = PROJECT_ROOT / "certs" / "dev.crt"
        key_file = PROJECT_ROOT / "certs" / "dev.key"
        if cert_file.exists() and key_file.exists():
            ssl_kwargs = {"ssl_certfile": str(cert_file), "ssl_keyfile": str(key_file)}
            scheme = "https"
        else:
            logger.warning("找不到 certs/dev.crt 与 certs/dev.key —— 本次仍以 http 启动。")
            logger.warning("  想用 https（手机调摄像头需要）先跑：python scripts/33_make_dev_cert.py")

    url = f"{scheme}://{args.host}:{args.port}"
    logger.info("=" * 62)
    logger.info("乡村农技多方言智能服务平台 后端服务")
    logger.info("  首页      %s/", url)
    logger.info("  接口文档  %s/docs", url)
    logger.info("  服务状态  %s/api/health", url)
    if scheme == "https":
        logger.info("  证书      ！！自签的，浏览器首次会提示不受信任：选「高级 → 继续前往」")
    logger.info("=" * 62)

    if not args.no_browser:
        def _open() -> None:
            time.sleep(2.0)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "backend.app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=str(env("LOG_LEVEL", "info")).lower(),
        **ssl_kwargs,
    )
    return 0


if __name__ == "__main__":
    # Windows 下 uvicorn 的多进程/重载要求入口受 __main__ 保护
    raise SystemExit(main())
