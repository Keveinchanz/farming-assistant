"""问诊单交付接口（阶段 7 延伸）—— 只读分享页、二维码、PDF。

前两条路（打印、复制成微信消息）都在诊断页上完成，不需要服务端；这里补的是
**农技员走了之后农户还能看**的那条，以及**不依赖浏览器打印菜单**的 PDF。

四个接口：

    POST /api/diagnose/share        存一份快照，返回短链（需登录）
    GET  /api/share/{token}         公开只读：按 token 取快照
    GET  /api/share/{token}/qr.svg  公开只读：该链接的二维码（矢量，打印不糊）
    POST /api/diagnose/report.pdf   服务端生成 A4 一页纸（需登录）

两条公开接口必然被爬、被猜 —— 所以 token 是 72 bit 随机串、30 天过期、
响应 `Cache-Control: no-store`，且**只能读到那一份快照**，读不到用户的任何
其它数据（见 007 迁移的理由）。
"""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, Request, Response

from backend.app.core.auth_deps import check_origin, current_user_id, db_conn
from backend.app.core.errors import (
    CODE_BAD_REQUEST,
    CODE_INTERNAL,
    CODE_NOT_FOUND,
    ApiError,
)
from backend.app.schemas.models import ApiResponse
from backend.app.schemas.sheet import (
    ShareData,
    SharedSheetData,
    SheetShareRequest,
)
from src.common.logging_utils import setup_logger
from src.farm import shares

logger = setup_logger("agri.api.share")

router = APIRouter(prefix="/api", tags=["问诊单交付"])

# 分享页的路径。**与前端文件同名**：改 share.html 时这里要一起改。
SHARE_PATH = "/share.html?t={token}"


def _public_base(request: Request) -> str:
    """按当前请求的 Host 拼绝对地址。

    不能写死配置里的域名：演示走 Cloudflare 隧道、本地走 127.0.0.1:8000，
    写死任何一个，另一个环境下二维码就指向错的地方。
    """
    return str(request.base_url).rstrip("/")


@router.post("/diagnose/share", response_model=ApiResponse[ShareData],
             summary="生成问诊单分享链接（只读）",
             description=(
                 "把当前这一张问诊单存成快照，返回可直接发给农户的短链与二维码地址。\n\n"
                 "快照里不含原图，也没有用户的其它数据 —— 链接公开可读，"
                 "但它读到的只有这一张纸。有效期由服务端决定（默认 30 天）。"
             ))
async def create_share(
    body: SheetShareRequest, request: Request,
    conn=Depends(db_conn), user_id: int = Depends(current_user_id),
):
    check_origin(request)
    try:
        info = shares.create_share(conn, user_id, body.payload.model_dump())
    except shares.ShareError as e:
        raise ApiError(str(e), CODE_BAD_REQUEST, http_status=400) from e

    path = SHARE_PATH.format(token=info["token"])
    logger.info("用户 %s 生成分享链接 %s（%s 到期）", user_id, info["token"], info["expires_at"])
    return {
        "code": 0, "msg": "ok",
        "data": {
            "token": info["token"],
            "path": path,
            "url": _public_base(request) + path,
            "expires_at": info["expires_at"],
            "days": info["days"],
        },
    }


@router.get("/share/{token}", response_model=ApiResponse[SharedSheetData],
            summary="读取分享的问诊单（公开）",
            description="不需要登录。链接不存在或已过期统一返回 404 —— 不区分「没有」和「过期」，"
                        "免得被用来试探哪些 token 曾经存在过。")
async def read_share(token: str, response: Response, conn=Depends(db_conn)):
    row = shares.get_share(conn, token)
    if row is None:
        raise ApiError("链接不存在或已过期", CODE_NOT_FOUND, http_status=404)
    response.headers["Cache-Control"] = "no-store"
    return {
        "code": 0, "msg": "ok",
        "data": {
            "payload": row["payload"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        },
    }


@router.get("/share/{token}/qr.svg",
            summary="分享链接的二维码（公开，SVG）",
            description="矢量图：村里打印成 A5 贴纸、或者放大贴在公告栏都不会糊。")
async def share_qr(token: str, request: Request, conn=Depends(db_conn)):
    if shares.get_share(conn, token) is None:
        raise ApiError("链接不存在或已过期", CODE_NOT_FOUND, http_status=404)

    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as e:  # pragma: no cover - 依赖缺失时给一句能读懂的话
        raise ApiError("二维码组件未安装（qrcode）", CODE_INTERNAL, http_status=503) from e

    url = _public_base(request) + SHARE_PATH.format(token=token)
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/diagnose/report.pdf",
             summary="下载问诊单 PDF（A4 一页纸）",
             description=(
                 "服务端直接出 PDF，不经过浏览器的打印对话框 —— 手机微信里打印入口常常没有，"
                 "但「下载 → 发到群里」这条路一直通。\n\n"
                 "内容与问诊单一致：六段 + 签字栏 + 免责说明；**不含用药剂量**。"
             ))
async def report_pdf(body: SheetShareRequest, request: Request,
                     user_id: int = Depends(current_user_id)):
    check_origin(request)
    try:
        from src.export.sheet_pdf import build_sheet_pdf
    except ImportError as e:  # pragma: no cover
        raise ApiError("PDF 组件未安装（reportlab）", CODE_INTERNAL, http_status=503) from e

    snap = body.payload.model_dump()
    pdf = build_sheet_pdf(snap)
    name = f"sheet-{snap.get('date') or 'report'}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            # 文件名用 ASCII：中文文件名要 RFC 5987 编码，各家浏览器对 filename*
            # 的支持参差，而这张纸发出去之后文件名几乎没人看，不值得为它冒险。
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
