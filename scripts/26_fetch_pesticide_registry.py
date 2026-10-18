#!/usr/bin/env python
"""任务 7.15 —— 采集农业农村部《农药登记信息》，建成「防治对象 → 登记农药」索引。

为什么做这件事
--------------
知识图书库原来的「用药提醒」是一段写死的通用安全提示（`library_build.SAFETY_NOTE`），
352 张卡片一模一样的同一句话，既没有信息量、也撑不起「可查证」这三个字。
这里去农业农村部农药管理司的官方登记公告里，把**真实登记过的农药**按防治对象抓下来
（登记证号、有效成分及含量、剂型、毒性、登记企业都是原文照录），
由 07 号脚本填进对应卡片。

数据链路（三跳，每一跳都落盘缓存）
----------------------------------
1. `POST http://www.chinapesticide.org.cn/zwb/getPage {id:753, pageNumber, limit}`
   这是中国农药信息网「数据中心 → 登记信息发布」栏目自己的分页接口（页面源码里
   laypage 回调直接这么调的），返回 JSON 数组：{id, title, processDate, info}。
2. `GET  http://www.chinapesticide.org.cn/zwb/detail/{id}`
   公告正文页，正文里有一句「请点击此处阅读原文并下载附件」，链到农业农村部站点。
3. `GET  https://zzys.moa.gov.cn/.../tXXXXXXXX_XXXXXXXX.htm`
   原文页的「附件下载」里给出真正的 `.xls`（BIFF8）——登记明细就在这张表里。
   表头行不在第 0 行（前两行是标题与说明），因此按「哪一行的单元格里有『登记证号』」
   来定位，而不是写死行号。

**只走 http 访问 chinapesticide.org.cn**：实测本机到该站的 443 不通（ConnectTimeout）、
80 通，所以把它写成 `HTTP_BASE`，不要顺手改成 https。

抓不到怎么办
------------
任何一跳失败都只记日志、不抛异常：这一趟拿到的文件写出，拿不到的跳过。
整趟一无所获时写出空索引，07 号脚本读到空索引会自动回退到原来的通用提醒，
页面照常可用（这是任务清单要求的降级路径）。

产出：data/pesticide_registry.json（运行期数据，已在 .gitignore 里）

用法：
    python scripts/26_fetch_pesticide_registry.py                # 抓最新 8 期公告
    python scripts/26_fetch_pesticide_registry.py --issues 20    # 多抓几期
    python scripts/26_fetch_pesticide_registry.py --refresh      # 忽略缓存重抓
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import write_json          # noqa: E402
from src.common.logging_utils import setup_logger   # noqa: E402
from src.common.paths import PROJECT_ROOT, path_of  # noqa: E402

logger = setup_logger("agri.pesticide")

# 该站的 443 在本机不通、80 通，改 https 会直接超时
HTTP_BASE = "http://www.chinapesticide.org.cn"
LIST_API = f"{HTTP_BASE}/zwb/getPage"
DETAIL_URL = HTTP_BASE + "/zwb/detail/{id}"
LIST_ID = 753                       # 「登记信息发布」栏目的 id（页面源码里写死的）

CACHE_DIR = PROJECT_ROOT / "data" / "pesticide_cache"
OUT_PATH = path_of("pesticide_registry")

REQ_GAP = 0.8                       # 站很小，别把人家打疼了：每个请求之间歇一下
TIMEOUT = 30
RETRIES = 3

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 表头别名 -> 内部字段名。官方几期的列名略有出入（「登记证持有人」/「登记企业」），
# 所以按别名表认列，认不出的列直接忽略（多一列少一列都不该让整张表读不出来）。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "reg_no": ("登记证号", "农药登记证号"),
    "product": ("农药名称", "产品名称"),
    "holder": ("登记证持有人", "登记企业", "生产企业", "登记证持有人名称"),
    "form": ("剂型",),
    "toxicity": ("毒性",),
    "ingredient": ("有效成分及其含量", "有效成分及含量", "有效成分"),
    "kind": ("农药类别", "类别"),
    "crop": ("作物/场所", "作物/场所名称", "作物及场所", "作物"),
    "target": ("防治对象", "防治对象名称"),
}

# 「防治对象」一格常常是一串：「稻瘟病、纹枯病」「一年生阔叶杂草」，
# 要拆开分别建索引，否则「纹枯病」永远查不到。
_TARGET_SPLIT = re.compile(r"[、,，;；/／]|和(?=[一-鿿]{2,})")

# 一格里的分隔符与括号尾巴：「稻瘟病(苗瘟)」「水稻田杂草」这类，建索引时保留原样，
# 另外再存一份去掉括号备注的形式，方便匹配。
_RE_PAREN = re.compile(r"[（(][^）)]{0,20}[）)]")


# ------------------------------------------------------------------ 抓取

def _get(url: str, *, refresh: bool = False, cache_name: str | None = None,
         binary: bool = False):
    """带重试与磁盘缓存的 GET。失败返回 None（不抛）。"""
    import requests

    cache_file = CACHE_DIR / cache_name if cache_name else None
    if cache_file and cache_file.exists() and not refresh:
        return cache_file.read_bytes() if binary else cache_file.read_text(encoding="utf-8")

    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            if binary:
                data = r.content
            else:
                r.encoding = r.apparent_encoding or "utf-8"
                data = r.text
            if cache_file:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                if binary:
                    cache_file.write_bytes(data)
                else:
                    cache_file.write_text(data, encoding="utf-8")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("第 %d/%d 次抓取失败：%s（%s）", attempt, RETRIES, url[:90], e)
            time.sleep(1.5 * attempt)
    logger.error("放弃抓取：%s（%s）", url[:110], last)
    return None


def fetch_issue_index(pages: int, *, refresh: bool = False) -> list[dict]:
    """取「登记信息发布」栏目的公告列表（该栏目自己的分页接口）。"""
    import requests

    out: list[dict] = []
    for page in range(1, pages + 1):
        cache_file = CACHE_DIR / f"issue_index_p{page}.json"
        if cache_file.exists() and not refresh:
            try:
                rows = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                rows = None
        else:
            rows = None

        if rows is None:
            try:
                r = requests.post(
                    LIST_API, headers={**UA, "X-Requested-With": "XMLHttpRequest"},
                    data={"id": LIST_ID, "pageNumber": page, "limit": 20}, timeout=TIMEOUT,
                )
                r.raise_for_status()
                rows = r.json()
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                logger.error("公告列表第 %d 页取不到：%s", page, e)
                break

        if not rows:
            break
        out.extend(rows)
        logger.info("公告列表第 %d 页：%d 条", page, len(rows))
        time.sleep(REQ_GAP)
    return out


_RE_MOA_LINK = re.compile(
    r"""<a[^>]+href=["']?(https?://[^"'\s>]*moa\.gov\.cn[^"'\s>]*)["']?[^>]*>\s*[^<]*(?:阅读原文|下载附件)"""
)
_RE_HREF_XLS = re.compile(r"""href=["']?([^"'\s>]*\.(?:xls|xlsx)[^"'\s>]*)""", re.I)
# 农业农村部的附件常包一层在线阅读器：/zxfile/reader?file=<真实文件地址>。
# 直接下这个壳子拿到的是 HTML，必须把 file= 里的真实地址抠出来。
_RE_READER_FILE = re.compile(r"[?&]file=([^&\s]+\.(?:xls|xlsx))", re.I)


