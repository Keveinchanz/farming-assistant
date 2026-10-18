-- 迁移 v1 —— 账号系统（任务清单 7.0.1）
--
-- 这是本项目第一次出现「可变数据」：此前所有产物都是只读文件，谁来读都一样；
-- 账号与会话是第一个会被改写、且按用户划分的数据，因此单独建库（data/farm/farm.db）。
--
-- 两种时间字段的含义刻意分开，不要混：
--   · created_at / last_login_at / expires_at / at  —— **时刻**，存 ISO-8601 UTC（带 Z）。
--     会话是否过期是跨时区、跨夏令时的判断，用本地时间会算错。
--   · sow_date / date 等 —— **日历日期**，存 'YYYY-MM-DD'（Asia/Shanghai）。
--     它们是「哪一天」而不是「哪一刻」，积温按日累加，用时刻表示反而要反复做时区换算。

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL,
    email         TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,          -- 每用户独立 16 字节随机盐（十六进制）
    nickname      TEXT    NOT NULL DEFAULT '',
    avatar        TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'active',   -- active | disabled
    created_at    TEXT    NOT NULL,
    last_login_at TEXT
);

-- username 与 email 各自唯一（7.0.1 明确要求）。分开建两条索引而不是一条复合索引：
-- 复合唯一索引只保证「两者组合」不重复，允许两个用户同名不同邮箱 —— 那就不唯一了。
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email    ON users(email);


CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,           -- secrets.token_urlsafe(32)
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    user_agent TEXT    NOT NULL DEFAULT '',
    ip         TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
-- 过期清理按 expires_at 扫，单独一条索引；否则每次登录都要全表扫一遍会话
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);


CREATE TABLE IF NOT EXISTS user_settings (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    theme            TEXT    NOT NULL DEFAULT 'auto',
    language         TEXT    NOT NULL DEFAULT 'zh-CN',
    default_province TEXT    NOT NULL DEFAULT '',
    default_city     TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL
);


-- 限流表（7.0.2：同 IP 5 次/分钟、同账号 10 次/小时）
--
-- 7.0.1 列的是三张**业务表**，这张是限流所需的基础设施表，清单里没列。
-- 为什么必须有地方存：只放进程内字典的话，服务一重启限流就归零，
-- 而且「10 次/小时」这种窗口根本没法写测试。scope 区分两种口径：
--   scope='ip'      → key 是客户端 IP
--   scope='account' → key 是用户名（大小写归一后）
CREATE TABLE IF NOT EXISTS login_attempts (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT    NOT NULL,
    key   TEXT    NOT NULL,
    at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_lookup ON login_attempts(scope, key, at);
