"""RAG 工厂 —— 按 .env 与依赖可用性决定用 ChromaDB 还是 Mock。

RAG_ENABLED 取值：
    auto   （默认）依赖齐全且索引已建好就用 ChromaDB，否则退回 Mock
    on     强制启用（缺依赖/缺索引直接报错，便于排查）
    off    强制关闭

降级判定顺序（任一不满足即降级，并说明原因）：
    1. chromadb / sentence-transformers 是否装好
    2. knowledge/vectorstore 里是否已有集合与数据
    3. 加载过程是否抛异常（含 Windows 路径 / sqlite 权限等环境问题）
"""

from __future__ import annotations

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.rag.base import BaseRAG
from src.rag.mock import MockRAG

logger = setup_logger("agri.rag")

_cached: BaseRAG | None = None
_cached_mode: str | None = None


def _deps_available() -> tuple[bool, str]:
    try:
        import chromadb  # noqa: F401
    except ImportError:
        return False, "未安装 chromadb"
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False, "未安装 sentence-transformers"
    return True, ""


def _index_ready() -> tuple[bool, str]:
    try:
        import chromadb

        from src.common.paths import path_of
        from src.rag.builder import COLLECTION_NAME

        persist = path_of("vectorstore_dir")
        if not persist.exists():
            return False, f"向量库目录不存在：{persist}"
        client = chromadb.PersistentClient(path=str(persist.resolve()))
        col = client.get_collection(COLLECTION_NAME)
        if col.count() == 0:
            return False, "向量库为空"
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"向量库不可用：{e}"


def get_rag(*, refresh: bool = False) -> BaseRAG:
    global _cached, _cached_mode

    mode = str(env("RAG_ENABLED", "auto") or "auto").strip().lower()
    if _cached is not None and _cached_mode == mode and not refresh:
        return _cached

    if mode == "off":
        rag: BaseRAG = MockRAG("RAG_ENABLED=off，已手动关闭")
        _cached, _cached_mode = rag, mode
        return rag

    ok, reason = _deps_available()
    if not ok:
        if mode == "on":
            raise RuntimeError(f"RAG_ENABLED=on 但{reason}；请先 pip install -r requirements-rag.txt")
        logger.warning("RAG 降级为 Mock：%s", reason)
        rag = MockRAG(reason)
        _cached, _cached_mode = rag, mode
        return rag

    ready, why = _index_ready()
    if not ready:
        if mode == "on":
            raise RuntimeError(f"RAG_ENABLED=on 但{why}；请先运行 python scripts/08_build_rag_index.py")
        logger.warning("RAG 降级为 Mock：%s", why)
        rag = MockRAG(why)
        _cached, _cached_mode = rag, mode
        return rag

    try:
        from src.rag.retriever import ChromaRAG

        rag = ChromaRAG()
        logger.info("RAG 能力：ChromaDB（真实检索）")
    except Exception as e:  # noqa: BLE001
        if mode == "on":
            raise
        logger.error("初始化 ChromaDB 失败，降级为 Mock：%s", e)
        rag = MockRAG(f"初始化失败：{e}")

    _cached, _cached_mode = rag, mode
    return rag


def current_mode() -> str:
    return _cached_mode or str(env("RAG_ENABLED", "auto") or "auto")
