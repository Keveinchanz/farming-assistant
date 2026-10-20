"""大模型能力的抽象接口（任务 3.3）。

无论真实实现还是 Mock，`generate_report` 都必须返回同一套结构：
    {
      "disease_name": str,
      "confidence": float,
      "symptom_match": str,                 # 症状匹配说明
      "treatment_sections": [{"title": str, "content": str}],   # 综合防治方案（结构化）
      "treatment_plan": str,                # 纯文本拼接版，供方言播报
      "medication_reminder": str,           # 用药提醒
      "safety_note": str,
      "summary": str,                       # 一句话结论
      "differential": [str],                # 需鉴别诊断的其他可能（来自候选，不是编造）
      "source": str                         # "deepseek" | "mock"
    }

约束设计：模型**只能在候选病害列表里选**（候选来自图像识别 top-k 与文字描述命中），
不允许自由发挥病名 —— 这是抑制大模型在专业领域幻觉最有效的一招。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseLLM(ABC):
    provider = "base"

    @abstractmethod
    def generate_report(self, context: dict[str, Any]) -> dict:
        """根据多模态上下文生成诊断报告。

        context 约定字段（由 src/llm/fusion.py 组装）：
            image_result   : 图像识别结果（可为 None）
            asr_text       : 语音转写文本（可为 ""）
            user_text      : 用户文字描述（可为 ""）
            candidates     : [{"label_cn","crop","condition","pathogen_type","confidence"}]
            retrieved      : RAG 检索到的片段 [{"title","text","source"}]
            dialect_name   : 目标方言名（仅用于报告措辞提示，不改变内容）
        """

    @abstractmethod
    def answer(self, question: str, *, context: str = "") -> str:
        """问答交互（任务 3.3 的"问答交互"能力）。"""

    # ---- 以下两个是给「AI 助手」用的可选能力，不是抽象方法 ----
    # 不做成 abstractmethod 的原因：Mock 与未来的 provider 不必为了通过类型检查
    # 硬写一堆空实现；调用方本来就要处理「返回空 -> 回退规则」的情况。

    def reply(self, message: str, *, context: str = "",
              history: list[dict[str, str]] | None = None) -> str:
        """自由对话。默认返回空串，表示「本 provider 不支持」，由调用方回退模板话术。"""
        return ""

    def classify_intent(self, text: str, labels: list[str]) -> dict:
        """把一句话分类到 labels 里的某个意图。默认返回空 dict（交给规则路由兜底）。"""
        return {}

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider}
