"""诊断报告分享快照 —— 农技员把结论交给农户的那一步。

一页纸问诊单有三种交付方式，共用同一份数据（前端 `_sheetPayload()` 组装）：

    · 打印 / 存 PDF —— 在地头撕给农户；
    · 复制成微信消息 —— 粘进聊天框；
    · **扫码看**（本模块）—— 村里贴一张二维码，或者微信里发一条短链。

为什么扫码这一路要单独做：前两条都要求「农技员在场」。可农技员走后农户还会
翻记录、会被邻居问「你家那块地咋说的」，一张纸会丢、微信消息会淹没在群里，
而链接一直都在。所以快照要能被一个**没登录的**手机打开。

三条硬约定：

    1. **存快照，不存 diagnosis_id** —— 公开链接不能成为读用户库的钥匙（见 007 迁移）。
    2. **不放原图** —— 二维码背后是村里的 4G，农户要看的是结论。
    3. **必须过期** —— 默认 30 天。贴出去的码不会自己消失，链接不能永远有效。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from src.farm.timeutil import parse_utc, utc_now, utc_now_iso

# 分享链接有效期（天）。30 天：够农户把一茬药打完、也够他拿给邻居看，
# 又不至于让一条去年夏天的链接明年还能打开。
SHARE_DAYS = 30

# 快照 JSON 的字节上限。一张问诊单的正文撑死十几 KB，给到 48 KB
# 是为了容纳长段处置建议，同时挡住「把整个 report 对象塞进来」这种用法。
MAX_PAYLOAD_BYTES = 48 * 1024


class ShareError(ValueError):
    """分享内容不合法（太长 / 不是对象）。路由层据此返回 400。"""


def _new_token() -> str:
    """12 个字符的 URL 安全随机串，约 72 bit —— 猜不出来，也不至于长到二维码变密。"""
    return secrets.token_urlsafe(9)


def purge_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM diag_shares WHERE expires_at <= ?", (utc_now_iso(),))
    return cur.rowcount


def create_share(
    conn: sqlite3.Connection,
    user_id: int,
    payload: dict[str, Any],
    *,
    days: int = SHARE_DAYS,
) -> dict:
    """把一份快照存下来，返回 {token, created_at, expires_at, days}。

    过期时间由**服务端**算，不接受前端传 —— 客户端能决定链接活多久，
    这个能力没有存在的理由。
    """
    if not isinstance(payload, dict) or not payload:
        raise ShareError("分享内容不能为空")

    text = json.dumps(payload, ensure_ascii=False)
    size = len(text.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise ShareError(f"分享内容过大（{size} 字节，上限 {MAX_PAYLOAD_BYTES}）")

    purge_expired(conn)
    token = _new_token()
    created = utc_now().replace(microsecond=0)
    expires = (created + timedelta(days=int(days))).replace(microsecond=0)
    conn.execute(
        "INSERT INTO diag_shares (token, user_id, payload, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (token, user_id, text, created.isoformat().replace("+00:00", "Z"),
         expires.isoformat().replace("+00:00", "Z")),
    )
    return {
        "token": token,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "days": int(days),
    }


def get_share(conn: sqlite3.Connection, token: str) -> dict | None:
    """按 token 取快照。过期即视为不存在（顺手删掉，免得表越攒越大）。"""
    if not token or len(token) > 64:
        return None
    row = conn.execute(
        "SELECT token, payload, created_at, expires_at FROM diag_shares WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if parse_utc(row["expires_at"]) <= utc_now():
        conn.execute("DELETE FROM diag_shares WHERE token = ?", (token,))
        return None
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return None
    return {
        "token": row["token"],
        "payload": payload,
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }
