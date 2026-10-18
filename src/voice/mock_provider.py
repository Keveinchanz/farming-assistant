"""语音能力的 Mock 实现 —— 没配语音 Key 时的降级路径。

原则：**宁可明确告知"没做"，也不伪造结果**。
    · ASR：不编造识别文本，返回空文本 + 明确的未配置说明，让诊断流程自然忽略语音这一路输入
      （多模态接口本身允许「图像/语音/文字」三者任意组合缺失）。
    · TTS：不生成假的音频，返回 mock=True + 说明；前端收到后用浏览器内置的
      speechSynthesis 兜底朗读，用户在无 Key 环境下依然能听到播报。

返回结构与 BaiduVoice / IflytekVoice 完全一致（同名的键），因此后端与前端无需区分。
提示语里的"该配什么 Key"按当前实际选中的 provider 生成（原先写死"百度"，换了讯飞
之后那句话会把人引到错的地方）。
"""

from __future__ import annotations

import time

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.dialect.config import asr_params, tts_params
from src.voice.base import ASRResult, BaseVoice, TTSResult

logger = setup_logger("agri.voice.mock")

BAIDU_HINT = "百度需 BAIDU_API_KEY / BAIDU_SECRET_KEY"
IFLYTEK_HINT = "讯飞需 IFLYTEK_APPID / IFLYTEK_API_KEY / IFLYTEK_API_SECRET"


def _credential_hint() -> str:
    """按 VOICE_PROVIDER 指向的服务，给出该服务缺哪几个 Key。"""
    mode = (env("VOICE_PROVIDER", "auto") or "auto").strip().lower()
    if mode == "iflytek":
        return IFLYTEK_HINT
    if mode == "baidu":
        return BAIDU_HINT
    return f"{IFLYTEK_HINT}；{BAIDU_HINT}"


def asr_notice() -> str:
    return (
        f"未配置语音服务（{_credential_hint()}），已跳过语音转写。"
        "填写 Key 后重启服务即可启用；当前诊断将基于图片与文字描述进行。"
    )


def tts_notice() -> str:
    return (
        f"未配置语音服务（{_credential_hint()}），无法生成方言语音。"
        "前端将改用浏览器内置语音朗读"
        "（浏览器朗读只支持普通话，不是真正的方言音色）。配置 Key 后可获得方言播报。"
    )


class MockVoice(BaseVoice):
    """降级实现：结构与真实实现一致，内容明确标注为未启用。"""

    provider = "mock"

    def transcribe(self, audio: bytes, *, dialect: str | None = None,
                   audio_format: str = "auto") -> ASRResult:
        t0 = time.perf_counter()
        params = asr_params(dialect)
        logger.info("Mock ASR 被调用（音频 %d 字节），未做真实转写", len(audio or b""))

        return ASRResult({
            "text": "",
            "matched_terms": [],
            "dialect": params["dialect"],
            "dialect_name": params["dialect_name"],
            "provider": self.provider,
            "dev_pid": params["dev_pid"],
            "asr_supported": params["asr_supported"],
            "fallback": params["asr_fallback"],
            "fallback_note": params["fallback_note"],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "mock": True,
            "message": asr_notice(),
            "audio_bytes": len(audio or b""),
        })

    def synthesize(self, text: str, *, dialect: str | None = None) -> TTSResult:
        params = tts_params(dialect)
        return TTSResult({
            "audio_bytes": None,
            "audio_url": None,
            "mime": None,
            "dialect": params["dialect"],
            "dialect_name": params["dialect_name"],
            "provider": self.provider,
            "voice_name": params["voice_name"],
            "voice_library": params["voice_library"],
            "used_fallback_voice": False,
            "elapsed_ms": 0.0,
            "mock": True,
            "message": tts_notice(),
            "text": (text or "")[:200],
        })

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "reason": f"未配置语音服务（{_credential_hint()}）",
            "asr_enabled": False,
            "tts_enabled": False,
            "frontend_tts_fallback": "browser-speechSynthesis",
        }
