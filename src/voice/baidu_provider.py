"""任务 3.1 —— 封装百度智能云多方言语音接口：语音识别（ASR）与语音合成（TTS）。

接口依据（均为官方 vop/tsn 接口，非自造）：
    · 鉴权：POST https://aip.baidubce.com/oauth/2.0/token
            grant_type=client_credentials&client_id=<API_KEY>&client_secret=<SECRET_KEY>
            -> {"access_token": "...", "expires_in": 2592000}
    · 短语音识别：POST https://vop.baidu.com/server_api
            JSON: {format, rate, channel=1, cuid, token, dev_pid, speech(base64), len}
            采样率只支持 16000 / 8000；格式只支持 pcm/wav/amr（m4a 仅极速版）
            返回: {"err_no": 0, "err_msg": "success.", "result": ["识别文本"]}
    · 短文本在线合成：POST https://tsn.baidu.com/text2audio
            {tex(需两次 URL 编码), tok, cuid, ctp=1, lan=zh, spd, pit, vol, per, aue=3}
            成功返回音频二进制，失败返回 JSON（含 err_no）
    两个接口的常量取自百度官方文档；方言的 dev_pid 与发音人 per 见 configs/dialect.yaml。

音频格式的现实约束：
    浏览器 MediaRecorder 默认产出 webm/opus，百度 REST 接口不接受该格式。
    因此前端「麦克风录制」改为用 Web Audio API 采集 16kHz 单声道 PCM 并自封 WAV 上传
    （见 frontend/assets/js/diagnose.js），后端这里也做一次格式校验并给出明确报错，
    而不是把 webm 直接丢给百度换回一个看不懂的错误码。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.dialect.config import asr_params, tts_params
from src.voice.base import ASRResult, BaseVoice, TTSResult
from src.voice.lexicon import find_terms

logger = setup_logger("agri.voice.baidu")

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
ASR_URL = "https://vop.baidu.com/server_api"
TTS_URL = "https://tsn.baidu.com/text2audio"

# 百度短语音识别 REST 支持的容器格式（m4a 仅极速版支持，这里不列入）
SUPPORTED_FORMATS = {"wav", "pcm", "amr"}
# 各格式的魔数，用于在发给百度之前先判一次，给出人话报错
_MAGIC = {
    b"RIFF": "wav",
    b"#!AMR": "amr",
    b"\x1a\x45\xdf\xa3": "webm",     # 常见但百度不支持
    b"OggS": "ogg",                   # 常见但百度不支持
    b"ID3": "mp3",                    # 不支持
    b"\xff\xfb": "mp3",
}


class BaiduVoice(BaseVoice):
    """百度智能云语音服务（ASR + TTS）。"""

    provider = "baidu"

    def __init__(self, *, api_key: str | None = None, secret_key: str | None = None) -> None:
        self.api_key = api_key or env("BAIDU_API_KEY")
        self.secret_key = secret_key or env("BAIDU_SECRET_KEY")
        if not self.api_key or not self.secret_key:
            raise ValueError("缺少 BAIDU_API_KEY / BAIDU_SECRET_KEY，请写入 .env")

        self.cuid = str(env("BAIDU_CUID", "rural-agri-platform"))
        self.lm_id = env("BAIDU_LM_ID", None, cast="int")
        self.spd = int(env("BAIDU_TTS_SPD", 5, cast="int") or 5)
        self.vol = int(env("BAIDU_TTS_VOL", 5, cast="int") or 5)
        self.pit = int(env("BAIDU_TTS_PIT", 5, cast="int") or 5)

        self._token: str | None = None
        self._token_expire_at: float = 0.0

    # ------------------------------------------------------------ 鉴权

    def _get_token(self) -> str:
        import requests

        if self._token and time.time() < self._token_expire_at - 60:
            return self._token

        r = requests.post(
            TOKEN_URL,
            params={
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.secret_key,
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if "access_token" not in data:
            raise RuntimeError(
                f"获取百度 access_token 失败：{data.get('error')} / {data.get('error_description')}"
            )

        self._token = data["access_token"]
        self._token_expire_at = time.time() + float(data.get("expires_in", 2592000))
        logger.info("已获取百度 access_token（有效期 %s 秒）", data.get("expires_in"))
        return self._token

    # ------------------------------------------------------------ ASR

    def transcribe(self, audio: bytes, *, dialect: str | None = None,
                   audio_format: str = "auto") -> ASRResult:
        import requests

        t0 = time.perf_counter()
        fmt = self._resolve_format(audio, audio_format)
        params = asr_params(dialect)

        payload = {
            "format": fmt,
            "rate": int(env("BAIDU_ASR_RATE", 16000, cast="int") or 16000),
            "channel": 1,
            "cuid": self.cuid,
            "token": self._get_token(),
            "dev_pid": params["dev_pid"],
            "speech": base64.b64encode(audio).decode("ascii"),
            "len": len(audio),
        }
        if self.lm_id and params["dev_pid"] == 1537:
            payload["lm_id"] = self.lm_id     # 自训练平台模型仅对 dev_pid=1537 生效

        r = requests.post(
            ASR_URL,
            params={"cuid": self.cuid, "token": payload["token"]},
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        if data.get("err_no") != 0:
            raise RuntimeError(f"百度语音识别失败：err_no={data.get('err_no')} {data.get('err_msg')}")

        raw_text = "".join(data.get("result") or []).strip()
        matched = find_terms(raw_text)
        if matched:
            logger.info("从识别文本中抽取到农业术语：%s", matched)

        return ASRResult({
            "text": raw_text,
            "matched_terms": matched,
            "dialect": params["dialect"],
            "dialect_name": params["dialect_name"],
            "provider": self.provider,
            "dev_pid": params["dev_pid"],
            "asr_supported": params["asr_supported"],
            "fallback": params["asr_fallback"],
            "fallback_note": params["fallback_note"],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "mock": False,
        })

    @staticmethod
    def _resolve_format(audio: bytes, audio_format: str) -> str:
        if audio_format != "auto":
            fmt = audio_format.lower().lstrip(".")
        else:
            fmt = ""
            for magic, name in _MAGIC.items():
                if audio.startswith(magic):
                    fmt = name
                    break
            if not fmt:
                # 无头部信息的裸 PCM 流
                fmt = "pcm"

        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"百度短语音识别不支持 {fmt} 格式的音频（支持 wav/pcm/amr）。"
                "浏览器录音请使用 16kHz 单声道 PCM/WAV 上传；"
                "若是 webm/opus 录音，请先转码："
                "ffmpeg -i in.webm -ar 16000 -ac 1 -f wav out.wav"
            )
        return fmt

    # ------------------------------------------------------------ TTS

    def synthesize(self, text: str, *, dialect: str | None = None) -> TTSResult:
        """合成语音。大模型音库音色若未开通，自动降级到基础音库发音人重试一次。"""
        import requests

        t0 = time.perf_counter()
        text = (text or "").strip()
        if not text:
            raise ValueError("待合成文本为空")
        if len(text) > 480:
            text = text[:480]                    # 短文本在线合成单次上限约 512 字节（GBK）

        params = tts_params(dialect)
        # 先试方言音色，失败（多为音色未开通权限）再退到基础音库音色重试一次
        candidates = [params["per"]]
        if params["per_fallback"] != params["per"]:
            candidates.append(params["per_fallback"])

        last_err: dict | None = None
        for per in candidates:
            r = requests.post(
                TTS_URL,
                params={
                    # 官方要求 tex 做两次 URL 编码
                    "tex": self._double_encode(text),
                    "tok": self._get_token(),
                    "cuid": self.cuid,
                    "ctp": 1,
                    "lan": "zh",
                    "spd": self.spd,
                    "pit": self.pit,
                    "vol": self.vol,
                    "per": per,
                    "aue": 3,                  # mp3
                },
                timeout=30,
            )
            r.raise_for_status()

            ctype = r.headers.get("Content-Type", "")
            if ctype.startswith("audio/"):
                return TTSResult({
                    "audio_bytes": r.content,
                    "audio_url": None,
                    "mime": ctype.split(";")[0],
                    "dialect": params["dialect"],
                    "dialect_name": params["dialect_name"],
                    "provider": self.provider,
                    "voice_name": params["voice_name"] if per == params["per"] else "度小美-标准女主播",
                    "voice_library": params["voice_library"] if per == params["per"] else "基础音库",
                    "used_fallback_voice": per != params["per"],
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                    "mock": False,
                    "message": "",
                })

            # 返回 JSON = 出错（常见是音色未开通权限）
            err = r.json() if ctype.startswith("application/json") else {"err_msg": r.text[:200]}
            logger.warning("百度语音合成失败（per=%s）：%s", per, err)
            last_err = err if isinstance(err, dict) else {"err_msg": str(err)}

        raise RuntimeError(
            f"百度语音合成失败（已尝试发音人 {candidates}）："
            f"{(last_err or {}).get('err_msg') or last_err}"
        )

    @staticmethod
    def _double_encode(text: str) -> str:
        from urllib.parse import quote

        return quote(quote(text, safe=""), safe="")

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "asr_endpoint": ASR_URL,
            "tts_endpoint": TTS_URL,
            "supported_formats": sorted(SUPPORTED_FORMATS),
            "custom_lexicon": bool(self.lm_id),
        }
