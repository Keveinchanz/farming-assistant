"""RAG 的 Mock 实现 —— 未安装向量库依赖或未建索引时使用。

行为：retrieve 一律返回空列表。
这样 /api/diagnose/multimodal 的 sources 字段为空数组，报告本身仍由（真实的或 Mock 的）
大模型生成，前端只是不展示"引用来源"区块。绝不返回编造的"参考资料"。
"""

from __future__ import annotations

from src.common.logging_utils import setup_logger
from src.rag.base import BaseRAG

logger = setup_logger("agri.rag.mock")


class MockRAG(BaseRAG):
    provider = "mock"

    def __init__(self, reason: str = "未启用 RAG") -> None:
        self.reason = reason
        logger.info("RAG：Mock（%s），检索结果将为空", reason)

    def retrieve(self, query: str, top_k: int | None = None, crop: str = "") -> list[dict]:
        return []

    def stats(self) -> dict:
        return {
            "provider": self.provider,
            "enabled": False,
            "reason": self.reason,
            "hint": "安装依赖并建索引："
                    "pip install -r requirements-rag.txt && python scripts/08_build_rag_index.py",
        }
