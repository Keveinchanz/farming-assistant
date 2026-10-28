"""技能接口：把两个模型技能暴露成统一的 HTTP 调用。

- `GET  /api/skills`           技能清单（不加载模型）
- `GET  /api/skills/{name}`    单个技能的 manifest 与 JSON Schema
- `POST /api/skills/{name}`    执行技能

两个技能都是公开接口：Skill A 与现有诊断接口一致；Skill B 是无状态纯计算，
输入自带田块数据，不读用户私有数据。技能执行放到线程池，避免 ONNX/LLM 阻塞
事件循环。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from backend.app.core.errors import (
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_MODEL_UNAVAILABLE,
    CODE_NOT_FOUND,
    CODE_PAYLOAD_TOO_LARGE,
    ApiError,
)
from backend.app.schemas.models import ApiResponse
from backend.app.schemas.skills import (
    SkillDetailData,
    SkillListData,
    SkillRunData,
)
from skills.errors import (
    SkillError,
    SkillInputError,
    SkillNotFoundError,
    SkillPayloadTooLargeError,
    SkillUnavailableError,
)
from skills.registry import execute, get_skill, list_skills
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api.skills")

router = APIRouter(prefix="/api/skills", tags=["技能接口"])

# base64 膨胀约 33%，单文件解码后上限 20 MB，因此请求体最多约 30 MB。
MAX_BODY_BYTES = 30 * 1024 * 1024


@router.get(
    "",
    response_model=ApiResponse[SkillListData],
    summary="技能清单",
    description="返回全部可用技能。只检查文件与配置模式，不加载模型。",
)
async def list_all() -> dict:
    items = list_skills()
    return {"code": 0, "msg": "ok", "data": {"count": len(items), "skills": items}}


@router.get(
    "/{name}",
    response_model=ApiResponse[SkillDetailData],
    summary="技能详情",
    description="返回技能说明、输入/输出 JSON Schema、示例与当前可用性。",
)
async def detail(name: str) -> dict:
    try:
        spec = get_skill(name)
    except SkillNotFoundError as e:
        raise ApiError(e.message, CODE_NOT_FOUND, http_status=404) from e
    return {"code": 0, "msg": "ok", "data": spec.manifest()}


@router.post(
    "/{name}",
    response_model=ApiResponse[SkillRunData],
    summary="执行技能",
    description=(
        "请求体是该技能的输入 JSON。成功时 `data.result` 为技能输出；"
        "`data.degraded` 原样透传技能内部的降级状态。"
    ),
)
async def run(name: str, request: Request) -> dict:
    try:
        get_skill(name)
    except SkillNotFoundError as e:
        raise ApiError(e.message, CODE_NOT_FOUND, http_status=404) from e

    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_BODY_BYTES:
                raise ApiError("请求体超过 30 MB", CODE_PAYLOAD_TOO_LARGE, http_status=413)
        except ValueError:
            pass

    raw = await request.body()
    if not raw:
        raise ApiError("请求体为空，需要 JSON 对象", CODE_BAD_REQUEST)
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError("请求体超过 30 MB", CODE_PAYLOAD_TOO_LARGE, http_status=413)

    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ApiError("请求体必须是 UTF-8 编码的 JSON", CODE_BAD_REQUEST) from e
    except json.JSONDecodeError as e:
        raise ApiError(f"请求体不是合法 JSON：{e}", CODE_BAD_REQUEST) from e
    if not isinstance(payload, dict):
        raise ApiError("请求体必须是 JSON 对象", CODE_BAD_REQUEST)

    try:
        result = await run_in_threadpool(execute, name, payload, allow_paths=False)
    except SkillInputError as e:
        raise ApiError(e.message, CODE_BAD_REQUEST) from e
    except SkillPayloadTooLargeError as e:
        raise ApiError(e.message, CODE_PAYLOAD_TOO_LARGE, http_status=413) from e
    except SkillUnavailableError as e:
        raise ApiError(e.message, CODE_MODEL_UNAVAILABLE, http_status=503) from e
    except SkillNotFoundError as e:
        raise ApiError(e.message, CODE_NOT_FOUND, http_status=404) from e
    except SkillError as e:
        raise ApiError(e.message, e.code if isinstance(e.code, int) else CODE_BAD_REQUEST) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("技能执行失败：%s", name)
        raise ApiError(f"技能执行失败：{e}", CODE_INTERNAL, http_status=500) from e

    return {"code": 0, "msg": "ok", "data": result}
