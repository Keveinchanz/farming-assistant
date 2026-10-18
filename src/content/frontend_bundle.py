"""本地预加载快照 —— 把首屏要用的数据打包成 JS 变量文件，不再让页面去等接口。

为什么是「JS 变量文件」而不是「再多一个 fetch」：
    首页原先要串行等三件事 —— echarts（1.0 MB）→ china.json（634 KB）→ /api/map/pest_top3（25 KB）。
    这条链上任何一环慢住或抛错，加载遮罩就既不会被隐藏、也不会报错，页面只能一直转圈。
    `<script>` 是同步阻塞加载：把它放在业务脚本之前，业务脚本执行时数据**一定**已经就位，
    不受请求时序、超时、CORS 或后端是否启动的影响。这才是"本地预加载"的字面兑现。

产出（都在 frontend/assets/data/ 下）：
    home_preload.js      window.__CHINA_GEO__     中国地图 GeoJSON（来自 assets/geo/china.json）
                         window.__PROVINCE_PEST__ 各省 × 季节 TOP3（来自 data/province_pest_top3.json）
    library_preload.js   window.__LIBRARY_PRELOAD__ 知识图书库的卡片**简要字段**（来自
                         knowledge/library/library_items.json，刻意不含正文，见下）

调用点：src/content/province_pest.py::build_and_save() 与
src/content/library_build.py::build_and_save() 末尾各会自动调用一次，
所以重跑 04 号 / 07 号脚本即同步，不存在"改了数据但页面还是旧快照"却没人管的情况。
也可以单独重跑：python scripts/15_bundle_frontend_data.py
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.common.io_utils import ensure_dir, read_json, write_text
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.bundle")

# 相对 frontend_dir 的路径
GEO_REL = ("assets", "geo", "china.json")
OUT_REL = ("assets", "data", "home_preload.js")
LIBRARY_REL = ("assets", "data", "library_preload.js")

# 图书库快照只放列表页真正会读的字段，与 backend/app/routers/library.py::_LIST_FIELDS
# 必须一致（有单测钉着两者相等）。**刻意不含 sections**：带正文的 library_items.json
# 已是 1.3 MB，同步脚本装不起，也不该装 —— 详情仍走 /api/library/{id}。
LIBRARY_BRIEF_FIELDS = ("id", "title", "category", "category_name", "crop", "cover",
                        "summary", "tags", "index")

# 这些字符出现在 JS 字符串字面量里在旧引擎上是非法的（U+2028/U+2029 是行分隔符）。
# 现代浏览器已按 ES2019 放行，但转义掉的成本几乎为零，顺手做掉。
_JS_UNSAFE = {0x2028: "\\u2028", 0x2029: "\\u2029"}


def home_preload_path() -> Path:
    """frontend/assets/data/home_preload.js 的绝对路径。"""
    return path_of("frontend_dir").joinpath(*OUT_REL)


def build_home_preload(province_pest: dict | None = None, *, quiet: bool = False) -> Path | None:
    """生成首页预加载快照。

    province_pest 为 None 时从 data/province_pest_top3.json 读。
    地图底图缺失时只告警不抛错 —— 缺了它前端会退回 fetch 路径，不该阻断数据生成脚本。
    返回写出路径；因缺底图而跳过时返回 None。
    """
    frontend = path_of("frontend_dir")
    geo_file = frontend.joinpath(*GEO_REL)

    if not geo_file.exists():
        logger.warning("地图底图不存在，跳过首页预加载打包：%s（前端会退回 fetch 路径）", geo_file)
        return None

    geo = read_json(geo_file)
    if province_pest is None:
        province_pest = read_json(path_of("province_pest"))

    out = frontend.joinpath(*OUT_REL)
    ensure_dir(out.parent)
    write_text(out, _render(geo, province_pest))

    if not quiet:
        logger.info(
            "首页预加载已写出：%s（%.0f KB：%d 个省份 × %d 个季节）",
            out, out.stat().st_size / 1024,
            len(province_pest.get("provinces", [])), len(province_pest.get("seasons", [])),
        )
    return out


def _render(geo: dict, province_pest: dict) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "/* 本文件由 src/content/frontend_bundle.py 自动生成，请勿手工编辑。\n"
        f" * 生成时间：{stamp}\n"
        " * 数据源：frontend/assets/geo/china.json + data/province_pest_top3.json\n"
        " * 重新生成：python scripts/15_bundle_frontend_data.py\n"
        " */\n"
        f"window.__CHINA_GEO__ = {_dumps(geo)};\n\n"
        f"window.__PROVINCE_PEST__ = {_dumps(province_pest)};\n"
    )


def _dumps(obj) -> str:
    """紧凑序列化：这份文件是给浏览器读的，不需要缩进，缩进会让体积翻倍。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).translate(_JS_UNSAFE)


# ---------------------------------------------------------------- 知识图书库快照

def library_preload_path() -> Path:
    """frontend/assets/data/library_preload.js 的绝对路径。"""
    return path_of("frontend_dir").joinpath(*LIBRARY_REL)


def build_library_preload(data: dict | None = None, *, quiet: bool = False) -> Path | None:
    """生成知识图书库的本地快照，供 library.html 在 library.js 之前同步加载。

    data 为 None 时从 knowledge/library/library_items.json 读；数据不存在时只告警不抛错
    （前端会退回 fetch 路径，不该阻断别的生成脚本）。
    返回写出路径；因缺数据而跳过时返回 None。
    """
    if data is None:
        src = path_of("library_dir") / "library_items.json"
        if not src.exists():
            logger.warning("知识图书库数据不存在，跳过打包：%s（请先运行 scripts/07_build_library.py）", src)
            return None
        data = read_json(src)

    snapshot = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": data.get("total", 0),
        "categories": data.get("categories", []),
        "items": [{k: it.get(k) for k in LIBRARY_BRIEF_FIELDS} for it in data.get("items", [])],
    }

    out = library_preload_path()
    ensure_dir(out.parent)
    write_text(out, _render_library(snapshot))

    if not quiet:
        logger.info("知识库预加载已写出：%s（%.0f KB：%d 张卡片 / %d 个分类）",
                    out, out.stat().st_size / 1024,
                    snapshot["total"], len(snapshot["categories"]))
    return out


def _render_library(snapshot: dict) -> str:
    return (
        "/* 本文件由 src/content/frontend_bundle.py 自动生成，请勿手工编辑。\n"
        f" * 生成时间：{snapshot['generated_at']}\n"
        " * 数据源：knowledge/library/library_items.json（只含列表所需字段，不含正文）\n"
        " * 重新生成：python scripts/15_bundle_frontend_data.py（07 号脚本会自动调）\n"
        " */\n"
        f"window.__LIBRARY_PRELOAD__ = {_dumps(snapshot)};\n"
    )
