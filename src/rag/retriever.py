"""任务 4.4 的检索部分 —— 基于 ChromaDB 的向量检索。

检索串 = 用户文字描述 + 语音转写 + 图像识别 top-3 病害名（由 src/llm/fusion.py 拼好传入），
这样即使描述口语化，也能靠病害名把相关防治资料召回。

作物约束：知识库按作物分册（南瓜白粉病、水稻稻瘟病……），纯向量检索会把别的作物
同名或近似的病害一起召回。调用方知道这条问诊的作物时传入 crop，先只在作物内检索；
作物内命中的不够，才用其他作物的片段补齐，并给这些片段打 `same_crop=False`，
由提示词与页面标注「其他作物，仅作参考」—— 不能让水稻田照着南瓜的方案打药。
"""

from __future__ import annotations

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.rag.base import BaseRAG
from src.rag.builder import COLLECTION_NAME, build_index

logger = setup_logger("agri.rag.retrieve")

# 知识库里有 22 条「多寄主」病害，这些资料对任何作物都成立，不该被作物过滤挡掉
MULTI_HOST_CROP = "多寄主"

# 补齐用的候选倍率：其他作物的片段要和作物内的去重后再挑，多取一些才有得选
FILL_FACTOR = 3


def _key(row: dict) -> tuple[str, str]:
    return (row.get("source", ""), row.get("text", "")[:48])


class ChromaRAG(BaseRAG):
    provider = "chromadb"

    def __init__(self, *, top_k: int | None = None) -> None:
        import chromadb

        self.top_k = top_k or int(env("RAG_TOP_K", 4, cast="int") or 4)
        persist_dir = path_of("vectorstore_dir")

        # 绝对路径：Windows + sqlite 对含空格的路径敏感
        self.client = chromadb.PersistentClient(path=str(persist_dir.resolve()))
        self.collection = self.client.get_collection(COLLECTION_NAME)
        self.crop_ready = self._probe_crop_metadata()
        logger.info("RAG 就绪：集合 %s，%d 个片段，目录 %s（作物过滤：%s）",
                    COLLECTION_NAME, self.collection.count(), persist_dir,
                    "可用" if self.crop_ready else "索引未带作物元数据，暂不启用")

    def _probe_crop_metadata(self) -> bool:
        """看索引里有没有 crop 字段。老索引没有，这时按作物过滤会一条都查不到。"""
        try:
            got = self.collection.get(limit=20, include=["metadatas"])
        except Exception as e:  # noqa: BLE001
            logger.warning("检查作物元数据失败：%s", e)
            return False
        for meta in got.get("metadatas") or []:
            if (meta or {}).get("crop"):
                return True
        return False

    def retrieve(self, query: str, top_k: int | None = None, crop: str = "") -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        total = self.collection.count()
        if total == 0:
            return []

        from src.rag.embeddings import get_encoder

        k = min(top_k or self.top_k, total)
        vector = get_encoder()([query])[0]

        crop = (crop or "").strip()
        out: list[dict] = []
        if crop and self.crop_ready:
            out = self._query(vector, n_results=k, crop=crop,
                              where={"crop": {"$in": [crop, MULTI_HOST_CROP]}})

        if len(out) < k:
            # 作物内不够：按语义补齐，标成其他作物。不补齐会把 recall 让出去 ——
            # 有些作物在库里只覆盖了几种病，硬过滤等于告诉用户「没有资料」。
            seen = {_key(r) for r in out}
            fill_n = min(max(k * FILL_FACTOR, k), total)
            for row in self._query(vector, n_results=fill_n, crop=crop):
                key = _key(row)
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
                if len(out) >= k:
                    break

        logger.info("RAG 检索「%s」-> %d 条（作物 %s，其中其他作物 %d 条，最高相似度 %.3f）",
                    query[:40], len(out), crop or "未指定",
                    sum(1 for r in out if r.get("same_crop") is False),
                    out[0]["score"] if out else 0.0)
        return out

    def _query(self, vector: list[float], *, n_results: int, crop: str = "",
               where: dict | None = None) -> list[dict]:
        kwargs: dict = {
            "query_embeddings": [vector],
            "n_results": max(1, n_results),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        try:
            res = self.collection.query(**kwargs)
        except Exception as e:  # noqa: BLE001 - 过滤条件写错就退回不过滤
            logger.warning("向量检索失败（where=%s）：%s", where, e)
            return []

        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]

        out: list[dict] = []
        for text, meta, dist in zip(docs, metas, dists):
            meta = meta or {}
            doc_crop = str(meta.get("crop") or "")
            same_crop: bool | None = None
            if crop:
                # 作物未知的片段（自行放入的官方文档）不下判断，免得把「不知道」说成「不同作物」
                same_crop = (doc_crop == MULTI_HOST_CROP or doc_crop == crop) if doc_crop else None
            # cosine 距离 -> 相似度（0~1），便于前端展示与阈值过滤
            score = max(0.0, 1.0 - float(dist))
            out.append({
                "title": meta.get("title", ""),
                "text": text,
                "source": meta.get("source", ""),
                "score": round(score, 4),
                "crop": doc_crop,
                "same_crop": same_crop,
            })
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
            "crop_filter": self.crop_ready,
        }
