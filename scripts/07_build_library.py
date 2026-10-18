#!/usr/bin/env python
"""任务 4.5（数据侧）—— 构建知识图书库：病害防治知识 + 虫害防治知识 + 农作物养护知识。

产出：
    knowledge/library/library_items.json      前端 5.4 页面读取的卡片数据
    knowledge/documents/*.txt                 4.4 RAG 的切分源（一卡一文件）
    frontend/assets/data/library_preload.js   图书库页首屏的本地快照（只含列表字段）

⚠ 卡片正文是按病原类型（病害）/ 取食方式类群（虫害）/ 作物类别（养护）套用规则模板
   生成的演示内容，每张卡片都带 content_source 字段，前端会显示「内容来源」标注。

用法：
    python scripts/07_build_library.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger        # noqa: E402
from src.content.library_build import build_and_save     # noqa: E402

logger = setup_logger("agri.library")


def main() -> int:
    data = build_and_save()
    logger.info("=" * 62)
    logger.info("完成：%d 张卡片（%s）", data["total"],
                "，".join(f"{c['name']} {c['count']} 张" for c in data["categories"]))
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
