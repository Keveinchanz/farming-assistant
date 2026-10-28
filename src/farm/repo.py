"""阶段 7 / 7.0.5 + 7.1 —— 数据访问层（唯一写 SQL 的地方）。

**本模块只有一条铁律**：每个函数的第一个参数都是 `user_id`，每条 SELECT / UPDATE /
DELETE 都带 `WHERE user_id = ?`。跨用户取不到数据时统一抛 `NotFound`，
由路由层转成 **404**（7.0.5：跨用户访问返回 404，不暴露存在性 —— 返 403 等于告诉
攻击者「这条记录是存在的，只是不属于你」，那本身就是一个信息泄露）。

自己写 SQL 而不是上 ORM，代价是这条铁律要靠人守。因此测试里有一组用例专门盯它：
A 用户取 B 用户的田块、A 用户**删** B 用户的田块（并断言 B 的数据还在）。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any, Iterable

from src.common.logging_utils import setup_logger
from src.farm import stages as stage_cfg
from src.farm.timeutil import days_between, is_date, utc_now_iso

logger = setup_logger("agri.farm.repo")


class NotFound(Exception):
    """记录不存在**或不属于该用户** —— 两种情况故意不区分（见模块说明）。"""


# ------------------------------------------------------------------ 工具

def new_id(prefix: str) -> str:
    """带前缀的短 id。前缀让日志和 URL 一眼能看出是哪张表的记录。"""
    return f"{prefix}_{secrets.token_hex(6)}"


def _dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _require_date(value: str, what: str) -> str:
    if not is_date(value):
        raise ValueError(f"{what}不是合法日期（应为 YYYY-MM-DD）：{value!r}")
    return value


# ------------------------------------------------------------------ 田块

def list_fields(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM fields WHERE user_id = ? ORDER BY created_at, id", (user_id,)
    ).fetchall()
    return _rows(rows)


def get_field(conn: sqlite3.Connection, user_id: int, field_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM fields WHERE user_id = ? AND id = ?", (user_id, field_id)
    ).fetchone()
    if row is None:
        raise NotFound(f"未找到田块：{field_id}")
    return dict(row)


def create_field(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    name: str,
    crop: str,
    area_mu: float = 0.0,
    province: str = "",
    city: str = "",
    county: str = "",
    lng: float | None = None,
    lat: float | None = None,
    sow_date: str,
    field_id: str | None = None,
) -> dict:
    """新建田块。crop 必须是 stages.supported_crops() 里的七个之一。

    province / city / county 是「选址」的三级，lng / lat 由前端按三级中心点
    取平均推导后一起提交（留空则该田块算不出积温与 3D，这是允许的降级）。
    """
    if not (name or "").strip():
        raise ValueError("田块名称不能为空")
    if not stage_cfg.is_supported(crop):
        raise ValueError(f"暂不支持作物「{crop}」，可选：{'、'.join(stage_cfg.supported_crops())}")
    _require_date(sow_date, "播种日")

    fid = field_id or new_id("fld")
    conn.execute(
        "INSERT INTO fields (user_id, id, name, crop, area_mu, province, city, county, lng, lat, "
        "sow_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, fid, name.strip(), crop.strip(), float(area_mu or 0), province or "",
         city or "", county or "", lng, lat, sow_date, utc_now_iso()),
    )
    return get_field(conn, user_id, fid)


# 允许通过 PATCH 改的字段。id / user_id / created_at 不在其中 ——
# 让前端能改主键或归属是最典型的越权入口。
FIELD_PATCHABLE = ("name", "crop", "area_mu", "province", "city", "county", "lng", "lat", "sow_date")


def update_field(conn: sqlite3.Connection, user_id: int, field_id: str, patch: dict) -> dict:
    """局部更新。空 patch 视为无改动（返回当前值，不报错）。"""
    current = get_field(conn, user_id, field_id)     # 不存在会抛 NotFound

    updates = {k: v for k, v in (patch or {}).items() if k in FIELD_PATCHABLE and v is not None}
    if "crop" in updates and not stage_cfg.is_supported(updates["crop"]):
        raise ValueError(f"暂不支持作物「{updates['crop']}」，"
                         f"可选：{'、'.join(stage_cfg.supported_crops())}")
    if "sow_date" in updates:
        _require_date(updates["sow_date"], "播种日")
    if "name" in updates and not str(updates["name"]).strip():
        raise ValueError("田块名称不能为空")
    if not updates:
        return current

    # 列名来自白名单常量（不是用户输入），值一律用占位符
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE fields SET {sets} WHERE user_id = ? AND id = ?",
        (*updates.values(), user_id, field_id),
    )
    return get_field(conn, user_id, field_id)


def delete_field(conn: sqlite3.Connection, user_id: int, field_id: str) -> None:
    """删除田块及其全部从属数据。

    先确认归属再删：`DELETE ... WHERE user_id = ? AND id = ?` 的 rowcount 为 0 时
    抛 NotFound —— 这样 A 用户删 B 用户的田块会是 404，且**B 的数据一行不少**。
    """
    get_field(conn, user_id, field_id)          # 归属校验（不属于自己即 NotFound）

    for table in ("logs", "daily_temp", "diagnoses", "growth_snapshots", "harvest_records"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ? AND field_id = ?", (user_id, field_id))
    conn.execute("DELETE FROM fields WHERE user_id = ? AND id = ?", (user_id, field_id))


# ------------------------------------------------------------------ 农事记录

LOG_TYPES = ("sow", "fertilize", "spray", "irrigate", "harvest", "diagnose", "other")
LOG_SOURCES = ("manual", "voice", "diagnose")


def create_log(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    field_id: str,
    type: str,
    date: str,
    item: str = "",
    amount: str = "",
    note: str = "",
    source: str = "manual",
    reg_no: str = "",
    interval_days: int | None = None,
    log_id: str | None = None,
) -> dict:
    get_field(conn, user_id, field_id)          # 不允许往别人的田里记账
    if type not in LOG_TYPES:
        raise ValueError(f"未知的农事类型「{type}」，可选：{'、'.join(LOG_TYPES)}")
    if source not in LOG_SOURCES:
        raise ValueError(f"未知的来源「{source}」，可选：{'、'.join(LOG_SOURCES)}")
    _require_date(date, "农事日期")

    # 登记证号与间隔期只对「打药」有意义。前端理论上只在 type=spray 时提交，
    # 但接口是公开的 —— 一条 type=fertilize 却带着间隔期的记录，会让台账把
    # 这块田误判成「还在间隔期」。所以这里按 type 收口，不信任调用方。
    if type != "spray":
        reg_no, interval_days = "", None

    lid = log_id or new_id("log")
    conn.execute(
        "INSERT INTO logs (user_id, id, field_id, type, date, item, amount, note, source, "
        "reg_no, interval_days, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, lid, field_id, type, date, item or "", amount or "", note or "",
         source, reg_no or "", interval_days, utc_now_iso()),
    )
    row = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND id = ?", (user_id, lid)
    ).fetchone()
    return dict(row) if row else {}


def list_logs(
    conn: sqlite3.Connection, user_id: int, *, field_id: str | None = None, limit: int = 200
) -> list[dict]:
    """历史记录，**倒序**（7.7 的历史卡片要求倒序）。

    排序键带上 id 作次级键：同一天可能记多条，只按 date 排的话 sqlite 返回顺序
    不保证稳定，界面每次刷新顺序都可能不一样。
    """
    sql = "SELECT * FROM logs WHERE user_id = ?"
    params: list[Any] = [user_id]
    if field_id:
        sql += " AND field_id = ?"
        params.append(field_id)
    sql += " ORDER BY date DESC, created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return _rows(conn.execute(sql, params).fetchall())


def recent_diagnosis_log(conn: sqlite3.Connection, user_id: int, field_id: str, *, days: int) -> dict | None:
    """最近若干天内的诊断记录（advisor 的「注意复查」要用）。"""
    from src.farm.timeutil import today_local
    from datetime import timedelta

    since = (today_local() - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT * FROM logs WHERE user_id = ? AND field_id = ? AND type = 'diagnose' AND date >= ? "
        "ORDER BY date DESC, created_at DESC LIMIT 1",
        (user_id, field_id, since),
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ 提醒暂存（7.7 的 [改天]）

def snooze_reminder(
    conn: sqlite3.Connection, user_id: int, field_id: str, reminder_key: str, *, days: int = 2
) -> str:
    """「改天」：这条提醒在 days 天内不再出现。返回 snooze_until。"""
    from datetime import timedelta

    from src.farm.timeutil import today_local

    get_field(conn, user_id, field_id)
    until = (today_local() + timedelta(days=int(days))).isoformat()
    conn.execute(
        "INSERT INTO reminder_snoozes (user_id, field_id, reminder_key, snooze_until, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, field_id, reminder_key) DO UPDATE SET "
        "snooze_until = excluded.snooze_until, created_at = excluded.created_at",
        (user_id, field_id, reminder_key, until, utc_now_iso()),
    )
    return until


def active_snoozes(conn: sqlite3.Connection, user_id: int, field_id: str) -> set[str]:
    """当前仍在暂存期内的提醒标识集合。

    过期的行**不在这里删** —— 删是写操作，而这个函数会被每个「今天该干啥」请求调用。
    顺手清理放在 snooze_reminder 里做（那里本来就在写）。
    """
    from src.farm.timeutil import today_str

    rows = conn.execute(
        "SELECT reminder_key FROM reminder_snoozes "
        "WHERE user_id = ? AND field_id = ? AND snooze_until > ?",
        (user_id, field_id, today_str()),
    ).fetchall()
    return {r["reminder_key"] for r in rows}


# ------------------------------------------------------------------ 逐日气温

def upsert_daily_temp(
    conn: sqlite3.Connection, user_id: int, field_id: str, rows: Iterable[dict], *, source: str
) -> int:
    """批量写入逐日气温：**幂等**，同日重复写是覆盖而不是叠加（7.12 有专测）。

    靠主键 (user_id, field_id, date) + ON CONFLICT DO UPDATE 实现。
    顺带把 tmean 的写入限制在**合理范围**（-60~60℃）：接口返回的异常值（比如
    解析错位带进来的湿度）一旦进了库，积温和生育期会一起歪掉，而且事后再查很难
    回溯是哪一天脏的。
    """
    payload = []
    for r in rows:
        date, tmean = r.get("date"), r.get("tmean")
        if not is_date(date or "") or tmean is None:
            continue
        t = float(tmean)
        if not (-60.0 <= t <= 60.0):
            logger.warning("丢弃异常气温 %s：%s", date, t)
            continue
        payload.append((
            user_id, field_id, date, t, source,
            r.get("tmax"), r.get("tmin"), r.get("precip"), r.get("humidity"),
        ))

    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO daily_temp (user_id, field_id, date, tmean, source, "
        "tmax, tmin, precip, humidity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, field_id, date) DO UPDATE SET "
        "tmean = excluded.tmean, source = excluded.source, "
        # 只有新数据带了这些值时才覆盖：否则一次「只补积温」的写入会把
        # 已有的降水/湿度抹成 NULL（COALESCE 保住旧值）。
        "tmax = COALESCE(excluded.tmax, daily_temp.tmax), "
        "tmin = COALESCE(excluded.tmin, daily_temp.tmin), "
        "precip = COALESCE(excluded.precip, daily_temp.precip), "
        "humidity = COALESCE(excluded.humidity, daily_temp.humidity)",
        payload,
    )
    return len(payload)


def list_daily_temp(
    conn: sqlite3.Connection, user_id: int, field_id: str, *, start: str, end: str
) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM daily_temp WHERE user_id = ? AND field_id = ? AND date BETWEEN ? AND ? "
        "ORDER BY date",
        (user_id, field_id, start, end),
    ).fetchall())


def latest_temp_date(conn: sqlite3.Connection, user_id: int, field_id: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM daily_temp WHERE user_id = ? AND field_id = ?",
        (user_id, field_id),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def latest_temp_has_detail(conn: sqlite3.Connection, user_id: int, field_id: str) -> bool:
    """最近一行缓存有没有最高/最低气温（迁移 v4 才加的列）。

    用途是**自愈**：从 v3 升级上来的库里，老行只有 tmean，tmax/tmin/precip 是 NULL，
    而训练特征要用它们。如果只看「缓存新不新」，这些行会因为 TTL 内而被跳过，
    特征就一直是空的（tmax_mean_30d 恒为 0）。所以缓存是否完整也要参与判断。
    """
    row = conn.execute(
        "SELECT tmax FROM daily_temp WHERE user_id = ? AND field_id = ? "
        "ORDER BY date DESC LIMIT 1",
        (user_id, field_id),
    ).fetchone()
    return bool(row) and row["tmax"] is not None


# ------------------------------------------------------------------ 气候基准值

def get_normals(conn: sqlite3.Connection, province: str, city: str) -> dict[str, dict]:
    """某地的全部 <'MM-DD' -> {avg, std}>。气候基准值不带 user_id（全局共享）。"""
    rows = conn.execute(
        "SELECT * FROM climate_normals WHERE province = ? AND city = ?", (province, city)
    ).fetchall()
    return {r["date_md"]: {"avg": r["tmean_avg"], "std": r["tmean_std"]} for r in rows}


def upsert_normals(
    conn: sqlite3.Connection, province: str, city: str, normals: dict[str, tuple[float, float]]
) -> int:
    if not normals:
        return 0
    payload = [(province, city, md, float(avg), float(std)) for md, (avg, std) in normals.items()]
    conn.executemany(
        "INSERT INTO climate_normals (province, city, date_md, tmean_avg, tmean_std) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(province, city, date_md) DO UPDATE SET "
        "tmean_avg = excluded.tmean_avg, tmean_std = excluded.tmean_std",
        payload,
    )
    return len(payload)


def has_normals(conn: sqlite3.Connection, province: str, city: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM climate_normals WHERE province = ? AND city = ?",
        (province, city),
    ).fetchone()
    return bool(row and row["n"])


# ------------------------------------------------------------------ 诊断入档

def create_diagnosis(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    field_id: str,
    date: str,
    image_path: str = "",
    label: str = "",
    confidence: float | None = None,
    pest_json: Any = None,
    report_json: Any = None,
    subject: str = "",
    recheck_date: str = "",
    diagnosis_id: str | None = None,
) -> dict:
    get_field(conn, user_id, field_id)
    _require_date(date, "诊断日期")
    # 回访日期允许为空（这次不排回访），但只要给了就必须是合法日期 ——
    # 否则它会在回访队列里变成一条永远不出现的记录，静默丢失。
    if recheck_date:
        _require_date(recheck_date, "回访日期")

    did = diagnosis_id or new_id("dgn")
    conn.execute(
        "INSERT INTO diagnoses (user_id, id, field_id, date, image_path, label, confidence, "
        "pest_json, report_json, subject, recheck_date, recheck_done, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (user_id, did, field_id, date, image_path or "", label or "", confidence,
         # pest_json 为 None 时**存 NULL**（不是字符串 'null'）：虫害模型不可用时
         # 就是「没有这一路证据」，读回来必须还是 None，不能在「无虫害证据」和
         # 「确实没有虫」之间含糊。
         json.dumps(pest_json, ensure_ascii=False) if pest_json is not None else None,
         json.dumps(report_json, ensure_ascii=False) if report_json is not None else None,
         (subject or "").strip(), recheck_date or "",
         utc_now_iso()),
    )
    row = conn.execute(
        "SELECT * FROM diagnoses WHERE user_id = ? AND id = ?", (user_id, did)
    ).fetchone()
    return _decode_diagnosis(dict(row)) if row else {}


def _decode_diagnosis(row: dict) -> dict:
    for key in ("pest_json", "report_json"):
        if row.get(key):
            try:
                row[key] = json.loads(row[key])
            except (ValueError, TypeError):
                row[key] = None
    return row


def list_diagnoses(
    conn: sqlite3.Connection, user_id: int, *, field_id: str | None = None, limit: int = 100
) -> list[dict]:
    sql = "SELECT * FROM diagnoses WHERE user_id = ?"
    params: list[Any] = [user_id]
    if field_id:
        sql += " AND field_id = ?"
        params.append(field_id)
    sql += " ORDER BY date DESC, created_at DESC LIMIT ?"
    params.append(int(limit))
    return [_decode_diagnosis(dict(r)) for r in conn.execute(sql, params).fetchall()]


def get_diagnosis(conn: sqlite3.Connection, user_id: int, diagnosis_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM diagnoses WHERE user_id = ? AND id = ?", (user_id, diagnosis_id)
    ).fetchone()
    if row is None:
        raise NotFound(f"未找到诊断记录：{diagnosis_id}")
    return _decode_diagnosis(dict(row))


# ------------------------------------------------------------------ 回访队列

def list_followups(
    conn: sqlite3.Connection, user_id: int, *, today: str, upcoming_days: int = 7
) -> dict:
    """待回访的诊断，按「已到期 / 未来 7 天」两组返回。

    判定只认 `recheck_date`：没排回访的诊断（空串）不参与，
    否则每次诊断都会在队列里冒出来，队列就没意义了。

    过期不隐藏、也不合并成一句「共 N 条」—— 农技员真正会漏掉的是**上周就该看、
    到现在还没看**的那几条，把它们和时间上更近的混在一起排，它们就会被淹掉。
    所以 overdue 单独一组，并按过期天数倒序（拖得最久的排最前）。
    """
    rows = conn.execute(
        "SELECT d.id, d.field_id, d.date, d.label, d.confidence, d.subject, d.recheck_date, "
        "       f.name AS field_name, f.crop AS crop "
        "FROM diagnoses d JOIN fields f ON f.id = d.field_id AND f.user_id = d.user_id "
        "WHERE d.user_id = ? AND d.recheck_done = 0 AND d.recheck_date != '' "
        "ORDER BY d.recheck_date ASC",
        (user_id,),
    ).fetchall()

    overdue: list[dict] = []
    today_due: list[dict] = []
    upcoming: list[dict] = []
    for r in rows:
        item = dict(r)
        gap = days_between(today, item["recheck_date"])
        item["days_left"] = gap          # 负数=已过期多少天，0=今天，正数=还有几天
        if gap < 0:
            overdue.append(item)
        elif gap == 0:
            today_due.append(item)
        elif gap <= upcoming_days:
            upcoming.append(item)

    # 同一批里最该先看的排前面：过期越久越前，其余按日期正序
    overdue.sort(key=lambda x: x["days_left"])
    return {"overdue": overdue, "today": today_due, "upcoming": upcoming}


def mark_followup_done(conn: sqlite3.Connection, user_id: int, diagnosis_id: str) -> dict:
    """把一条诊断标成「已回访」。**已经回访过的再点一次不算错**（幂等）：
    列表是异步刷新的，农技员连点两下很常见。"""
    row = conn.execute(
        "SELECT id, recheck_done FROM diagnoses WHERE user_id = ? AND id = ?",
        (user_id, diagnosis_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"未找到诊断记录：{diagnosis_id}")
    conn.execute(
        "UPDATE diagnoses SET recheck_done = 1 WHERE user_id = ? AND id = ?",
        (user_id, diagnosis_id),
    )
    return {"diagnosis_id": diagnosis_id, "done": True}


def count_pending_followups(conn: sqlite3.Connection, user_id: int, *, today: str) -> int:
    """到期待回访条数（含已过期的）。给首页/工具栏一个角标用，只数不取内容。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM diagnoses "
        "WHERE user_id = ? AND recheck_done = 0 AND recheck_date != '' AND recheck_date <= ?",
        (user_id, today),
    ).fetchone()
    return int(row["n"] if row else 0)


