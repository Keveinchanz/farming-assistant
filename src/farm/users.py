"""阶段 7 / 7.0 —— 用户与会话的账号层（在 auth.py 的密码学原语之上）。

分成两个文件是为了让职责清楚：
    auth.py  —— 密码哈希、token、限流窗口（不碰「用户」这个概念，只认字符串与连接）
    users.py —— 用户的增删改查、设置、登录记账

对外只暴露 dict，不把 sqlite3.Row 漏出去 —— 路由层要把它塞进 pydantic 模型，
拿 Row 会得到一堆查不到的类型提示。
"""

from __future__ import annotations

import sqlite3

from src.common.logging_utils import setup_logger
from src.farm.auth import (
    check_email,
    check_username,
    hash_password,
    new_salt,
    normalize_username,
    password_problem,
    verify_password,
)
from src.farm.repo import NotFound
from src.farm.timeutil import utc_now_iso

logger = setup_logger("agri.farm.users")

# 对外的用户字段白名单 —— **绝不包含 password_hash / salt**。
# 在这一层就切掉，而不是指望每个调用方记得 del。
PUBLIC_FIELDS = ("id", "username", "email", "nickname", "avatar", "status", "created_at", "last_login_at")


def public(user: dict | sqlite3.Row | None) -> dict | None:
    if user is None:
        return None
    d = dict(user)
    return {k: d.get(k) for k in PUBLIC_FIELDS}


class UsernameTaken(Exception):
    pass


class EmailTaken(Exception):
    pass


def get_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if row is None:
        raise NotFound(f"用户不存在：{user_id}")
    return dict(row)


def find_by_username(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (normalize_username(username),)
    ).fetchone()
    return dict(row) if row else None


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    email: str,
    password: str,
    nickname: str = "",
) -> dict:
    """注册。三条校验（用户名、邮箱、密码强度）在这里集中做一次，
    路由层只用 pydantic 兜「字段在不在」，业务规则不重复写两遍。

    **email 可以留空**：7.0.7 的注册页只要求「用户名 + 密码 + 确认密码」，
    没有邮箱这一项（7.17 也明确不做邮箱验证）。但 users 表上的 email 是
    NOT NULL + UNIQUE，所以空邮箱在这里补成一个**不会与真实邮箱冲突**的占位值：
    `{用户名}@agri.local`（`.local` 是保留域，永远不可能是真实地址）。
    这样表结构与清单一致，注册页也与清单一致；个人中心会把占位邮箱显示成「未填写」，
    而不是拿 @agri.local 去骗用户说他有邮箱。
    """
    name = normalize_username(username)
    addr = (email or "").strip().lower() or f"{name}@agri.local"

    problems = [check_username(username), password_problem(password)]
    # 只有用户真的填了邮箱才校验格式；占位值是我们自己生成的，不用校验
    if (email or "").strip():
        problems.append(check_email(email))
    for problem in problems:
        if problem:
            raise ValueError(problem)

    # 先查一次给出明确的中文提示；再靠唯一索引兜住并发（见下面的 IntegrityError）
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
        raise UsernameTaken(f"用户名「{username.strip()}」已被注册")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (addr,)).fetchone():
        raise EmailTaken(f"邮箱 {addr} 已被注册")

    salt = new_salt()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, salt, nickname, avatar, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, '', 'active', ?)",
            (name, addr, hash_password(password, salt), salt,
             (nickname or username).strip(), utc_now_iso()),
        )
    except sqlite3.IntegrityError as e:
        # 两个请求同时注册同一个名字时，唯一索引才是最终裁决者。
        # 这里把它翻译成人话，而不是让用户看到 "UNIQUE constraint failed: users.username"。
        raise UsernameTaken("用户名或邮箱已被注册") from e

    user = get_user(conn, int(cur.lastrowid))
    logger.info("新用户注册：id=%s username=%s", user["id"], user["username"])
    return user


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    """校验用户名密码。失败一律返回 None，**由调用方给统一的提示语**。

    为什么不让这里区分「用户不存在」和「密码错误」：那等于提供了一个账号枚举接口，
    攻击者可以拿一份用户名字典逐个试，从提示语里筛出哪些账号真实存在。
    7.12 有一条测试专门锁「登录错误密码统一提示」。
    """
    user = find_by_username(conn, username)
    if user is None:
        # 用户不存在时也照样做一次哈希运算：否则「不存在」会比「密码错」快 0.4 秒，
        # 响应时间本身就泄露了账号是否存在。
        hash_password(password or "", new_salt())
        return None
    if user["status"] != "active":
        return None
    if not verify_password(password or "", user["password_hash"], user["salt"]):
        return None
    return user


