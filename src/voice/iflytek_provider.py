"""任务 3.1 扩展 —— 科大讯飞「方言识别大模型」语音能力（ASR + TTS）。

为什么换讯飞：百度短语音识别 REST 只有普通话/粤语/四川话三个方言模型，
其余方言只能回退普通话（见 src/dialect/config.py 顶部说明）。讯飞的方言大模型
用**一个** accent="mulacc" 覆盖 202 种方言，八种方言全部免切换识别 —— 这正是换它的收益。

接口依据（讯飞开放平台 WebAPI，非自造）：
    · 鉴权：WebSocket 握手 URL 上拼 authorization / date / host 三个 query 参数。
            待签名串 = f"host: {host}\\ndate: {date}\\nGET {path} HTTP/1.1"，
            用 api_secret 做 HMAC-SHA256，标准 base64 得 signature，再拼成
            api_key="…", algorithm="hmac-sha256", headers="host date request-line",
            signature="…" 并整体 base64 成 authorization。
            date 为 RFC1123 GMT 格式，服务端允许 300 秒偏差。
            ⚠ api_secret 直接以 UTF-8 字节当 HMAC 密钥，**不能**先 base64 解码。
    · 语音识别（方言大模型）：wss://iat.cn-huabei-1.xf-yun.com/v1
            header{app_id, status} + parameter.iat{domain:"slm", language:"zh_cn",
            accent:"mulacc", eos, dwa:"wpgs", result{encoding,compress,format}}
            + payload.audio{encoding:"raw", sample_rate, channels, bit_depth, seq,
            status, audio(base64)}
            ⚠ result 归 parameter.iat（响应字段配置），**且方言大模型不接受缺省** ——
            漏了它会收到 code=10106，而那句报错里的 "output data" 极易误读成音频问题。
            详见 src/dialect/config.py 的 xfyun_asr_params()。
            响应 payload.result.text 是 **base64 → JSON** 两层：
            {"ws":[{"cw":[{"w":"字"}]}], "pgs":"apd"|"rpl", "rg":[起,止], "sn":序号}
    · 语音合成：wss://tts-api.xfyun.cn/v2/tts
            common{app_id} + business{aue:"lame", vcn, ...} + data{status:2, text(base64)}
            响应 data.audio 为 base64 的 mp3 分片，data.status==2 表示发完。

两个必须注意的坑，都在代码里落成了显式处理：
    1. encoding="raw" 要的是**裸 PCM**。浏览器上传的是自封 WAV，必须先剥掉容器头；
       而 WAV 里可能有 LIST/fact 等额外 chunk，真实 data 偏移常常不是 44 —— 见 pcm_from_wav()。
    2. dwa="wpgs" 打开后结果是**动态修正**分片：pgs=rpl 表示 [rg[0], rg[1]] 这几段被重识别、
       要整段替换。忽略它会把「玉米大斑病」拼成「玉米玉米大斑病」—— 见 merge_pgs()。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from email.utils import formatdate
from urllib.parse import urlencode, urlparse

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.dialect.config import xfyun_asr_params, xfyun_tts_params
from src.voice.base import ASRResult, BaseVoice, TTSResult
from src.voice.lexicon import find_terms

logger = setup_logger("agri.voice.iflytek")

ASR_URL = "wss://iat.cn-huabei-1.xf-yun.com/v1"
TTS_URL = "wss://tts-api.xfyun.cn/v2/tts"

# 官方示例的分帧大小：1280 字节 ≈ 40ms @ 16kHz / 16bit / 单声道
FRAME_BYTES = 1280
# 只把真正发给讯飞的业务参数挑出来；xfyun_asr_params() 里还混着给接口响应用的字段
ASR_WIRE_KEYS = ("domain", "language", "accent", "eos", "dwa", "result")

DEFAULT_TIMEOUT = 20.0
# 单次合成的文本上限。后端 TTSRequest.text 也是 480，两边保持一致。
TTS_MAX_CHARS = 480


# ------------------------------------------------------------------ 纯函数
# 下面三个函数不碰网络，可以单独做单元测试（见 tests/test_units.py）。


def auth_url(url: str, api_key: str, api_secret: str, *, date: str | None = None) -> str:
    """在 WebSocket URL 上拼好讯飞鉴权参数。date 可传入以便测试断言固定签名。"""
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    if date is None:
        date = formatdate(usegmt=True)

    origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    digest = hmac.new(
        api_secret.encode("utf-8"), origin.encode("utf-8"), hashlib.sha256
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")

    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("ascii")

    return f"{url}?{urlencode({'authorization': authorization, 'date': date, 'host': host})}"


def pcm_from_wav(audio: bytes) -> bytes:
    """取出裸 PCM。不是 WAV（已是裸 PCM）就原样返回。

    不能想当然地切掉前 44 字节：WAV 允许在 data 之前插入任意 chunk
    （LIST/fact/bext 等），那样会带进一堆头字节当音频送给引擎。
    这里按 RIFF chunk 逐个走，按字节偏移定位真正的 data 段。
    """
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio

    pos = 12
    while pos + 8 <= len(audio):
        chunk_id = audio[pos:pos + 4]
        size = int.from_bytes(audio[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"data":
            return audio[body:body + size] if size else audio[body:]
        pos = body + size + (size & 1)      # chunk 按偶数字节对齐，奇数长度要补 1

    raise ValueError("WAV 文件里找不到 data 段，无法取出 PCM 数据")


def asr_frames(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> list[tuple[int, int, bytes]]:
    """把 PCM 切成讯飞要的帧，返回 [(seq, status, 数据)]。

    status：0=首帧，1=中间帧，2=末帧。收尾用的是官方示例的写法 ——
    数据帧走完之后再补一个**空音频**的 status=2 帧，而不是把最后一个数据帧标成末帧。
    """
    chunks = [pcm[i:i + frame_bytes] for i in range(0, len(pcm), frame_bytes)] or [b""]
    frames = [(i + 1, 0 if i == 0 else 1, c) for i, c in enumerate(chunks)]
    frames.append((len(chunks) + 1, 2, b""))
    return frames


def merge_pgs(segments: dict[int, str], chunk: dict) -> str:
    """把动态修正（dwa=wpgs）的分片拼成完整文本，返回当前全文。

    pgs="apd"：第 sn 段是新增内容，直接放进去。
    pgs="rpl"：第 [rg[0], rg[1]] 段被重新识别，要先删掉旧段再写入新内容 ——
               漏掉这步就会得到「玉米玉米大斑病」这种重复文本。
    """
    piece = "".join(
        item.get("w", "")
        for group in chunk.get("ws") or []
        for item in group.get("cw") or []
    )

    if chunk.get("pgs") == "rpl" and chunk.get("rg"):
        start, end = int(chunk["rg"][0]), int(chunk["rg"][1])
        for i in range(start, end + 1):
            segments.pop(i, None)

    sn = chunk.get("sn")
    segments[int(sn) if sn is not None else (max(segments) + 1 if segments else 1)] = piece
    return "".join(segments[i] for i in sorted(segments))


def _connect(url: str, timeout: float):
    """同步 WebSocket 连接。

    刻意用 websockets 的**同步**客户端，而不是 asyncio.run()：
    fusion.diagnose() 是同步函数、被 async 路由直接调用，在运行中的事件循环里
    调 asyncio.run() 会抛 RuntimeError（cannot be called from a running event loop）。
    """
    from websockets.sync.client import connect

    # max_queue=None（不设上限）：我们是「先把音频帧全发完、再统一收结果」，
    # 期间引擎返回的动态修正分片没人取，默认的 16 条队列一满就会停止读 socket、
    # 触发 TCP 背压，服务端可能因此判定会话超时。这些分片本身很小，不设上限没有负担。
    return connect(
        url, open_timeout=timeout, close_timeout=5,
        max_size=8 * 1024 * 1024, max_queue=None,
    )


# ------------------------------------------------------------------ 实现


class IflytekVoice(BaseVoice):
    """科大讯飞方言识别大模型（ASR）+ 在线语音合成（TTS）。"""

    provider = "iflytek"

    def __init__(self, *, app_id: str | None = None, api_key: str | None = None,
                 api_secret: str | None = None) -> None:
        self.app_id = app_id or env("IFLYTEK_APPID")
        self.api_key = api_key or env("IFLYTEK_API_KEY")
        self.api_secret = api_secret or env("IFLYTEK_API_SECRET")
        if not (self.app_id and self.api_key and self.api_secret):
            raise ValueError(
                "缺少 IFLYTEK_APPID / IFLYTEK_API_KEY / IFLYTEK_API_SECRET，请写入 .env"
            )
        self.timeout = float(env("VOICE_TIMEOUT", DEFAULT_TIMEOUT, cast="float") or DEFAULT_TIMEOUT)
        # 发帧间隔（秒）。1280 字节 = 40ms 音频，默认按实时速率发。
        self.frame_interval = float(
            env("IFLYTEK_ASR_FRAME_INTERVAL", 0.04, cast="float")
        )

    # ------------------------------------------------------------ ASR

    def transcribe(self, audio: bytes, *, dialect: str | None = None,
                   audio_format: str = "auto") -> ASRResult:
        t0 = time.perf_counter()
        if not audio:
            raise ValueError("音频内容为空")

        pcm = pcm_from_wav(audio)
        if not pcm:
            raise ValueError("音频里没有 PCM 数据（只剩一个 WAV 头？）")

        params = xfyun_asr_params(dialect)
        iat = {k: params[k] for k in ASR_WIRE_KEYS}
        url = auth_url(ASR_URL, self.api_key, self.api_secret)

        segments: dict[int, str] = {}
        with _connect(url, self.timeout) as ws:
            for seq, status, chunk in asr_frames(pcm):
                ws.send(json.dumps({
                    "header": {"app_id": self.app_id, "status": status},
                    "parameter": {"iat": iat},
                    "payload": {"audio": {
                        "encoding": "raw",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bit_depth": 16,
                        "seq": seq,
                        "status": status,
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }},
                }))
                # 按官方示例的节奏发帧（1280 字节 / 40ms）。一把梭全推过去并不违规，
                # 但引擎对灌入速率有节流判定，按实时速率发最不容易踩到会话超时。
                if status != 2:
                    time.sleep(self.frame_interval)
            text = self._collect_asr(ws, segments)

        matched = find_terms(text)
        if matched:
            logger.info("从识别文本中抽取到农业术语：%s", matched)

        return ASRResult({
            "text": text,
            "matched_terms": matched,
            "dialect": params["dialect"],
            "dialect_name": params["dialect_name"],
            "provider": self.provider,
            # 讯飞没有 dev_pid 概念（一个 mulacc 覆盖全部方言），如实留空
            "dev_pid": None,
            "asr_supported": params["asr_supported"],
            "fallback": params["fallback"],
            "fallback_note": params["fallback_note"],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "mock": False,
        })

    def _collect_asr(self, ws, segments: dict[int, str]) -> str:
        """收结果帧直到 header.status == 2。"""
        while True:
            msg = self._recv_json(ws, "语音识别")
            header = msg.get("header") or {}
            code = header.get("code", 0)
            if code != 0:
                raise RuntimeError(
                    f"讯飞语音识别失败：code={code} {header.get('message') or ''}"
                    f"{self._hint(code)}"
                )

            result = (msg.get("payload") or {}).get("result") or {}
            if result.get("text"):
                try:
                    chunk = json.loads(base64.b64decode(result["text"]).decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as e:
                    raise RuntimeError(f"讯飞返回的识别结果无法解析：{e}") from e
                merge_pgs(segments, chunk)

            if header.get("status") == 2:
                break

        return "".join(segments[i] for i in sorted(segments)).strip()

    # ------------------------------------------------------------ TTS

    def synthesize(self, text: str, *, dialect: str | None = None) -> TTSResult:
        """合成语音。方言发音人失败（多为未在控制台开通）时自动降级到普通话发音人重试。"""
        t0 = time.perf_counter()
        text = (text or "").strip()
        if not text:
            raise ValueError("待合成文本为空")
        if len(text) > TTS_MAX_CHARS:
            text = text[:TTS_MAX_CHARS]

        params = xfyun_tts_params(dialect)
        candidates = [params["vcn"]]
        if params["vcn_fallback"] and params["vcn_fallback"] != params["vcn"]:
            candidates.append(params["vcn_fallback"])

        last_err: Exception | None = None
        for vcn in candidates:
            try:
                audio = self._tts_once(text, vcn)
            except Exception as e:  # noqa: BLE001 - 换下一个发音人再试
                logger.warning("讯飞语音合成失败（vcn=%s）：%s", vcn, e)
                last_err = e
                continue

            if not audio:
                last_err = RuntimeError("讯飞未返回任何音频分片")
                continue

            used_fallback = vcn != params["vcn"] or not params["tts_supported"]
            message = ""
            if used_fallback:
                message = (
                    f"讯飞暂无已核实的「{params['dialect_name']}」发音人，"
                    f"已改用普通话音色播报。{params['note']}"
                ).strip()
            return TTSResult({
                "audio_bytes": audio,
                "audio_url": None,
                "mime": "audio/mpeg",       # business.aue="lame" -> mp3
                "dialect": params["dialect"],
                "dialect_name": params["dialect_name"],
                "provider": self.provider,
                "voice_name": params["voice_name"] if vcn == params["vcn"] else "讯飞普通话发音人",
                "voice_library": params["voice_library"],
                "used_fallback_voice": used_fallback,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
                "mock": False,
                "message": message,
            })

        raise RuntimeError(
            f"讯飞语音合成失败（已尝试发音人 {candidates}）：{last_err}"
        )

    def _tts_once(self, text: str, vcn: str) -> bytes:
        url = auth_url(TTS_URL, self.api_key, self.api_secret)
        body = {
            "common": {"app_id": self.app_id},
            "business": {
                "aue": "lame",          # 返回 mp3 分片，浏览器 <audio> 直接可播
                "sfl": 1,               # 开启流式返回
                "tte": "UTF8",
                "vcn": vcn,
                "speed": int(env("IFLYTEK_TTS_SPEED", 50, cast="int") or 50),
                "volume": int(env("IFLYTEK_TTS_VOLUME", 50, cast="int") or 50),
                "pitch": int(env("IFLYTEK_TTS_PITCH", 50, cast="int") or 50),
            },
            "data": {"status": 2, "text": base64.b64encode(text.encode("utf-8")).decode("ascii")},
        }

        chunks: list[bytes] = []
        with _connect(url, self.timeout) as ws:
            ws.send(json.dumps(body))
            while True:
                msg = self._recv_json(ws, "语音合成")
                code = msg.get("code", 0)
                if code != 0:
                    raise RuntimeError(
                        f"讯飞语音合成失败：code={code} {msg.get('message') or ''}"
                        f"{self._hint(code)}"
                    )
                data = msg.get("data") or {}
                if data.get("audio"):
                    chunks.append(base64.b64decode(data["audio"]))
                if data.get("status") == 2:
                    break

        return b"".join(chunks)

    # ------------------------------------------------------------ 公共

    def _recv_json(self, ws, what: str) -> dict:
        try:
            raw = ws.recv(timeout=self.timeout)
        except TimeoutError as e:
            raise RuntimeError(
                f"讯飞{what}超时（{self.timeout:g} 秒内没有返回结果）"
            ) from e
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"讯飞{what}返回了非 JSON 消息：{str(raw)[:200]}") from e

    @staticmethod
    def _hint(code) -> str:
        """把最常见的错误码翻成人话，省得用户对着 code 查文档。"""
        try:
            code = int(code)
        except (TypeError, ValueError):
            return ""
        if code == 11200:
            return (
                "（功能未授权：请到讯飞开放平台控制台为当前应用开通「方言识别大模型」"
                "及所需发音人；若只是发音人没开通，把 configs/dialect.yaml 里该方言的 "
                "xfyun_vcn 改成实际已开通的发音人即可）"
            )
        if code == 10313:
            return "（app_id 与 api_key/api_secret 不匹配，请核对 .env 中三项是否来自同一个应用）"
        return ""

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "asr_endpoint": ASR_URL,
            "tts_endpoint": TTS_URL,
            "asr_accent": "mulacc（一个模型覆盖 202 种方言，无需按方言切换）",
            "supported_formats": ["wav", "pcm"],
            "timeout_seconds": self.timeout,
        }
