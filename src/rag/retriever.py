"""任务 4.4 的检索部分 —— 基于 ChromaDB 的向量检索。

检索串 = 用户文字描述 + 语音转写 + 图像识别 top-3 病害名（由 src/llm/fusion.py 拼好传入），
这样即使描述口语化，也能靠病害名把相关防治资料召回。
"""

from __future__ import annotations

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.rag.base import BaseRAG
from src.rag.builder import COLLECTION_NAME, build_index

logger = setup_logger("agri.rag.retrieve")


class ChromaRAG(BaseRAG):
    provider = "chromadb"

    def __init__(self, *, top_k: int | None = None) -> None:
        import chromadb

        self.top_k = top_k or int(env("RAG_TOP_K", 4, cast="int") or 4)
        persist_dir = path_of("vectorstore_dir")

        # 绝对路径：Windows + sqlite 对含空格的路径敏感
        self.client = chromadb.PersistentClient(path=str(persist_dir.resolve()))
        self.collection = self.client.get_collection(COLLECTION_NAME)
        logger.info("RAG 就绪：集合 %s，%d 个片段，目录 %s",
                    COLLECTION_NAME, self.collection.count(), persist_dir)

    def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        if self.collection.count() == 0:
            return []

        from src.rag.embeddings import get_encoder

        k = min(top_k or self.top_k, self.collection.count())
        vector = get_encoder()([query])[0]

        res = self.collection.query(
            query_embeddings=[vector],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )

        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]

        out: list[dict] = []
        for text, meta, dist in zip(docs, metas, dists):
            # cosine 距离 -> 相似度（0~1），便于前端展示与阈值过滤
            score = max(0.0, 1.0 - float(dist))
            out.append({
                "title": (meta or {}).get("title", ""),
                "text": text,
                "source": (meta or {}).get("source", ""),
                "score": round(score, 4),
            })
        logger.info("RAG 检索「%s」-> %d 条（最高相似度 %.3f）",
                    query[:40], len(out), out[0]["score"] if out else 0.0)
        return out

    def build(self, *, force: bool = False) -> dict:
        return build_index(force=force, reset=force)

    def stats(self) -> dict:
        try:
            count = self.collection.count()
        except Exception as e:  # noqa: BLE001
            return {"provider": self.provider, "enabled": True, "error": str(e)}
        return {
            "provider": self.provider,
            "enabled": True,
            "collection": COLLECTION_NAME,
            "chunks": count,
            "top_k": self.top_k,
        }
