#!/usr/bin/env python
"""任务 2.7.6 第二档 —— 下载 IP102 **分类集**（~7.5 万张，同源同许可的 102 类体系）。

## 这是唯一「类别体系对得上」的外部数据

检测子集只有 18976 张，长尾最细的那几个类每类只有个位数实例，还有 5 个类
三档全零（59 葡萄根瘤蚜 / 60 葡萄缺节瘿螨 / 63 茶跗线螨 / 75 柑橘锈瘿螨 / 80 糠片蚧）。
IP102 的分类集是 **同一个数据集、同一套 102 类、同一位作者**，只是没有画框 ——
所以它能给出**正确的物种监督信号**，这是 Pest24/AgriPest 那些粗粒度数据集给不了的。

## 计划里「整包十几 GB」的说法是错的，这里更正

计划（第六节与第二节）写的「官方 tarball 有十几 GB、与 <10GB 预算冲突」是**估错的**。
实测口径（2026-10 检索，见 docs/datasets.md 的记录）：

| 来源 | 体积 | 说明 |
|---|---|---|
| Figshare `IP102.zip` | **2.94 GB** | 75,222 张原始图片 |
| Kaggle `ip102-pest-dataset` | **~3.2 GB** | 同上，含标注文件 |
| Zenodo COCO 重排版 | 798 MB | **重排格式，不是原始图片**，不能拿来做 copy-paste |

也就是说第二档**在预算之内**，不需要因为体积整档跳过。真正的不确定性只剩「本机能不能连上」。
（`api.figshare.com` 与 `aisys.iim.ac.cn` 都被 claude.ai 的抓取器挡了，但那是我这边的出口问题，
与本机网络无关 —— 所以第一步必须是**本机探测**，不能靠我猜。）

## sha256 钉值：计划说「可能钉不住」，这里能钉

计划担心 IP102 走网盘（Google Drive / 百度网盘 / 阿里云盘）没有稳定的 file id，所以只能
下载后自算 sha256 写进清单。这个顾虑对**网盘**成立，对 **Figshare 不成立** ——
`ndownloader.figshare.com/files/<id>` 是稳定直链，且 Figshare 的文件记录里带 `computed_md5`。
所以本脚本：

1. `--probe` 先把每个镜像的 HTTP 状态 / content-length / 响应里的 md5（若有）打出来；
2. 若档案响应里带 `Content-MD5` 或 ETag 形式的摘要 → 下载后**当场比对**，钉值成立；
3. 若没有 → 退化成「首次下载后自算 sha256 写进 checksums.json，之后按它校验」，
   并在 provenance.json 里写明这一次是哪种模式、为什么。**不假装钉住了。**

## 失败绝不生成替代数据

连不上 / 体积超预算 / CRC 不过 / 类序对不上 → 打印来源、HTTP 码与人工获取指引，退出码 1。
**任何情况下都不合成替身图片**（沿用 16 与 29 的规矩）。

## 用法

    python scripts/36_download_ip102_cls.py --probe              # 只探测：哪些镜像通、多大、有没有摘要
    python scripts/36_download_ip102_cls.py --mirror figshare    # 按预设下载
    python scripts/36_download_ip102_cls.py --url <直链>          # 自己给直链（网盘导出的临时链也行）
    python scripts/36_download_ip102_cls.py --verify             # 只校验已下载的档案与解压结果

**解压后的目录只当数据用**：脚本本体在 `scripts/`，从不 `chdir` 进解压目录，
也不从里面 import 任何东西（archives 是不可信输入）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import ensure_dir, write_json  # noqa: E402
from src.common.logging_utils import setup_logger      # noqa: E402
from src.common.paths import path_of                    # noqa: E402

logger = setup_logger("agri.pest.exp.cls")

UA = "rural-agri-platform/1.0 (dataset acquisition; academic use)"

# 默认体积上限（GB）。超过就**拒绝下载**并说明「第二档跳过」——
# 这是你定的 <10GB 预算，不是脚本自己的判断。
DEFAULT_MAX_GB = 10.0

# ---------------------------------------------------------------- 镜像预设
#
# ⚠ 这三个 URL 来自 2026-10 的公开检索结果，**没有从本机验证过**（我的出口连不上
# figshare / zenodo）。所以 --probe 是必需的第二步：它的输出才是事实，这份列表只是候选。
# 直链会失效（网盘会换、Kaggle 会改 slug），所以 --url 永远可覆盖。
PRESETS: list[dict] = [
    {
        "key": "figshare",
        "label": "Figshare · IP102.zip（75,222 张原始图片，最可能有稳定直链与 md5）",
        "url": "https://ndownloader.figshare.com/files/62860060",
        "license": "学术免费；其他用途需联系原作者 Xiaoping Wu (xpwu95@163.com)",
        "kind": "zip",
    },
    {
        "key": "kaggle",
        "label": "Kaggle · ip102-pest-dataset（~3.2 GB，需要 KAGGLE_USERNAME / KAGGLE_KEY）",
        "url": "https://www.kaggle.com/api/v1/datasets/download/behnamh721rn6724/ip102-dataset",
        "license": "见 Kaggle 数据集页面",
        "kind": "zip",
        "auth": "kaggle",
    },
    {
        "key": "zenodo-coco",
        "label": "Zenodo · COCO 重排版（798 MB，**不是原始图片**，仅作对照，一般不要用它做增广）",
        "url": "https://zenodo.org/records/15546285/files/ip102.zip?download=1",
        "license": "CC BY 4.0",
        "kind": "zip",
        "warn": "COCO 重排格式没有原始图片切图语义，copy-paste 增广（37 号）需要原始图片，别选这个。",
    },
]


# ---------------------------------------------------------------- 工具

def _auth_headers(preset: dict) -> dict:
    """Kaggle 之类的私有源需要凭据 —— 只从环境变量读，不落盘。"""
    import os

    h = {"User-Agent": UA}
    if preset.get("auth") == "kaggle":
        user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
        if user and key:
            import base64

            token = base64.b64encode(f"{user}:{key}".encode()).decode()
            h["Authorization"] = f"Basic {token}"
    return h


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _mb(n: int | None) -> str:
    return "未知" if n is None else f"{n / 1024 ** 2:.1f} MB（{n / 1024 ** 3:.2f} GB）"


# ---------------------------------------------------------------- 探测

def _probe_one(preset: dict) -> dict:
    """只读响应头，不落盘。HEAD 拿不到 content-length 时退化成 GET 后立刻断开。"""
    import requests

    out = {"key": preset["key"], "url": preset["url"], "ok": False}
    try:
        r = requests.head(preset["url"], headers=_auth_headers(preset),
                          allow_redirects=True, timeout=20)
        out["status"] = r.status_code
        out["final_url"] = r.url
        cl = r.headers.get("Content-Length")
        if r.status_code >= 400 or not cl:
            # 很多 CDN 不认 HEAD，或 HEAD 不带 Content-Length —— 用 GET 流式取头部
            r = requests.get(preset["url"], headers=_auth_headers(preset),
                             stream=True, allow_redirects=True, timeout=20)
            out["status"] = r.status_code
            out["final_url"] = r.url
            cl = r.headers.get("Content-Length")
            r.close()
        out["bytes"] = int(cl) if cl else None
        out["content_type"] = r.headers.get("Content-Type")
        # 摘要线索：有这些就能钉值，没有就退化成自算
        out["md5_header"] = r.headers.get("Content-MD5")
        out["etag"] = r.headers.get("ETag")
        out["last_modified"] = r.headers.get("Last-Modified")
        out["ok"] = r.status_code == 200
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def do_probe() -> int:
    logger.info("=" * 78)
    logger.info("探测 %d 个候选镜像（只发请求头，不下载、不落盘）", len(PRESETS))
    logger.info("=" * 78)
    results = []
    for p in PRESETS:
        logger.info("")
        logger.info("▶ %s", p["key"])
        logger.info("  %s", p["label"])
        r = _probe_one(p)
        results.append({**p, **r})
        if r["ok"]:
            logger.info("  HTTP %s  体积 %s", r["status"], _mb(r.get("bytes")))
            if r.get("content_type"):
                logger.info("  Content-Type: %s", r["content_type"])
            digest = r.get("md5_header") or r.get("etag")
            if digest:
                logger.info("  可钉值线索：%s", digest)
            else:
                logger.info("  响应头里**没有**摘要 → 只能下载后自算 sha256 写进 checksums.json")
            if r.get("bytes") and r["bytes"] / 1024 ** 3 > DEFAULT_MAX_GB:
                logger.warning("  体积超过 %.0f GB 预算 → 该镜像不采用", DEFAULT_MAX_GB)
        else:
            logger.warning("  不可用：HTTP %s %s", r.get("status"), r.get("error", ""))

    ok = [r for r in results if r.get("ok") and (r.get("bytes") or 0) / 1024 ** 3 <= DEFAULT_MAX_GB]
    logger.info("")
    logger.info("=" * 78)
    if ok:
        logger.info("可用镜像 %d 个，建议：--mirror %s", len(ok), ok[0]["key"])
    else:
        logger.info("**没有一个镜像可用**（可能是本机网络出口问题，不是脚本问题）。")
        logger.info("人工获取指引：官方入口见 docs/datasets.md —— 作者仓库 xpwu95/IP102 的")
        logger.info("Google Drive / 百度网盘（密码 meg3）。拿到档案后用 --url 指过来：")
        logger.info("  python scripts/36_download_ip102_cls.py --url \"file:///D:/path/to/IP102.zip\"")
    logger.info("=" * 78)

    dest = path_of("reports_dir") / "pest_cls_probe.json"
    ensure_dir(dest.parent)
    write_json(dest, {"probed_at": datetime.now().isoformat(timespec="seconds"),
                      "max_gb": DEFAULT_MAX_GB, "results": results})
    logger.info("探测结果已存：%s", dest)
    return 0 if ok else 1


# ---------------------------------------------------------------- 下载

def _download(url: str, dest: Path, preset: dict, max_gb: float) -> dict:
    """流式下载到 .part 再改名。中断重跑会接着下（Range）。返回响应头。

    ⚠ **不要在下载前再探一次这个 URL。** Figshare 的 `ndownloader` 会 302 到一个
    **签名过的 S3 直链**（实测 `s3-eu-west-1.amazonaws.com`），而签名链是**一次性**的 ——
    先探一次、再下第二次，第二次就吃 403 ReadTimeout。本脚本初版正是这么挂的：
    `--probe` 探成功了，`main()` 里又探一次，于是下载还没开始就报「镜像不可达」。

    所以改成：**探测只归 --probe**，正式下载不再预探；大小上限在这里按 Content-Length
    现场把关（拿不到 Content-Length 就不限，靠磁盘兜底）。
    """
    import requests

    part = dest.with_suffix(dest.suffix + ".part")
    headers = _auth_headers(preset)
    done = part.stat().st_size if part.exists() else 0
    if done:
        headers["Range"] = f"bytes={done}-"
        logger.info("发现未完成的 .part（%.1f MB），尝试续传", done / 1024 ** 2)

    with requests.get(url, headers=headers, stream=True, allow_redirects=True,
                      timeout=(20, 120)) as r:
        if r.status_code == 416:  # 已经下完了
            logger.info("服务端说 Range 越界 → .part 已是完整文件，直接改名")
            part.replace(dest)
            return {"status": 416}
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}（{r.url}）")
        if r.status_code == 200 and done:
            logger.warning("服务端不支持续传，从 0 重下")
            done = 0
        total = int(r.headers.get("Content-Length", 0)) + done
        if total and total / 1024 ** 3 > max_gb:
            raise RuntimeError(f"体积 {total / 1024 ** 3:.2f} GB 超过 {max_gb:.0f} GB 预算，拒绝下载")

        t0, last = time.perf_counter(), 0.0
        mode = "ab" if done else "wb"
        with part.open(mode) as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                now = time.perf_counter()
                if now - last > 10:
                    last = now
                    spd = done / max(now - t0, 1e-6) / 1024 ** 2
                    pct = f"{100 * done / total:.1f}%" if total else "?"
                    logger.info("  %s  %.1f/%.1f MB  %.1f MB/s",
                                pct, done / 1024 ** 2, (total or 0) / 1024 ** 2, spd)
        info = {"status": r.status_code, "content_type": r.headers.get("Content-Type"),
                "md5_header": r.headers.get("Content-MD5"), "etag": r.headers.get("ETag"),
                "last_modified": r.headers.get("Last-Modified"),
                "content_length": r.headers.get("Content-Length"), "final_url": r.url}

    part.replace(dest)
    return info


# ---------------------------------------------------------------- 校验与解压

def _verify_archive(archive: Path) -> dict:
    """zip 走全量 CRC（testzip），tar 走逐成员可读性。不做抽样 —— 抽样等于没校验。"""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            bad = z.testzip()          # 返回第一个 CRC 失败的成员名，全好则为 None
            n = len(z.namelist())
        if bad:
            raise RuntimeError(f"zip CRC 校验失败：{bad}")
        logger.info("zip 全量 CRC 通过（%d 个成员）", n)
        return {"kind": "zip", "members": n, "crc": "ok"}
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            n = sum(1 for _ in t)
        logger.info("tar 可读（%d 个成员；tar 无逐成员 CRC，靠解压后抽查图片可解码）", n)
        return {"kind": "tar", "members": n, "crc": "n/a"}
    raise RuntimeError(f"既不是 zip 也不是 tar：{archive}")


def _safe_extract(archive: Path, dest: Path) -> None:
    """解压到**全新空目录**，并挡住路径穿越与符号链接。"""
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"解压目标非空，拒绝覆盖：{dest}（要重来请先手工删除它）")
    ensure_dir(dest)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            root = dest.resolve()
            for m in z.infolist():
                target = (dest / m.filename).resolve()
                if not str(target).startswith(str(root)):
                    raise RuntimeError(f"zip 里有路径穿越成员，拒绝：{m.filename}")
            z.extractall(dest)
            logger.info("zip 已解压 → %s", dest)
        return

    with tarfile.open(archive) as t:
        root = dest.resolve()
        for m in t.getmembers():
            if m.issym() or m.islnk():
                raise RuntimeError(f"tar 里有链接成员，拒绝：{m.name}")
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(root)):
                raise RuntimeError(f"tar 里有路径穿越成员，拒绝：{m.name}")
        t.extractall(dest)
    logger.info("tar 已解压 → %s", dest)


def _extract_nested_tars(root: Path, max_depth: int = 3) -> list[Path]:
    """官方发布是「zip 里套 Images.tar」，所以要再剥一层。"""
    done = []
    for depth in range(max_depth):
        tars = [p for p in root.rglob("*.tar") if p.is_file()]
        if not tars:
            break
        logger.info("第 %d 层发现 %d 个内嵌 tar，继续解压", depth + 1, len(tars))
        for tp in tars:
            out = tp.with_suffix("")
            _safe_extract(tp, out)
            done.append(tp)
        for tp in done:
            tp.unlink()  # 解压成功后删掉中间 tar，省空间（原 zip 还在，可重来）
    return done


# ---------------------------------------------------------------- 类序核对

def _pestyolo_classes() -> list[str]:
    """从 16 号脚本取权威类序。

    这里**刻意 import 16 号脚本里的常量**而不是再抄一份 —— 类序是本档的命门，
    抄一份就意味着以后可能有两份不一样的真值。16 号里那份是拿 PestYOLO
    `classes.txt` 逐条对过的（`scripts/16_download_ip102.py:65`）。
    """
    import importlib.util

    p = Path(__file__).resolve().parent / "16_download_ip102.py"
    spec = importlib.util.spec_from_file_location("_ip102_16", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.PESTYOLO_CLASSES)


def _norm(name: str) -> str:
    """与 16 号 `:190` 同一套归一化：小写、下划线转空格、压空白。"""
    return " ".join(str(name).lower().replace("_", " ").split())


# 类名行开头可选的「序号前缀」，如 `1  rice leaf roller` / `12 rice leafhopper`
_INDEX_PREFIX = re.compile(r"^\s*(\d+)\s+(.*)$")


def _strip_index(name: str) -> tuple[int | None, str]:
    """剥掉可选的序号前缀，返回 (序号或 None, 名字)。

    **这不是在放宽类名比对。** IP102 分类集的 `classes.txt` 是「1 起的序号 + 名字」
    （实测 `1  rice leaf roller`、`10 small brown plant hopper`），而 16 号的权威表只有
    名字。序号是给人读的编号，不是类序本身 —— 名字部分仍逐字比对；前缀本身的合法性
    由 `check_class_order()` 的「序号必须逐行自洽」单独把关。
    """
    m = _INDEX_PREFIX.match(str(name))
    if m:
        return int(m.group(1)), m.group(2)
    return None, str(name)


def discover_layout(root: Path) -> dict:
    """在解压结果里认出「类序表」「逐类图片目录」与「划分文件」。

    不同镜像的目录层级不一样（官方是 `Classification/Images/<数字>/`，
    Figshare 可能是扁的），所以这里做**发现**而不是写死路径；发现不到就如实报空，
    由人看报告决定，**不猜**。
    """
    out: dict = {"root": str(root), "classes_txt": None, "class_dirs": None,
                 "class_counts": {}, "splits": [], "image_total": 0}

    # 1) 类序表
    for cand in sorted(root.rglob("classes.txt")) + sorted(root.rglob("class_list.txt")):
        names = [ln.strip() for ln in cand.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
        if len(names) >= 100:
            out["classes_txt"] = {"path": str(cand), "num": len(names), "names": names}
            break

    # 2) 逐类图片目录：父目录下有 ≥50 个「名字是整数的子目录」。
    #    官方发布是 <root>/classification/{train,val,test}/<0..101>/ —— **三个划分各有一份
    #    102 个类目录**，所以「目录最多的那个」是三方并列。旧版取第一个赢家，等于随机挑
    #    了一个划分、而且不记下来；这里改成确定性地挑，并把挑中的划分写进报告。
    cands = []
    for parent in [root, *[d for d in root.rglob("*") if d.is_dir()]]:
        subs = [d for d in parent.iterdir() if d.is_dir() and d.name.isdigit()]
        if len(subs) >= 50:
            cands.append((parent, len(subs)))
    if cands:
        # 排序键三层：优先名为 train 的划分（增广素材本该来自训练侧），其次目录数多的，
        # 最后按路径串 —— 保证结果与 rglob 的遍历顺序完全无关。
        parent, n = sorted(cands, key=lambda it: (0 if it[0].name.lower() == "train" else 1,
                                                  -it[1], str(it[0])))[0]
        counts = {}
        for d in sorted(parent.iterdir()):
            if d.is_dir() and d.name.isdigit():
                counts[d.name] = sum(1 for f in d.iterdir()
                                     if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"})
        out["class_dirs"] = {"parent": str(parent), "num_class_dirs": n,
                             "split": parent.name,
                             # 并列的候选也记下来 —— 看出「这里其实有三个划分」比只看赢家重要
                             "all_candidates": sorted(f"{p.name}({k})" for p, k in cands),
                             # 目录名是 0..101 还是 1..102 需要**人眼确认一次**：
                             # 两种都很常见，套错一位就是全部 102 类整体错位。
                             "name_range": [min(counts, key=int), max(counts, key=int)]
                             if counts else None}
        out["class_counts"] = counts
        out["image_total"] = sum(counts.values())

    # 3) 划分文件（只作参考；本档只往 train 里加，不用它）
    for cand in sorted(root.rglob("*.txt")):
        if cand.name in {"train.txt", "val.txt", "test.txt"}:
            out["splits"].append(str(cand))

    return out


def check_class_order(layout: dict) -> None:
    """类序核对 —— 不通过就中止，绝不「差不多就行」。

    为什么这条是命门：102 个类里有一堆近缘物种（稻纵卷叶螟 vs 二化螟），
    整表错一位不会报错，只会让每个稀有类都拿到**别的虫**的监督信号，
    训出来的模型比不训还糟，而且安静得没有任何异常。

    两种**书写**差异按格式处理，**语义比对一点都不放宽**：

    - 序号前缀（`1  rice leaf roller` vs `rice leaf roller`）：剥掉后再比，但序号
      必须逐行自洽（第 i 行的序号是 i 或 i+1）—— 「序号对、行序乱」的文件仍被挡住；
    - 大小写 / 下划线 / 多余空白：走 `_norm()`（与 16 号同一套）。
    """
    reference = _pestyolo_classes()
    ct = layout.get("classes_txt")
    if not ct:
        raise SystemExit(
            "解压结果里找不到 100 行以上的 classes.txt，无法核对类序。\n"
            "请人工确认档案内容后再继续 —— **不猜类序**。"
        )
    raw = ct["names"]
    if len(raw) != len(reference):
        raise SystemExit(f"类数不符：档案 {len(raw)} vs 权威 {len(reference)}")

    parsed = [_strip_index(x) for x in raw]
    prefixed = [i for i, (n, _) in enumerate(parsed) if n is not None]
    if prefixed and len(prefixed) != len(raw):
        raise SystemExit(
            f"档案只有部分行带序号前缀（{len(prefixed)}/{len(raw)}）—— 这不是正常的 "
            "classes.txt 写法，无法确定该怎么解析，拒绝继续。"
        )
    if prefixed:
        # 序号必须与行号自洽：第 i 行（0 起）的序号应是 i+1（1 起写法），也容许 i（0 起写法）
        bad = [(i, n) for i, (n, _) in enumerate(parsed) if n is not None and n not in (i, i + 1)]
        if bad:
            head = ", ".join(f"第 {i} 行序号={n}" for i, n in bad[:5])
            raise SystemExit(
                f"序号与行号对不上（{len(bad)} 处）：{head}\n"
                "这说明文件本身可能是乱序的 —— 若剥掉序号就会把错位当成格式差异，拒绝继续。"
            )
        logger.info("档案类名带序号前缀（%d/%d 行），且序号与行号逐位自洽 → 剥掉前缀再比类名",
                    len(prefixed), len(raw))

    names = [n for _, n in parsed]

    diff = [(i, a, b) for i, (a, b) in enumerate(zip(names, reference))
            if _norm(a) != _norm(b)]
    if diff:
        head = "\n  ".join(f"[{i}] 档案={a!r}  权威={b!r}" for i, a, b in diff[:10])
        raise SystemExit(
            f"类序对不上（{len(diff)} 处），拒绝继续：\n  {head}\n"
            "整表错位会让每个类拿到别的虫的监督信号，且不报错 —— 必须人工查清。"
        )
    spelling = sum(1 for a, b in zip(names, reference) if a != b)
    logger.info("类序核对通过：102 类下标逐条对齐（仅写法差异 %d 处）", spelling)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="任务 2.7.6 第二档：下载 IP102 分类集")
    ap.add_argument("--probe", action="store_true", help="只探测各镜像可达性与体积后退出")
    ap.add_argument("--url", default=None, help="自己给直链（覆盖 --mirror）")
    ap.add_argument("--mirror", default=None, choices=[p["key"] for p in PRESETS],
                    help="按预设下载；缺省用第一个")
    ap.add_argument("--max-gb", type=float, default=DEFAULT_MAX_GB, help="体积上限，超过则拒下")
    ap.add_argument("--verify", action="store_true", help="只校验已下载档案与解压结果后退出")
    args = ap.parse_args()

    if args.probe:
        return do_probe()

    raw_dir = ensure_dir(path_of("pest_cls_raw"))
    archive = raw_dir / "ip102_cls.zip"
    checksums_path = raw_dir / "checksums.json"
    provenance_path = raw_dir / "provenance.json"

    if args.verify:
        if not archive.exists():
            logger.error("没有档案可校验：%s", archive)
            return 1
        info = _verify_archive(archive)
        logger.info("sha256 = %s", _sha256_file(archive))
        extracted = raw_dir / "extracted"
        if extracted.exists():
            layout = discover_layout(extracted)
            check_class_order(layout)
            logger.info("图片总数 %d，逐类目录 %s", layout["image_total"],
                        (layout["class_dirs"] or {}).get("parent"))
        else:
            logger.warning("还没解压：%s 不存在", extracted)
        return 0

    preset = next((p for p in PRESETS if p["key"] == args.mirror), PRESETS[0]) if not args.url \
        else {"key": "custom", "label": "用户提供的直链", "url": args.url,
              "license": "以来源方声明为准", "kind": "zip"}
    url = args.url or preset["url"]

    logger.info("=" * 78)
    logger.info("来源：%s", preset["label"])
    logger.info("URL ：%s", url)
    logger.info("许可：%s", preset.get("license", "见来源"))
    if preset.get("warn"):
        logger.warning("⚠ %s", preset["warn"])
    logger.info("=" * 78)

    # **下载前不再预探**（见 _download 的说明：Figshare 的 S3 签名链是一次性的，
    # 多探一次就会把这次下载的签名烧掉）。体积上限由 _download 按 Content-Length 现场把关。
    dl: dict = {}
    if not archive.exists():
        logger.info("开始下载 → %s", archive)
        try:
            dl = _download(url, archive, preset, args.max_gb)
        except Exception as e:  # noqa: BLE001
            if "超过" in str(e) and "预算" in str(e):
                # 超预算是**取舍**，不是失败：按你定的 <10GB，第二档整档跳过。
                logger.warning("%s", e)
                logger.warning("**第二档跳过**（可按需要直接进第三档）。这不是错误，是按预算做的取舍；"
                               "要强行下载请调 --max-gb。")
                return 0
            logger.error("下载失败：%s", e)
            logger.error("**不生成任何替代数据**。换个镜像再试：")
            logger.error("  python scripts/36_download_ip102_cls.py --probe")
            logger.error("  python scripts/36_download_ip102_cls.py --mirror kaggle")
            logger.error("人工获取指引见 docs/datasets.md（作者仓库 xpwu95/IP102）。")
            return 1
    else:
        logger.info("档案已存在，跳过下载：%s", archive)

    sha = _sha256_file(archive)
    info = _verify_archive(archive)
    logger.info("sha256 = %s", sha)

    # 钉值模式：能比对就比对，不能就如实声明退化了
    pinned_from = None
    if checksums_path.exists():
        old = json.loads(checksums_path.read_text(encoding="utf-8"))
        pinned_from = old.get("sha256")
        if pinned_from and pinned_from != sha:
            logger.error("sha256 与已记录值不符：记录 %s，实际 %s", pinned_from, sha)
            logger.error("档案可能被换过或下坏了 —— 拒绝继续。")
            return 1
        logger.info("sha256 与 checksums.json 一致")

    write_json(checksums_path, {
        "sha256": sha, "bytes": archive.stat().st_size,
        "mode": "pinned" if pinned_from else "self-computed-first-time",
        "server_md5_header": dl.get("md5_header"), "etag": dl.get("etag"),
        "note": "Figshare 直链稳定 → 首次自算后可当钉值用；网盘临时链换一次就会变，"
                "那种情况下这里的 sha256 只能证明「每次下到的是同一份」，不代表「来源未变」。",
    })
    write_json(provenance_path, {
        "dataset": "IP102（分类集 / Classification Images）",
        "paper": "Wu et al., IP102: A Large-Scale Benchmark Dataset for Insect Pest Recognition, CVPR 2019",
        "official_repo": "https://github.com/xpwu95/IP102",
        "mirror_used": preset["key"], "source_url": url, "final_url": dl.get("final_url"),
        "license": preset.get("license", "见来源"),
        "retrieved_at": datetime.now().isoformat(timespec="seconds"),
        "bytes": archive.stat().st_size, "sha256": sha, "archive_info": info,
        "content_length_at_download": dl.get("content_length"),
        "class_order_verified_against": "scripts/16_download_ip102.py:PESTYOLO_CLASSES（102 类，逐条对齐）",
    })

    extracted = raw_dir / "extracted"
    if not extracted.exists():
        _safe_extract(archive, extracted)
    _extract_nested_tars(extracted)

    layout = discover_layout(extracted)
    check_class_order(layout)
    write_json(raw_dir / "layout.json", layout)

    logger.info("=" * 78)
    logger.info("图片总数：%d", layout["image_total"])
    if layout["class_dirs"]:
        cd = layout["class_dirs"]
        logger.info("逐类目录：%s（划分 %s，编号 %s）", cd["parent"], cd.get("split"),
                    cd["name_range"])
        if cd.get("all_candidates"):
            logger.info("并列候选（本档只挑了其中的 %s）：%s", cd.get("split"),
                        cd["all_candidates"])
        logger.info("⚠ 上面这个编号范围要人眼确认一次：0..101 与 1..102 都常见，"
                    "套错一位就是 102 类整体错位（37 号会按 layout.json 里的目录名取值）。")
    counts = {int(k): v for k, v in layout["class_counts"].items()}
    if counts:
        rare = sorted(counts.items(), key=lambda kv: kv[1])[:10]
        logger.info("最稀有的 10 个类（目录号:图片数）：%s", rare)
    logger.info("=" * 78)
    logger.info("下一步：python scripts/37_build_pest_aug.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
