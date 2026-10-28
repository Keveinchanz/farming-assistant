-- 迁移 v7 —— 诊断报告的只读分享快照（扫码 / 短链 / PDF 同一份数据）
--
-- 为什么存快照，而不是只存一个 diagnosis_id：
--   1. 分享页是**公开**的（农户扫码就能看，不需要账号）。只存 id 就意味着
--      一个 token 能顺着去读用户库里的东西；存快照则「分享出去的就是当时那一张纸」，
--      之后的删档、改档都不会牵动已经发给农户的链接。
--   2. 快照里不含原图：一张病叶照 2~4 MB，扫码的人多半在村里 4G 上，
--      而他要看的是结论和「先干什么」，不是那张照片。
--
-- payload 是前端组装好的 JSON（结构见 backend/app/schemas/models.py 的
-- SheetSnapshot）。服务端只做字段级校验与长度上限，不解析业务含义 ——
-- 它是一张纸，不是一个可查询的接口。
--
-- expires_at 是必须的：村口贴着的二维码会一直存在，链接不能一直有效。

CREATE TABLE IF NOT EXISTS diag_shares (
    token      TEXT    PRIMARY KEY,          -- secrets.token_urlsafe(9)，12 字符
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

-- 清理只按过期时间扫，索引就建在这一列上
CREATE INDEX IF NOT EXISTS idx_diag_shares_expires ON diag_shares(expires_at);
