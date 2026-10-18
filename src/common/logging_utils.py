"""日志配置：控制台 + 可选文件双通道，UTF-8，Windows 下不乱码。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logger(
    name: str = "agri",
    level: str = "INFO",
    log_file: str | Path | None = None,
) -> logging.Logger:
    """取一个配置好的 logger；重复调用不会叠加 handler。"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(formatter)
    # Windows 控制台默认 GBK，显式转 UTF-8 避免中文路径/类别名报 UnicodeEncodeError
    if hasattr(stream.stream, "reconfigure"):
        try:
            stream.stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 某些终端不支持 reconfigure，忽略即可
            pass
    logger.addHandler(stream)

    if log_file is not None:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p, encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def silence_matplotlib_cjk() -> None:
    """让 matplotlib 正确渲染中文（混淆矩阵的 271 个中文类名需要）。"""
    import matplotlib

    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False
