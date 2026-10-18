"""接口数据模型 —— 用于生成 Swagger 文档与做响应校验。

字段与 src/ 层各模块的返回值一一对应，改动时两边要同步（前端也按这些字段渲染）。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应包。code=0 表示成功。"""

    code: int = Field(0, description="0 表示成功，非 0 为业务错误码")
    msg: str = Field("ok", description="中文提示信息")
    data: T | None = Field(None, description="业务数据")


# ---------------------------------------------------------------- 通用

class TopKItem(BaseModel):
    index: int = Field(..., description="类别索引 0..270")
    label_cn: str = Field(..., description="类别中文名，如「玉米大斑病」")
    crop: str = Field(..., description="作物大类")
    condition: str = Field(..., description="病害描述")
    pathogen_type: str = Field(..., description="病原类型：真菌/细菌/病毒/生理性/药害肥害/健康/其他")
    is_healthy: bool = False
    confidence: float = Field(..., ge=0, le=1)


class Recognition(BaseModel):
    top1: TopKItem | None = None
    topk: list[TopKItem] = Field(default_factory=list)
    low_confidence: bool = False
    confidence_threshold: float = 0.45
    elapsed_ms: float = 0
    backend: str = ""


# ---------------------------------------------------------------- 4.2 图像识别

class DiagnoseImageData(BaseModel):
    top1: TopKItem | None = None
    topk: list[TopKItem] = Field(default_factory=list)
    low_confidence: bool = False
    confidence_threshold: float = 0.45
    elapsed_ms: float = 0
    backend: str = ""
    filename: str = ""


# ---------------------------------------------------------------- 4.3 多模态诊断

class AsrInfo(BaseModel):
    text: str = ""
    matched_terms: list[str] = Field(default_factory=list)
    dialect: str = ""
    dialect_name: str = ""
    provider: str = ""
    dev_pid: int | None = None
    asr_supported: bool = False
    fallback: bool = False
    fallback_note: str = ""
    elapsed_ms: float = 0
    mock: bool = False
    message: str = ""


class TreatmentSection(BaseModel):
    title: str
    content: str


class PestFinding(BaseModel):
    """报告里的单条虫害结论（任务 2.7）。

    `crop` / `pest_type` 两个字段**只有 Mock 会填** —— 它直接对着检测结果取值；
    DeepSeek provider 只回 `name / confidence / advice` 三个字段（由
    `_normalize_pest` 反查检测结果后过滤）。所以这里一律给默认值：
    真把它们写成必填，DeepSeek 那条路会在响应校验上直接 500。

    `name` 必须是**检测结果里出现过的中文名**（`label_cn`）：病害靠 candidates
    这个闭集防幻觉，虫害没有等价的闭集，就改成拿检测结果反查。
    """

    name: str = ""
    confidence: float = Field(0.0, ge=0, le=1)
    crop: str = ""
    pest_type: str = ""
    advice: str = ""


class PestDetection(BaseModel):
    """单个检测框。`bbox` 是**原图**像素坐标 [x1, y1, x2, y2]，不是 letterbox 后的 640 空间。"""

    index: int = 0
    label_cn: str = ""
    label_en: str = ""
    crop: str = ""
    pest_type: str = ""
    confidence: float = Field(0.0, ge=0, le=1)
    bbox: list[float] = Field(default_factory=list)


class PestResult(BaseModel):
    """虫害检测结果（任务 2.7）。

    与 `recognition` 是**两套独立的标签空间**：这里是 102 类 IP102 目标检测，
    上面是 271 类 PDD271 图像分类。刻意不复用 Recognizer 的字段形状。

    `image_size` 是 `[宽, 高]`，前端靠它把 `bbox` 换算成百分比定位覆盖层；
    少了它，检测框在缩略图上会整体错位。

    `confidence_threshold` / `iou_threshold` 是**这次检测实际用的阈值**，由
    `pest_detector.detect()` 一并返回、前端拿来做「本次图片在检测阈值 xx% 以上
    未发现虫害」这句文案。它们曾经漏在这里没声明 —— pydantic 的 response_model
    静默把两个字段丢了，前端只能显示「阈值 0%」。凡是 detector 返回值里会用到
    的键，都必须在这里有对应字段，否则就是又一次静默丢失。
    """

    detections: list[PestDetection] = Field(default_factory=list)
    count: int = 0
    image_size: list[int] = Field(default_factory=list)
    confidence_threshold: float = 0
    iou_threshold: float = 0
    backend: str = ""
    elapsed_ms: float = 0


