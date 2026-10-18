"""语音能力的抽象接口 —— 语音识别（ASR）与语音合成（TTS）。

真实实现与 Mock 实现必须返回完全相同的结构，这样上层（后端 4.3、前端「方言播报」）
在"有没有配 Key"两种情况下走的代码路径一致，接口契约不变。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ASRResult(dict):
    """语音识别结果。

    字段：
        text          识别出的文本（Mock 时给出明确说明，不伪造内容）
        dialect       使用的方言 key
        dialect_name  方言中文名
        provider      实际提供方（baidu / mock）
        fallback      是否因该方言无独立模型而回退到普通话模型
        fallback_note 回退说明
        elapsed_ms    耗时
        mock          是否走了 Mock
    """


class TTSResult(dict):
    """语音合成结果。

    字段：
        audio_url   可播放的音频地址（Mock 时为 None）
        audio_bytes 音频二进制（直接返回流时使用）
        dialect     方言 key
        provider    实际提供方
        voice_name  发音人名称
        mock        是否走了 Mock（前端据此改用浏览器 speechSynthesis 兜底）
        message     给用户看的说明
    """


class BaseASR(ABC):
    provider = "base"

    @abstractmethod
    def transcribe(self, audio: bytes, *, dialect: str | None = None,
                   audio_format: str = "wav") -> ASRResult:
        """把音频转成文本。"""


class BaseTTS(ABC):
    provider = "base"

    @abstractmethod
    def synthesize(self, text: str, *, dialect: str | None = None) -> TTSResult:
        """把文本合成语音。"""


class BaseVoice(ABC):
    """语音能力门面：同时提供 ASR 与 TTS。"""

    provider = "base"

    @abstractmethod
    def transcribe(self, audio: bytes, *, dialect: str | None = None,
                   audio_format: str = "wav") -> ASRResult: ...

    @abstractmethod
    def synthesize(self, text: str, *, dialect: str | None = None) -> TTSResult: ...

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider}
