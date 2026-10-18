-- 迁移 v2 —— 农田数据模型（任务清单 7.1）
--
-- 数据隔离（7.0.5）在**建表时**就落进结构里，而不是事后补：
--   · 除 climate_normals 外，每张 farm 表都带 user_id（清单明确把 climate_normals 排除在外 ——
--     它存的是「某省某市某月某日的历史平均气温与标准差」，是气候事实，不属于任何用户，
--     按用户各存一份只会让同一地点被反复拉取和重复存储）。
--   · 每张表都建 (user_id, ...) 复合索引：所有查询都带 WHERE user_id = ?，
--     没有这条索引的话，用户一多就是全表扫。
--   · ON DELETE CASCADE：注销账号时数据跟着走，不留孤儿行。
--
-- 若你手上已有 v1 建好、但 farm 表还没有 user_id 的旧库：往 src/farm/db.py 的
-- MIGRATIONS 末尾追加一条 `ALTER TABLE fields ADD COLUMN user_id INTEGER ...` 即可，
-- 迁移器按 PRAGMA user_version 顺序补，不需要手工改库。

CREATE TABLE IF NOT EXISTS fields (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id         TEXT    NOT NULL PRIMARY KEY,      -- fld_xxx，与知识库 lib_xxx 同一套风格
    name       TEXT    NOT NULL,
    crop       TEXT    NOT NULL,                  -- 只支持 水稻 / 玉米 / 油菜（见 7.17）
    area_mu    REAL    NOT NULL DEFAULT 0,
    province   TEXT    NOT NULL DEFAULT '',
    city       TEXT    NOT NULL DEFAULT '',
    lng        REAL,
    lat        REAL,
    sow_date   TEXT    NOT NULL,                  -- 'YYYY-MM-DD' 本地日历日
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fields_user ON fields(user_id, created_at);


CREATE TABLE IF NOT EXISTS logs (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id         TEXT    NOT NULL PRIMARY KEY,      -- log_xxx
    field_id   TEXT    NOT NULL,
    type       TEXT    NOT NULL,                  -- sow|fertilize|spray|irrigate|harvest|diagnose|other
    date       TEXT    NOT NULL,                  -- 'YYYY-MM-DD'
    item       TEXT    NOT NULL DEFAULT '',
    amount     TEXT    NOT NULL DEFAULT '',       -- 保留原样文本（「20 斤」「两袋」），不换算
    note       TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT 'manual', -- manual|voice|diagnose
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_user_field ON logs(user_id, field_id, date);


-- 逐日气温。PK 就是幂等的依据：同一天重复写走 ON CONFLICT DO UPDATE 覆盖，
-- 而不是插第二行 —— 「同一天的积温被算两遍」会让生育期整体提前，是个静默且难查的错。
CREATE TABLE IF NOT EXISTS daily_temp (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_id TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    tmean    REAL    NOT NULL,
    source   TEXT    NOT NULL,                    -- archive|forecast|normal（三态缺一不可）
    PRIMARY KEY (user_id, field_id, date)
);


-- 气候基准值：某省某市某月某日（'MM-DD'）的多年平均气温与标准差。
-- 无 user_id —— 见文件头说明。它是「预报不可用时的替代数据源」（7.2 降级规则第三条）。
CREATE TABLE IF NOT EXISTS climate_normals (
    province   TEXT NOT NULL,
    city       TEXT NOT NULL,
    date_md    TEXT NOT NULL,                     -- 'MM-DD'
    tmean_avg  REAL NOT NULL,
    tmean_std  REAL NOT NULL,
    PRIMARY KEY (province, city, date_md)
);


CREATE TABLE IF NOT EXISTS diagnoses (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id          TEXT    NOT NULL PRIMARY KEY,     -- dgn_xxx
    field_id    TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    image_path  TEXT    NOT NULL DEFAULT '',      -- 相对 data/farm_images/ 的路径
    label       TEXT    NOT NULL DEFAULT '',      -- 病害中文名（来自 label_map 闭集）
    confidence  REAL,
    pest_json   TEXT,                             -- 虫害检测结果；模型不可用时为 NULL（不编）
    report_json TEXT,                             -- 完整诊断报告
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnoses_user_field ON diagnoses(user_id, field_id, date);


CREATE TABLE IF NOT EXISTS growth_snapshots (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_id   TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    gdd        REAL    NOT NULL DEFAULT 0,
    stage      TEXT    NOT NULL DEFAULT '',
    height     REAL,
    leaf_count INTEGER,
    color_idx  REAL,
    source     TEXT    NOT NULL DEFAULT 'gdd',    -- gdd|measured
    PRIMARY KEY (user_id, field_id, date)
);


-- 收获记录：既是「我收了」的落库处，也是以后训练 XGBoost 的样本来源（7.3.4 的触发条件）。
-- features_json 存**数值特征**，训练脚本只提取它 —— 不带 user_id、田块名与位置（7.14 的隐私要求）。
CREATE TABLE IF NOT EXISTS harvest_records (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    id            TEXT    NOT NULL PRIMARY KEY,    -- har_xxx
    field_id      TEXT    NOT NULL,
    crop          TEXT    NOT NULL,
    sow_date      TEXT    NOT NULL,
    harvest_date  TEXT    NOT NULL,
    actual_days   INTEGER NOT NULL,
    gdd_at_harvest REAL,
    features_json TEXT,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_harvest_user  ON harvest_records(user_id, created_at);
-- 全平台统计「够不够 30 条」要走这条索引，不能按用户过滤
CREATE INDEX IF NOT EXISTS idx_harvest_crop  ON harvest_records(crop);
