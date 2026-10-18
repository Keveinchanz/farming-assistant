"""任务 1.1 数据整理：把原始样本归档为「作物大类/病虫害名称」二级目录。

原始数据形态（已核实）：
    Sample/<数字目录号>/*.JPG            261 个类，每类 10 张
    Sample/<数字目录号>/<同名目录号>/*.JPG  10 个大类多嵌套一层，每类 500~1122 张
    Sample/label_rename.txt              每行「中文类别名 <空格> 目录号」，UTF-8 带 BOM

本模块把两种形态统一拍平，输出：
    data/processed/dataset/<作物大类>/<病虫害名称>/<目录号>_<序号>.jpg
    data/processed/manifest.json         每个类别的解析结果与图片数

关于 IDADP：任务清单 1.1 列了 IDADP（1.5TB，官网当前不可直连）与 PDD271 两个数据集。
本仓库以已到位的 PDD271 公开样本为唯一数据源；IDADP 下载器做成独立可插拔脚本
（scripts/00_download_datasets.py），默认跳过，不阻塞后续阶段。
"""

from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path

from src.common.io_utils import ensure_dir, list_images, write_json, write_text
from src.common.logging_utils import setup_logger
from src.common.paths import PROJECT_ROOT, path_of
from src.data.crop_parser import ParsedLabel, parse_many

logger = setup_logger("agri.organize")


@dataclass
class ClassResult:
    parsed: ParsedLabel
    src_dir: Path
    out_dir: Path
    num_images: int
    copied: int
    skipped: int


def read_label_rename(path: Path) -> list[tuple[str, str]]:
    """读 label_rename.txt -> [(中文类别名, 目录号), ...]。"""
    rows: list[tuple[str, str]] = []
    for raw in io.open(path, encoding="utf-8-sig"):
        line = raw.strip()
        if not line:
            continue
        parts = line.rsplit(" ", 1)          # 从右边切，类别名本身不含空格，但这样更稳
        if len(parts) != 2:
            logger.warning("label_rename.txt 行格式异常，已跳过：%r", line)
            continue
        name, dir_id = parts[0].strip(), parts[1].strip()
        rows.append((name, dir_id))
    return rows


def organize(
    raw_dir: Path | None = None,
    rename_file: Path | None = None,
    out_root: Path | None = None,
    *,
    force: bool = False,
) -> dict:
    """执行归档，返回统计信息。"""
    raw_dir = raw_dir or path_of("raw_dataset")
    rename_file = rename_file or path_of("label_rename")
    out_root = out_root or path_of("processed_dataset")

    if not raw_dir.exists():
        raise FileNotFoundError(f"原始数据目录不存在：{raw_dir}")
    if not rename_file.exists():
        raise FileNotFoundError(f"标签映射文件不存在：{rename_file}")

    rows = read_label_rename(rename_file)
    logger.info("读入 %d 条类别映射：%s", len(rows), rename_file)

    ok, unparsed = parse_many(rows)
    _dump_unparsed(unparsed)

    results: list[ClassResult] = []
    total_images = 0
    total_copied = 0

    for item in ok:
        src_dir = raw_dir / item.dir_id
        if not src_dir.is_dir():
            logger.warning("目录不存在，跳过：%s（类别 %s）", src_dir, item.label_cn)
            continue

        images = list_images(src_dir, recursive=True)     # 兼容多嵌套一层的大类
        out_dir = out_root / item.crop / item.label_cn
        copied, skipped = _materialize(images, out_dir, item.dir_id, force=force)

        total_images += len(images)
        total_copied += copied
        results.append(
            ClassResult(item, src_dir, out_dir, len(images), copied, skipped)
        )

    manifest = _build_manifest(results)
    write_json(out_root.parent / "manifest.json", manifest)

    _log_summary(results, total_images, total_copied)
    return manifest


def _materialize(images: list[Path], out_dir: Path, dir_id: str, *, force: bool) -> tuple[int, int]:
    """把某类别的图片复制并重命名到目标目录。返回 (复制数, 跳过数)。"""
    ensure_dir(out_dir)
    copied = skipped = 0
    for idx, src in enumerate(images):
        dst = out_dir / f"{dir_id}_{idx:04d}{src.suffix.lower()}"
        if dst.exists() and not force and dst.stat().st_size == src.stat().st_size:
            skipped += 1
            continue
        shutil.copy2(src, dst)
        copied += 1
    return copied, skipped


def _build_manifest(results: list[ClassResult]) -> dict:
    classes = []
    for r in results:
        d = r.parsed.as_dict()
        d.update(
            num_images=r.num_images,
            out_dir=str(r.out_dir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        )
        classes.append(d)

    crops: dict[str, dict] = {}
    for r in results:
        c = crops.setdefault(
            r.parsed.crop,
            {"category": r.parsed.crop_category, "num_classes": 0, "num_images": 0, "classes": []},
        )
        c["num_classes"] += 1
        c["num_images"] += r.num_images
        c["classes"].append(r.parsed.label_cn)

    return {
        "version": 1,
        "source": "PDD271 公开样本（Sample/ 目录，label_rename.txt 提供类别名映射）",
        "num_classes": len(results),
        "num_crops": len(crops),
        "num_images": sum(r.num_images for r in results),
        "crops": crops,
        "classes": classes,
    }


def _dump_unparsed(unparsed: list[ParsedLabel]) -> None:
    report = path_of("reports_dir") / "unparsed_crops.txt"
    if unparsed:
        lines = [f"{p.label_cn}\t{p.dir_id}" for p in unparsed]
        write_text(report, "\n".join(lines) + "\n")
        logger.warning("有 %d 个类别未能解析出作物大类，已写入 %s", len(unparsed), report)
    else:
        write_text(report, "")
        logger.info("作物解析覆盖率 100%%，无未命中类别（报告：%s）", report)


def _log_summary(results: list[ClassResult], total_images: int, total_copied: int) -> None:
    counts = sorted((r.num_images for r in results), reverse=True)
    logger.info("归档类别数：%d", len(results))
    logger.info("归档图片总数：%d（本次新复制 %d 张）", total_images, total_copied)
    if counts:
        logger.info("每类图片数：最多 %d，最少 %d，中位 %d", counts[0], counts[-1], counts[len(counts) // 2])

    long_tail = [r for r in results if r.num_images > 10]
    if long_tail:
        logger.info("长尾大类 %d 个（>10 张）：", len(long_tail))
        for r in sorted(long_tail, key=lambda x: -x.num_images):
            logger.info("    %-10s %-16s %5d 张", r.parsed.dir_id, r.parsed.label_cn, r.num_images)


if __name__ == "__main__":
    organize()
