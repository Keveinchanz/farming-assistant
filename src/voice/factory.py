"""语音能力工厂 —— 按 .env 决定用哪个真实实现还是 Mock。

VOICE_PROVIDER 取值：
    auto      （默认）优先讯飞（配了 IFLYTEK_* 三项）→ 退百度（配了 BAIDU_* 两项）→ Mock
    iflytek   强制科大讯飞方言识别大模型（缺 Key 直接报错，便于尽早暴露配置问题）
    baidu     强制百度智能云
    mock      强制 Mock

"拿到 Key 只改 .env" 能成立，就是因为这里是唯一的分支点。
"""

from __future__ import annotations

from src.common.config import env, env_mode
from src.common.logging_utils import setup_logger
from src.voice.base import BaseVoice

logger = setup_logger("agri.voice")

_cached: BaseVoice | None = None
_cached_mode: str | None = None

PROVIDERS = ("iflytek", "baidu", "mock")


def _has_iflytek_credentials() -> bool:
    return (
        bool(env("IFLYTEK_APPID"))
        and bool(env("IFLYTEK_API_KEY"))
        and bool(env("IFLYTEK_API_SECRET"))
    )


def _has_baidu_credentials() -> bool:
    return bool(env("BAIDU_API_KEY")) and bool(env("BAIDU_SECRET_KEY"))


def _auto_choice() -> str:
    """auto 模式下的选择顺序：讯飞优先（用户已确认以讯飞方言大模型为主）。"""
    if _has_iflytek_credentials():
        return "iflytek"
    if _has_baidu_credentials():
        return "baidu"
    return "mock"


def _missing_credentials_note() -> str:
    return (
        "未配置语音凭证：讯飞需 IFLYTEK_APPID / IFLYTEK_API_KEY / IFLYTEK_API_SECRET，"
        "百度需 BAIDU_API_KEY / BAIDU_SECRET_KEY"
    )


def get_voice(*, refresh: bool = False) -> BaseVoice:
    """取语音能力实例（进程内缓存，避免每次请求都重新鉴权）。"""
    global _cached, _cached_mode

    mode = env_mode("VOICE_PROVIDER", auto_when=True, real=_auto_choice())
    requested = (env("VOICE_PROVIDER", "auto") or "auto").strip().lower()

    # 显式写了 iflytek/baidu 但没配对应 Key 时，env_mode 会原样返回该值 ——
    # 这里补一次校验，否则会拿着空 Key 去构造 provider，报出的错很难看懂。
    downgraded = mode in ("iflytek", "baidu") and not _has_credentials_for(mode)
    if downgraded:
        logger.error(
            "VOICE_PROVIDER=%s 但缺少对应凭证，降级为 Mock。%s", mode, _missing_credentials_note()
        )
        mode = "mock"

    if _cached is not None and _cached_mode == mode and not refresh:
        return _cached

    if mode == "iflytek":
        voice = _build("iflytek")
    elif mode == "baidu":
        voice = _build("baidu")
    else:
        from src.voice.mock_provider import MockVoice

        voice = MockVoice()
        if downgraded:
            reason = "指定了真实服务但凭证不全（见上一条 error）"
        elif requested != "auto":
            reason = f"被 VOICE_PROVIDER={requested} 强制指定"
        else:
            reason = _missing_credentials_note()
        logger.warning("语音能力：Mock（%s）", reason)

    _cached, _cached_mode = voice, mode
    return voice


def _has_credentials_for(mode: str) -> bool:
    if mode == "iflytek":
        return _has_iflytek_credentials()
    if mode == "baidu":
        return _has_baidu_credentials()
    return True


def _build(mode: str) -> BaseVoice:
    """构造真实 provider；失败时降级为 Mock 并记 error（服务不因缺 Key 起不来）。"""
    display = "科大讯飞方言大模型" if mode == "iflytek" else "百度智能云"
    try:
        if mode == "iflytek":
            from src.voice.iflytek_provider import IflytekVoice

            voice: BaseVoice = IflytekVoice()
        else:
            from src.voice.baidu_provider import BaiduVoice

            voice = BaiduVoice()
        logger.info("语音能力：%s（真实调用）", display)
        return voice
    except Exception as e:  # noqa: BLE001 - 配置不全时降级而不是让整个服务起不来
        logger.error("初始化%s语音失败，降级为 Mock：%s", display, e)
        from src.voice.mock_provider import MockVoice

        return MockVoice()


def current_provider() -> str:
    """当前 provider 名（iflytek / baidu / mock）。

    给 /api/dialect/list 用：同一方言在讯飞与百度下 asr_supported 结论不同
    （讯飞 mulacc 覆盖八种方言，百度 REST 只有三种），列表必须按实际 provider 渲染。
    """
    return current_mode()


def current_mode() -> str:
    return _cached_mode or env_mode("VOICE_PROVIDER", auto_when=True, real=_auto_choice())