def _real_xls_urls(html: str) -> list[str]:
    """从一页 HTML 里取出**真实可下载**的 .xls 直链（剥掉 reader 外壳）。"""
    urls: list[str] = []
    for m in _RE_HREF_XLS.finditer(html):
        href = m.group(1)
        # /zxfile/reader?file=... 这种壳子把真实地址放在 file= 参数里
        inner = _RE_READER_FILE.search(href)
        url = inner.group(1) if inner else href
        if not url.lower().startswith("http"):
            continue
        # 只看路径部分，别让 ?file=....xls 这种把 HTML 页面也当成附件
        if not re.search(r"\.(?:xls|xlsx)$", url.split("?")[0], re.I):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def find_attachment_urls(issue: dict, *, refresh: bool = False) -> list[str]:
    """一个公告 -> 它附件里所有 .xls 的直链（经 detail 页 -> moa 原文页两跳）。"""
    iid = issue["id"]
    html = _get(DETAIL_URL.format(id=iid), refresh=refresh, cache_name=f"detail_{iid}.html")
    if not html:
        return []
    time.sleep(REQ_GAP)

    m = _RE_MOA_LINK.search(html)
    if not m:
        # 有些期次的正文把原文链接写成普通 <a>，没有那句提示语，退一步找任意 moa 链接
        m = re.search(r"""<a[^>]+href=["']?(https?://[^"'\s>]*moa\.gov\.cn[^"'\s>]*)""", html)
    if not m:
        logger.info("  %s 正文里没有外链，跳过", issue.get("title", iid))
        return []

    moa_url = m.group(1)
    digest = hashlib.md5(moa_url.encode("utf-8")).hexdigest()[:12]
    moa_html = _get(moa_url, refresh=refresh, cache_name=f"moa_{digest}.html")
    if not moa_html:
        return []
    time.sleep(REQ_GAP)

    urls = _real_xls_urls(moa_html)
    if not urls:
        logger.info("  %s 原文页没有 .xls 附件", issue.get("title", iid))
    return urls


