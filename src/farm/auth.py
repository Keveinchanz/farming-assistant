"""阶段 7 / 7.0 —— 密码、会话与登录限流。

这个模块是纯逻辑：拿到一个 sqlite 连接就干活，不引用 FastAPI，也不碰 Cookie。
把「怎么发 Cookie、怎么从请求里取 token」留给 backend/app/core/auth_deps.py ——
那样这里的每一条规则都能直接写单元测试，不需要起服务。

三件事的取舍都写在各自函数上方，这里先说一条总原则：
**密码相关的一切都用常数时间比较**（hmac.compare_digest），不要用 ==。
`==` 会在第一个不同的字节处提前返回，逐字节的耗时差异足以让攻击者一位一位地
猜出哈希或 token。这个项目是演示项目，但这条没有例外。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import timedelta

from src.farm.timeutil import parse_utc, utc_now, utc_now_iso

# 用户名只允许 ASCII（原因见 check_username 的注释）
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,24}$")

# 7.0.2 明确写的参数
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
MIN_PASSWORD_LEN = 8
SESSION_DAYS = 7

# 7.0.2：同 IP 5 次/分钟，同账号 10 次/小时。写成 (次数, 窗口秒数)
IP_LIMIT = (5, 60)
ACCOUNT_LIMIT = (10, 3600)


# ------------------------------------------------------------------ 密码

def new_salt() -> str:
    """每用户独立的 16 字节随机盐（十六进制）。"""
    return os.urandom(SALT_BYTES).hex()


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256，600000 轮。

    600k 是 7.0.2 指定的轮数。它让一次校验耗时约 0.3~0.5 秒 —— 这个「慢」
    就是目的：攻击者拿库里的哈希去爆破时，每次猜测都要付同样的代价。
    （代价是单元测试里每次注册/登录也要付这 0.4 秒，属于必要开销。）
    """
    raw = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return raw.hex()


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """常数时间比较。盐损坏（不是合法十六进制）时返回 False，不抛异常 ——
    否则一条坏数据能把登录接口打成 500，还不如让他登录失败。"""
    try:
        expected = hash_password(password, salt)
    except ValueError:
        return False
    return hmac.compare_digest(expected, password_hash or "")


def password_problem(password: str) -> str:
    """7.0.2 的强度规则。合规返回空串，不合规返回中文原因（调用方原样抛给前端）。

    刻意返回原因而不是布尔值：前端要把「密码太短」和「密码要同时含字母和数字」
    显示成不同的提示，只说一句「密码不合规」等于让用户自己猜。
    """
    if not password:
        return "密码不能为空"
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if not any(c.isalpha() for c in password):
        return "密码需要包含字母"
    if not any(c.isdigit() for c in password):
        return "密码需要包含数字"
    return ""


# ------------------------------------------------------------------ 会话

def new_token() -> str:
    """32 字节 URL 安全随机串，约 43 个字符。"""
    return secrets.token_urlsafe(32)


def create_session(
    conn: sqlite3.Connection, user_id: int, *, ip: str = "", user_agent: str = ""
) -> tuple[str, str]:
    """开一个会话，返回 (token, 过期时刻)。

    登录成功时**重新生成** token（7.0.8）—— 不复用旧的：会话固定攻击里，
    攻击者先塞给受害者一个自己知道的 token，等受害者登录后这个 token 就「升级」
    成了已登录会话。每次登录换新 token，那条路就断了。
    """
    purge_expired(conn)
    token = new_token()
    expires = (utc_now() + timedelta(days=SESSION_DAYS)).replace(microsecond=0).isoformat()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent, ip) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, user_id, utc_now_iso(), expires, (user_agent or "")[:300], ip or ""),
    )
    return token, expires


def get_session_user_id(conn: sqlite3.Connection, token: str) -> int | None:
    """有效会话返回 user_id；过期或不存在返回 None（并把过期行删掉）。"""
    if not token:
        return None

    row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None

    if parse_utc(row["expires_at"]) <= utc_now():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return int(row["user_id"])


def touch_session(conn: sqlite3.Connection, token: str) -> None:
    """每次访问续期（7.0.8：会话 7 天，每次访问续期）。

    只在剩余不足一半时续 —— 每个请求都写一次库毫无必要，而「用到一半突然掉登录」
    才是真问题：只要用户 7 天内有动作，会话就一直往后推。
    """
    row = conn.execute("SELECT expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
    if row is None:
        return
    expires = parse_utc(row["expires_at"])
    now = utc_now()
    if expires - now > timedelta(days=SESSION_DAYS) / 2:
        return
    new_expires = (now + timedelta(days=SESSION_DAYS)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?", (new_expires, token))


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    """登出：直接删记录（7.0.8）。删行而不是标记失效 —— 没有「已登出但还能用」的中间态。"""
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """改密码 / 注销时清掉该用户的全部会话。"""
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired(conn: sqlite3.Connection) -> int:
    """清掉过期会话，返回清除条数。每次开新会话时顺手调用。"""
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now_iso(),))
    return cur.rowcount or 0


