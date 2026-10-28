-- 迁移 v6 —— 诊断记录补「服务对象」与「回访」两个字段（农技员问诊台的闭环）
--
-- 为什么加在 diagnoses 上而不是新开一张表：
--   「这条结论要给谁看、几天后回访」本来是**同一次问诊**的属性。单开一张
--   followups 表就要在落档时写两处、删档时级联两处，而它的所有字段都是
--   1:1 的。等以后真出现「一次诊断排多次回访」（比如隔 3 天、隔 10 天各一次）
--   再拆表，那时才有拆的理由。
--
-- subject 存的是**服务对象**（农户名/合作社名），不是账号。农技员用自己的账号
-- 登录，替十几个农户问诊；只记 field_id 的话，回访列表里全是「东头那块」，
-- 他分不清昨天服务的是王家还是李家。
--
-- recheck_date 为空串表示「这次没排回访」，不是 NULL —— 与项目里其它
-- 「可选但非空」的文本字段保持同一种写法，读的时候不用区分两种空值。
-- recheck_done 用 0/1 而不是时间戳：农技员只关心「还没回访」，不关心回访
-- 是在几点几分点的。

ALTER TABLE diagnoses ADD COLUMN subject      TEXT    NOT NULL DEFAULT '';
ALTER TABLE diagnoses ADD COLUMN recheck_date TEXT    NOT NULL DEFAULT '';
ALTER TABLE diagnoses ADD COLUMN recheck_done INTEGER NOT NULL DEFAULT 0;

-- 回访队列的查询是「我的、没回访的、日期 <= 今天」，
-- 三个条件都在这条索引里，用户多了也不会退化成全表扫。
CREATE INDEX IF NOT EXISTS idx_diagnoses_recheck
    ON diagnoses(user_id, recheck_done, recheck_date);