# ------------------------------------------------------------------ 解析

def _rows_from_grid(grid: list[list[str]], source: dict) -> list[dict]:
    """把「二维字符串表」按表头映射成记录。BIFF 与 HTML 两条路都汇到这里。

    表头行不写死：实测第 0 行空、第 1 行是大标题「农药登记延续信息」、第 2 行才是
    真表头，而不同期次的空行数不一样，所以按「哪一行里有『登记证号』」来找。
    """
    header_row, cols = -1, {}
    for r, cells in enumerate(grid[:10]):
        if not any(c in ("登记证号", "农药名称") for c in cells):
            continue
        mapping: dict[str, int] = {}
        for key, aliases in COLUMN_ALIASES.items():
            for c, cell in enumerate(cells):
                if cell in aliases:
                    mapping[key] = c
                    break
        if {"reg_no", "product", "target"} <= set(mapping):
            header_row, cols = r, mapping
            break
    if header_row < 0:
        return []

    rows: list[dict] = []
    for cells in grid[header_row + 1:]:
        def cell(key: str) -> str:
            c = cols.get(key)
            if c is None or c >= len(cells):
                return ""
            return cells[c]

        reg_no, target = cell("reg_no"), cell("target")
        if not reg_no or not target:
            continue
        rows.append({
            "reg_no": reg_no, "product": cell("product"), "holder": cell("holder"),
            "form": cell("form"), "toxicity": cell("toxicity"),
            "ingredient": cell("ingredient"), "kind": cell("kind"), "crop": cell("crop"),
            "target": target, "source": source["title"], "source_url": source["url"],
        })
    logger.info("  表：%d 行（表头在第 %d 行）", len(rows), header_row + 1)
    return rows


def _html_grid(content: bytes) -> list[list[str]] | None:
    """政府站点的 .xls 有一小部分其实是「HTML 表格改后缀」，那种不需要 Excel 库。"""
    head = content[:1024].lstrip().lower()
    if not (head.startswith(b"<") or b"<html" in head or b"<table" in head):
        return None
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    grid = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", text):
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)
        grid.append([re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in cells])
    return grid or None


def parse_xls(content: bytes, source: dict) -> list[dict]:
    """一张登记明细表 -> 记录列表。读不动就返回空表（不抛）。"""
    fname = source.get("file") or source.get("url", "?")

    grid = _html_grid(content)
    if grid is not None:
        rows = _rows_from_grid(grid, source)
        if rows:
            return rows
        logger.warning("  %s 是 HTML 伪装的表格，但没认出表头，跳过", fname)
        return []

    try:
        import xlrd
    except ImportError:
        logger.error("  没有 xlrd，读不了 BIFF 的 .xls（见 requirements.txt）")
        return []

    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as e:  # noqa: BLE001
        logger.error("  xls 打不开（%s）：%s", fname, e)
        return []

    rows: list[dict] = []
    for sheet in book.sheets():
        grid = [[str(sheet.cell_value(r, c)).strip().replace("  ", " ")
                 for c in range(sheet.ncols)] for r in range(sheet.nrows)]
        # 整数化的浮点（序号/年份常被存成 1.0）去掉尾巴，免得写进数据里难看
        grid = [[re.sub(r"\.0$", "", c) if re.fullmatch(r"-?\d+\.0", c) else c for c in row]
                for row in grid]
        rows.extend(_rows_from_grid(grid, source))
    return rows


