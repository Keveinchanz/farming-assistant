#!/usr/bin/env python
"""任务 6.6 —— 把随仓库提供的 `runtime_assets/` 铺到应用读取的位置。

## 为什么需要这一步

仓库里只提交了运行所需的最小资源集，放在 `runtime_assets/` 下（模型、知识库、
卡片封面、运行时 JSON）。而应用通过 `src/common/paths.py` 按
`configs/default.yaml` 的相对路径读取，落在 `data/`、`knowledge/`、`models/` 三个目录。

**全新 clone 之后这三个目录是空的**（它们在 `.gitignore` 里），所以直接启动会报
「图像识别模型不可用：缺少标签映射」。本脚本做一次合并复制，把两者对齐。

幂等：重复执行只会覆盖同名文件，不会删除已有的农田数据库或用户上传的图片。

用法：python scripts/53_prepare_runtime.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "runtime_assets"

# 铺完之后必须存在的文件，缺一个就明确报错
REQUIRED = [
    "data/label_map.json",
    "models/onnx/inference_model.onnx",
    "models/onnx/pest_yolo.onnx",
    "knowledge/vectorstore/chroma.sqlite3",
]


def main() -> int:
    if not ASSETS.exists():
        print("找不到 runtime_assets/，请确认仓库完整下载")
        return 1

    for name in ("data", "knowledge", "models"):
        src = ASSETS / name
        if not src.exists():
            continue
        dst = ROOT / name
        dst.mkdir(exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        print(f"  {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")

    # 运行期目录
    for rel in ("outputs/logs", "outputs/reports", "outputs/figures", "data/farm_images"):
        (ROOT / rel).mkdir(parents=True, exist_ok=True)

    missing = [r for r in REQUIRED if not (ROOT / r).exists()]
    if missing:
        print("**资源不完整，缺少：**")
        for m in missing:
            print("   ", m)
        return 1

    print("运行资源已就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
