"""阶段 7 的接口数据模型（账号 + 农田）。

**为什么每个字段都要显式写出来**：这个项目已经被 pydantic 的 response_model
静默过滤咬过三次 —— 模型里没声明的字段不会报错，只是安静地消失，前端拿到
一个少了字段的对象，而接口返回 200。所以约定是：**凡是前端要用的字段，
必须在这里出现**，哪怕它只是透传 src/ 层的一个值。

3D 参数（7.3.5）的 12 个字段尤其如此：少一个，田里的植株就少一个形态特征，
而你看不到任何报错。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 账号（7.0）

class AuthUserData(BaseModel):
    id: int
    username: str = Field(..., description="登录名（纯 ASCII）")
    email: str
    nickname: str = Field("", description="显示名，可以是中文")
    avatar: str = Field("", description="头像地址，空则前端用昵称首字母")
    status: str = Field("active", description="active | disabled")
    created_at: str | None = None
    last_login_at: str | None = None


class AuthStatusData(BaseModel):
    """GET /api/auth/status —— 公开接口，顶部导航用它决定显示「登录/注册」还是用户名。"""

    logged_in: bool
    is_admin: bool = False
    user: AuthUserData | None = None


# ---------------------------------------------------------------- 田块（7.1）

class FieldData(BaseModel):
    id: str
    name: str
    crop: str = Field(..., description="水稻 | 玉米 | 油菜 | 小麦 | 大豆 | 棉花 | 马铃薯")
    area_mu: float
    province: str
    city: str
    county: str = Field("", description="县/区；旧记录或未填时为空串")
    lng: float | None = None
    lat: float | None = None
    sow_date: str = Field(..., description="YYYY-MM-DD")
    created_at: str | None = None


class FieldsData(BaseModel):
    fields: list[FieldData] = Field(default_factory=list)
    count: int = 0
    supported_crops: list[str] = Field(default_factory=list)
    default_province: str = ""
    default_city: str = ""


# ---------------------------------------------------------------- 农事记录

class LogData(BaseModel):
    id: str
    field_id: str
    type: str = Field(..., description="sow|fertilize|spray|irrigate|harvest|diagnose|other")
    date: str
    item: str = ""
    amount: str = Field("", description="原样文本（「20 斤」），不做单位换算")
    note: str = ""
    source: str = Field("manual", description="manual|voice|diagnose")
    created_at: str | None = None


class LogsData(BaseModel):
    logs: list[LogData] = Field(default_factory=list)
    count: int = 0


# ---------------------------------------------------------------- 天气（7.2）

class WeatherDay(BaseModel):
    date: str
    tmax: float
    tmin: float
    tmean: float
    precip: float = 0.0
    humidity: float = 0.0


class WeatherData(BaseModel):
    """天气区块。

    degraded / source_note 是**降级的诚实表达**（7.2）：
    历史接口拿不到就用缓存、缓存也没有就只显示已累积积温 —— 无论哪条路，
    都必须让用户知道手里这份数据「截至哪天、来自哪里」，绝不能悄悄换成编的数据。
    """

    city: str = ""
    province: str = ""
    days: list[WeatherDay] = Field(default_factory=list, description="今明后三天（预报）")
    source: str = Field("forecast", description="forecast | normal（用气候基准值代替）")
    source_note: str = ""
    as_of: str = Field("", description="数据截至哪一天")
    degraded: bool = False
    degraded_reason: str = ""


# ---------------------------------------------------------------- 生长（7.3）

class GrowthData(BaseModel):
    field_id: str
    crop: str
    sow_date: str
    days_since_sow: int = 0
    gdd_cum: float = 0.0
    gdd_remain: float = 0.0
    maturity_gdd: float = 0.0
    stage: str = ""
    stage_index: int = 0
    stage_gdd: float = 0.0
    next_stage: str | None = None
    next_gdd: float | None = None
    progress: float = Field(0.0, description="0~1 的全生育期进度，封顶 1.0")
    matured: bool = False
    actions: list[str] = Field(default_factory=list, description="当前阶段的农事提示")
    confidence: str = Field("中", description="低 | 中 | 高 —— 数据不全时就该是「低」")
    as_of: str = ""
    source_note: str = ""


class ModelData(BaseModel):
    """GET /api/farm/model/{id} —— 3D 植株参数（7.3.5 的 12 个字段，一个都不能少）。"""

    field_id: str
    crop: str
    progress: float
    stage: str
    height: float = Field(..., description="株高 cm")
    leafCount: int = Field(..., description="叶片数")
    leafColor: str = Field(..., description="叶色 hex")
    leafColorName: str = Field("", description="叶色的中文名（嫩绿/中绿/深绿/黄绿/金黄）")
    leafDroop: float = Field(..., description="叶片下垂度 0~1")
    stemColor: str = Field(..., description="茎色 hex")
    earColor: str = Field(..., description="穗/角果色 hex")
    earDroop: float = Field(..., description="穗下垂度 0~1")
    dryLeafRatio: float = Field(..., description="枯叶比例 0~1")
    hasEar: bool = Field(..., description="是否已抽穗/结角果")
    earSize: float = Field(..., description="穗大小 0~1")
    hasBlackLayer: bool = Field(..., description="玉米籽粒黑层（完熟标志）")
    milkLineGone: bool = Field(..., description="玉米乳线消失（完熟标志）")
    matured: bool = False
    gdd_cum: float = 0.0
    as_of: str = ""
    degraded: bool = False


# ---------------------------------------------------------------- 建议（7.4）

class AdviceData(BaseModel):
    """一条农事建议。7.4.4 定的字段名固定，不要改。"""

    key: str = Field("", description="稳定标识，供「改天」按条暂存")
    type: str = Field(..., description="如 fertilize / spray / irrigate / disease / harvest")
    reason: str = Field("", description="为什么现在提这一条")
    action: str = Field(..., description="该干什么")
    window: str = Field("", description="建议的时间窗，可为空")
    severity: str = Field("info", description="info | warn | danger")
    priority: str = Field("中", description="高 | 中 | 低")


class RemindersData(BaseModel):
    reminders: list[AdviceData] = Field(default_factory=list)
    count: int = 0
    weather_used: bool = Field(False, description="这些提醒里是否用到了天气预报")
    notes: list[str] = Field(default_factory=list, description="降级说明，如「天气不可用，已跳过天气类提醒」")


# ---------------------------------------------------------------- 收获预测（7.3.4）

class HarvestData(BaseModel):
    field_id: str
    crop: str
    earliest: str | None = Field(None, description="预计最早收获日")
    most_likely: str | None = Field(None, description="最可能")
    latest: str | None = Field(None, description="预计最晚")
    window_days: int = Field(0, description="区间半宽（天）")
    window_text: str = Field("", description="推荐收割窗口（7.4.3 的产物，已含雨前/雨停后的修正）")
    method: str = Field(..., description="xgboost | gdd_fallback")
    basis: str = Field("", description="一句话说明依据（如「积温法（XGBoost 已积累 12/30 条记录）」）")
    matured: bool = False
    overdue: bool = Field(False, description="已过预计成熟期（示例田的油菜就属于这种）")
    sample_count: int = Field(0, description="全平台已有的收获记录条数")
    sample_needed: int = 30
    maturity_shift: float = Field(
        0.0, description="施肥记录对成熟日的影响天数（正=推迟，负=提前），零点为「未施肥」")
    management_note: str = Field(
        "", description="这条影响是怎么来的：档位、氮磷充足度、计入的记录数、未计入的部分")
    management_sources: list[str] = Field(
        default_factory=list, description="影响幅度依据的文献出处（逐条可核对）")


# ---------------------------------------------------------------- 今天该干啥（7.7）

class TodayData(BaseModel):
    """GET /api/farm/today —— 信息面板一次拿齐，避免开页面打五个请求。"""

    field: FieldData
    weather: WeatherData
    growth: GrowthData
    harvest: HarvestData
    harvest_advice: AdviceData | None = None
    reminders: list[AdviceData] = Field(default_factory=list)
    reminders_notes: list[str] = Field(
        default_factory=list,
        description="提醒的降级说明，例如「天气不可用，已跳过天气类提醒」——"
                    "不显示的话，用户会把「没有天气提醒」误读成「天气没问题」")
    logs: list[LogData] = Field(default_factory=list)
    narrative: str = Field("", description="大模型叙述层（无 Key 时为模板拼装）")
    narrative_why: str = Field("", description="「为什么是这个收获日期」的解释（7.5 的第二个职责）")
    narrative_source: str = Field("template", description="llm | template")


# ---------------------------------------------------------------- 诊断入档（7.9）

class AttachDiagnosisRequest(BaseModel):
    field_id: str
    date: str = ""
    label: str = ""
    confidence: float | None = None
    image_base64: str = Field("", description="可选：图片的 base64（不带 data: 前缀）")
    image_name: str = ""
    pest: dict | None = None
    report: dict | None = None


class AttachDiagnosisData(BaseModel):
    diagnosis_id: str
    field_id: str
    date: str
    label: str
    image_path: str = ""
    log_id: str = ""


# ---------------------------------------------------------------- 语音录入（7.8）

class VoiceParseRequest(BaseModel):
    text: str = Field("", description="识别出的文字；为空则解析失败")
    field_id: str = ""


class VoiceParseFields(BaseModel):
    """解析出来的结构化字段。**解析不出来就是空串，绝不猜**（7.8.2）。"""

    field_id: str = ""
    field_name: str = ""
    type: str = ""
    type_name: str = ""
    date: str = ""
    item: str = ""
    amount: str = ""


class VoiceParseData(BaseModel):
    text: str = ""
    fields: VoiceParseFields = Field(default_factory=VoiceParseFields)
    ok: bool = Field(False, description="是否解析出了足够落库的字段")
    reason: str = Field("", description="解析失败或不完整时的明确原因")
    matched: list[str] = Field(default_factory=list, description="命中的词表项，便于用户核对")


class VoiceConfirmRequest(BaseModel):
    """确认落库（7.8：不点确认绝不落库）。"""

    field_id: str
    type: str
    date: str
    item: str = ""
    amount: str = ""
    note: str = ""
    raw_text: str = Field("", description="语音原文，存进 note 便于回溯")


class HarvestMarkRequest(BaseModel):
    field_id: str
    harvest_date: str = ""
    note: str = ""


# ---------------------------------------------------------------- 请求体

class CreateFieldRequest(BaseModel):
    name: str
    crop: str = Field(..., description="水稻 | 玉米 | 油菜 | 小麦 | 大豆 | 棉花 | 马铃薯")
    area_mu: float = 0.0
    province: str = ""
    city: str = ""
    county: str = ""
    lng: float | None = None
    lat: float | None = None
    sow_date: str


class UpdateFieldRequest(BaseModel):
    """PATCH：只传要改的字段。id / user_id / created_at 不在可改之列。"""

    name: str | None = None
    crop: str | None = None
    area_mu: float | None = None
    province: str | None = None
    city: str | None = None
    county: str | None = None
    lng: float | None = None
    lat: float | None = None
    sow_date: str | None = None


class CreateLogRequest(BaseModel):
    field_id: str
    type: str = Field(..., description="sow|fertilize|spray|irrigate|harvest|diagnose|other")
    date: str
    item: str = ""
    amount: str = ""
    note: str = ""
    source: str = Field("manual", description="manual|voice|diagnose")


class SnoozeRequest(BaseModel):
    """7.7 的「[改天]」—— 把某条提醒按 key 暂存若干天。"""

    field_id: str
    reminder_key: str
    days: int = 2
