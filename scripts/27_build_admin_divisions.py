#!/usr/bin/env python
"""阶段 7 / 7.1 —— 构建「省 → 市 → 县」三级离线行政区划数据集（含中心经纬度）。

产出两份东西，**内容同源**（同一趟抓取、同一套清洗规则）：

    data/admin_divisions.json               完整版（带 adcode，供脚本/接口用，可作为来源追溯）
    frontend/assets/data/admin_divisions.js 前端打包版（window.__ADMIN_DIVISIONS__）

为什么要有它：
    「我的农田」新建田块时，用户只该填到「县」为止 —— 经纬度是**推导出来的**，
    不该让农户手抄一串小数。推导的口径见 frontend/assets/js/farm.js：
    省/市/县三级坐标**取平均**，缺哪一级就用有哪一级的。

数据源：阿里 DataV geoatlas（areas_v3），与 frontend/assets/geo/china.json **同源同格式**
    https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json        34 个省
    https://geo.datav.aliyun.com/areas_v3/bound/{省adcode}_full.json    该省的地级市
    https://geo.datav.aliyun.com/areas_v3/bound/{市adcode}_full.json    该市的区县

    直辖市（北京/天津/上海/重庆）这一层的 `_full.json` 直接返回**区县**（level=district），
    没有地级市这一层。本脚本对这种情形合成一个同名「市」—— 否则三级联动会在第二级断掉。
    港澳同理（18 个区直接挂在特别行政区下）。台湾等抓不到的省份**不编**：
    cities 为空，前端退回省级中心，页面照样能用。

缓存：每个 adcode 的原始响应落到 data/admin_divisions_cache/{adcode}.json，
    重复运行不重复下载（--refresh 可强制重来）。缓存目录是中间产物，不进版本库。

用法：
    python scripts/27_build_admin_divisions.py            # 用缓存，缺什么补什么
    python scripts/27_build_admin_divisions.py --refresh  # 忽略缓存重新抓
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.io_utils import write_json                      # noqa: E402
from src.common.logging_utils import setup_logger               # noqa: E402
from src.common.paths import PROJECT_ROOT, path_of              # noqa: E402

logger = setup_logger("agri.admin_divisions")

DATA_URL = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
CHINA_ADCODE = "100000"

# 请求间隔（秒）。这是个公开的静态资源站，没有 robots 限制，但一趟要打 ~380 次请求，
# 不客气一点就是白嫖带宽。0.12s 下整趟约 1~2 分钟。
REQUEST_GAP = 0.12
RETRIES = 3

_CACHE_DIR: Path | None = None
_REFRESH = False
_LAST_CALL = [0.0]


# ------------------------------------------------------------------ 抓取

def _http_get(url: str) -> str:
    """取文本。优先 requests（项目已有依赖），没有就退回 urllib。"""
    try:
        import requests
    except ImportError:  # pragma: no cover - 环境里一般都有
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    resp = requests.get(url, headers={"User-Agent": _UA, "Referer": "https://datav.aliyun.com/"}, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_area(adcode: str) -> dict | None:
    """取某个 adcode 的 `_full.json`（自身 + 下一级）。

    返回 None 表示**这个 adcode 抓不到**（404、超时、重试用尽）。
    刻意不抛异常：台湾/港澳这类查不到的省份只是「没有下级」，不该让整趟抓取作废。
    """
    assert _CACHE_DIR is not None
    cache = _CACHE_DIR / f"{adcode}.json"
    if cache.exists() and not _REFRESH:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass  # 缓存坏了就当没有

    url = DATA_URL.format(adcode=adcode)
    for attempt in range(1, RETRIES + 1):
        gap = REQUEST_GAP - (time.time() - _LAST_CALL[0])
        if gap > 0:
            time.sleep(gap)
        _LAST_CALL[0] = time.time()
        try:
            text = _http_get(url)
            data = json.loads(text)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
            return data
        except Exception as e:  # noqa: BLE001 - 网络异常种类多，统一按「抓不到」处理
            if attempt >= RETRIES:
                logger.warning("抓取 %s 失败（已重试 %d 次）：%s", url, RETRIES, e)
                return None
            time.sleep(0.6 * attempt)   # 退避
    return None


def _children(payload: dict | None) -> list[dict]:
    """从 GeoJSON 里取出下一级的 (adcode, name, center, level)。

    只保留 6 位数字 adcode 的要素 —— DataV 的省级文件里混着一个 `100000_JD`
    （南海诸岛示意），它不是行政区，塞进省市县联动里只会变成一个选不出来的空选项。
    """
    out: list[dict] = []
    for feat in (payload or {}).get("features", []):
        p = feat.get("properties") or {}
        code = str(p.get("adcode") or "")
        if not (len(code) == 6 and code.isdigit()):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        center = p.get("center") or p.get("centroid")
        out.append({
            "adcode": code,
            "name": name,
            "level": str(p.get("level") or ""),
            "center": _point(center),
        })
    return out


def _point(center) -> list[float] | None:
    """规范化成 [lng, lat]；取不到就是 None（而不是 [0, 0] —— 那会落到几内亚湾）。"""
    try:
        lng, lat = float(center[0]), float(center[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        return None
    return [round(lng, 4), round(lat, 4)]


# ------------------------------------------------------------------ 组装

def build(*, refresh: bool = False) -> dict:
    global _CACHE_DIR, _REFRESH
    _CACHE_DIR = PROJECT_ROOT / "data" / "admin_divisions_cache"
    _REFRESH = refresh

    china = fetch_area(CHINA_ADCODE)
    provinces_raw = _children(china)
    if not provinces_raw:
        raise RuntimeError(
            "没能取到全国省级列表（data/admin_divisions_cache/100000.json 为空或抓取失败）。"
            "请检查网络后重跑，或删掉 --refresh 用已有缓存。"
        )

    provinces: list[dict] = []
    n_cities = n_counties = 0

    for i, prov in enumerate(provinces_raw, 1):
        kids = _children(fetch_area(prov["adcode"]))

        if kids and all(k["level"] == "district" for k in kids):
            # 直辖市 / 特别行政区：没有地级市这一层，合成一个同名市，
            # 否则三级联动的第二级是空的，用户选完省就卡住了。
            cities = [{
                "name": prov["name"], "adcode": prov["adcode"],
                "center": prov["center"], "synthetic": True,
                "counties": [{"name": k["name"], "adcode": k["adcode"], "center": k["center"]}
                             for k in kids],
            }]
        else:
            cities = []
            for city in kids:
                districts = _children(fetch_area(city["adcode"])) if city["level"] == "city" else []
                cities.append({
                    "name": city["name"], "adcode": city["adcode"], "center": city["center"],
                    "counties": [{"name": d["name"], "adcode": d["adcode"], "center": d["center"]}
                                 for d in districts
                                 if d["level"] in ("district", "")],
                })

        n_cities += len(cities)
        n_counties += sum(len(c["counties"]) for c in cities)
        provinces.append({
            "name": prov["name"], "adcode": prov["adcode"], "center": prov["center"],
            "cities": cities,
        })
        logger.info("[%2d/%d] %s：%d 市 / %d 县区",
                    i, len(provinces_raw), prov["name"],
                    len(cities), sum(len(c["counties"]) for c in cities))

    payload = {
        "version": 1,
        "source": "阿里 DataV geoatlas（areas_v3/bound，与 frontend/assets/geo/china.json 同源）",
        "note": "坐标是行政区**中心点**（DataV 的 center），用于选址与推导经纬度，不是地块坐标。",
        "province_count": len(provinces),
        "city_count": n_cities,
        "county_count": n_counties,
        "provinces": provinces,
    }
    write_json(path_of("admin_divisions"), payload)
    logger.info("已写出 %s", path_of("admin_divisions"))

    _write_frontend_bundle(payload)
    return payload


def _write_frontend_bundle(payload: dict) -> None:
    """给前端的紧凑版：只留名字与坐标，去掉 adcode 与溯源字段。

    结构刻意做成「按名字索引」而不是数组 —— 三级联动是按名字取值的
    （<option value="武汉市">），数组的话每次 change 都要 find 一遍。
    """
    bundle = {}
    for prov in payload["provinces"]:
        cities = {}
        for city in prov["cities"]:
            cities[city["name"]] = {
                "c": city["center"],
                "d": {c["name"]: c["center"] for c in city["counties"] if c["center"]},
            }
        bundle[prov["name"]] = {"c": prov["center"], "m": cities}

    target = path_of("frontend_dir") / "assets" / "data" / "admin_divisions.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    header = (
        "/* 由 scripts/27_build_admin_divisions.py 自动生成 —— 请勿手改。\n"
        f"   省 {payload['province_count']} / 市 {payload['city_count']} / 县区 {payload['county_count']}；\n"
        "   结构：{ 省: { c: [lng,lat], m: { 市: { c: [lng,lat], d: { 县: [lng,lat] } } } } }\n"
        "   坐标是行政区中心点，只用于推导田块经纬度（见 farm.js 的三级取平均）。 */\n"
    )
    with target.open("w", encoding="utf-8", newline="\n") as f:
        f.write(header)
        f.write("window.__ADMIN_DIVISIONS__ = ")
        f.write(body)
        f.write(";\n")
    logger.info("已写出 %s（%.0f KB）", target, target.stat().st_size / 1024)


def main() -> int:
    ap = argparse.ArgumentParser(description="构建省市区离线数据集")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新抓取")
    args = ap.parse_args()

    logger.info("=" * 62)
    data = build(refresh=args.refresh)
    logger.info("=" * 62)
    logger.info("完成：%d 省 / %d 市 / %d 县区",
                data["province_count"], data["city_count"], data["county_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
