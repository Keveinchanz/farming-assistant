"""方言与语音接口 —— 支撑前端导航栏的「方言切换」与结果区的「方言播报」。

    GET  /api/dialect/list   八大方言列表（驱动导航栏下拉）
    POST /api/voice/tts      文本转方言语音（供「方言播报」「方言朗读」按钮）
    POST /api/voice/asr      语音转文本（供独立测试；诊断页走 /api/diagnose/multimodal）

Mock 降级时：
    · /api/voice/tts 返回 mock=true 且 audio_url 为 null，前端据此改用浏览器 speechSynthesis
    · /api/voice/asr 返回空文本 + 明确的未配置说明（绝不伪造识别内容）
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field

from backend.app import deps
from backend.app.core.config import get_settings
from backend.app.core.errors import CODE_BAD_REQUEST, CODE_UPSTREAM_ERROR, ApiError
from backend.app.schemas.models import ApiResponse, AsrInfo, DialectItem, TTSData
from src.common.logging_utils import setup_logger
from src.dialect.config import list_dialects
from src.voice.factory import current_provider

logger = setup_logger("agri.api.voice")

router = APIRouter(tags=["方言与语音"])


class TTSRequest(BaseModel):
    text: str = Field(..., description="待合成的文本", max_length=480)
    dialect: str = Field("", description="方言 key，留空用默认方言")


@router.get(
    "/api/dialect/list",
    response_model=ApiResponse[list[DialectItem]],
    summary="八大方言列表",
    description=(
        "返回可切换的方言及其语音参数。\n\n"
        "八种方言的取舍标准是「讯飞 /v2/tts 接口确实有发音人、且实测出声」——"
        "没有发音人的方言（上海话/闽南语/北京话查不到，天津话只有属「超拟人」接口的"
        "x4_zijin_oral、实测 11200）已直接从配置里删除，不留「回退普通话音色」的假支持。\n\n"
        "注意 asr_supported 字段随当前语音服务（/api/health 的 voice.provider）变化：\n"
        "  · 讯飞方言大模型：一个 accent=mulacc 覆盖 202 种方言，八种全部为 true；\n"
        "  · 百度短语音识别 REST：只有普通话/粤语/四川话三个方言模型，其余回退普通话。"
    ),
)
async def dialect_list():
    deps.get_voice()        # 先确保 provider 已解析（首屏可能先请求本接口再请求 /api/health）
    return {"code": 0, "msg": "ok", "data": list_dialects(provider=current_provider())}


@router.post(
    "/api/voice/tts",
    response_model=ApiResponse[TTSData],
    summary="文本转方言语音",
    description=(
        "把诊断结论或知识正文合成为方言语音，返回可播放的音频地址（讯飞返回 mp3）。\n\n"
        "used_fallback_voice=true 表示该方言在当前语音服务下没有可用发音人"
        "（八种方言都实测有发音人，正常配置下不会出现），实际播的是普通话音色。\n\n"
        "未配置语音 Key 时返回 mock=true 且 audio_url 为 null，"
        "前端应改用浏览器内置 speechSynthesis 朗读（仅普通话）。"
    ),
)
async def text_to_speech(req: TTSRequest):
    text = (req.text or "").strip()
    if not text:
        raise ApiError("待合成文本不能为空", CODE_BAD_REQUEST)

    settings = get_settings()
    voice = deps.get_voice()
    try:
        result = voice.synthesize(text, dialect=req.dialect or settings.diagnose_default_dialect)
    except Exception as e:  # noqa: BLE001
        logger.exception("语音合成失败")
        raise ApiError(f"语音合成失败：{e}", CODE_UPSTREAM_ERROR, http_status=502) from e

    audio_url = None
    if result.get("audio_bytes"):
        audio_url = _persist_audio(result["audio_bytes"], result.get("mime") or "audio/mpeg")
        result["audio_url"] = audio_url

    result.pop("audio_bytes", None)     # 二进制不放进 JSON 响应
    result["dialect_name"] = result.get("dialect_name", "")
    logger.info("语音合成：方言=%s provider=%s mock=%s -> %s",
                result.get("dialect"), result.get("provider"), result.get("mock"), audio_url)
    return {"code": 0, "msg": "ok", "data": result}


@router.post(
    "/api/voice/asr",
    response_model=ApiResponse[AsrInfo],
    summary="语音转文本",
    description=(
        "上传 16kHz 单声道音频，返回识别文本与命中的农业专业术语。\n\n"
        "格式支持随语音服务而异：百度支持 WAV/PCM/AMR，讯飞方言大模型只接受裸 PCM"
        "（上传 WAV 时后端会自动剥掉容器头，前端无需转码）。\n\n"
        "诊断页的完整流程请用 /api/diagnose/multimodal，本接口用于独立测试语音链路。"
    ),
)
async def speech_to_text(
    audio: UploadFile = File(..., description="16kHz 单声道 WAV / PCM（百度另支持 AMR）"),
    dialect: str = Form("", description="方言 key"),
):
    data = await audio.read()
    if not data:
        raise ApiError("音频文件为空", CODE_BAD_REQUEST)

    settings = get_settings()
    voice = deps.get_voice()
    try:
        result = voice.transcribe(data, dialect=dialect or settings.diagnose_default_dialect)
    except ValueError as e:
        raise ApiError(str(e), CODE_BAD_REQUEST) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("语音识别失败")
        raise ApiError(f"语音识别失败：{e}", CODE_UPSTREAM_ERROR, http_status=502) from e

    return {"code": 0, "msg": "ok", "data": result}


def _persist_audio(data: bytes, mime: str) -> str:
    """把合成音频落盘到 frontend/audio/，通过 /audio/... 提供访问。

    前端目录是挂在根路径上的（见 backend/app/main.py），因此不需要 /static 前缀。
    """
    import uuid

    from src.common.io_utils import ensure_dir
    from src.common.paths import path_of

    ext = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav"}.get(mime, ".mp3")
    out_dir = ensure_dir(path_of("frontend_dir") / "audio")
    name = f"tts_{uuid.uuid4().hex[:12]}{ext}"
    (out_dir / name).write_bytes(data)
    return f"/audio/{name}"