class PrimaryConclusion(BaseModel):
    """本次诊断的**主结论**是谁 —— 病害还是虫害。

    背景：病害分类器总会给出一个 Top-1，照片上真正的麻烦是虫时它照样报一个病名。
    所以检出虫害时（`count > 0`）由 `fusion._apply_pest_primary` 把主结论切成虫害、
    防治方案一并换过去，病害降级为 `note` 里的「另疑似」。

    **字段必须逐个声明**：嵌套模型同样会被 response_model 静默丢字段，加子字段要在这里加。
    """
    kind: str = Field("disease", description="disease / pest / unknown")
    name: str = ""
    confidence: float = Field(0.0, ge=0, le=1)
    crop: str = ""
    detail: str = Field("", description="虫害填类群 pest_type；病害填病原类型 pathogen_type")
    count: int = Field(0, description="虫害检出框数；病害恒为 0")
    note: str = Field("", description="次要结论，例如「另疑似病害：小麦斑枯病（25%）」")


class DiagnosisReport(BaseModel):
    disease_name: str = ""
    confidence: float = 0
    summary: str = ""
    symptom_match: str = ""
    treatment_sections: list[TreatmentSection] = Field(default_factory=list)
    treatment_plan: str = Field("", description="纯文本版防治方案，供方言播报直接朗读")
    medication_reminder: str = ""
    safety_note: str = ""
    differential: list[str] = Field(default_factory=list)
    source: str = Field("", description="deepseek 或 mock")
    notice: str | None = None
    primary: PrimaryConclusion = Field(
        default_factory=PrimaryConclusion,
        description="主结论。必须给默认值：设成必填会让直接调 provider、"
                    "不经过 fusion 的调用方（如 scripts/09）校验失败")
    pest_findings: list[PestFinding] = Field(
        default_factory=list,
        description="虫害结论。未启用检测器时上游根本不写这个字段，"
                    "这里会补成空列表——前端是拿 `pest` 是否为 null 来判三态的，不受影响")


class SourceItem(BaseModel):
    title: str = ""
    source: str = ""
    score: float | None = None


class DegradedFlags(BaseModel):
    llm: bool = Field(False, description="大模型是否走了规则模板降级")
    rag: bool = Field(False, description="知识库检索是否降级（无引用来源）")
    voice: bool = Field(False, description="语音服务是否走了 Mock")
    pest: bool = Field(
        False, description="虫害检测是否失败。注意与「模型没装」不同："
                           "没装时这里是 false、`pest` 为 null；装了但这次跑挂了才是 true")


class InputSummary(BaseModel):
    has_image: bool = False
    has_audio: bool = False
    has_text: bool = False


class CandidateItem(TopKItem):
    origin: str = Field("", description="候选来源：图像识别 / 文字描述匹配")


class DiagnoseMultimodalData(BaseModel):
    recognition: Recognition | None = None
    asr: AsrInfo | None = None
    fused_summary: str = ""
    report: DiagnosisReport
    candidates: list[CandidateItem] = Field(default_factory=list)
    sources: list[SourceItem] = Field(default_factory=list)
    degraded: DegradedFlags
    dialect: dict[str, str] = Field(default_factory=dict)
    llm_provider: str = ""
    input_summary: InputSummary
    # 虫害检测（任务 2.7）。**必须在这里显式声明** —— pydantic 的 response_model
    # 会把不在模型里的字段静默过滤掉（LibraryDetailData 上面那段注释记的就是这个坑）。
    # 三种状态各有含义，前端据此决定显示「疑似虫害」区块还是「未检出」：
    #   null   = 没装检测模型，或这次检测失败（后者同时 degraded.pest = true）
    #   有值   = 确实跑过检测，detections 可能为空列表
    pest: PestResult | None = None


# ---------------------------------------------------------------- 4.5 知识图书库

class LibraryItem(BaseModel):
    id: str
    title: str
    category: str = Field(..., description="pest 病害防治知识 / insect 虫害防治知识 / care 农作物养护知识")
    category_name: str
    crop: str
    cover: str | None = None
    summary: str
    tags: list[str] = Field(default_factory=list)
    index: int | None = None


class LibraryCategory(BaseModel):
    key: str
    name: str
    count: int


class LibraryListData(BaseModel):
    total: int
    page: int
    size: int
    categories: list[LibraryCategory]
    items: list[LibraryItem]


class LibrarySection(BaseModel):
    type: str
    title: str
    text: str
    images: list[str] = Field(default_factory=list)


