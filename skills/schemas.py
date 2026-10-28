"""技能输入/输出契约。

输入模型用于**真正校验**请求；输出模型只用于生成 schema 与文档，**不校验**
handler 的返回值 —— 本项目已经多次踩过 `response_model` / `model_validate`
把未声明字段静默过滤掉的坑，技能输出必须原样透传。

输出模型统一 `extra="allow"`，即使只声明了关键字段也不会丢数据。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

INCLUDE_KEYS = ("growth", "model", "harvest", "advice", "weather")


def _check_iso_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        date.fromisoformat(text)
    except ValueError as e:
        raise ValueError("日期格式应为 YYYY-MM-DD") from e
    return text


# ---------------------------------------------------------------- 输入

class CropDiagnosisInput(BaseModel):
    """病虫害检测与多模态诊断的输入。

    图片/音频各支持 base64 与本地路径二选一：HTTP 只允许 base64，CLI 两种都行。
    至少提供图片、语音、文字中的一项。
    """

    image_base64: str | None = Field(None, description="图片 base64；允许 data:image/...;base64, 前缀")
    image_path: str | None = Field(None, description="本地图片路径，仅 CLI 可用")
    audio_base64: str | None = Field(None, description="音频 base64；允许 data:audio/...;base64, 前缀")
    audio_path: str | None = Field(None, description="本地音频路径，仅 CLI 可用")
    audio_format: str = Field("auto", description="auto / wav / pcm / amr / mp3")
    text: str = Field("", max_length=4000, description="文字症状描述")
    dialect: str | None = Field(None, description="方言 key；留空用默认方言")
    topk: int = Field(5, ge=1, le=10, description="病害候选数量")

    @model_validator(mode="after")
    def _validate_inputs(self) -> "CropDiagnosisInput":
        if self.image_base64 and self.image_path:
            raise ValueError("image_base64 与 image_path 只能二选一")
        if self.audio_base64 and self.audio_path:
            raise ValueError("audio_base64 与 audio_path 只能二选一")
        if not any((
            self.image_base64, self.image_path,
            self.audio_base64, self.audio_path,
            (self.text or "").strip(),
        )):
            raise ValueError("请至少提供图片、语音或文字中的一项")
        return self


class FieldInput(BaseModel):
    """田块。字段与农田库的 field 保持一致，技能不读数据库。"""

    id: str = "skill_field"
    name: str = "技能演示田"
    crop: str = Field(..., min_length=1, description="作物名，如 水稻 / 玉米 / 油菜")
    sow_date: str = Field(..., description="播种日期 YYYY-MM-DD")
    area_mu: float = Field(1.0, gt=0, description="面积（亩）")
    province: str = ""
    city: str = ""
    county: str = ""
    lng: float | None = None
    lat: float | None = None

    @field_validator("sow_date")
    @classmethod
    def _validate_sow_date(cls, value: str) -> str:
        return _check_iso_date(value)


class WeatherRow(BaseModel):
    """逐日气温。`tmean` 缺省时由技能补 (tmax + tmin) / 2。"""

    date: str
    tmax: float
    tmin: float
    tmean: float | None = None
    precip: float | None = None
    humidity: float | None = None

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        return _check_iso_date(value)


class ManagementInput(BaseModel):
    """施肥等管理措施对成熟期的影响。"""

    days: float = 0.0
    note: str = ""
    sources: list[str] = Field(default_factory=list)


class FarmGrowthInput(BaseModel):
    """农田生长、收获预测与 3D 参数的输入。

    有 `history` 就直接算；没有但给了经纬度就联网取；两者都没有则返回完整结构 +
    降级标记，不编造收获日期。
    """

    field: FieldInput
    today: str | None = Field(None, description="计算基准日 YYYY-MM-DD；留空用今天")
    history: list[WeatherRow] = Field(default_factory=list, description="逐日历史气温")
    forecast: list[WeatherRow] = Field(default_factory=list, description="未来天气预报")
    management: ManagementInput | None = None
    sample_count: int | None = Field(None, ge=0, description="收获样本数；传了就不查数据库")
    include: list[str] = Field(
        default_factory=lambda: list(INCLUDE_KEYS),
        description="要返回的块：growth / model / harvest / advice / weather",
    )

    @field_validator("today")
    @classmethod
    def _validate_today(cls, value: str | None) -> str | None:
        return _check_iso_date(value) if value else None

    @field_validator("include")
    @classmethod
    def _validate_include(cls, value: list[str]) -> list[str]:
        unknown = [k for k in value if k not in INCLUDE_KEYS]
        if unknown:
            raise ValueError(f"未知的 include 项：{'、'.join(unknown)}")
        return list(dict.fromkeys(value)) or list(INCLUDE_KEYS)


# ---------------------------------------------------------------- 输出（仅文档）

class SourceNoteOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    organization: str = ""
    document: str = ""
    kind: str = ""


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    disease_name: str = ""
    confidence: float = 0.0
    summary: str = ""
    treatment_sections: list[dict[str, Any]] = Field(default_factory=list)
    treatment_plan: str = ""
    medication_reminder: str = ""
    safety_note: str = ""
    source: str = ""
    primary: dict[str, Any] = Field(default_factory=dict)
    source_notes: list[SourceNoteOut] = Field(default_factory=list)


class CropDiagnosisOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    recognition: dict[str, Any] | None = Field(None, description="病害 Top-K；无图时为 null")
    asr: dict[str, Any] | None = Field(None, description="语音转写；无音频时为 null")
    pest: dict[str, Any] | None = Field(None, description="虫害检测；null 表示本次没跑检测")
    fused_summary: str = ""
    report: ReportOut
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    degraded: dict[str, Any] = Field(default_factory=dict)
    dialect: dict[str, str] = Field(default_factory=dict)
    llm_provider: str = ""
    input_summary: dict[str, Any] = Field(default_factory=dict)


class GrowthOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    field_id: str = ""
    crop: str = ""
    sow_date: str = ""
    days_since_sow: int = 0
    gdd_cum: float = 0.0
    gdd_remain: float = 0.0
    maturity_gdd: float = 0.0
    stage: str = ""
    stage_index: int = 0
    progress: float = 0.0
    matured: bool = False
    actions: list[str] = Field(default_factory=list)
    confidence: str = ""
    as_of: str = ""
    source_note: str = ""


class ModelOut(BaseModel):
    """3D 植株参数，可直接交给 Farm3D.setModel(model)。"""

    model_config = ConfigDict(extra="allow")

    field_id: str = ""
    crop: str = ""
    progress: float = 0.0
    stage: str = ""
    height: float = 0.0
    leafCount: int = 0
    leafColor: str = ""
    leafColorName: str = ""
    leafDroop: float = 0.0
    stemColor: str = ""
    earColor: str = ""
    earDroop: float = 0.0
    dryLeafRatio: float = 0.0
    hasEar: bool = False
    earSize: float = 0.0
    hasBlackLayer: bool = False
    milkLineGone: bool = False


class HarvestOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    field_id: str = ""
    crop: str = ""
    earliest: str | None = None
    most_likely: str | None = None
    latest: str | None = None
    method: str = Field("", description="xgboost 或 gdd_fallback")
    basis: str = ""
    matured: bool = False
    overdue: bool = False
    sample_count: int = 0
    sample_needed: int = 0
    maturity_shift: float = 0.0
    management_note: str = ""


class FarmGrowthOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    field: dict[str, Any]
    today: str = ""
    weather: dict[str, Any] = Field(default_factory=dict)
    growth: GrowthOut | None = None
    model: ModelOut | None = None
    harvest: HarvestOut | None = None
    harvest_advice: dict[str, Any] | None = None
    harvest_window: str = ""
    degraded: dict[str, bool] = Field(default_factory=dict)
    input_summary: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "CropDiagnosisInput", "CropDiagnosisOutput",
    "FarmGrowthInput", "FarmGrowthOutput",
    "FieldInput", "WeatherRow", "ManagementInput",
    "GrowthOut", "ModelOut", "HarvestOut", "ReportOut", "SourceNoteOut",
    "INCLUDE_KEYS",
]
