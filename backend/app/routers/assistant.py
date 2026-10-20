"""AI 助手（页面内常驻宠物）—— 自由对话 + 页面意图。

**为什么单独一个路由而不是复用 `/api/diagnose/ask`**：那个接口的产品语义是
「针对诊断结果追问」，system prompt 是诊断专家、要求严格 JSON，也没有历史与方言参数。
宠物是常驻入口，需要的是「随便聊两句 + 判断该去哪个页面」，两者不该共用一套提示词。

**线程模型**：这里用同步 `def`，FastAPI 会把它放进线程池执行。DeepSeek 与语音都是
同步 `requests` 调用，写成 `async def` 会阻塞事件循环 —— `/api/diagnose/ask` 就是这么写的，
不要再复制一遍。

**降级**：大模型不可用或超时时，回答回退到按意图准备的模板话术，接口始终 200。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.app import deps
from backend.app.core.auth_deps import get_current_user_optional
from backend.app.schemas.models import (
    ApiResponse,
    AssistantChatData,
    AssistantChatRequest,
    AssistantDegraded,
    AssistantIntent,
)
from src.assistant.intent import route
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api.assistant")

router = APIRouter(prefix="/api/assistant", tags=["AI 助手"])

# 大模型不可用时的兜底话术：每句都能独立成意，不假装已经替用户打开了页面
TEMPLATES: dict[str, str] = {
    "diagnose": "拍张照片，或者用文字说说叶子上的症状，我带你去诊断页看看。",
    "library": "知识图书库里能查病害和虫害的防治办法，我带你过去。",
    "policy": "惠农政策都在政策页，我带你过去。",
    "farm": "田块、积温和收获预测都在「我的农田」，我带你过去。",
    "home": "首页地图能看到各省当季的病虫害，我带你过去。",
    "chat": "我是小禾。你可以让我带你去诊断、图书库、惠农政策或者我的农田。",
}


def _fallback(intent: str) -> str:
    return TEMPLATES.get(intent) or TEMPLATES["chat"]


@router.post(
    "/chat",
    response_model=ApiResponse[AssistantChatData],
    summary="AI 助手对话",
    description=(
        "接收一句话，返回回答与页面意图。命中意图时 `intent.route` 是可以直接跳转的站内路径；\n"
        "未命中时 `intent.name` 为 `chat`。大模型不可用时 `degraded.llm` 为 true，回答回退为模板话术。"
    ),
)
def chat(
    body: AssistantChatRequest,
    _user: dict | None = Depends(get_current_user_optional),
):
    llm = deps.get_llm()

    # 规则优先；规则拿不准时才让大模型判断
    routed = route(body.message, llm=llm)
    intent_name = routed["intent"]

    answer = ""
    try:
        answer = llm.reply(body.message, context=body.page, history=body.history)
    except Exception as e:  # noqa: BLE001
        logger.warning("助手回答失败，回退模板：%s", e)

    llm_ok = bool(answer)
    if not llm_ok:
        answer = _fallback(intent_name)

    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "answer": answer,
            "intent": AssistantIntent(
                name=intent_name,
                confidence=float(routed.get("confidence") or 0.0),
                source=str(routed.get("source") or "default"),
                route=str(routed.get("route") or ""),
                slots=dict(routed.get("slots") or {}),
            ),
            "provider": str(getattr(llm, "provider", "")),
            "degraded": AssistantDegraded(
                llm=not llm_ok,
                intent=routed.get("source") == "default",
            ),
        },
    }
