"""阶段 7 / 7.10 —— 农田接口（13 个 + 1 个清单漏掉的）。

清单的接口表逐个实现（方法/路径/是否需登录完全一致）。**额外加了第 14 个**：
`POST /api/farm/reminder_snooze` —— 7.7 的交互里明确要求「[改天] snooze 2 天」，
但 7.10 的表里没有对应接口，而暂存必须落库（只放前端的话，刷新或换设备就失效了）。
这是清单自身的一处遗漏，补上并在此说明，而不是假装不需要。

所有接口都走 `Depends(current_user_id)`，且**每个 repo 调用都带 user_id** ——
跨用户访问由 repo 抛 NotFound，这里统一转 404（7.0.5）。
"""

from __future__ import annotations

import base64
import binascii
import sqlite3

from fastapi import APIRouter, Depends, Query, Request

from backend.app.core.auth_deps import check_origin, current_user_id, db_conn
from backend.app.core.errors import CODE_BAD_REQUEST, CODE_NOT_FOUND, ApiError
from backend.app.schemas.farm import (
    AttachDiagnosisData,
    AttachDiagnosisRequest,
    CreateFieldRequest,
    CreateLogRequest,
    FieldData,
    FieldsData,
    GrowthData,
    HarvestMarkRequest,
    LogData,
    LogsData,
    ModelData,
    SnoozeRequest,
    TodayData,
    UpdateFieldRequest,
    VoiceParseData,
    VoiceParseRequest,
)
from backend.app.schemas.models import ApiResponse
from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.farm import (
    advisor,
    gdd,
    growth,
    harvest_model,
    management,
    narrator,
    repo,
    stages,
    users,
    voice_parse,
)
from src.farm.repo import NotFound
from src.farm.timeutil import days_between, today_str

logger = setup_logger("agri.api.farm")

router = APIRouter(prefix="/api/farm", tags=["我的农田"])


def _bad(msg: str) -> ApiError:
    return ApiError(msg, CODE_BAD_REQUEST, http_status=400)


def _field_or_404(conn: sqlite3.Connection, user_id: int, field_id: str) -> dict:
    try:
        return repo.get_field(conn, user_id, field_id)
    except NotFound as e:
        raise ApiError(str(e), CODE_NOT_FOUND, http_status=404) from e


def _default_field(conn: sqlite3.Connection, user_id: int) -> dict:
    fields = repo.list_fields(conn, user_id)
    if not fields:
        raise ApiError("你还没有田块，请先新建一块田", CODE_NOT_FOUND, http_status=404)
    return fields[0]


def _compute(conn: sqlite3.Connection, user_id: int, field: dict) -> tuple[dict, dict, list, dict, dict]:
    """一次算齐：天气状态 / 生长 / 积温序列 / 3D 参数 / 收获预测。

    四个接口（today / growth / model / harvest）都要这几样，算一遍共用，
    避免同一个请求里把积温算四遍、把天气读四遍。
    """
    status = growth.ensure_history(conn, user_id, field)
    g = growth.compute_growth(field, status)
    cfg = stages.crop_config(field["crop"])
    series = gdd.accumulate(status["rows"], cfg)
    m = growth.compute_model(field, g, status, degraded=status["degraded"])
    # 施肥记录要读一次 logs —— 生长模型本身不吃这块信息（见 src/farm/management.py 的说明），
    # 所以在这里单独算好再交给收获预测。只读不写，不动 schema。
    nutrition = management.field_nutrition(conn, user_id, field)
    mgmt = management.maturity_shift_days(nutrition, cfg)
    h = harvest_model.predict_harvest(field, g, series, management=mgmt)
    return status, g, series, m, h


# ---------------------------------------------------------------- 田块

@router.get("/fields", response_model=ApiResponse[FieldsData], summary="我的田块列表")
async def list_fields(conn=Depends(db_conn), user_id: int = Depends(current_user_id)):
    fields = repo.list_fields(conn, user_id)
    settings = users.get_settings(conn, user_id)
    return {
        "code": 0, "msg": "ok",
        "data": {
            "fields": fields, "count": len(fields),
            "supported_crops": list(stages.supported_crops()),
            "default_province": settings.get("default_province", ""),
            "default_city": settings.get("default_city", ""),
        },
    }


