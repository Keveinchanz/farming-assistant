"""生成首页 3D 沙盘用的两份底图数据（一次性，产物进仓库）。

    frontend/assets/geo/china_relief.json   高程（DEM）
    frontend/assets/geo/china_water.json    河流与湖泊

**高程**主源是 OpenTopoData 的 SRTM90m（一次 100 个坐标，每秒 1 次），
不通时依次退到 Open-Meteo Elevation API、再退到**合成的 25 个地形单元场**，
并把用的是哪一条写进文件的 `source` 字段 —— 静默降级是最糟的选择。

**河流湖泊**走 Natural Earth 的 50m 河流/湖泊（公有领域）。同样有合成回退：
手写的 20 条主要河流折线 + 6 个主要湖泊。

用法：
    python -I scripts/28_build_china_geo.py            # 全量
    python -I scripts/28_build_china_geo.py --relief   # 只做高程
    python -I scripts/28_build_china_geo.py --water    # 只做水系
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
GEO = ROOT / "frontend" / "assets" / "geo"
CACHE = ROOT / "outputs" / "_geo_cache"
DL = ROOT / "outputs" / "_dl"          # 外部下载一律落在这里，和源码分开

# 网格范围：比国界稍微放宽一圈，让沙盘的底板把国土整个兜住。
LON0, LON1, DLON = 70.0, 138.0, 0.5
LAT0, LAT1, DLAT = 15.0, 56.0, 0.5
COLS = int(round((LON1 - LON0) / DLON)) + 1      # 137
ROWS = int(round((LAT1 - LAT0) / DLAT)) + 1      # 83
CHUNK = 100                                       # 接口一次最多 100 个坐标

# 高程两个来源。**主用 OpenTopoData**：从国内连 Open-Meteo 的 elevation 接口
# 实测成功率只有两三成（SSL UNEXPECTED_EOF / 连接被重置轮着来），一百多批根本拉不完；
# OpenTopoData 同一台机器上 10/10 通得过，而且是标准的 SRTM 90m。
# 两个都按「一次 100 个坐标、至少间隔 1 秒」用 —— 这是人家免费额度的明文限制。
OPENTOPO = "https://api.opentopodata.org/v1/srtm90m"    # 坐标是 lat,lon（纬度在前！）
ELEV_API = "https://api.open-meteo.com/v1/elevation"    # 坐标是 latitude= / longitude=

# Natural Earth 50m 水系。GitHub raw 在国内经常解析不了，jsdelivr 的镜像稳。
NE = "https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/geojson/"
RIVERS_URL = NE + "ne_50m_rivers_lake_centerlines.geojson"
LAKES_URL = NE + "ne_50m_lakes.geojson"

# 沙盘只画中国这一块，超出的折线直接丢
TILE_LON0, TILE_LON1 = 72.0, 136.5
TILE_LAT0, TILE_LAT1 = 17.0, 55.0


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------- 高程

def _get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "agri-sandbox/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read()


def _elev_batch(lats: list[float], lons: list[float]) -> list[int]:
    """一批（≤100 个）坐标的高程。先 OpenTopoData，不行再 Open-Meteo。

    两个都是 GET，返回的点序与请求一致。海面上的点 SRTM 会给 null，
    这里当 0 米 —— 沙盘的海面本来就是一块平底板。
    """
    tried = []
    # ---- 主：OpenTopoData（lat,lon 用 | 分隔）
    loc = "|".join(f"{la:.4f},{lo:.4f}" for la, lo in zip(lats, lons))
    url = OPENTOPO + "?locations=" + urllib.parse.quote(loc, safe="|,")
    for attempt in range(4):
        try:
            data = json.loads(_get(url).decode("utf-8"))
            res = data["results"]
            if len(res) != len(lats):
                raise ValueError(f"点数不对：{len(res)} != {len(lats)}")
            return [int(round(float(r["elevation"]))) if r.get("elevation") is not None else 0
                    for r in res]
        except Exception as e:
            tried.append(f"opentopodata#{attempt + 1}: {e}")
            time.sleep(1.2 * (attempt + 1))
    # ---- 备：Open-Meteo（lon,lat，两个逗号列表）
    url = (ELEV_API
           + "?latitude=" + ",".join(f"{v:.4f}" for v in lats)
           + "&longitude=" + ",".join(f"{v:.4f}" for v in lons))
    for attempt in range(3):
        try:
            data = json.loads(_get(url).decode("utf-8"))
            vals = [int(round(float(v))) for v in data["elevation"]]
            if len(vals) != len(lats):
                raise ValueError(f"点数不对：{len(vals)} != {len(lats)}")
            return vals
        except Exception as e:
            tried.append(f"open-meteo#{attempt + 1}: {e}")
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("；".join(tried[-3:]))


def fetch_relief() -> dict:
    """按 100 点一批把高程拉下来，**逐批存盘**，断了可以接着跑。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    part = CACHE / "relief_partial.json"
    elev: list[int | None] = [None] * (COLS * ROWS)
    done_batches: dict[str, list[int]] = {}
    if part.exists():
        saved = json.loads(part.read_text(encoding="utf-8"))
        if saved.get("cols") == COLS and saved.get("rows") == ROWS:
            done_batches = {k: v for k, v in saved.get("batches", {}).items()}
            log(f"  接上次的进度：已有 {len(done_batches)} 批")

    batches = []
    for start in range(0, COLS * ROWS, CHUNK):
        idx = list(range(start, min(start + CHUNK, COLS * ROWS)))
        batches.append(idx)

    failed = 0
    for n, idx in enumerate(batches):
        key = str(n)
        if key in done_batches:
            vals = done_batches[key]
        else:
            lats, lons = [], []
            for i in idx:
                r, c = divmod(i, COLS)
                lons.append(LON0 + c * DLON)
                lats.append(LAT1 - r * DLAT)          # 第 0 行在最北
            vals = None
            for attempt in range(3):
                try:
                    vals = _elev_batch(lats, lons)
                    break
                except Exception as e:              # 网络抖、限流都重试
                    failed += 1
                    wait = 2.0 * (attempt + 1)
                    log(f"    批 {n} 第 {attempt + 1} 次失败（{str(e)[:80]}），{wait:.1f}s 后重试")
                    time.sleep(wait)
            if vals is None:
                log(f"  批 {n} 三次都没成 —— 进度已存，重跑可续")
                part.write_text(json.dumps({"cols": COLS, "rows": ROWS,
                                            "batches": done_batches}), encoding="utf-8")
                raise SystemExit(f"高程只拿到 {len(done_batches)}/{len(batches)} 批，"
                                 f"检查网络后重跑同一命令即可接着拉")
            done_batches[key] = vals
            part.write_text(json.dumps({"cols": COLS, "rows": ROWS,
                                        "batches": done_batches}), encoding="utf-8")
            time.sleep(1.1)                         # OpenTopoData 免费额度：每秒 1 次
        for k, i in enumerate(idx):
            elev[i] = vals[k]
        if n % 20 == 0:
            log(f"  高程 {n + 1}/{len(batches)} 批")

    assert all(v is not None for v in elev), "有格子没填上"
    part.unlink(missing_ok=True)
    return {
        "source": "srtm90m",
        "note": "中国及周边 0.5° 网格高程。数据来源 OpenTopoData 的 srtm90m 数据集"
                "（NASA SRTM 90 米）；该源不通时依次退到 Open-Meteo Elevation API"
                "（Copernicus DEM GLO-90）。行优先、第 0 行在最北。",
        "lon0": LON0, "lat0": LAT1, "dlon": DLON, "dlat": -DLAT,
        "cols": COLS, "rows": ROWS, "unit": "m",
        "elev": [int(v) for v in elev],
    }


