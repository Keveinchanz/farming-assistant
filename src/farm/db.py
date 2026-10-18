"""阶段 7 —— 数据库连接与迁移（任务清单 7.0.1 / 7.0.10 / 7.1）。

为什么是 sqlite3（stdlib）而不是 SQLAlchemy：
    项目此前**没有任何数据库**，也没有 ORM 依赖。阶段 7 需要的是一张能被备份、
    能被删掉重建、能跟着项目一起拷走的小库 —— sqlite3 是标准库，零新依赖，
    单文件，正好。层级上也刻意保持单纯：src/farm 只管「表和 SQL」，
    FastAPI 的依赖注入与权限在 backend/app 里，两边不互相污染。

**迁移机制**（这就是 7.0.10 要的「迁移脚本」）：
    以 `PRAGMA user_version` 为版本号，MIGRATIONS 里的脚本按序号顺序执行，
    已经在库上的版本会跳过。因此：
        · 新库 → 从 v1 一路补到最新；
        · 旧库 → 只补缺的那几条；
        · 重复跑 scripts/23_init_farm_db.py 不会重复建表（幂等）。
    给已有库加字段（例如 7.0.10 的「farm 表补 user_id」）的做法是在 MIGRATIONS
    末尾追加一条脚本，内容是 `ALTER TABLE fields ADD COLUMN user_id INTEGER;`
    之类的语句；不要手工改库，否则别人的机器上版本号对不上。

连接策略：**每次调用开一个连接**（connect() 作上下文管理器），不共享全局连接。
    sqlite 打开一个本地文件是微秒级操作，WAL 模式下并发读写也不互相阻塞，
    在这个数据量下每次开连接完全划算；共享一个全局连接反而要自己加锁。

    为什么必须 check_same_thread=False：
        FastAPI 的同步依赖（backend/app/core/auth_deps.py 的 db_conn）跑在线程池里，
        而 `async def` 的路由函数跑在事件循环线程上 —— 同一次请求里，
        「建连接」和「用连接」**可能不在同一个线程**。sqlite3 默认禁止跨线程使用
        连接，于是会抛 "SQLite objects created in a thread can only be used in
        that same thread"（这个错真的在这里出现过，注册接口 500）。
        关掉这个检查是安全的：一个连接只属于一次请求，请求内部是**顺序**使用，
        从来不会被两个线程同时读写。跨请求之间连接不共享，所以并不存在并发访问。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.farm.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# (版本号, 脚本文件名)。版本号必须从 1 开始连续递增 —— 中间跳号会让旧库永远补不上。
# 追加迁移时**只许在末尾加**，已发布的脚本一个字都不能改（别人的库已经按旧内容执行过了）。
MIGRATIONS: list[tuple[int, str]] = [
    (1, "001_accounts.sql"),        # 7.0.1：users / sessions / user_settings / login_attempts
    (2, "002_farm.sql"),            # 7.1：fields / logs / daily_temp / climate_normals / …
    (3, "003_reminder_snooze.sql"), # 7.7 的「[改天] snooze 2 天」需要一个落库的地方
    (4, "004_daily_temp_detail.sql"),  # 7.3.4 的 11 个特征要用最高/最低气温与降水
    (5, "005_add_field_county.sql"),   # 田块选址补「县/区」一级（省市区联动 → 推经纬度）
]


def farm_db_path() -> Path:
    """库文件位置，走 configs/default.yaml 的 paths.farm_db（不写死路径）。"""
    return path_of("farm_db")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开连接；退出时提交并关闭。出异常则回滚。

    三条 PRAGMA 都是必须的：
        · foreign_keys=ON —— sqlite 默认**不**强制外键，不打开的话
          `ON DELETE CASCADE` 是一句装饰，注销账号会留下一堆孤儿行。
        · journal_mode=WAL —— 读写不互相阻塞（默认的 rollback journal 会让
          写操作锁住整个库）。
        · busy_timeout —— 万一两个写入撞上，等 5 秒再报错，而不是立刻抛
          database is locked。
    """
    target = path or farm_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def applied_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, *, verbose: bool = True) -> list[int]:
    """把库补到最新版本，返回本次实际执行的版本号列表（已是最新则返回空列表）。"""
    current = applied_version(conn)
    done: list[int] = []

    for version, filename in MIGRATIONS:
        if version <= current:
            continue
        sql_file = MIGRATIONS_DIR / filename
        if not sql_file.exists():
            raise FileNotFoundError(f"迁移脚本缺失：{sql_file}（版本 v{version}）")

        conn.executescript(sql_file.read_text(encoding="utf-8"))
        # PRAGMA 不支持参数占位符，只能拼字符串；version 来自本模块的常量表，
        # 不是外部输入，没有注入面（int() 再兜一道）。
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
        done.append(version)
        if verbose:
            logger.info("已执行迁移 v%d：%s", version, filename)

    if not done and verbose:
        logger.info("数据库已是最新版本（v%d），无需迁移", current)
    return done


def init_db(*, verbose: bool = True) -> Path:
    """建库（首次）+ 迁移到最新版本。幂等，可反复调用。返回库文件路径。"""
    path = farm_db_path()
    with connect(path) as conn:
        done = migrate(conn, verbose=verbose)
    if verbose and done:
        logger.info("数据库就绪：%s（v%s）", path, applied_version_of(path))
    return path


def applied_version_of(path: Path | None = None) -> int:
    """只读地取一下当前版本（不建库、不迁移）。库文件不存在时返回 0。"""
    target = path or farm_db_path()
    if not target.exists():
        return 0
    with connect(target) as conn:
        return applied_version(conn)