def touch_login(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utc_now_iso(), int(user_id)))


USER_PATCHABLE = ("nickname", "avatar")


def update_user(conn: sqlite3.Connection, user_id: int, patch: dict) -> dict:
    """改昵称 / 头像。用户名与邮箱不允许改 —— 7.17 没要求，且改了要重做唯一性校验，
    这次不做就不留半成品入口。"""
    updates = {k: v for k, v in (patch or {}).items() if k in USER_PATCHABLE and v is not None}
    if "nickname" in updates and not str(updates["nickname"]).strip():
        raise ValueError("昵称不能为空")
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), int(user_id)))
    return get_user(conn, user_id)


def change_password(conn: sqlite3.Connection, user_id: int, old_password: str, new_password: str) -> None:
    """已登录改密码（7.17：不做邮件找回，只做这个）。"""
    user = get_user(conn, user_id)
    if not verify_password(old_password or "", user["password_hash"], user["salt"]):
        raise ValueError("原密码不正确")
    if old_password == new_password:
        raise ValueError("新密码不能与原密码相同")
    problem = password_problem(new_password)
    if problem:
        raise ValueError(problem)

    salt = new_salt()
    conn.execute(
        "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
        (hash_password(new_password, salt), salt, int(user_id)),
    )
    logger.info("用户 %s 修改了密码", user_id)


def delete_user(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """注销账号（7.0.3 的 DELETE /api/auth/me）。要验密码 —— 一个误点的请求不该毁掉全部数据。

    外键的 ON DELETE CASCADE 负责把 sessions / fields / logs / diagnoses 等一并带走；
    但**磁盘上的图片不会自己消失**，那个由调用方处理（路由层删 data/farm_images/{user_id}/）。
    """
    user = get_user(conn, user_id)
    if not verify_password(password or "", user["password_hash"], user["salt"]):
        raise ValueError("密码不正确，无法注销账号")
    conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
    logger.info("用户已注销：%s", user["username"])


# ------------------------------------------------------------------ 用户设置

def get_settings(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (int(user_id),)).fetchone()
    if row is None:
        # 老账号（或手工建的账号）没有设置行时补一条默认的，而不是报错 ——
        # 「设置不存在」不该让个人中心打不开。
        conn.execute(
            "INSERT INTO user_settings (user_id, theme, language, default_province, "
            "default_city, created_at) VALUES (?, 'auto', 'zh-CN', '', '', ?)",
            (int(user_id), utc_now_iso()),
        )
        row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (int(user_id),)).fetchone()
    return dict(row) if row else {}


SETTINGS_PATCHABLE = ("theme", "language", "default_province", "default_city")


def update_settings(conn: sqlite3.Connection, user_id: int, patch: dict) -> dict:
    get_settings(conn, user_id)      # 确保有行
    updates = {k: v for k, v in (patch or {}).items() if k in SETTINGS_PATCHABLE and v is not None}
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE user_settings SET {sets} WHERE user_id = ?", (*updates.values(), int(user_id))
        )
    return get_settings(conn, user_id)


def is_admin(user_id: int) -> bool:
    """管理员仅 id=1（7.0.6 / 7.0.7）。没有角色表 —— 7.17 明确不做管理员以外的角色体系。"""
    return int(user_id) == 1