# 合成回退：25 个地形单元 + 高斯平滑。取不到网时用这套，
# 形状明显不如真 DEM，但至少能看出"西高东低、三级阶梯"。
UNITS = [
    # (名称, 经度, 纬度, 半径°, 峰高 m)
    ("青藏高原", 88, 33, 14, 4800), ("羌塘", 86, 34, 8, 4900),
    ("喜马拉雅", 86, 29, 7, 6000), ("昆仑山", 84, 36, 9, 5500),
    ("天山", 82, 42, 7, 4200), ("阿尔泰山", 88, 47, 4, 2800),
    ("塔里木盆地", 82, 39, 6, 900), ("准噶尔盆地", 86, 45, 5, 500),
    ("柴达木盆地", 93, 37, 4, 2800), ("祁连山", 99, 38, 4, 4200),
    ("秦岭", 108, 34, 4, 2200), ("大巴山", 108, 32, 3, 1800),
    ("太行山", 113, 37, 3, 1500), ("吕梁山", 111, 37, 2, 1600),
    ("黄土高原", 108, 36, 5, 1300), ("内蒙古高原", 112, 43, 8, 1200),
    ("大兴安岭", 122, 49, 5, 1200), ("小兴安岭", 128, 48, 3, 700),
    ("长白山", 128, 42, 3, 1400), ("四川盆地", 105, 30, 4, 400),
    ("云贵高原", 104, 26, 6, 1900), ("南岭", 113, 25, 4, 900),
    ("华北平原", 116, 36, 4, 60), ("东北平原", 125, 45, 5, 180),
    ("东南丘陵", 117, 27, 4, 500),
]


