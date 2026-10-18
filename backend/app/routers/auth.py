"""阶段 7 / 7.0.3 + 7.0.7 + 7.0.8 —— 账号接口。

八个接口照清单逐条实现：
    POST   /api/auth/register          否
    POST   /api/auth/login             否
    POST   /api/auth/logout            是
    GET    /api/auth/status            否   ← 顶部导航用它决定显示「登录/注册」还是用户名
    GET    /api/auth/me                是
    PATCH  /api/auth/me                是
    POST   /api/auth/change_password   是
    DELETE /api/auth/me                是

两个刻意为之的地方：
  · **注册成功即登录**（下发 Cookie）。清单没明说，但注册完还要再登一次是纯粹的折磨；
    更重要的是注册流程末尾会预置三条示例田，用户理应立刻看到它们。
  · **登录失败的提示语统一**为「用户名或密码不正确」。区分「用户不存在」和「密码错误」
    等于免费送出一个账号枚举接口。7.12 有一条测试锁这句话。
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Request, Response

from backend.app.core.auth_deps import (
    check_origin,
    clear_session_cookie,
    client_ip,
    current_user_id,
    db_conn,
    get_current_user,
    get_current_user_optional,
    set_session_cookie,
)
from backend.app.core.errors import CODE_BAD_REQUEST, ApiError
from backend.app.schemas.farm import AuthStatusData, AuthUserData
from backend.app.schemas.models import ApiResponse
from pydantic import BaseModel, Field
from src.common.logging_utils import setup_logger
from src.common.paths import path_of
from src.farm import auth as farm_auth
from src.farm import seed, users
from src.farm.auth import RateLimited

logger = setup_logger("agri.api.auth")

router = APIRouter(prefix="/api/auth", tags=["账号"])


# ---------------------------------------------------------------- 请求体

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=24)
    # 邮箱可选：7.0.7 的注册页只有「用户名 + 密码 + 确认密码」，
    # 留空时后端补一个 @agri.local 占位（见 src/farm/users.py 的 create_user）
    email: str = ""
    password: str
    nickname: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateMeRequest(BaseModel):
    nickname: str | None = None
    avatar: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class DeleteMeRequest(BaseModel):
    password: str = Field(..., description="注销是不可逆操作，必须再验一次密码")


# ---------------------------------------------------------------- 内部工具

def _bad(msg: str) -> ApiError:
    return ApiError(msg, CODE_BAD_REQUEST, http_status=400)


def _cleanup_user_files(user_id: int) -> None:
    """删掉该用户的入档图片目录。

    数据库的外键级联管不到磁盘：diagnoses 行会随用户一起消失，但
    data/farm_images/{user_id}/ 里的 jpg 不会。注销时一并删掉，
    否则「注销」只删了索引、照片还在盘上。"""
    folder = path_of("farm_images") / str(user_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
        logger.info("已清理用户 %s 的入档图片目录", user_id)


# ---------------------------------------------------------------- 接口

@router.post("/register", response_model=ApiResponse[AuthUserData], summary="注册",
             description="注册成功即登录（下发 HttpOnly 会话 Cookie），并预置三条示例田。")
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    conn=Depends(db_conn),
):
    check_origin(request)
    try:
        user = users.create_user(
            conn, username=body.username, email=body.email,
            password=body.password, nickname=body.nickname,
        )
        # 预置示例田放在同一个事务里：注册失败不该留下半截状态
        seed.seed_demo_fields(conn, user["id"])
    except users.UsernameTaken as e:
        raise _bad(str(e))
    except users.EmailTaken as e:
        raise _bad(str(e))
    except ValueError as e:
        raise _bad(str(e))

    token, _ = farm_auth.create_session(
        conn, user["id"],
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    users.touch_login(conn, user["id"])
    set_session_cookie(response, token)
    logger.info("注册并登录：%s（示例田已预置）", user["username"])
    return {"code": 0, "msg": "ok", "data": users.public(users.get_user(conn, user["id"]))}


@router.post("/login", response_model=ApiResponse[AuthUserData], summary="登录")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    conn=Depends(db_conn),
):
    check_origin(request)
    ip = client_ip(request)

    # 先查限流：超限直接拒，连密码都不验（7.0.2）
    try:
        farm_auth.check_rate_limit(conn, ip=ip, username=body.username)
    except RateLimited as e:
        raise ApiError(e.msg, code=429, http_status=429)

    user = users.authenticate(conn, body.username, body.password)
    if user is None:
        farm_auth.record_failure(conn, ip=ip, username=body.username)
        # 统一提示语 —— 不区分「用户不存在」与「密码错误」（见模块说明）
        raise ApiError("用户名或密码不正确", CODE_BAD_REQUEST, http_status=400)

    farm_auth.clear_account_failures(conn, body.username)
    # 登录成功**重新生成** token（7.0.8：防会话固定）
    farm_auth.delete_user_sessions(conn, user["id"])
    token, _ = farm_auth.create_session(
        conn, user["id"], ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    users.touch_login(conn, user["id"])
    set_session_cookie(response, token)
    logger.info("登录成功：%s", user["username"])
    return {"code": 0, "msg": "ok", "data": users.public(user)}


@router.post("/logout", response_model=ApiResponse[dict], summary="退出登录")
async def logout(
    request: Request,
    response: Response,
    conn=Depends(db_conn),
    user: dict = Depends(get_current_user),
):
    token = request.cookies.get("session_token", "")
    if token:
        farm_auth.delete_session(conn, token)
    clear_session_cookie(response)
    logger.info("退出登录：%s", user["username"])
    return {"code": 0, "msg": "ok", "data": {"logged_out": True}}


@router.get("/status", response_model=ApiResponse[AuthStatusData], summary="当前登录状态",
            description="公开接口：未登录也返回 200，用 logged_in 表示状态。顶部导航调它。")
async def status(
    user: dict | None = Depends(get_current_user_optional),
):
    return {
        "code": 0, "msg": "ok",
        "data": {
            "logged_in": user is not None,
            "is_admin": bool(user and users.is_admin(user["id"])),
            "user": user,
        },
    }


@router.get("/me", response_model=ApiResponse[AuthUserData], summary="我的资料")
async def me(
    conn=Depends(db_conn),
    user_id: int = Depends(current_user_id),
):
    return {"code": 0, "msg": "ok", "data": users.public(users.get_user(conn, user_id))}


@router.patch("/me", response_model=ApiResponse[AuthUserData], summary="改昵称 / 头像")
async def update_me(
    body: UpdateMeRequest,
    request: Request,
    conn=Depends(db_conn),
    user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        user = users.update_user(conn, user_id, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise _bad(str(e))
    return {"code": 0, "msg": "ok", "data": users.public(user)}


@router.post("/change_password", response_model=ApiResponse[dict], summary="修改密码")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    conn=Depends(db_conn),
    user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        users.change_password(conn, user_id, body.old_password, body.new_password)
    except ValueError as e:
        raise _bad(str(e))

    # 改完密码把所有会话踢掉（包括当前这个），强制重新登录 ——
    # 密码泄露过的情况下，这一步才是真正的止损。顺手把浏览器手里的 Cookie 也清掉，
    # 否则它还会继续发一个已经失效的 token，每次请求都撞一次 401。
    farm_auth.delete_user_sessions(conn, user_id)
    clear_session_cookie(response)
    logger.info("用户 %s 修改密码，已清空其全部会话", user_id)
    return {"code": 0, "msg": "ok", "data": {"relogin_required": True}}


@router.delete("/me", response_model=ApiResponse[dict], summary="注销账号",
               description="不可逆：用户的田块、记录、诊断与外键关联数据全部删除，入档图片一并清理。")
async def delete_me(
    body: DeleteMeRequest,
    request: Request,
    response: Response,
    conn=Depends(db_conn),
    user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        users.delete_user(conn, user_id, body.password)
    except ValueError as e:
        raise _bad(str(e))

    _cleanup_user_files(user_id)
    clear_session_cookie(response)
    logger.info("账号已注销：user_id=%s", user_id)
    return {"code": 0, "msg": "ok", "data": {"deleted": True}}
