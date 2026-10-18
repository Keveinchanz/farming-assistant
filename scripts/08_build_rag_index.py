#!/usr/bin/env python
"""任务 4.4 —— 构建农业 RAG 知识库：加载植保文档、文本切分、构建向量数据库。

产出：knowledge/vectorstore/（ChromaDB 持久化目录）

前置：
    1. python scripts/07_build_library.py     生成 knowledge/documents/*.txt
    2. pip install -r requirements-rag.txt    安装 chromadb / sentence-transformers / langchain
    （也可把自己的官方植保文档放进 knowledge/documents/ 一起建索引）

用法：
    python scripts/08_build_rag_index.py
    python scripts/08_build_rag_index.py --force    # 删掉旧集合全量重建
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logging_utils import setup_logger   # noqa: E402

logger = setup_logger("agri.rag.build")


def main() -> int:
    parser = argparse.ArgumentParser(description="任务 4.4 构建 RAG 向量索引")
    parser.add_argument("--force", action="store_true", help="删除旧集合后全量重建")
    args = parser.parse_args()

    try:
        from src.rag.builder import build_index
    except ImportError as e:
        logger.error("缺少 RAG 依赖：%s", e)
        logger.error("请先安装：python -m pip install -r requirements-rag.txt")
        return 2

    stats = build_index(force=args.force, reset=args.force)

    logger.info("=" * 62)
    if stats.get("built"):
        logger.info("索引构建完成：%d 个片段（文档 %s 个）", stats["chunks"], stats.get("documents"))
    else:
        logger.info("未重建：%s", stats.get("reason"))
    logger.info("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
