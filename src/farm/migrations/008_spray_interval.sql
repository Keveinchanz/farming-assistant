-- 用药台账（方案 A + B）
--
-- 两列都加在农事记录上，而不是另起一张「用药表」：
-- 打药本来就是一条农事记录（logs.type = 'spray'），把它拆到另一张表会立刻出现
-- 「历史卡片要看两张表」「按时间排要 union」这些麻烦，而它并不需要独立的生命周期。
--
-- reg_no         登记证号（PD20110955 这种）。空串 = 没从登记库里选，是手填的药名。
-- interval_days  安全间隔期天数。**允许为 NULL**，因为公开的农药登记数据里没有这一项
--                （它只印在瓶身标签上）。NULL 和 0 是两件事：NULL 是「没记录」，
--                0 是「标签上写着 0 天」。台账里绝不能把「没记录」当成「打了就能吃」。
ALTER TABLE logs ADD COLUMN reg_no TEXT NOT NULL DEFAULT '';
ALTER TABLE logs ADD COLUMN interval_days INTEGER;

-- 台账要按「某块田最近一次打药」取数，这个组合索引覆盖它。
CREATE INDEX IF NOT EXISTS idx_logs_spray ON logs (user_id, field_id, type, date);