# ------------------------------------------------------------------ 登录限流

class RateLimited(Exception):
    """超过限流阈值。route 层转成 429。"""

    def __init__(self, msg: str, retry_after: int) -> None:
        super().__init__(msg)
        self.msg = msg
        self.retry_after = retry_after


def _count(conn: sqlite3.Connection, scope: str, key: str, window: int) -> int:
    since = (utc_now() - timedelta(seconds=window)).replace(microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts WHERE scope = ? AND key = ? AND at > ?",
        (scope, key, since),
    ).fetchone()
    return int(row["n"])


def check_rate_limit(conn: sqlite3.Connection, *, ip: str, username: str) -> None:
    """登录前先查——超限直接拒绝，连密码都不验。

    两种口径分别检查，任一超限即拒（7.0.2）：
        · 同 IP    5 次/分钟  —— 挡住「一个来源猛刷很多账号」
        · 同账号   10 次/小时 —— 挡住「盯着一个人猛猜」
    """
    for scope, key, (limit, window), what in (
        ("ip", ip, IP_LIMIT, "本机"),
        ("account", normalize_username(username), ACCOUNT_LIMIT, "该账号"),
    ):
        if not key:
            continue
        n = _count(conn, scope, key, window)
        if n >= limit:
            raise RateLimited(
                f"登录尝试过于频繁（{what} {window // 60} 分钟内已失败 {n} 次），"
                f"请稍后再试",
                retry_after=window,
            )


def record_failure(conn: sqlite3.Connection, *, ip: str, username: str) -> None:
    """记一次失败。只在**失败**时记 —— 成功不占额度，否则正常用户连续登录几次就把自己锁了。"""
    now = utc_now_iso()
    rows = [("ip", ip, now)]
    account = normalize_username(username)
    if account:
        rows.append(("account", account, now))
    conn.executemany(
        "INSERT INTO login_attempts (scope, key, at) VALUES (?, ?, ?)",
        [r for r in rows if r[1]],
    )


def clear_account_failures(conn: sqlite3.Connection, username: str) -> None:
    """登录成功后清掉该账号的失败计数（IP 的留着 —— 它说的是「这个来源」而不是「这个人」，
    而且共享出口 IP（学校/公司/NAT）下清 IP 会让一个人把全楼的额度刷掉。"""
    conn.execute(
        "DELETE FROM login_attempts WHERE scope = 'account' AND key = ?",
        (normalize_username(username),),
    )


def purge_old_attempts(conn: sqlite3.Connection, *, keep_seconds: int = 86400) -> int:
    """清掉 24 小时前的失败记录（它们早已落在所有窗口之外）。"""
    since = (utc_now() - timedelta(seconds=keep_seconds)).replace(microsecond=0).isoformat()
    cur = conn.execute("DELETE FROM login_attempts WHERE at <= ?", (since,))
    return cur.rowcount or 0


# ------------------------------------------------------------------ 用户名校验

def normalize_username(username: str) -> str:
    """用户名归一：去首尾空白 + 转小写。

    「Zhang」和「zhang」必须是同一个账号，否则用户换个大小写就绕过唯一约束、
    也绕过了按账号限流。展示时用用户注册时写的原始大小写（存在 nickname/username 里）。
    """
    return (username or "").strip().lower()


def check_username(username: str) -> str:
    """用户名校验，合规返回空串，否则返回中文原因。

    刻意限定为**纯 ASCII** 的字母/数字/下划线。注意这里不能图省事写
    `c.isalnum()` —— Python 的 `str.isalnum()` 对汉字返回 True，
    「张三」会一路通过，而提示语写着「只能包含字母、数字与下划线」，等于骗人；
    更麻烦的是非 ASCII 用户名的同形异义字（如全角「ａ」与半角「a」）会让
    「看起来同一个名字」实际是两个账号。

    中文名请填**昵称**（nickname 是自由文本）—— 那个才是给别人看的名字，
    username 只是登录标识。
    """
    name = (username or "").strip()
    if not name:
        return "用户名不能为空"
    if not USERNAME_RE.match(name):
        return "用户名只能用英文字母、数字与下划线，长度 2~24（中文名请填在昵称里）"
    if not any(c.isalpha() for c in name):
        return "用户名需要包含英文字母"
    return ""


def check_email(email: str) -> str:
    """邮箱只做基本形状校验。

    7.17 明确「不做邮箱验证」，所以这里**不**发验证信、也不假装能确认邮箱真实存在 ——
    只挡掉明显不是邮箱的输入（没有 @、没有域名点、有空格）。
    """
    addr = (email or "").strip()
    if not addr:
        return "邮箱不能为空"
    if len(addr) > 120:
        return "邮箱过长"
    if " " in addr or addr.count("@") != 1:
        return "邮箱格式不正确"
    local, _, domain = addr.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return "邮箱格式不正确"
    return ""