def synthetic_relief() -> dict:
    out = []
    for r in range(ROWS):
        lat = LAT1 - r * DLAT
        for c in range(COLS):
            lon = LON0 + c * DLON
            h = 0.0
            for _, ulon, ulat, rad, peak in UNITS:
                d2 = ((lon - ulon) * math.cos(math.radians((lat + ulat) / 2))) ** 2 \
                     + (lat - ulat) ** 2
                h = max(h, peak * math.exp(-d2 / (2 * rad * rad)))
            # 再叠一点起伏，免得是几个光滑的馒头
            h += 60 * math.sin(lon * 0.9) * math.cos(lat * 0.7)
            out.append(max(0, int(h)))
    return {
        "source": "synthetic:25-units",
        "note": "**合成回退**：Open-Meteo 高程接口取不到时用的 25 个地形单元"
                "（青藏/天山/秦岭/华北平原…）高斯叠加场，只有大概轮廓，不是实测。"
                "行优先、第 0 行在最北。",
        "lon0": LON0, "lat0": LAT1, "dlon": DLON, "dlat": -DLAT,
        "cols": COLS, "rows": ROWS, "unit": "m",
        "elev": out,
    }


# --------------------------------------------------------------------- 水系

# 合成回退：手写的 20 条主要河流折线（只取拐点，够画一条带子）
FALLBACK_RIVERS = [
    ("长江", [(90.5, 33.5), (97.0, 32.0), (99.5, 28.5), (101.5, 26.1), (104.4, 28.8),
              (106.6, 29.6), (110.0, 30.8), (114.3, 30.6), (117.2, 31.8), (118.8, 32.1),
              (120.0, 31.9), (121.8, 31.6)]),
    ("黄河", [(96.5, 35.0), (100.2, 34.9), (102.0, 36.1), (103.8, 36.1), (106.5, 38.6),
              (107.0, 40.7), (110.5, 39.5), (110.5, 36.0), (110.4, 34.8), (112.5, 34.8),
              (114.5, 35.0), (116.5, 35.8), (118.0, 37.0), (119.1, 37.8)]),
    ("珠江", [(104.0, 24.5), (106.0, 24.2), (108.5, 23.8), (110.3, 23.5), (112.5, 23.2),
              (113.5, 22.8), (113.6, 22.3)]),
    ("黑龙江", [(120.0, 50.0), (124.0, 51.5), (127.5, 50.3), (130.5, 48.5), (132.5, 47.8)]),
    ("松花江", [(127.0, 42.5), (126.5, 45.5), (127.5, 46.5), (130.5, 46.5), (132.5, 47.7)]),
    ("雅鲁藏布江", [(83.0, 30.5), (87.0, 29.5), (91.0, 29.3), (94.0, 29.3), (95.3, 29.0)]),
    ("澜沧江", [(94.5, 33.5), (97.5, 31.5), (99.2, 28.0), (100.2, 25.0), (100.8, 22.0)]),
    ("怒江", [(92.5, 32.5), (96.5, 30.5), (98.7, 27.5), (98.7, 25.0), (98.2, 24.0)]),
    ("淮河", [(112.5, 33.5), (114.5, 32.8), (116.5, 32.6), (118.0, 32.9), (119.5, 33.3)]),
    ("海河", [(113.0, 40.5), (115.5, 39.5), (117.0, 39.2), (117.7, 38.9)]),
    ("辽河", [(118.0, 43.0), (121.5, 43.5), (123.5, 42.5), (122.3, 40.8)]),
    ("鸭绿江", [(125.5, 41.6), (127.5, 41.2), (129.5, 40.2)]),
    ("图们江", [(128.0, 42.4), (130.0, 42.8), (130.6, 42.4)]),
    ("闽江", [(117.5, 27.3), (118.5, 26.5), (119.5, 26.0), (119.8, 26.0)]),
    ("钱塘江", [(118.0, 29.3), (119.5, 29.5), (120.5, 30.2), (120.8, 30.3)]),
    ("韩江", [(115.5, 25.5), (116.5, 24.5), (116.8, 23.6)]),
    ("塔里木河", [(78.5, 40.5), (81.5, 40.8), (85.0, 40.8), (87.5, 40.5)]),
    ("伊犁河", [(82.5, 43.5), (81.0, 43.8), (80.4, 43.9)]),
    ("怒江(云南段)", [(98.7, 25.0), (98.9, 24.0), (98.5, 24.0)]),
    ("额尔齐斯河", [(88.0, 47.0), (85.5, 47.5), (84.0, 48.0)]),
]

FALLBACK_LAKES = [
    ("青海湖", 100.2, 36.9, 1.1, 0.65),
    ("鄱阳湖", 116.3, 29.1, 0.6, 1.0),
    ("洞庭湖", 112.9, 29.3, 0.6, 0.7),
    ("太湖", 120.2, 31.2, 0.35, 0.3),
    ("呼伦湖", 117.4, 48.9, 0.5, 0.35),
    ("纳木错", 90.6, 30.7, 0.4, 0.3),
]


def _ellipse_lakes():
    """合成湖泊也吐成**闭合点列**，和 Natural Earth 那条路一个形状 ——
    前端就只需要认一种数据，不用分「真湖泊 / 椭圆」两种情况。"""
    out = []
    for name, lon, lat, rx, ry in FALLBACK_LAKES:
        ring = []
        for k in range(28):
            a = 2 * math.pi * k / 28
            ring.append([round(lon + rx * math.cos(a), 4), round(lat + ry * math.sin(a), 4)])
        out.append({"name": name, "pts": ring})
    return out


def _decode_ring(coords):
    """Natural Earth 的 GeoJSON 里 MultiLineString / Polygon 的形状不一样，
    统一成"一串 [(lon, lat), ...]"。"""
    out = []
    if isinstance(coords[0][0], (int, float)):
        out.append([(float(x), float(y)) for x, y in coords])
    else:
        for part in coords:
            out.append([(float(x), float(y)) for x, y in part])
    return out


def _in_tile(lon, lat):
    return TILE_LON0 <= lon <= TILE_LON1 and TILE_LAT0 <= lat <= TILE_LAT1


# ---- 落在国界以内才算数 ---------------------------------------------------
#
# 只按矩形裁是**不够**的：72–136.5°E / 17–55°N 这个框把贝加尔湖、伊塞克湖、
# 斋桑泊、安加拉河、额尔齐斯河上游全兜进来了。这些水体在沙盘上会悬在国土
# 之外的空处，看着像画错了。所以真正的判据是「点在中国境内」，
# 用现成的 frontend/assets/geo/china.json 做射线法判定。

_COUNTRY_RINGS: list | None = None


def country_rings():
    """国界外环 + 包围盒。内环（飞地）忽略 —— 影响只到极少数点。"""
    global _COUNTRY_RINGS
    if _COUNTRY_RINGS is not None:
        return _COUNTRY_RINGS
    geo = json.loads((ROOT / "frontend" / "assets" / "geo" / "china.json")
                     .read_text(encoding="utf-8"))
    rings = []
    for feat in geo.get("features", []):
        g = feat.get("geometry") or {}
        if not g.get("coordinates"):
            continue
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            for r in poly:
                xs = [p[0] for p in r]
                ys = [p[1] for p in r]
                rings.append(((min(xs), min(ys), max(xs), max(ys)), r))
    _COUNTRY_RINGS = rings
    return rings


