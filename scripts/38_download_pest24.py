#!/usr/bin/env python
"""任务 2.7.6 第三档 —— Pest24 的**零物体图**当纯负样本（背景）。

## 为什么只取「零物体」的图，而不是把能映射的框也拿来用

计划里把这条约束写死了，理由值得在这里再抄一遍：**跨数据集画框习惯不同**。
Pest24 是 2095×1944 的极小目标数据集，它的框常常只盖住虫体躯干的一小块；
IP102 的框是整只虫。把 Pest24 的框当正样本喂进去，等于教模型「半只虫也算」——
这是**灌标签噪声**，而现在的瓶颈恰恰是 precision（0.4785 val / 0.5166 test）。

那「物体全不可映射」的图能不能当负样本？**不采用**。Pest24 按设计整份都是虫害图，
「不可映射」只说明我的类名表没覆盖它，**不代表图上没虫**。拿它当负样本就是在教模型
「看到这只虫就闭嘴」，直接压 recall。所以本脚本的默认规则极简：

    一张图只有当它的 XML 里**一个 object 都没有**时，才够格当负样本。

这条规则的好处是**完全不依赖类名映射表**——也就不依赖任何我这边无法核实的东西。
（`--allow-unmapped` 留了个口子，但要显式开，且会把用了多少张如实写进报告。）

## 可达性必须先探，且我探不了

`http://aisys.iim.ac.cn/zhibao.html`（中科院合肥智能机械研究所）被 claude.ai 的抓取器
挡了，**我这边无法确认下载入口通不通**（`api.figshare.com`、`aicomp.cn` 同样被挡）。
这是我这侧出口的限制，与本机网络无关 —— 所以第一步必须是**本机探测**，
而且我**不会**因为探不到就编造一个「替代数据集」。探不到就如实报告并给人工获取指引。

## 用法

    python scripts/38_download_pest24.py --probe                  # 只探可达性
    python scripts/38_download_pest24.py --url <直链>              # 下载
    python scripts/38_download_pest24.py --archive D:\\pest24.zip   # 已经手工下好了，指过来
    python scripts/38_download_pest24.py --report-objects         # 统计各 object 名称出现次数
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.exp.pest24")

# 官方入口（未见验证）。--probe 会把真实 HTTP 码打出来，以实际结果为准。
LANDING_PAGE = "http://aisys.iim.ac.cn/zhibao.html"
CANDIDATE_URLS = [
    "http://aisys.iim.ac.cn/zhibao.html",
]
LICENSE = "研究/非商业用途（中科院合肥智能机械研究所）"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}


def _mb(n) -> str:
    return "未知" if not n else f"{n / 1024 ** 2:.1f} MB（{n / 1024 ** 3:.2f} GB）"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 探测

def do_probe() -> int:
    import requests

    logger.info("=" * 78)
    logger.info("探测 Pest24 入口可达性（本机网络，不是我这侧）")
    logger.info("=" * 78)
    any_ok = False
    for url in CANDIDATE_URLS:
        try:
            r = requests.get(url, timeout=20, stream=True, allow_redirects=True,
                             headers={"User-Agent": "rural-agri-platform/1.0 (dataset acquisition)"})
            logger.info("%s → HTTP %s", url, r.status_code)
            ct = r.headers.get("Content-Type")
            if ct:
                logger.info("  Content-Type: %s", ct)
            body = r.raw.read(4096, decode_content=True) if hasattr(r.raw, "read") else b""
            r.close()
            text = body.decode("utf-8", errors="replace")
            # 页面里若挂着 zip/rar 链接，直接打出来 —— 省得人工翻页面
            import re

            for m in set(re.findall(r'href=["\']([^"\']+\.(?:zip|rar|7z|tar\.gz))["\']', text, re.I)):
                logger.info("  页面里发现档案链接：%s", m)
                any_ok = True
            if r.status_code == 200:
                any_ok = True
        except Exception as e:  # noqa: BLE001
            logger.warning("%s → 不可达：%s: %s", url, type(e).__name__, e)

    logger.info("")
    if any_ok:
        logger.info("入口可达。若页面里的档案链接需要人工点击，请手工下载后 --archive 指过来。")
    else:
        logger.info("**入口不可达**。人工获取指引：")
        logger.info("  1. 浏览器打开 %s", LANDING_PAGE)
        logger.info("  2. 按站点说明申请/下载 Pest24 档案（可能需填表或邮件）")
        logger.info("  3. python scripts/38_download_pest24.py --archive <下载到的档案>")
        logger.info("**不会生成任何替代数据。**")
    logger.info("=" * 78)
    return 0 if any_ok else 1


# ---------------------------------------------------------------- 获取

def _download(url: str, dest: Path) -> Path:
    import requests

    part = dest.with_suffix(dest.suffix + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "rural-agri-platform/1.0 (dataset acquisition)"}
    if done:
        headers["Range"] = f"bytes={done}-"
        logger.info("续传 .part（已有 %.1f MB）", done / 1024 ** 2)

    with requests.get(url, headers=headers, stream=True, allow_redirects=True, timeout=60) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length", 0)) + (done if r.status_code == 206 else 0)
        with part.open("ab" if done else "wb") as f:
            t0, last = time.perf_counter(), 0.0
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if time.perf_counter() - last > 5:
                    last = time.perf_counter()
                    logger.info("  %.1f/%.1f MB  %.1f MB/s", done / 1024 ** 2,
                                (total or 0) / 1024 ** 2,
                                done / max(last - t0, 1e-6) / 1024 ** 2)
    part.replace(dest)
    return dest


def _safe_extract(archive: Path, dest: Path) -> None:
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"解压目标非空，拒绝覆盖：{dest}")
    ensure_dir(dest)
    root = dest.resolve()

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"zip CRC 校验失败：{bad}")
            for m in z.infolist():
                if not str((dest / m.filename).resolve()).startswith(str(root)):
                    raise RuntimeError(f"路径穿越成员，拒绝：{m.filename}")
            z.extractall(dest)
        logger.info("zip 已解压（全量 CRC 通过）→ %s", dest)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if m.issym() or m.islnk():
                    raise RuntimeError(f"链接成员，拒绝：{m.name}")
                if not str((dest / m.name).resolve()).startswith(str(root)):
                    raise RuntimeError(f"路径穿越成员，拒绝：{m.name}")
            t.extractall(dest)
        logger.info("tar 已解压 → %s", dest)
        return
    raise RuntimeError(f"既不是 zip 也不是 tar：{archive}")


# ---------------------------------------------------------------- VOC 解析

def _find_dirs(root: Path) -> tuple[Path | None, Path | None]:
    """认出 Annotations（XML）与 JPEGImages（图片）目录。"""
    ann = jpg = None
    for d in root.rglob("*"):
        if not d.is_dir():
            continue
        n = d.name.lower()
        if ann is None and ("annotation" in n or n == "xmls"):
            if any(f.suffix.lower() == ".xml" for f in list(d.iterdir())[:50]):
                ann = d
        if jpg is None and ("jpegimages" in n or n in {"images", "jpg", "imageset"}):
            if any(f.suffix.lower() in IMAGE_EXT for f in list(d.iterdir())[:50]):
                jpg = d
    return ann, jpg


def _objects(xml: Path) -> list[str]:
    try:
        root = ET.parse(xml).getroot()
    except ET.ParseError:
        return []
    out = []
    for obj in root.findall("object"):
        nm = obj.findtext("name")
        if nm:
            out.append(nm.strip())
    return out


def analyze(ann: Path) -> dict:
    """逐图统计 object 数，给出「够格当负样本」的名单与名称直方图。"""
    names: Counter[str] = Counter()
    empty, nonempty = [], []
    for x in sorted(ann.glob("*.xml")):
        objs = _objects(x)
        for o in objs:
            names[o] += 1
        (empty if not objs else nonempty).append(x.stem)
    return {"num_xml": len(empty) + len(nonempty),
            "empty_stems": empty, "nonempty_count": len(nonempty),
            "object_name_histogram": dict(names.most_common())}


def report_objects(root: Path) -> int:
    ann, jpg = _find_dirs(root)
    if not ann:
        logger.error("在 %s 里找不到 Annotations（XML）目录", root)
        return 1
    a = analyze(ann)
    logger.info("XML 共 %d 个；其中零 object 的 %d 个，有 object 的 %d 个",
                a["num_xml"], len(a["empty_stems"]), a["nonempty_count"])
    logger.info("object 名称直方图（前 40）：")
    for k, v in list(a["object_name_histogram"].items())[:40]:
        logger.info("  %-40s %d", k, v)
    logger.info("**这份名字要人眼过一遍**：将来若要做「正样本对照」实验，")
    logger.info("只有同物种、同作物语境的确定性匹配才允许进映射表。本脚本不做那个映射。")
    dest = path_of("reports_dir") / "pest24_objects.json"
    ensure_dir(dest.parent)
    write_json(dest, a)
    logger.info("已写：%s", dest)
    return 0


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.6 第三档：Pest24 零物体图当负样本")
    ap.add_argument("--probe", action="store_true", help="只探测入口可达性后退出")
    ap.add_argument("--url", default=None, help="下载直链")
    ap.add_argument("--archive", default=None, help="已手工下好的档案路径")
    ap.add_argument("--report-objects", action="store_true", help="只统计 object 名称")
    ap.add_argument("--allow-unmapped", action="store_true",
                    help="允许「有 object 但类名不在表里」的图当负样本（默认关闭，见 docstring）")
    args = ap.parse_args()

    raw_dir = ensure_dir(path_of("pest24_raw"))
    extracted = raw_dir / "extracted"

    if args.probe:
        return do_probe()

    if args.report_objects:
        if not extracted.exists():
            logger.error("还没解压：%s", extracted)
            return 1
        return report_objects(extracted)

    # --- 取得档案 ---
    archive = Path(args.archive) if args.archive else raw_dir / "pest24.zip"
    if args.archive:
        if not archive.exists():
            logger.error("档案不存在：%s", archive)
            return 1
        logger.info("使用手工提供的档案：%s", archive)
    elif archive.exists():
        logger.info("档案已存在，跳过下载：%s", archive)
    else:
        if not args.url:
            logger.error("需要 --url 或 --archive。先跑 --probe 看入口，或手工下载后用 --archive。")
            return 1
        try:
            _download(args.url, archive)
        except Exception as e:  # noqa: BLE001
            logger.error("下载失败：%s", e)
            logger.error("**不生成替代数据**；请手工获取后用 --archive 指过来。")
            return 1

    sha = _sha256(archive)
    logger.info("sha256 = %s", sha)
    write_json(raw_dir / "checksums.json", {
        "sha256": sha, "bytes": archive.stat().st_size,
        "mode": "self-computed（本数据集没有稳定 file id 可钉值）",
        "note": "Pest24 走的是机构站点而非 Dataverse/Figshare 这类有 file id 的仓库，"
                "所以没有可对照的官方摘要 —— 这里的 sha256 只证明「每次拿到的是同一份」。",
    })
    write_json(raw_dir / "provenance.json", {
        "dataset": "Pest24", "institution": "中国科学院合肥智能机械研究所",
        "landing_page": LANDING_PAGE, "source_url": args.url,
        "license": LICENSE, "retrieved_at": datetime.now().isoformat(timespec="seconds"),
        "bytes": archive.stat().st_size, "sha256": sha,
        "usage_here": "只把**零 object** 的图当纯负样本（空 label），不做正样本、不写入 102 类体系",
    })

    if not extracted.exists():
        _safe_extract(archive, extracted)

    ann, jpg = _find_dirs(extracted)
    logger.info("Annotations=%s  JPEGImages=%s", ann, jpg)
    if not ann or not jpg:
        logger.error("没能同时认出 XML 与图片目录 —— 先看目录结构再决定，不猜。")
        return 1

    a = analyze(ann)
    logger.info("XML %d 个：零 object %d 个，有 object %d 个",
                a["num_xml"], len(a["empty_stems"]), a["nonempty_count"])

    allow = set(a["empty_stems"])
    if args.allow_unmapped:
        allowing = [x.stem for x in ann.glob("*.xml") if x.stem not in allow]
        allow.update(allowing)
        logger.warning("--allow-unmapped 已开：额外把 %d 张「有 object」的图当负样本。"
                       "**这会压 recall**，仅作对照实验用。", len(allowing))

    # --- 落负样本：图片 + 空 label ---
    out = ensure_dir(path_of("pest24_dataset"))
    img_out = ensure_dir(out / "images" / "all")
    lbl_out = ensure_dir(out / "labels" / "all")

    made = missing = 0
    for stem in allow:
        src = next((p for ext in IMAGE_EXT for p in [jpg / f"{stem}{ext}"] if p.exists()), None)
        if src is None:
            # XML 的 stem 与图片文件名偶尔差个前缀，兜底找一次
            cands = list(jpg.glob(f"{stem}.*"))
            src = cands[0] if cands else None
        if src is None:
            missing += 1
            continue
        shutil.copyfile(src, img_out / src.name)
        # **空 label 就是负样本的全部语义**：这张图上没有任何目标
        (lbl_out / f"{src.stem}.txt").write_text("", encoding="utf-8")
        made += 1

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "num_xml": a["num_xml"], "num_negatives": made,
        "num_disqualified_had_objects": a["nonempty_count"],
        "num_missing_image": missing,
        "allow_unmapped": args.allow_unmapped,
        "rule": "只有 XML 里零 object 的图才当负样本；有 object 的一律剔除，"
                "因为 Pest24 整份都是虫害图，「不可映射」不等于「图上没虫」。",
        "object_name_histogram": a["object_name_histogram"],
        "license": LICENSE,
    }
    dest = path_of("reports_dir") / "pest24_report.json"
    ensure_dir(dest.parent)
    write_json(dest, manifest)
    write_json(out / "manifest.json", manifest)

    logger.info("=" * 78)
    logger.info("负样本 %d 张（丢掉「有 object」的 %d 张）→ %s", made, a["nonempty_count"], img_out)
    logger.info("报告：%s", dest)
    logger.info("=" * 78)
    logger.info("下一步：python scripts/39_build_pest_mixed.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
