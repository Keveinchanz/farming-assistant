"""农业 RAG 知识库的抽象接口（任务 4.4）。

`retrieve` 必须返回统一结构，Mock 与真实实现一致：
    [{"title": str, "text": str, "source": str, "score": float|None,
      "crop": str, "same_crop": bool|None}, ...]

`crop` 是调用方给出的作物约束（可以留空 = 不约束）；`same_crop` 表示这条片段
是不是同一作物的资料，留空表示索引里没有该片段的作物信息、无从判断。

没有向量库时返回空列表（而不是抛错），这样多模态诊断接口的 sources 字段为空数组，
整个流程照常走完，前端只是不显示引用来源。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseRAG(ABC):
    provider = "base"

    @abstractmethod
    def retrieve(self, query: str, top_k: int | None = None, crop: str = "") -> list[dict]:
        """按查询串检索相关片段；crop 非空时优先只在该作物内检索。"""

    def build(self, *, force: bool = False) -> dict:
        """构建/重建索引。默认实现为不支持（Mock 用）。"""
        return {"built": False, "reason": f"{self.provider} 不支持构建索引"}

    def stats(self) -> dict[str, Any]:
        return {"provider": self.provider, "enabled": False}
