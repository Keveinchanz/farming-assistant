"""任务 4.2 / 4.3 —— 病虫害识别接口、多模态融合诊断接口，以及 3.3 的问答交互接口。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from backend.app import deps
from backend.app.core.config import get_settings
from backend.app.core.errors import CODE_BAD_REQUEST, CODE_UPSTREAM_ERROR, ApiError
from backend.app.schemas.models import (
    ApiResponse,
    DiagnoseImageData,
    DiagnoseMultimodalData,
)
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api.diagnose")

router = APIRouter(prefix="/api/diagnose", tags=["智能诊断"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/bmp", "image/webp", "image/jpg"}


async def _read_upload(file: UploadFile, *, kind: str) -> bytes:
    """读取上传文件并做大小校验。"""
    settings = get_settings()
    data = await file.read()
    if not data:
        raise ApiError(f"{kind}文件为空", CODE_BAD_REQUEST)
    if len(data) > settings.max_upload_bytes:
        raise ApiError(
            f"{kind}文件过大（{len(data) / 1024 / 1024:.1f} MB），"
            f"上限 {settings.max_upload_mb} MB",
            CODE_BAD_REQUEST,
        )
    return data


# -------------------------------------------------------------------- 4.2 图像识别

@router.post(
    "/image",
    response_model=ApiResponse[DiagnoseImageData],
    summary="病虫害图像识别",
    description="上传一张作物叶片/植株照片，返回最可能的病害类别与 Top-5 候选及置信度。",
)
async def diagnose_image(file: UploadFile = File(..., description="作物叶片或植株照片")):
    if file.content_type and file.content_type.lower() not in ALLOWED_IMAGE_TYPES:
        raise ApiError(f"不支持的图片格式：{file.content_type}；请上传 JPG / PNG / BMP / WEBP",
                       CODE_BAD_REQUEST)

    data = await _read_upload(file, kind="图片")

    try:
        result = deps.get_predictor().predict_bytes(data)
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("图像识别失败")
        raise ApiError(f"图像识别失败：{e}", CODE_UPSTREAM_ERROR, http_status=502) from e

    result["filename"] = file.filename or ""
    return {"code": 0, "msg": "ok", "data": result}


# -------------------------------------------------------------------- 4.3 多模态融合

@router.post(
    "/multimodal",
    response_model=ApiResponse[DiagnoseMultimodalData],
    summary="多模态融合诊断",
    description=(
        "整合「图片识别结果 + 语音转写内容 + 文字描述」，调用大模型生成完整诊断方案。\n\n"
        "三个输入至少要提供一个（可为空则忽略该路）。\n\n"
        "- 语音请上传 16kHz 单声道 WAV/PCM（浏览器录音请用前端页面自带的录音功能，"
        "它会直接产出合规的 WAV；讯飞侧后端会自行剥掉 WAV 头，无需前端转码）\n"
        "- 未配置讯飞/百度/DeepSeek Key 时会自动降级为 Mock，响应里的 `degraded` 字段会如实标注"
    ),
)
async def diagnose_multimodal(
    image: UploadFile | None = File(None, description="作物照片（可选）"),
    audio: UploadFile | None = File(None, description="语音描述（可选，16kHz WAV/PCM/AMR）"),
    text: str = Form("", description="文字症状描述（可选）"),
    dialect: str = Form("", description="方言 key，留空用默认（见 /api/dialect/list）"),
):
    if image is None and audio is None and not (text or "").strip():
        raise ApiError("请至少提供一项输入：图片 / 语音 / 文字描述", CODE_BAD_REQUEST)

    image_bytes = await _read_upload(image, kind="图片") if image is not None else None
    audio_bytes = await _read_upload(audio, kind="音频") if audio is not None else None

    settings = get_settings()
    try:
        result = deps.get_fusion().diagnose(
            image_bytes=image_bytes,
            audio_bytes=audio_bytes,
            user_text=(text or "").strip(),
            dialect=dialect or settings.diagnose_default_dialect,
        )
    except ApiError:
        raise
    except ValueError as e:
        # 典型场景：音频格式不被语音接口接受（如 webm），lib 层已给出人话说明
        raise ApiError(str(e), CODE_BAD_REQUEST) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("多模态诊断失败")
        raise ApiError(f"多模态诊断失败：{e}", CODE_UPSTREAM_ERROR, http_status=502) from e

    return {"code": 0, "msg": "ok", "data": result}


# -------------------------------------------------------------------- 3.3 问答交互

class AskRequest(BaseModel):
    question: str = Field(..., description="农户的追问，如「这个病要不要马上打药？」", max_length=500)
    context: str = Field("", description="诊断结论等上下文，由前端把结果摘要带上", max_length=2000)


class AskData(BaseModel):
    question: str
    answer: str
    provider: str = Field("", description="deepseek 或 mock")


@router.post(
    "/ask",
    response_model=ApiResponse[AskData],
    summary="农技问答（任务 3.3 的问答交互能力）",
    description=(
        "针对诊断结果追问，例如「什么时候打药最合适」「会不会传染给旁边的地块」。\n\n"
        "前端会把诊断结论摘要放进 `context`，让模型在已知病情的前提下作答。\n"
        "未配置 DeepSeek Key 时返回演示提示，引导用户查阅知识图书库。"
    ),
)
async def ask(req: AskRequest):
    question = (req.question or "").strip()
    if not question:
        raise ApiError("问题不能为空", CODE_BAD_REQUEST)

    llm = deps.get_llm()
    try:
        answer = llm.answer(question, context=(req.context or "").strip())
    except Exception as e:  # noqa: BLE001
        logger.exception("问答失败")
        raise ApiError(f"问答失败：{e}", CODE_UPSTREAM_ERROR, http_status=502) from e

    return {
        "code": 0, "msg": "ok",
        "data": {"question": question, "answer": answer,
                 "provider": getattr(llm, "provider", "unknown")},
    }
