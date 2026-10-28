"""技能注册表：清单、JSON Schema、执行入口。

技能 handler 用字符串保存（`skills.crop_diagnosis.handler:run`），执行时才导入。
这样 `GET /api/skills` 不会连带把 onnxruntime / chromadb 拉进进程。
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from skills.availability import availability, is_available
from skills.errors import SkillInputError, SkillNotFoundError
from skills.schemas import (
    CropDiagnosisInput,
    CropDiagnosisOutput,
    FarmGrowthInput,
    FarmGrowthOutput,
)


@dataclass(frozen=True)
class SkillSpec:
    name: str
    title: str
    description: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: str
    requires: tuple[str, ...]
    examples: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def endpoint(self) -> str:
        return f"/api/skills/{self.name}"

    def input_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        if self.name == "crop-diagnosis":
            # Pydantic 的 model_validator 不会进 JSON Schema，手工补跨字段约束
            schema["anyOf"] = [
                {"required": ["image_base64"], "properties": {"image_base64": {"minLength": 1}}},
                {"required": ["image_path"], "properties": {"image_path": {"minLength": 1}}},
                {"required": ["audio_base64"], "properties": {"audio_base64": {"minLength": 1}}},
                {"required": ["audio_path"], "properties": {"audio_path": {"minLength": 1}}},
                {"required": ["text"], "properties": {"text": {"minLength": 1}}},
            ]
            schema["not"] = {
                "anyOf": [
                    {"required": ["image_base64", "image_path"]},
                    {"required": ["audio_base64", "audio_path"]},
                ]
            }
        return schema

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "endpoint": self.endpoint,
            "requires": list(self.requires),
            "available": is_available(self.name),
        }

    def manifest(self) -> dict[str, Any]:
        data = self.summary()
        data.update({
            "handler": self.handler,
            "input_schema": self.input_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "examples": list(self.examples),
            "availability": availability(self.name),
        })
        return data


SKILLS: dict[str, SkillSpec] = {
    spec.name: spec
    for spec in (
        SkillSpec(
            name="crop-diagnosis",
            title="病虫害检测与多模态融合诊断",
            description=(
                "上传作物照片、方言语音或文字症状，返回病害候选、虫害检测框、"
                "融合诊断报告与防治方案。支持只有图片或只有文字的调用。"
            ),
            version="1.0.0",
            input_model=CropDiagnosisInput,
            output_model=CropDiagnosisOutput,
            handler="skills.crop_diagnosis.handler:run",
            requires=("disease_classifier", "pest_detector", "llm", "rag", "voice"),
            examples=(
                {"text": "水稻叶片有褐色斑点，最近多雨"},
                {"image_base64": "<base64>", "text": "看看这是什么病"},
            ),
        ),
        SkillSpec(
            name="farm-growth",
            title="农田生长、收获预测与 3D 参数",
            description=(
                "由田块、播种日期与逐日气温计算积温、生育阶段、收获区间和 "
                "3D 植株参数。不依赖登录与数据库，可直接注入 history。"
            ),
            version="1.0.0",
            input_model=FarmGrowthInput,
            output_model=FarmGrowthOutput,
            handler="skills.farm_growth.handler:run",
            requires=("gdd", "harvest_model", "weather"),
            examples=(
                {
                    "field": {
                        "id": "fld_demo", "name": "演示田", "crop": "水稻",
                        "sow_date": "2026-05-01", "area_mu": 1.2,
                        "province": "湖北省", "city": "武汉市",
                        "lng": 114.3, "lat": 30.58,
                    },
                    "today": "2026-10-22",
                    "history": [
                        {"date": "2026-08-01", "tmax": 32, "tmin": 24},
                        {"date": "2026-08-02", "tmax": 33, "tmin": 25},
                    ],
                },
            ),
        ),
    )
}


def get_skill(name: str) -> SkillSpec:
    spec = SKILLS.get(name)
    if spec is None:
        raise SkillNotFoundError(f"未找到技能：{name}")
    return spec


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "输入不合法"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg") or "输入不合法")
    return f"{location}: {message}" if location else message


def execute(
    name: str,
    payload: dict[str, Any],
    *,
    allow_paths: bool = False,
) -> dict[str, Any]:
    """执行一个技能，返回统一信封。

    只捕获 Pydantic 的输入校验错误并转成 400；其余异常原样抛给 HTTP 层/CLI，
    避免把模型加载失败、上游超时等真实错误伪装成「输入不合法」。
    """
    spec = get_skill(name)
    if not isinstance(payload, dict):
        raise SkillInputError("请求体必须是 JSON 对象")

    module_name, _, func_name = spec.handler.partition(":")
    handler: Callable[..., dict[str, Any]] = getattr(
        importlib.import_module(module_name), func_name
    )

    started = time.perf_counter()
    try:
        result = handler(payload, allow_paths=allow_paths)
    except ValidationError as e:
        raise SkillInputError(_validation_message(e)) from e
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    degraded = result.get("degraded") if isinstance(result, dict) else None
    return {
        "skill": spec.name,
        "version": spec.version,
        "result": result,
        "degraded": degraded if isinstance(degraded, dict) else {},
        "elapsed_ms": elapsed_ms,
    }


def list_skills() -> list[dict[str, Any]]:
    return [spec.summary() for spec in SKILLS.values()]


__all__ = ["SkillSpec", "SKILLS", "get_skill", "execute", "list_skills"]
