"""大模型工厂 —— 按 .env 决定用 DeepSeek 还是规则化 Mock。

LLM_PROVIDER 取值：
    auto   （默认）配了 DEEPSEEK_API_KEY 就用 DeepSeek，否则用 Mock
    deepseek  强制真实调用（Key 缺失直接报错）
    mock      强制 Mock
"""

from __future__ import annotations

from src.common.config import env, env_mode
from src.common.logging_utils import setup_logger
from src.llm.base import BaseLLM

logger = setup_logger("agri.llm")

_cached: BaseLLM | None = None
_cached_mode: str | None = None


def get_llm(*, refresh: bool = False) -> BaseLLM:
    global _cached, _cached_mode

    has_key = bool(env("DEEPSEEK_API_KEY"))
    mode = env_mode("LLM_PROVIDER", auto_when=has_key, real="deepseek")
    if _cached is not None and _cached_mode == mode and not refresh:
        return _cached

    if mode == "deepseek":
        from src.llm.deepseek_provider import DeepSeekProvider

        try:
            llm: BaseLLM = DeepSeekProvider()
            logger.info("大模型能力：DeepSeek（真实调用）")
        except Exception as e:  # noqa: BLE001
            logger.error("初始化 DeepSeek 失败，降级为 Mock：%s", e)
            from src.llm.mock_provider import MockLLM

            llm = MockLLM()
            mode = "mock"
    else:
        from src.llm.mock_provider import MockLLM

        llm = MockLLM()
        logger.warning("大模型能力：Mock（%s），在 .env 填入 DEEPSEEK_API_KEY 即可切换为真实调用",
                       "未配置 Key" if not has_key else "被 LLM_PROVIDER 强制指定")

    _cached, _cached_mode = llm, mode
    return llm


def current_mode() -> str:
    return _cached_mode or env_mode(
        "LLM_PROVIDER", auto_when=bool(env("DEEPSEEK_API_KEY")), real="deepseek"
    )
