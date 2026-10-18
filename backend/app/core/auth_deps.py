"""阶段 7 / 7.0.4 + 7.0.8 —— 认证依赖注入与 Cookie。

放在 backend/app 而不是 src/farm，是因为这里全是 FastAPI 概念（Request / Depends /
ApiError）。src/farm/auth.py 负责密码学与数据，这一层负责「从 HTTP 请求里把身份取出来」。

三个导出：
    db_conn()                  —— 每个请求一个 sqlite 连接（用完自动提交/回滚）
    get_current_user()         —— 必须登录，否则 401
    get_current_user_optional()—— 可选登录（用于顶部导航显示用户名 / 登录状态）
    require_admin()            —— 仅 id=1（7.0.6）
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Request
from fastapi.responses import Response
from urllib.parse import urlparse

from src.common.logging_utils import setup_logger
from src.farm import auth as farm_auth
from src.farm import users
from src.farm.db import connect
from src.farm.repo import NotFound
from backend.app.core.errors import ApiError

logger = setup_logger("agri.api.auth")

COOKIE_NAME = "session_token"
# 与 auth.SESSION_DAYS 保持一致：Cookie 的存活时间比会话略长一点没关系
# （真正的过期判定在 sessions.expires_at），但比会话短就会出现「库里还有效、
# 浏览器却已经不发了」的怪状态。
COOKIE_MAX_AGE = farm_auth.SESSION_DAYS * 24 * 3600


def db_conn() -> Iterator:
    """请求级 sqlite 连接。

    yield 出去之后 FastAPI 会在响应结束后回到这里，connect() 的上下文管理器负责
    提交（正常）或回滚（异常）并关闭。因此路由里**不需要**手动 commit。
    """
    with connect() as conn:
        yield conn


def _token_from(request: Request) -> str:
    return request.cookies.get(COOKIE_NAME, "") or ""


def get_current_user_optional(
    request: Request, conn=Depends(db_conn)
) -> dict | None:
    """有有效会话返回用户 dict（不含哈希/盐），否则 None。

    顺手做两件事：把 user_id 注入 request.state（7.0.4 的要求），以及续期。
    续期只在剩余不足一半时真写库（见 auth.touch_session），所以每个请求调它不心疼。
    """
    token = _token_from(request)
    if not token:
        return None

    user_id = farm_auth.get_session_user_id(conn, token)
    if user_id is None:
        return None

    request.state.user_id = user_id
    farm_auth.touch_session(conn, token)
    try:
        return users.public(users.get_user(conn, user_id))
    except NotFound:
        # 会话还在、用户却没了（被注销）：按未登录处理，并把这个野会话清掉
        farm_auth.delete_session(conn, token)
        return None


def get_current_user(user: dict | None = Depends(get_current_user_optional)) -> dict:
    """必须登录。未登录 / 会话过期统一 401。"""
    if user is None:
        raise ApiError("请先登录后再访问", code=401, http_status=401)
    return user


def current_user_id(user: dict = Depends(get_current_user)) -> int:
    """路由里更常用的形状：直接拿到 int 型的 user_id。

    单独给一个依赖而不是让每个路由写 `user["id"]`：`repo` 的每个函数第一个参数都是
    user_id，路由里到处都是 `user["id"]` 时，有一次手滑写成别的字段就会变成
    「按 id=None 查询」——那会安静地返回空列表，而不是报错。
    """
    return int(user["id"])


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """仅 id=1（7.0.6），且排除预置的演示账号。其他人 404 —— 不是 403：不暴露「这个接口存在且你需要权限」。

    排除演示账号的原因：全新部署时数据库为空，演示账号会是第一条记录（id=1）。
    只按 id 判定的话，一个密码公开的账号就拿到了管理员权限。
    """
    if not users.is_admin(user["id"]) or users.is_demo_account(user):
        raise ApiError("未找到该接口", code=404, http_status=404)
    return user


def check_origin(request: Request) -> None:
    """关键写操作的 Origin 校验（7.0.8：防跨站请求伪造）。

    规则：**带 Origin 且与本站不同源就拒绝**；不带 Origin 则放行。
    为什么允许「不带」：命令行 / 冒烟测试（urllib）不发 Origin，浏览器对同源
    表单提交在部分情况下也不发。把它设成强制，等于要求所有调用方都伪造一个
    Origin 头，反而把这道防线变成形式主义。真正兜底的是 SameSite=Lax 的 Cookie ——
    跨站请求根本带不上 session_token，Origin 只是第二道。
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return

    try:
        host = urlparse(origin).netloc
    except Exception:  # noqa: BLE001
        raise ApiError("请求来源不合法", code=403, http_status=403)

    request_host = request.headers.get("host", "")
    if host and request_host and host.lower() != request_host.lower():
        logger.warning("拒绝跨站写操作：Origin=%s Host=%s", host, request_host)
        raise ApiError("请求来源不合法", code=403, http_status=403)


def set_session_cookie(response: Response, token: str) -> None:
    """写会话 Cookie。

    HttpOnly —— 前端 JS 读不到它，XSS 也就偷不走（7.0.8）。
    SameSite=Lax —— 跨站请求不会带上它，挡住绝大多数 CSRF；同时不影响
    从别处点链接跳回来（Lax 允许顶级导航的 GET）。
    secure=False —— 本地开发是 http://127.0.0.1，置 True 会导致 Cookie 根本发不出去。
    部署到 https 时应由环境变量打开，这里留了明确的注记而不是假装安全。
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")


def client_ip(request: Request) -> str:
    """取客户端 IP 用于限流。

    只在没有反向代理时可信 —— X-Forwarded-For 是客户端可以随便写的头。
    这里**不读** XFF：读了等于让攻击者每次请求带一个新的伪造 IP，把 IP 限流变成摆设。
    若将来真的放在 nginx 后面，应改成「只信任最后一跳代理写入的值」并写进部署说明。
    """
    return request.client.host if request.client else ""
