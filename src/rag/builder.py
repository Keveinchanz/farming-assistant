"""任务 4.4 的建索引部分 —— 加载植保文档、文本切分、构建向量数据库（ChromaDB）。

知识源目录 knowledge/documents/*.txt 里有两类内容：
    · 由 scripts/07_build_library.py 从知识图书库渲染出的卡片文本（规则模板生成，已标注来源）
    · 你自行放入的官方植保文档（如全国农技中心的防治手册、地方植保站的技术意见）

切分：优先用 LangChain 的 RecursiveCharacterTextSplitter（任务清单 4.4 指定的技术栈），
      langchain 未安装时退回按段落/长度切分的等价实现，保证功能不因缺依赖而中断。

存储：ChromaDB 的 PersistentClient，落在 knowledge/vectorstore/。
      Windows 下该路径含空格（D:\\code file\\insert\\...），sqlite 对此敏感，
      因此这里显式用绝对路径，且初始化失败时由工厂自动降级为 MockRAG，不让后端起不来。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.common.config import env
from src.common.io_utils import ensure_dir, list_images  # noqa: F401  (list_images 供扩展使用)
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.rag.build")

COLLECTION_NAME = "agri_knowledge"


def load_documents(docs_dir: Path | None = None) -> list[dict]:
    """读取知识源目录下的全部 .txt。返回 [{"path","title","text","source"}]。"""
    docs_dir = docs_dir or path_of("documents_dir")
    if not docs_dir.exists():
        logger.warning("知识源目录不存在：%s", docs_dir)
        return []

    docs: list[dict] = []
    for path in sorted(docs_dir.rglob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        title = text.splitlines()[0].lstrip("标题：").strip() or path.stem
        docs.append({
            "path": path,
            "title": title,
            "text": text,
            "source": str(path.relative_to(path_of("knowledge_dir"))).replace("\\", "/"),
        })
    logger.info("载入知识文档：%d 个（目录 %s）", len(docs), docs_dir)
    return docs


def split_documents(docs: list[dict], *, chunk_size: int | None = None,
                    overlap: int | None = None) -> list[dict]:
    """把文档切成片段，保留标题与来源，便于检索结果里展示出处。"""
    chunk_size = chunk_size or int(env("RAG_CHUNK_SIZE", 500, cast="int") or 500)
    overlap = overlap if overlap is not None else int(env("RAG_CHUNK_OVERLAP", 80, cast="int") or 80)
    overlap = max(0, min(overlap, chunk_size // 2))

    splitter = _make_splitter(chunk_size, overlap)
    chunks: list[dict] = []
    for doc in docs:
        pieces = splitter(doc["text"])
        for i, piece in enumerate(pieces):
            piece = piece.strip()
            if len(piece) < 20:
                continue
            chunks.append({
                "id": f"{doc['source']}::{i}",
                "title": doc["title"],
                "text": piece,
                "source": doc["source"],
            })
    logger.info("切分完成：%d 个文档 -> %d 个片段（chunk_size=%d, overlap=%d）",
                len(docs), len(chunks), chunk_size, overlap)
    return chunks


def _make_splitter(chunk_size: int, overlap: int):
    """优先用 LangChain 的切分器；不可用时退回等价实现。"""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        logger.info("切分器：LangChain RecursiveCharacterTextSplitter")
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            separators=["\n\n", "\n", "。", "；", "，", " ", ""],
        )
        return lambda text: splitter.split_text(text)
    except ImportError:
        logger.info("切分器：内置实现（未安装 langchain-text-splitters）")
        return lambda text: _simple_split(text, chunk_size, overlap)


def _simple_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按段落聚合到接近 chunk_size，再对超长段落按句号切。"""
    paragraphs: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            paragraphs.append(para)
            continue
        buf = ""
        for sent in para.replace("。", "。\n").split("\n"):
            if len(buf) + len(sent) > chunk_size and buf:
                paragraphs.append(buf)
                buf = buf[-overlap:] if overlap else ""
            buf += sent
        if buf.strip():
            paragraphs.append(buf.strip())
    return paragraphs


def build_index(*, force: bool = False, reset: bool = False) -> dict:
    """构建（或增量重建）向量索引。返回统计信息。"""
    from src.rag.embeddings import get_encoder

    persist_dir = ensure_dir(path_of("vectorstore_dir"))
    docs = load_documents()
    if not docs:
        return {"built": False, "reason": "knowledge/documents 下没有可用文档",
                "hint": "先运行：python scripts/07_build_library.py"}

    chunks = split_documents(docs)
    if not chunks:
        return {"built": False, "reason": "切分后没有有效片段"}

    encode = get_encoder()
    client = _chroma_client(persist_dir)

    if reset or force:
        try:
            client.delete_collection(COLLECTION_NAME)
            logger.info("已删除旧集合 %s，开始重建", COLLECTION_NAME)
        except Exception:  # noqa: BLE001 - 集合不存在时忽略
            pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )

    # 增量：跳过已经存在的片段 id（用 md5 保证同一内容重复运行不产生重复记录）
    existing = set(collection.get(include=[]).get("ids", []) or []) if not reset else set()
    todo = [c for c in chunks if _chunk_id(c) not in existing]

    if not todo:
        logger.info("索引已是最新：%d 个片段全部存在，无需重建", len(chunks))
        return {"built": False, "reason": "already-up-to-date",
                "chunks": len(chunks), "collection": COLLECTION_NAME}

    logger.info("开始向量化 %d 个片段（共 %d 个，跳过已存在 %d 个）……",
                len(todo), len(chunks), len(chunks) - len(todo))

    batch = 64
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vectors = encode([c["text"] for c in part])
        collection.add(
            ids=[_chunk_id(c) for c in part],
            documents=[c["text"] for c in part],
            embeddings=vectors,
            metadatas=[{"title": c["title"], "source": c["source"]} for c in part],
        )
        logger.info("    已写入 %d/%d", min(i + batch, len(todo)), len(todo))

    total = collection.count()
    logger.info("索引构建完成：集合 %s，共 %d 个片段，位置 %s",
                COLLECTION_NAME, total, persist_dir)
    return {
        "built": True,
        "chunks": total,
        "added": len(todo),
        "documents": len(docs),
        "collection": COLLECTION_NAME,
        "persist_dir": str(persist_dir),
    }


def _chunk_id(chunk: dict) -> str:
    return hashlib.md5(f"{chunk['source']}|{chunk['text']}".encode("utf-8")).hexdigest()


def _chroma_client(persist_dir: Path):
    import chromadb

    # 显式传绝对路径：Windows 下含空格的中文路径对 sqlite 敏感
    return chromadb.PersistentClient(path=str(persist_dir.resolve()))
