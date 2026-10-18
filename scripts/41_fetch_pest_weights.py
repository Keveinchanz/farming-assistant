#!/usr/bin/env python
"""任务 2.8.2 —— 拉取更大的 YOLO 预训练权重（经 gh-proxy 中转）。

## 为什么需要单独一个脚本

本机只有 `yolov8n.pt`。要试 yolov8s/m/l 就得下载，而 **`github.com` 直连超时**，
ultralytics 的权重恰好托管在 GitHub Releases 上 —— 照 `YOLO("yolov8s.pt")` 的默认路径走，
它会自己去 github 拉，只会静静卡住。项目里 `scripts/16_download_ip102.py:56` 早就用过
`https://gh-proxy.com` 中转（实测 15.5 MB/s 下过 778 MB 的数据集），这里沿用同一套办法。

**gh-proxy 是分块流式响应，不带 Content-Length / Accept-Ranges，断流不能续传** ——
这条教训来自 16 号脚本，所以这里只做「重下」，不做断点续传。权重最大 88 MB，重试成本可以忽略。

## 校验

下完必须过两关，否则宁可删掉重来：
1. **大小对得上**（每个权重的官方字节数写死在 `WEIGHTS` 里，差一个字节就重下）；
2. **`YOLO(path)` 能加载并跑通一次真实推理** —— 半个文件同样会被 torch 读成「格式错误的 zip」，
   但大小校验已经先挡了一道，两步一起才算稳。

## 用法

    python scripts/41_fetch_pest_weights.py                     # 拉 yolov8s / yolov8m（默认档）
    python scripts/41_fetch_pest_weights.py --which yolov8l yolo11m
    python scripts/41_fetch_pest_weights.py --force             # 已有也重下
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir                          # noqa: E402
from src.common.logging_utils import setup_logger                   # noqa: E402
from src.common.paths import project_path                           # noqa: E402

logger = setup_logger("agri.pest.weights")

# 官方字节数（2026-10-14 经 gh-proxy HEAD 实测），用来判断下载是否完整
WEIGHTS: dict[str, tuple[str, int]] = {
    "yolov8n.pt":  ("v8.3.0", 6_549_796),
    "yolov8s.pt":  ("v8.3.0", 22_588_772),
    "yolov8m.pt":  ("v8.3.0", 52_136_884),
    "yolov8l.pt":  ("v8.3.0", 87_792_836),
    "yolo11m.pt":  ("v8.3.0", 40_684_120),
}

MIRRORS = [
    ("gh-proxy", "https://gh-proxy.com/https://github.com/ultralytics/assets/releases/download/{tag}/{name}"),
    ("ghfast", "https://ghfast.top/https://github.com/ultralytics/assets/releases/download/{tag}/{name}"),
    ("ghproxy.net", "https://ghproxy.net/https://github.com/ultralytics/assets/releases/download/{tag}/{name}"),
]

# 权重统一放这里（yolov8n.pt 已经在这儿）
DEST_DIR_KEY = "weights"


def download(url: str, dest: Path) -> Path:
    """流式下载 + 进度条。gh-proxy 不给 Content-Length 时 total=0，进度条退化成纯计数。"""
    import requests
    from tqdm import tqdm

    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=(15, 300)) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with tmp.open("wb") as f, tqdm(
            total=total or None, unit="B", unit_scale=True, unit_divisor=1024,
            desc=f"下载 {dest.name}", leave=False,
        ) as bar:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                bar.update(len(chunk))
    tmp.replace(dest)
    return dest


def verify(path: Path, expect_size: int) -> None:
    """两道校验：大小 + 真的能加载并推理一次。任一条不过就抛，调用方负责删掉重来。"""
    got = path.stat().st_size
    if got != expect_size:
        raise ValueError(f"{path.name} 大小不对：{got:,} B，应为 {expect_size:,} B（多半是下了一半）")

    from ultralytics import YOLO

    model = YOLO(str(path))
    sample = next(iter((project_path("data", "pest", "images", "val")).glob("*.jpg")), None)
    if sample is None:
        logger.warning("找不到样例图，只做了加载校验")
        return
    res = model.predict(source=str(sample), imgsz=640, verbose=False)
    n = len(res[0].boxes) if res and res[0].boxes is not None else 0
    logger.info("  校验通过：%s（%d 个框 / %s）", path.name, n, sample.name)


def fetch_one(name: str, *, force: bool) -> bool:
    tag, size = WEIGHTS[name]
    dest = project_path("weights", name)
    if dest.exists() and not force:
        try:
            verify(dest, size)
            logger.info("%s 已存在且校验通过，跳过", name)
            return True
        except Exception as e:  # noqa: BLE001 —— 已存在的也可能是坏的，重下
            logger.warning("%s 已存在但校验不过（%s），重下", name, e)

    for label, tpl in MIRRORS:
        url = tpl.format(tag=tag, name=name)
        logger.info("  试 %s：%s", label, url)
        try:
            download(url, dest)
            verify(dest, size)
            logger.info("%s 就绪：%.1f MB", name, dest.stat().st_size / 1024 / 1024)
            return True
        except Exception as e:  # noqa: BLE001 —— 一个中转挂了就换下一个
            logger.warning("  %s 失败：%s", label, e)
            dest.unlink(missing_ok=True)
    logger.error("%s 所有中转都没拉下来", name)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="经 gh-proxy 拉取 YOLO 预训练权重")
    ap.add_argument("--which", nargs="*", default=["yolov8s.pt", "yolov8m.pt"],
                    choices=sorted(WEIGHTS), help="要拉哪些（默认 s 与 m）")
    ap.add_argument("--force", action="store_true", help="已存在也重下")
    args = ap.parse_args()

    ok = 0
    for name in args.which:
        logger.info("== %s", name)
        if fetch_one(name, force=args.force):
            ok += 1
    logger.info("完成：%d/%d", ok, len(args.which))
    return 0 if ok == len(args.which) else 1


if __name__ == "__main__":
    raise SystemExit(main())
