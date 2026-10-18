-- 迁移 v3 —— 提醒「改天」的暂存（任务清单 7.7 的交互要求）
--
-- 7.7 的交互写着「[改天] snooze 2 天」，但 7.10 的接口清单里没有对应接口 ——
-- 清单自己漏了这一条。要让「这两天别再提醒我」在刷新后依然有效，就必须落库：
-- 只存在前端的 sessionStorage 里，用户换台设备或清一下缓存，提醒就又冒出来了。
--
-- 为什么不复用 logs 表记一条 type='other'：那张表是**农事历史**，
-- 「今天该干啥」里点了「改天」不是干过的农活，混进去会让历史卡片出现
-- 「10-09 改天」这种没有意义的条目，而历史卡片是要给人看实际做过什么的。
--
-- reminder_key 是提醒的稳定标识（由 type + action 拼成），
-- 主键 (user_id, field_id, reminder_key) 保证同一条提醒只存一行，重复点是覆盖。

CREATE TABLE IF NOT EXISTS reminder_snoozes (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_id      TEXT    NOT NULL,
    reminder_key  TEXT    NOT NULL,
    snooze_until  TEXT    NOT NULL,          -- 'YYYY-MM-DD'，这天之前不再提醒
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (user_id, field_id, reminder_key)
);

CREATE INDEX IF NOT EXISTS idx_snooze_lookup ON reminder_snoozes(user_id, field_id, snooze_until);