class LibraryDetailData(LibraryItem):
    """卡片详情。

    注意：pydantic 的 response_model 会把不在模型里的字段**静默过滤掉**，
    所以数据文件里有的字段（condition / pathogen_type / is_mixed / is_healthy /
    pest_type / latin）必须在这里显式声明，否则接口不返回、前端拿不到。
    虫害卡片用的就是 pest_type（类群）与 latin（学名）这两个 ——
    它们和病害卡片的 pathogen_type 是两套口径，前者不参与拼接。
    """

    crop_category: str = ""
    condition: str = ""
    pathogen_type: str = ""
    pest_type: str = Field("", description="虫害类群（螟蛾类/刺吸类/螨类…），病害卡片为空")
    latin: str = Field("", description="拉丁学名，仅虫害卡片有")
    is_mixed: bool = False
    is_healthy: bool = False
    sections: list[LibrarySection] = Field(default_factory=list)
    read_text: str = Field("", description="纯文本版内容，供方言朗读")
    content_source: str = ""


# ---------------------------------------------------------------- 4.6 地图

class PestTop3Item(BaseModel):
    rank: int
    pest: str
    crop: str
    pathogen_type: str = ""
    risk: str = Field("", description="高 / 中")
    peak_window: str = Field("", description="高发时段，如 3～5月")
    note: str = ""
    library_id: str = Field("", description="点击跳转知识库详情用的卡片 id")


class SeasonItem(BaseModel):
    key: str
    name: str


class ProvinceItem(BaseModel):
    name: str = Field(..., description="与地图 GeoJSON 一致的官方全称，如「内蒙古自治区」")
    short_name: str = ""
    adcode: str = ""
    crops: list[str] = Field(default_factory=list)
    top3: list[PestTop3Item] = Field(default_factory=list)
    threat_scope: int = Field(
        0, description="病害种类基数：该省主要作物涉及的病害类别数（21~74），用于地图分级上色"
    )


class MapPestTop3Data(BaseModel):
    season: str
    seasons: list[SeasonItem]
    provinces: list[ProvinceItem]
    demo_data: bool = Field(True, description="true 表示是合成的演示数据，前端应显示角标")
    disclaimer: str = ""


# ---------------------------------------------------------------- 4.7 惠农政策

class PolicySubItem(BaseModel):
    """政策原文里的一级章节或叶子条目（只有编号和条目名）。"""

    index: int = 0
    title: str


class PolicyPoint(BaseModel):
    """一条逐字照抄的原文要点：条目名 + 该条正文的第一句话。"""

    title: str = Field("", description="要点所属条目名；无编号的新闻稿/技术意见为空")
    text: str = Field(..., description="原文句子，未改写；过长时以「……」标出截断处")


class PolicyItem(BaseModel):
    id: str
    title: str
    publish_unit: str
    publish_date: str = Field(..., description="如 2026年3月")
    publish_date_iso: str = Field("", description="页面实际日期，如 2026-03-25")
    type: str
    type_name: str
    summary: str
    item_count: int = 0
    sections: list[PolicySubItem] = Field(default_factory=list, description="一级章节")
    items: list[PolicySubItem] = Field(default_factory=list, description="叶子条目")
    points: list[PolicyPoint] = Field(default_factory=list, description="原文要点，逐字摘录")
    source_url: str = Field("", description="官方原文地址，前端标题链到这里")
    fetched: bool = False


class PolicyType(BaseModel):
    key: str
    name: str


class PolicyListData(BaseModel):
    total: int
    types: list[PolicyType]
    items: list[PolicyItem]
    year: int = 2026
    demo_data: bool = False


# ---------------------------------------------------------------- 语音 / 方言

class DialectItem(BaseModel):
    key: str
    name: str
    baidu_name: str = ""
    regions: list[str] = Field(default_factory=list)
    asr_supported: bool = Field(
        False, description="当前 provider 下该方言是否有可用的识别模型（讯飞下八种全为 true）")
    asr_dev_pid: int | None = Field(None, description="百度专用；讯飞无此概念，为 null")
    tts_voice_name: str = ""
    tts_library: str = ""
    xfyun_vcn: str | None = Field(None, description="讯飞发音人 id；无可靠发音人时为 null")
    xfyun_tts_supported: bool = Field(
        False, description="讯飞是否有该方言的发音人（false 时播报会回退普通话音色）")


class TTSData(BaseModel):
    audio_url: str | None = None
    dialect: str = ""
    dialect_name: str = ""
    provider: str = ""
    voice_name: str = ""
    voice_library: str = ""
    used_fallback_voice: bool = Field(
        False, description="true 表示该方言无可用发音人，实际用的是普通话音色")
    elapsed_ms: float = 0
    mock: bool = Field(False, description="true 时前端应改用浏览器 speechSynthesis 兜底")
    message: str = ""


class HealthData(BaseModel):
    status: str
    model: dict[str, Any] = Field(default_factory=dict)
    voice: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    rag: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