# ------------------------------------------------------------------ 生长快照

def upsert_snapshot(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    field_id: str,
    date: str,
    gdd: float,
    stage: str,
    height: float | None = None,
    leaf_count: int | None = None,
    color_idx: float | None = None,
    source: str = "gdd",
) -> None:
    """按 (user_id, field_id, date) 幂等写入 —— 同一天重复请求不该堆出多行。"""
    _require_date(date, "快照日期")
    conn.execute(
        "INSERT INTO growth_snapshots (user_id, field_id, date, gdd, stage, height, leaf_count, "
        "color_idx, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, field_id, date) DO UPDATE SET "
        "gdd = excluded.gdd, stage = excluded.stage, height = excluded.height, "
        "leaf_count = excluded.leaf_count, color_idx = excluded.color_idx, source = excluded.source",
        (user_id, field_id, date, float(gdd), stage, height, leaf_count, color_idx, source),
    )


def list_snapshots(
    conn: sqlite3.Connection, user_id: int, field_id: str, *, limit: int = 60
) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM growth_snapshots WHERE user_id = ? AND field_id = ? "
        "ORDER BY date DESC LIMIT ?",
        (user_id, field_id, int(limit)),
    ).fetchall())


# ------------------------------------------------------------------ 收获记录

def create_harvest(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    field_id: str,
    crop: str,
    sow_date: str,
    harvest_date: str,
    actual_days: int,
    gdd_at_harvest: float | None = None,
    features: dict | None = None,
    harvest_id: str | None = None,
) -> dict:
    get_field(conn, user_id, field_id)
    hid = harvest_id or new_id("har")
    conn.execute(
        "INSERT INTO harvest_records (user_id, id, field_id, crop, sow_date, harvest_date, "
        "actual_days, gdd_at_harvest, features_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, hid, field_id, crop, sow_date, harvest_date, int(actual_days),
         gdd_at_harvest,
         json.dumps(features, ensure_ascii=False) if features else None,
         utc_now_iso()),
    )
    row = conn.execute(
        "SELECT * FROM harvest_records WHERE user_id = ? AND id = ?", (user_id, hid)
    ).fetchone()
    return dict(row) if row else {}


