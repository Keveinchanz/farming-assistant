#!/usr/bin/env python
"""任务 6.4 —— 摘出「让演示跑起来」所需的最小资源集，落到 `runtime_assets/`。

## 为什么要有这一步

整个工作区 20 多 GB，但**跑起来只需要其中极小的一部分**。直接整包推仓库既超限也没必要：

| 目录 | 体积 | 运行时要不要 |
|---|---|---|
| `data/pest_cls_raw/`（识别集） | 6.06 GB | ❌ 只训练伪标注时用 |
| `data/pest_pseudo/` | 1.34 GB | ❌ 训练用 |
| `data/pest/`（检测集 781 MB） | 781 MB | **只要 97 张**（知识卡片的封面） |
| `data/processed/` | 121 MB | **只要 770 张**（知识卡片的配图） |
| `Sample/`、`data/phenology/` | 256 MB | ❌ 训练用 |
| `models/checkpoints/`、`pest_staging/` | 635 MB | ❌ `.pt` 只训练用，线上吃 ONNX |
| `models/pretrained/`（嵌入模型） | 568 MB | ✅ 但**不放进仓库**，构建镜像时从 hf-mirror 下 |

卡片引用的图片是从 `knowledge/library/*.json` 里**实际解析出来的路径**，不是猜的 ——
脚本会逐条核对存在性，缺一张就报错，免得线上出现破图。

## 产物

    runtime_assets/
      data/{processed/dataset, pest/images, 各种 json 与缓存, farm_images, splits}
      knowledge/            # 文档 + 卡片 + 向量库 + 词表
      models/{onnx, farm, registry.json}

`docker build` 时把这些整体 COPY 进 `/app`，路径与本地完全一致，
所以 `src/common/paths.py` 那套相对 PROJECT_ROOT 的解析**一行都不用改**。

用法：python scripts/50_build_runtime_assets.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.paths import path_of, project_path  # noqa: E402

OUT = project_path("runtime_assets")

# 运行期要带上的零散 JSON / 缓存目录（对应 configs/default.yaml 的 paths 段）
DATA_FILES = [
    "label_map.json",
    "pest_label_map.json",
    "province_pest_top3.json",
    "pesticide_registry.json",
    "admin_divisions.json",
    "policy_list.json",
]
DATA_DIRS = [
    "admin_divisions_cache",
    "pesticide_cache",
    "splits",
]


def _referenced_images() -> tuple[set[str], set[str]]:
    """从知识卡片里解析出真正被引用到的图片相对路径。

    返回 (`/media` 那批, `/media-pest` 那批)。两条静态挂载分别对应
    `data/processed/dataset` 与 `data/pest/images`（见 backend/app/main.py:136-147）。
    """
    lib_dir = path_of("library_dir")
    files = sorted(lib_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"找不到知识卡片：{lib_dir}")
    media: set[str] = set()
    pest: set[str] = set()
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        media |= set(re.findall(r"/media/([^\"'\s]+?\.(?:jpg|jpeg|png))", text, re.I))
        pest |= set(re.findall(r"/media-pest/([^\"'\s]+?\.(?:jpg|jpeg|png))", text, re.I))
    return media, pest


def _copy(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return src.stat().st_size


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    total = 0
    report: dict[str, dict] = {}

    # ---- 1. 被卡片引用的图片 ----
    media, pest = _referenced_images()
    root_media = path_of("processed_dataset")
    root_pest = path_of("pest_dataset") / "images"

    for name, rel_set, root in (("知识卡片配图 /media", media, root_media),
                                ("虫害封面 /media-pest", pest, root_pest)):
        n = size = 0
        missing = []
        for rel in sorted(rel_set):
            src = root / rel
            if not src.exists():
                missing.append(rel)
                continue
            # 目标路径保留 `/media/...` 与 `/media-pest/...` 的原始相对结构
            sub = "processed/dataset" if root == root_media else "pest/images"
            size += _copy(src, OUT / "data" / sub / rel)
            n += 1
        if missing:
            print(f"  **{name}：缺少 {len(missing)} 张**，例如 {missing[:3]}")
            return 1
        report[name] = {"files": n, "mb": round(size / 1024 ** 2, 1)}
        total += size
        print(f"  {name}: {n} 张（{size/1024**2:.1f} MB）")

    # ---- 2. 运行时 JSON 与缓存 ----
    # data_dir 只有函数、不是 configs 里的键，所以从 pest_dataset（data/pest）的父目录推
    data_root = path_of("pest_dataset").parent
    n_json = 0
    for name in DATA_FILES:
        src = data_root / name
        if src.exists():
            total += _copy(src, OUT / "data" / name)
    for name in DATA_DIRS:
        src = data_root / name
        if src.exists():
            for f in src.rglob("*"):
                if f.is_file():
                    total += _copy(f, OUT / "data" / name / f.relative_to(src))
    # 农田图片目录要存在（app 启动时 mount，没有会报错）
    (OUT / "data" / "farm_images").mkdir(parents=True, exist_ok=True)
    n_json = sum(1 for _ in (OUT / "data").rglob("*.json"))
    print(f"  运行时 JSON 与缓存：{n_json} 个 json")

    # ---- 3. 知识库 ----
    kd = path_of("knowledge_dir")
    for f in kd.rglob("*"):
        if f.is_file():
            total += _copy(f, OUT / "knowledge" / f.relative_to(kd))
    print(f"  knowledge/: {(sum(1 for _ in (OUT/'knowledge').rglob('*') if _.is_file()))} 个文件")

    # ---- 4. 模型（只 ONNX，不带 .pt；嵌入模型也不带）----
    onnx_dir = path_of("onnx_dir")
    for f in onnx_dir.glob("*.onnx"):
        total += _copy(f, OUT / "models" / "onnx" / f.name)
    farm_dir = path_of("farm_models")
    if farm_dir.exists():
        for f in farm_dir.rglob("*"):
            if f.is_file():
                total += _copy(f, OUT / "models" / "farm" / f.relative_to(farm_dir))

    # registry.json：把本机绝对路径改写成仓库内的相对路径
    reg = json.loads(path_of("model_registry").read_text(encoding="utf-8"))
    for key, val in reg.items():
        if isinstance(val, dict) and "checkpoint" in val:
            val["checkpoint"] = f"models/checkpoints/{Path(val['checkpoint']).name}"
    (OUT / "models").mkdir(parents=True, exist_ok=True)
    (OUT / "models" / "registry.json").write_text(
        json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("  models/：ONNX + registry.json（.pt 与嵌入模型不入库）")

    print("")
    print("=" * 68)
    print("runtime_assets 合计 %.1f MB" % (total / 1024 ** 2))
    print("  落点：%s" % OUT)
    large = sorted(((f.stat().st_size, f) for f in OUT.rglob("*") if f.is_file()),
                   reverse=True)[:5]
    for size, f in large:
        print("    %8.1f MB  %s" % (size / 1024 ** 2, f.relative_to(OUT)))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
