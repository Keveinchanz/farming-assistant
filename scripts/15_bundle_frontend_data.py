#!/usr/bin/env python
"""任务 5.2 配套 —— 重新打包前端本地预加载快照（两个页面各一份）。

产出：frontend/assets/data/home_preload.js
     window.__CHINA_GEO__     中国地图底图
     window.__PROVINCE_PEST__ 各省 × 季节 TOP3
     frontend/assets/data/library_preload.js
     window.__LIBRARY_PRELOAD__ 知识图书库的卡片简要字段（不含正文）

首页（index.html）在 map.js 之前、图书库页（library.html）在 library.js 之前，
分别用 <script> 同步加载它们，因此首屏不发任何网络请求，
也不会因为接口慢/接口挂了而卡在「正在加载…」。图书库那份只放列表字段，
约 190 KB；带正文的 library_items.json 是 1.3 MB，同步脚本装不起，所以详情仍走接口。

平时不需要单独跑：scripts/04_gen_province_pest.py 生成省份数据、
scripts/07_build_library.py 生成知识库时会各自自动刷新对应那一份。
只有在「换了地图底图 china.json」或「手工改过 province_pest_top3.json」时才需要跑这一下。

用法：
    python scripts/15_bundle_frontend_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger                       # noqa: E402
from src.content.frontend_bundle import build_home_preload, build_library_preload  # noqa: E402

logger = setup_logger("agri.bundle")


def main() -> int:
    # 两份各自独立：缺地图底图不影响知识库快照，缺知识库也不影响首页，
    # 所以不用 and 短路，也不要因为一份失败就跳过另一份。
    home = build_home_preload()
    library = build_library_preload()

    logger.info("=" * 62)
    if home is not None:
        logger.info("首页快照完成：可离线渲染，无需请求 /api/map/pest_top3")
    if library is not None:
        logger.info("知识库快照完成：图书库页可离线渲染列表中，无需请求 /api/library")
    logger.info("=" * 62)

    # 两份都没写成才算失败（各自的缺失原因上面已经打出来了）
    return 0 if (home is not None or library is not None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