def count_harvests(conn: sqlite3.Connection) -> int:
    """**全平台**计数（7.3.4 的训练触发条件：累计 ≥ 30 条）。

    刻意不带 user_id：这条阈值说的是「整个平台的样本量够不够训练」，
    单个用户收再多茬也不能凭一己之力触发。
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM harvest_records").fetchone()
    return int(row["n"]) if row else 0


def list_harvest_features(conn: sqlite3.Connection, *, limit: int = 2000) -> list[dict]:
    """训练用的样本。

    **只返回数值与作物名**，不返回 user_id / field_id / 田块名 / 位置 ——
    7.14 的隐私要求：训练只提取数值特征，不带能定位到人的字段。
    这一步在 SQL 里就切干净，而不是指望训练脚本自己记得过滤。
    """
    rows = conn.execute(
        "SELECT crop, sow_date, harvest_date, actual_days, gdd_at_harvest, features_json "
        "FROM harvest_records ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["features"] = json.loads(item.pop("features_json") or "{}")
        except (ValueError, TypeError):
            item["features"] = {}
            item.pop("features_json", None)
        out.append(item)
    return out


def list_harvests(conn: sqlite3.Connection, user_id: int, *, field_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM harvest_records WHERE user_id = ?"
    params: list[Any] = [user_id]
    if field_id:
        sql += " AND field_id = ?"
        params.append(field_id)
    sql += " ORDER BY harvest_date DESC"
    return _rows(conn.execute(sql, params).fetchall())