def _point_in_ring(ring, lon, lat) -> bool:
    """射线法。"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            if lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def in_china(lon, lat) -> bool:
    for (x0, y0, x1, y1), r in country_rings():
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _point_in_ring(r, lon, lat):
            return True
    return False


def _runs_masked(pts, keep, min_len=3):
    """把一串点里「连续在境内」的段落切出来。河流出境又入境时必须断开，
    否则会有一条直线横穿境外那块空白。"""
    out = []
    cur = []
    for p, ok in zip(pts, keep):
        if ok:
            cur.append(p)
        else:
            if len(cur) >= min_len:
                out.append(cur)
            cur = []
    if len(cur) >= min_len:
        out.append(cur)
    return out


def _thin(pts, min_step=0.12):
    """抽稀：删掉挨得太近的点，JSON 能小一大半，画出来看不出差别。"""
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_step:
            out.append(p)
    if len(out) >= 2 and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def fetch_water() -> dict:
    DL.mkdir(parents=True, exist_ok=True)
    rivers, lakes = [], []

    try:
        for url, bucket, kind in ((RIVERS_URL, rivers, "river"), (LAKES_URL, lakes, "lake")):
            raw = _get(url, timeout=90)
            name = "rivers" if kind == "river" else "lakes"
            (DL / f"ne_50m_{name}.geojson").write_bytes(raw)
            gj = json.loads(raw.decode("utf-8"))
            for feat in gj.get("features", []):
                geom = feat.get("geometry") or {}
                if not geom.get("coordinates"):
                    continue
                nm = (feat.get("properties") or {}).get("name") or ""
                for line in _decode_ring(geom["coordinates"]):
                    keep = [_in_tile(p[0], p[1]) and in_china(p[0], p[1]) for p in line]
                    if kind == "river":
                        for run in _runs_masked(line, keep):
                            bucket.append({"name": nm, "pts": _thin(run)})
                        continue
                    # 湖泊：整圈都在境内的直接留；跨境的（兴凯湖、贝尔湖）只留
                    # 境内那一弧 —— 那才是这张图该画的部分。
                    hit = [p for p, k in zip(line, keep) if k]
                    if len(hit) < 8:
                        continue
                    lons = [p[0] for p in hit]
                    lats = [p[1] for p in hit]
                    if (max(lons) - min(lons)) * (max(lats) - min(lats)) < 0.015:
                        continue
                    if all(keep):
                        bucket.append({"name": nm, "pts": _thin(line, 0.05)})
                        continue
                    start = keep.index(False)            # 从境外那点断开，环就成了一条折线
                    order = [line[(start + 1 + k) % len(line)] for k in range(len(line))]
                    okeep = [keep[(start + 1 + k) % len(line)] for k in range(len(line))]
                    best = []
                    for run in _runs_masked(order, okeep, 8):
                        if len(run) > len(best):
                            best = run
                    if len(best) >= 8:
                        bucket.append({"name": nm, "pts": _thin(best, 0.05)})
        if not rivers or not lakes:
            raise ValueError(f"筛完只剩 河 {len(rivers)} / 湖 {len(lakes)} 条，数据不对")
        src = "natural-earth:ne_50m"
        note = ("Natural Earth 1:50m 河流湖泊（公有领域，"
                "ne_50m_rivers_lake_centerlines / ne_50m_lakes）。"
                "**已用 frontend/assets/geo/china.json 的国界裁过**"
                "（贝加尔湖、伊塞克湖这些框内的境外水体已剔除），并抽稀。")
    except Exception as e:
        log(f"  Natural Earth 取不到（{e}），改用合成水系")
        rivers = [{"name": n, "pts": p} for n, p in FALLBACK_RIVERS]
        lakes = _ellipse_lakes()
        src = "synthetic:hand-authored"
        note = ("**合成回退**：手写的 20 条主要河流折线 + 6 个主要湖泊，"
                "只有大致走向，不是实测河道。")

    return {"source": src, "note": note,
            "rivers": rivers, "lakes": lakes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relief", action="store_true")
    ap.add_argument("--water", action="store_true")
    ap.add_argument("--synthetic", action="store_true",
                    help="跳过网络，强制用合成场（测渲染时用）")
    a = ap.parse_args()
    do_relief = a.relief or not (a.relief or a.water)
    do_water = a.water or not (a.relief or a.water)

    GEO.mkdir(parents=True, exist_ok=True)

    if do_relief:
        out = GEO / "china_relief.json"
        if a.synthetic:
            data = synthetic_relief()
        else:
            log("高程：从 OpenTopoData（SRTM90m）拉网格，断点续传…")
            try:
                data = fetch_relief()
            except SystemExit as e:
                # **不要**在这里偷偷退回合成场：那会写出一个"看着像、其实没数据"
                # 的文件。半截的进度存在 _geo_cache/relief_partial.json 里，
                # 原样重跑就能接着拉；真拉不动了再显式 --synthetic。
                log(f"  {e}")
                log("  （进度已存，重跑同一命令即可续；确实拉不动再加 --synthetic）")
                raise
        out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        log(f"  写入 {out.relative_to(ROOT)}  source={data['source']} "
            f"{data['cols']}x{data['rows']} {out.stat().st_size / 1024:.0f} KB")

    if do_water:
        out = GEO / "china_water.json"
        if a.synthetic:
            data = {"source": "synthetic:hand-authored",
                    "note": "**合成回退**：手写的 20 条主要河流折线 + 6 个主要湖泊，"
                            "只有大致走向，不是实测河道。",
                    "rivers": [{"name": n, "pts": p} for n, p in FALLBACK_RIVERS],
                    "lakes": _ellipse_lakes()}
        else:
            log("水系：从 Natural Earth 拉…")
            data = fetch_water()
        out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        log(f"  写入 {out.relative_to(ROOT)}  source={data['source']} "
            f"河 {len(data['rivers'])} 条 / 湖 {len(data['lakes'])} 个 "
            f"{out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
