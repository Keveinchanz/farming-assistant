"""技能接口的响应模型。

`SkillRunData.result` 故意声明为 `dict[str, Any]`：技能输出字段很多，
逐字段建模会让 `response_model` 静默过滤掉未声明字段。技能层已经负责契约，
这里只保证外层信封稳定。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SkillSummary(BaseModel):
    name: str
    title: str
    description: str
    version: str
    endpoint: str
    requires: list[str] = Field(default_factory=list)
    available: bool = True


class SkillListData(BaseModel):
    count: int = 0
    skills: list[SkillSummary] = Field(default_factory=list)


class SkillDetailData(SkillSummary):
    handler: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    availability: dict[str, Any] = Field(default_factory=dict)


class SkillRunData(BaseModel):
    skill: str
    version: str
    result: dict[str, Any] = Field(default_factory=dict)
    degraded: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: float = 0.0


__all__ = ["SkillSummary", "SkillListData", "SkillDetailData", "SkillRunData"]
