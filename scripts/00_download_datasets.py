#!/usr/bin/env python
"""任务 1.1（数据获取部分）—— 数据集下载器：IDADP + PDD271。

设计：可插拔、默认跳过，不阻塞后续阶段。

· PDD271：完整版 220,592 张未公开，GitHub 仅放出每类约 10 张的公开样本。
          本仓库已把公开样本放在 Sample/ 目录，因此这里默认标记为「已就位」。
· IDADP ：国家基础学科公共科学数据中心，10 万余条 / 超 1.5TB，官网需要注册并按
          数据集逐一下载。当前网络下 http://www.icgroupcas.cn 的 443 端口不可直连，
          自动下载不可行 —— 因此默认跳过，只打印人工获取指引。

用法：
    python scripts/00_download_datasets.py                # 只做体检，不下载
    python scripts/00_download_datasets.py --check-sample # 校验 Sample/ 是否完整
    python scripts/00_download_datasets.py --idadp-url <URL>   # 给一个直链时才真正下载
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import list_images          # noqa: E402
from src.common.logging_utils import setup_logger    # noqa: E402
from src.common.paths import PROJECT_ROOT, path_of   # noqa: E402

logger = setup_logger("agri.download")

IDADP_PORTAL = "http://www.icgroupcas.cn/website_bchtk/index.html"
PDD271_REPO = "https://github.com/liuxindazz/PDD271"


def check_sample() -> int:
    """校验 Sample/ 目录的类别数与图片数。返回图片总数；为 0 则报错。"""
    raw = path_of("raw_dataset")
    rename = path_of("label_rename")
    if not raw.is_dir():
        logger.error("原始数据目录不存在：%s", raw)
        return 0

    images = list_images(raw, recursive=True)
    class_dirs = [d for d in raw.iterdir() if d.is_dir()]
    logger.info("Sample/ 目录：%d 个子目录，%d 张图片", len(class_dirs), len(images))

    if rename.exists():
        lines = [ln for ln in rename.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        logger.info("label_rename.txt：%d 条类别映射", len(lines))
    else:
        logger.warning("缺少 label_rename.txt，类别名无法解析：%s", rename)

    if not images:
        logger.error("未找到任何图片，请确认数据已放入 %s", raw)
    return len(images)


def download_idadp(url: str | None, dest: Path) -> None:
    """IDADP 下载。仅在显式提供直链时才执行。"""
    if not url:
        logger.info("IDADP 未提供直链，跳过下载。")
        logger.info("  数据来源：%s", IDADP_PORTAL)
        logger.info("  规模：10 万余条记录 / 超 1.5TB，需注册后逐数据集下载。")
        logger.info("  拿到直链后：python scripts/00_download_datasets.py --idadp-url <URL>")
        return

    import requests

    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(url.split("?")[0]).name
    logger.info("开始下载 IDADP：%s -> %s", url, target)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with target.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and done % (50 << 20) < (1 << 20):
                    logger.info("  已下载 %.1f%% (%.0f MB)", done * 100 / total, done / 1048576)
    logger.info("IDADP 下载完成：%s", target)


def main() -> int:
    parser = argparse.ArgumentParser(description="数据集获取与体检（任务 1.1）")
    parser.add_argument("--check-sample", action="store_true", help="校验 Sample/ 目录是否完整")
    parser.add_argument("--idadp-url", default=None, help="IDADP 数据集直链（可选，默认跳过）")
    parser.add_argument("--idadp-dest", default=str(PROJECT_ROOT / "data" / "raw_idadp"),
                        help="IDADP 下载目标目录")
    args = parser.parse_args()

    logger.info("PDD271 公开样本仓库：%s", PDD271_REPO)
    n = check_sample()

    if args.idadp_url:
        download_idadp(args.idadp_url, Path(args.idadp_dest))
    else:
        download_idadp(None, Path(args.idadp_dest))

    if n == 0:
        return 1
    logger.info("体检通过：已有 %d 张图片可用于 1.1 归档（下一步 python scripts/01_organize_data.py）", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
