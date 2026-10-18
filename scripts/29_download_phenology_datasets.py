#!/usr/bin/env python
"""阶段 7 / 7.3.4（外部数据）—— 下载中国县级水稻物候日历（ChinaRiceCalendar）。

## 为什么需要外部数据集

本项目的收获日模型（`src/farm/harvest_model.py`）是一个可选叠加层：没有模型时
走纯积温法，照样能用。要让模型真的有价值，训练样本必须能回答
**「从今天起还要几天成熟」** —— 而 `scripts/24_train_harvest_model.py` 手上只有
「收获当天的单点快照」，`days_since_sow ≈ actual_days`，模型只会学成一个恒等映射
（详见该脚本头部的说明）。**一个生长季里要有多个观察截止日**，才可能学到
「进度越靠后 → 剩余天数越小」这条映射。公开物候数据集正好给的就是它。

## 为什么是这个数据集

    ChinaRiceCalendar —— 中国 2503 个县 × 2003–2022 的水稻物候日历
    Harvard Dataverse，DOI 10.7910/DVN/EUP8EY，**许可 CC0 1.0**（可自由使用）
    直链 https://dataverse.harvard.edu/api/access/datafile/13999251

它一行的四要素齐全：**县 + 年份 + 稻型（早/中/晚）+ 移栽/抽穗/成熟 三个日期的
年内序日（DOY）**。这正是构造「某年某县的作物起点 → 成熟日」所需要的全部信息。

**顺带说明为什么不用 `docs/datasets.md` 里原先推荐的第 1 步 CY-Bench**：
它的 `crop_calendar_*` 只有 `crop_name, adm_id, sos, eos` 四列，而且 `sos/eos`
官方标注为 **static** —— 那是 WorldCereal 的多年平均日历，**不随年份变化**，
给不出「哪一年哪天收的」，因此跑不通我们的标签。（另外本机 `zenodo.org`
DNS 解析失败，那份也下不动。）

## 取到的是什么

zip（约 45 MB）解开后有若干文件，本脚本只认其中这一份：

    County-level Annual ChinaRiceCalendar/county-level annual ChinaRiceCalendar.xlsx

三个 sheet：`transplant` / `heading` / `maturity`，每格是一个 DOY（小数），
空格子表示该县该年没有提取出来（**不是 0，也不是缺省值 —— 该县该年作废**）。
同包里的 `xian.shp`（53 MB）**不用**：县质心改用项目已有的
`data/admin_divisions.json`（含 adcode 与 center），以此完全避开 shapefile/GDAL。

## 用法

    python scripts/29_download_phenology_datasets.py
    python scripts/29_download_phenology_datasets.py --force   # 重下

## 失败时的行为

打印来源与 HTTP 码，**退出码 1**。**绝不生成替代数据、绝不跳过校验继续跑** ——
编一份假的物候日历出来，会让后面整条训练管线在错误的标签上「成功」。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json                # noqa: E402
from src.common.logging_utils import setup_logger                     # noqa: E402
from src.common.paths import PROJECT_ROOT                             # noqa: E402

logger = setup_logger("agri.phenology.download")

DOI = "10.7910/DVN/EUP8EY"
LICENSE = "CC0 1.0"

# Dataverse 的 file id 是**稳定**的（换版本会换 id，那就应该重跑并更新这里）。
DATAVERSE_FILE_ID = "13999251"
SOURCES = [
    ("dataverse", f"https://dataverse.harvard.edu/api/access/datafile/{DATAVERSE_FILE_ID}"),
]

# 下载完的 sha256。第一次运行时会打印出来，之后把它钉在这里 ——
# 从网络上下来的归档是不可信数据，钉住哈希是「这份文件确实是当初那一份」的唯一证据。
EXPECTED_SHA256 = "e0576dcea27be62a27da89c59118d743e701feb25acddc8eb644b562fd3cdc8c"

# zip 里我们真正要的那份表
WANTED_XLSX = "county-level annual ChinaRiceCalendar.xlsx"

RAW_DIR = PROJECT_ROOT / "data" / "phenology" / "_raw"
ZIP_PATH = RAW_DIR / "china_rice_calendar.zip"
EXTRACT_DIR = RAW_DIR / "_extract"


def download(url: str, dest: Path) -> Path:
    """流式下载到 *.part 再改名 —— 中途断掉不会留下一个「看起来完整」的半截文件。"""
    import requests
    from tqdm import tqdm

    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=(15, 300)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
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


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_zip(*, force: bool) -> Path:
    if ZIP_PATH.exists() and not force:
        logger.info("已存在原始 zip，跳过下载：%s", ZIP_PATH)
        return ZIP_PATH

    errors = []
    for name, url in SOURCES:
        logger.info("尝试下载源 %s：%s", name, url)
        try:
            download(url, ZIP_PATH)
            logger.info("下载完成：%.1f MB", ZIP_PATH.stat().st_size / 1024 / 1024)
            return ZIP_PATH
        except Exception as e:  # noqa: BLE001
            logger.warning("下载源 %s 失败：%s", name, e)
            errors.append(f"{name} ({url}): {e}")

    logger.error("所有下载源均失败，**不生成替代数据**：")
    for line in errors:
        logger.error("  %s", line)
    logger.error("人工获取指引：浏览器打开 https://doi.org/%s ，", DOI)
    logger.error("  下载 'County-level Annual ChinaRiceCalendar'，")
    logger.error("  另存为 %s 后重跑本脚本。", ZIP_PATH)
    raise SystemExit(1)


def verify_zip(zip_path: Path) -> None:
    """全量 CRC 校验再解压。

    半边 zip 在 zipfile 眼里往往仍然「能打开」，不校验就会静默解出残缺数据，
    等到训练时才发现某些县是空的 —— 那时已经浪费了几小时。
    """
    logger.info("校验 zip CRC…")
    with zipfile.ZipFile(zip_path) as z:
        broken = z.testzip()
        if broken:
            logger.error("zip 损坏，第一个出错的成员：%s", broken)
            logger.error("把它删掉后重跑本脚本（--force 亦可）。")
            raise SystemExit(1)
        logger.info("CRC 通过，共 %d 个成员", len(z.namelist()))


def extract(zip_path: Path) -> Path:
    """解压到**全新的空目录**，返回 xlsx 的路径。

    下载下来的归档是不可信数据，所以：解压目标固定、先清空、内容一律当数据看，
    不执行其中任何东西。读它的解析代码在 src/farm/harvest_external.py，
    不要放进解压目录里跑。
    """
    if EXTRACT_DIR.exists():
        shutil.rmtree(EXTRACT_DIR, ignore_errors=True)
    ensure_dir(EXTRACT_DIR)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(EXTRACT_DIR)

    found = [p for p in EXTRACT_DIR.rglob("*.xlsx") if p.name == WANTED_XLSX]
    if not found:
        names = [p.name for p in EXTRACT_DIR.rglob("*.xlsx")][:10]
        logger.error("解压后没找到 %s", WANTED_XLSX)
        logger.error("  实际解出的 xlsx：%s", names or "（一个都没有）")
        logger.error("  上游结构可能变了 —— 不要把别的表当成本表。")
        raise SystemExit(1)
    if len(found) > 1:
        logger.error("解压后有多份同名 %s，无法确定用哪一份：", WANTED_XLSX)
        for p in found:
            logger.error("  %s", p.relative_to(EXTRACT_DIR))
        raise SystemExit(1)

    logger.info("县表：%s", found[0].relative_to(EXTRACT_DIR))
    return found[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 ChinaRiceCalendar 县级水稻物候日历")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载与解压")
    args = ap.parse_args()

    zip_path = fetch_zip(force=args.force)

    digest = sha256_of(zip_path)
    logger.info("sha256 = %s", digest)
    if not EXPECTED_SHA256:
        logger.warning("本脚本尚未钉住 sha256（EXPECTED_SHA256 为空）。")
        logger.warning("  请把上面这一行填进 scripts/29_download_phenology_datasets.py 后重跑。")
    elif digest != EXPECTED_SHA256:
        logger.error("sha256 与钉住的值不符 —— 文件可能已损坏或被换过：")
        logger.error("  期望 %s", EXPECTED_SHA256)
        logger.error("  实际 %s", digest)
        logger.error("删掉 %s 后重跑；若重下仍是这个值，说明上游确实换了文件，", zip_path)
        logger.error("需要人工确认后再更新脚本里钉住的哈希。")
        return 1
    else:
        logger.info("sha256 与钉住的值一致")

    verify_zip(zip_path)
    xlsx = extract(zip_path)

    write_json(RAW_DIR / "provenance.json", {
        "dataset": "ChinaRiceCalendar（县级年度水稻物候日历）",
        "doi": DOI,
        "license": LICENSE,
        "source_url": SOURCES[0][1],
        "dataverse_file_id": DATAVERSE_FILE_ID,
        "zip_sha256": digest,
        "zip_bytes": zip_path.stat().st_size,
        "xlsx": str(xlsx.relative_to(EXTRACT_DIR)).replace("\\", "/"),
        "note": "县质心不用同包的 xian.shp，改用 data/admin_divisions.json（避开 GDAL）",
    })

    logger.info("=" * 70)
    logger.info("就绪：%s", xlsx)
    logger.info("  sha256  %s", digest)
    logger.info("=" * 70)
    logger.info("下一步：python scripts/30_fetch_phenology_weather.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