@router.post("/fields", response_model=ApiResponse[FieldData], summary="新建田块")
async def create_field(
    body: CreateFieldRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        field = repo.create_field(
            conn, user_id, name=body.name, crop=body.crop, area_mu=body.area_mu,
            province=body.province, city=body.city, county=body.county,
            lng=body.lng, lat=body.lat, sow_date=body.sow_date,
        )
    except ValueError as e:
        raise _bad(str(e))
    return {"code": 0, "msg": "ok", "data": field}


@router.patch("/fields/{field_id}", response_model=ApiResponse[FieldData], summary="修改田块")
async def update_field(
    field_id: str, body: UpdateFieldRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        field = repo.update_field(conn, user_id, field_id, body.model_dump(exclude_unset=True))
    except NotFound as e:
        raise ApiError(str(e), CODE_NOT_FOUND, http_status=404) from e
    except ValueError as e:
        raise _bad(str(e))
    return {"code": 0, "msg": "ok", "data": field}


@router.delete("/fields/{field_id}", response_model=ApiResponse[dict], summary="删除田块",
               description="连带删除该田块的农事记录、逐日气温、诊断与生长快照。")
async def delete_field(
    field_id: str, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        repo.delete_field(conn, user_id, field_id)
    except NotFound as e:
        # 不属于自己的田块同样是 404 —— 不暴露它是否存在（7.0.5）
        raise ApiError(str(e), CODE_NOT_FOUND, http_status=404) from e
    return {"code": 0, "msg": "ok", "data": {"deleted": True, "field_id": field_id}}


# ---------------------------------------------------------------- 农事记录

@router.post("/logs", response_model=ApiResponse[LogData], summary="记一笔农事")
async def create_log(
    body: CreateLogRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    date = body.date or today_str()
    try:
        log = repo.create_log(
            conn, user_id, field_id=body.field_id, type=body.type, date=date,
            item=body.item, amount=body.amount, note=body.note, source=body.source,
        )
    except NotFound as e:
        raise ApiError(str(e), CODE_NOT_FOUND, http_status=404) from e
    except ValueError as e:
        raise _bad(str(e))
    return {"code": 0, "msg": "ok", "data": log}


@router.get("/logs", response_model=ApiResponse[LogsData], summary="农事记录（倒序）")
async def list_logs(
    field_id: str = Query("", description="留空则返回全部田块的记录"),
    limit: int = Query(200, ge=1, le=1000),
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    if field_id:
        _field_or_404(conn, user_id, field_id)      # 不属于自己的田块 → 404
    logs = repo.list_logs(conn, user_id, field_id=field_id or None, limit=limit)
    return {"code": 0, "msg": "ok", "data": {"logs": logs, "count": len(logs)}}


# ---------------------------------------------------------------- 今天该干啥（7.7）

@router.get("/today", response_model=ApiResponse[TodayData], summary="今天该干啥（信息面板一次拿齐）",
            description="不传 field_id 时用第一块田。返回天气、生长、收获预测、提醒与历史。")
async def today(
    field_id: str = Query("", description="田块 id，留空用第一块"),
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    field = _field_or_404(conn, user_id, field_id) if field_id else _default_field(conn, user_id)
    status, g, series, m, h = _compute(conn, user_id, field)
    weather = growth.next_days(conn, user_id, field, days=3)

    rem = advisor.reminders(conn, user_id, field, g, series, weather)
    h_advice = advisor.harvest_advice(g, m, h, weather)
    window = advisor.harvest_window(series, h, weather, maturity_gdd=g["maturity_gdd"])

    logs = repo.list_logs(conn, user_id, field_id=field["id"], limit=50)
    talk = narrator.narrative(field, g, weather, h, rem["reminders"], logs)

    # 记一条当日生长快照：仓库里那张表就是为这个存在的，
    # 「同一天重复请求」由主键幂等兜住，不会堆出多行。
    repo.upsert_snapshot(
        conn, user_id, field_id=field["id"], date=today_str(),
        gdd=g["gdd_cum"], stage=g["stage"], height=m["height"],
        leaf_count=m["leafCount"], color_idx=None,
        source="gdd",
    )

    return {
        "code": 0, "msg": "ok",
        "data": {
            "field": field,
            "weather": weather,
            "growth": g,
            "harvest": {**h, "window_text": window},
            "harvest_advice": h_advice,
            # 收割窗口挂在收获建议上一起给前端（7.4.3 的产物就是这个字符串）
            "reminders": rem["reminders"],
            "reminders_notes": rem["notes"],
            "logs": logs,
            "narrative": talk["text"],
            "narrative_why": talk["why"],
            "narrative_source": talk["source"],
        },
    }


@router.post("/reminder_snooze", response_model=ApiResponse[dict], summary="[改天]：暂缓某条提醒",
               description="清单 7.10 的接口表漏了这一条，但 7.7 的交互要求「[改天] snooze 2 天」。")
async def reminder_snooze(
    body: SnoozeRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        until = repo.snooze_reminder(
            conn, user_id, body.field_id, body.reminder_key, days=body.days
        )
    except NotFound as e:
        raise ApiError(str(e), CODE_NOT_FOUND, http_status=404) from e
    return {"code": 0, "msg": "ok", "data": {"snooze_until": until}}


# ---------------------------------------------------------------- 生长 / 3D / 收获

@router.get("/growth/{field_id}", response_model=ApiResponse[GrowthData], summary="积温与生育阶段")
async def field_growth(
    field_id: str,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    field = _field_or_404(conn, user_id, field_id)
    _status, g, _series, _m, _h = _compute(conn, user_id, field)
    return {"code": 0, "msg": "ok", "data": g}


@router.get("/model/{field_id}", response_model=ApiResponse[ModelData], summary="3D 植株参数（12 项）")
async def field_model(
    field_id: str,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    field = _field_or_404(conn, user_id, field_id)
    _status, g, _series, m, _h = _compute(conn, user_id, field)
    return {"code": 0, "msg": "ok", "data": m}


@router.post("/harvest_mark", response_model=ApiResponse[dict], summary="[我收了]：记一次收获",
               description="写入 harvest_records（同时记一条 harvest 农事），并把当日的数值特征存下来，"
                           "供全平台累计 ≥30 条后训练 XGBoost。特征不含任何身份信息。")
async def harvest_mark(
    body: HarvestMarkRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    field = _field_or_404(conn, user_id, body.field_id)
    date = body.harvest_date or today_str()
    if days_between(field["sow_date"], date) < 0:
        raise _bad("收获日不能早于播种日")

    _status, g, series, _m, h = _compute(conn, user_id, field)
    features = harvest_model.build_features(field, g, series)
    record = repo.create_harvest(
        conn, user_id, field_id=field["id"], crop=field["crop"],
        sow_date=field["sow_date"], harvest_date=date,
        actual_days=days_between(field["sow_date"], date),
        gdd_at_harvest=g["gdd_cum"], features=features,
    )
    repo.create_log(
        conn, user_id, field_id=field["id"], type="harvest", date=date,
        item=field["crop"], note=body.note or "标记为已收获", source="manual",
    )
    total = repo.count_harvests(conn)
    return {
        "code": 0, "msg": "ok",
        "data": {
            "harvest_id": record["id"], "actual_days": record["actual_days"],
            "gdd_at_harvest": record["gdd_at_harvest"],
            "sample_count": total, "sample_needed": harvest_model.TRAIN_MIN_SAMPLES,
            "note": ("全平台样本已够，可以运行 scripts/24_train_harvest_model.py 训练模型"
                     if total >= harvest_model.TRAIN_MIN_SAMPLES
                     else f"全平台已积累 {total}/{harvest_model.TRAIN_MIN_SAMPLES} 条收获记录"),
            "prediction_method": h["method"],
        },
    }


# ---------------------------------------------------------------- 病虫害入档（7.9）

DIAGNOSIS_MAX_BYTES = 6 * 1024 * 1024


@router.post("/attach_diagnosis", response_model=ApiResponse[AttachDiagnosisData],
             summary="把一次诊断记入我的农田（7.9）",
             description="图片存 data/farm_images/{user_id}/{field_id}/{date}_{id}.jpg，"
                         "同时写 diagnoses 与一条 type=diagnose 的农事记录。")
async def attach_diagnosis(
    body: AttachDiagnosisRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    field = _field_or_404(conn, user_id, body.field_id)
    date = body.date or today_str()

    saved_path = ""
    image_bytes = b""
    if body.image_base64:
        try:
            image_bytes = base64.b64decode(body.image_base64, validate=True)
        except (binascii.Error, ValueError):
            raise _bad("图片数据不是合法的 base64")
        if len(image_bytes) > DIAGNOSIS_MAX_BYTES:
            raise _bad(f"图片过大（>{DIAGNOSIS_MAX_BYTES // 1024 // 1024} MB）")

    # 先建记录拿到 id，再按 7.1 约定的路径落盘：{user_id}/{field_id}/{date}_{id}.jpg
    record = repo.create_diagnosis(
        conn, user_id, field_id=field["id"], date=date,
        label=body.label, confidence=body.confidence,
        pest_json=body.pest,      # 虫害模型不可用时这里是 None → 存 NULL，不编（7.9）
        report_json=body.report,
    )

    if image_bytes:
        folder = path_of("farm_images") / str(user_id) / field["id"]
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".png" if (body.image_name or "").lower().endswith(".png") else ".jpg"
        target = folder / f"{date}_{record['id']}{suffix}"
        target.write_bytes(image_bytes)
        saved_path = str(target.relative_to(path_of("farm_images")))
        conn.execute(
            "UPDATE diagnoses SET image_path = ? WHERE user_id = ? AND id = ?",
            (saved_path, user_id, record["id"]),
        )

    log = repo.create_log(
        conn, user_id, field_id=field["id"], type="diagnose", date=date,
        item=body.label or "病虫害诊断",
        note="来自诊断页" if body.report else "",
        source="diagnose",
    )

    logger.info("诊断入档：user=%s field=%s label=%s", user_id, field["id"], body.label)
    return {
        "code": 0, "msg": "ok",
        "data": {
            "diagnosis_id": record["id"], "field_id": field["id"], "date": date,
            "label": body.label, "image_path": saved_path, "log_id": log.get("id", ""),
        },
    }


# ---------------------------------------------------------------- 语音录入（7.8）

@router.post("/voice_parse", response_model=ApiResponse[VoiceParseData],
             summary="解析一句话（语音识别的结果）",
             description="规则优先，认不全时用大模型兜底；仍认不全就返回空字段 + 明确原因，绝不猜。"
                         "**这个接口只解析，不落库** —— 落库要用户点确认（POST /api/farm/logs）。")
async def voice_parse_endpoint(
    body: VoiceParseRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    fields = repo.list_fields(conn, user_id)
    result = voice_parse.parse(body.text, fields)
    if not result["ok"]:
        result = voice_parse.parse_with_llm(body.text, result, fields)
    return {"code": 0, "msg": "ok", "data": result}


@router.post("/voice_reparse", response_model=ApiResponse[VoiceParseData],
             summary="改完文字后重新解析（7.8.2 的双向联动）",
             description="与 voice_parse 同一套逻辑；单独一个路径是为了让前端意图清晰 ——"
                         "用户改了识别文字之后重新解析，而不是重新说一遍。")
async def voice_reparse(
    body: VoiceParseRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    fields = repo.list_fields(conn, user_id)
    result = voice_parse.parse(body.text, fields)
    if not result["ok"]:
        result = voice_parse.parse_with_llm(body.text, result, fields)
    return {"code": 0, "msg": "ok", "data": result}