def _target_keys(text: str) -> list[str]:
    """把一个「防治对象」单元格拆成若干可检索的键。"""
    keys: list[str] = []
    for part in _TARGET_SPLIT.split(text):
        part = part.strip(" 　。.")
        if len(part) < 2:
            continue
        if part not in keys:
            keys.append(part)
        bare = _RE_PAREN.sub("", part).strip()
        if bare and len(bare) >= 2 and bare not in keys:
            keys.append(bare)
    return keys


def build_registry(issues: list[dict], *, refresh: bool = False) -> dict:
    records: list[dict] = []
    used_sources: list[dict] = []
    seen_reg: set[tuple[str, str]] = set()

    for issue in issues:
        title = issue.get("title", "")
        urls = find_attachment_urls(issue, refresh=refresh)
        for url in urls:
            fname = url.rsplit("/", 1)[-1]
            # 缓存名带上 URL 摘要：同一个文件名可能来自 reader 外壳与真实附件两个不同的
            # URL，只按文件名缓存会让"壳子"的 HTML 覆盖掉真正的表格（踩过一次）。
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
            content = _get(url, refresh=refresh, cache_name=f"{digest}_{fname}", binary=True)
            if not content:
                continue
            time.sleep(REQ_GAP)
            src = {"title": title, "url": url, "file": fname,
                   "page": DETAIL_URL.format(id=issue["id"])}
            rows = parse_xls(content, src)
            added = 0
            for row in rows:
                # 同一条登记可能在不同期次里重复出现（延续公告会重复列出），按证号+对象去重
                key = (row["reg_no"], row["target"])
                if key in seen_reg:
                    continue
                seen_reg.add(key)
                records.append(row)
                added += 1
            if added:
                used_sources.append({**src, "rows": added})
                logger.info("  %s -> %s：新增 %d 条", title, fname, added)
            else:
                logger.warning("  %s -> %s：没解析出记录（附件可能不是表格）", title, fname)

    # 按防治对象建索引
    targets: dict[str, list[dict]] = {}
    for row in records:
        for key in _target_keys(row["target"]):
            targets.setdefault(key, []).append(row)

    return {
        "version": 1,
        "source_name": "农业农村部农药管理司《农药登记信息》",
        "source_url": f"{HTTP_BASE}/zwb/dataCenter",
        "list_api": LIST_API,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "notice": (
            "本索引逐条摘自农业农村部农药管理司公开发布的《农药登记信息》附件（.xls），"
            "登记证号、农药名称、有效成分及含量、剂型、毒性、登记企业与防治对象均为原文照录，"
            "未做任何改写。登记信息会随公告更新，请以最新公告与农药登记标签为准。"
        ),
        "total_records": len(records),
        "target_count": len(targets),
        "sources": used_sources,
        "targets": targets,
    }


# ------------------------------------------------------------------ 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="抓取农业农村部《农药登记信息》建索引")
    ap.add_argument("--issues", type=int, default=8, help="抓最新多少期公告（默认 8）")
    ap.add_argument("--pages", type=int, default=2, help="公告列表翻几页（每页 20 条）")
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存重新抓")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 70)
    logger.info("取公告列表……")
    all_issues = fetch_issue_index(args.pages, refresh=args.refresh)
    issues = [it for it in all_issues if "登记信息" in it.get("title", "")]
    if not issues:
        issues = all_issues
    issues = issues[: args.issues]
    logger.info("选取 %d 期公告", len(issues))
    for it in issues:
        logger.info("   %s  %s", it.get("title", ""), it.get("processDate", ""))

    data = build_registry(issues, refresh=args.refresh)
    write_json(OUT_PATH, data)

    logger.info("=" * 70)
    logger.info("已写出：%s", OUT_PATH)
    logger.info("登记记录 %d 条、防治对象 %d 个、来源文件 %d 个",
                data["total_records"], data["target_count"], len(data["sources"]))
    top = sorted(data["targets"].items(), key=lambda kv: -len(kv[1]))[:15]
    for key, rows in top:
        logger.info("   %-14s %d 个登记农药", key, len(rows))

    if not data["total_records"]:
        logger.warning("一条都没抓到。07 号脚本会自动回退到通用用药提醒，页面不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
