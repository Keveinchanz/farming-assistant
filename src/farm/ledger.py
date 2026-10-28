"""用药台账的聚合（方案 B 的那一页）。

把「农事记录里 type=spray 的那些行」按三个视角重排：按地块、按药剂、用药史。
不做任何新的采集 —— 台账的每一条都来自 `logs` 表，来源是同一个「记一笔」表单。
这也是为什么先做方案 A：A 不补那两列，这张表就是空的。

**统计口径写在这里，免得前端各算各的**：

    · 施药次数：区间内 type=spray 的记录条数（一次记录 = 一次施药）；
    · 涉及药剂：按登记证号分组（手填没有证号的按药名分组）；
    · 仍在间隔期：该田「所有记了间隔期的施药」里解禁日最晚那次还没到（见 spray.py）；
    · 未记录间隔期：区间内有打药记录、但一条间隔期都没填的田 —— 这是「不知道」，
      单独计数，绝不并进「已过」。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from src.farm import pesticides, repo, spray

# 「本季」的定义：从最早一块田的播种日算起（那才是这一批田共同的生长季起点），
# 但最多回看 365 天 —— 否则一块两年前的旧田会把区间拉到毫无意义。
MAX_SEASON_DAYS = 365
DEFAULT_RECENT_DAYS = 90


def _as_date(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def resolve_range(fields: list[dict], period: str = "season",
                  today: str | None = None) -> tuple[str, str, str]:
    """返回 (from, to, 人话标签)。period: season | recent | all | 自定义(传自定义的走参数)。"""
    ref = _as_date(today) or date.today()
    end = ref.isoformat()

    if period == "all":
        return "", end, "全部记录"
    if period == "recent":
        return (ref - timedelta(days=DEFAULT_RECENT_DAYS)).isoformat(), end, f"近 {DEFAULT_RECENT_DAYS} 天"

    # season：取最早播种日，夹在 [今天-365, 今天-7] 之间
    sows = [_as_date(f.get("sow_date", "")) for f in fields or []]
    sows = [d for d in sows if d and d <= ref]
    if not sows:
        start = ref - timedelta(days=DEFAULT_RECENT_DAYS)
    else:
        start = min(sows)
        floor = ref - timedelta(days=MAX_SEASON_DAYS)
        if start < floor:
            start = floor
        if start > ref - timedelta(days=7):
            start = ref - timedelta(days=7)
    return start.isoformat(), end, f"本季（{start.isoformat()} 起）"


def _ingredients(text: str) -> set[str]:
    """「嘧菌酯 21.6%、井冈霉素A 12%」→ {'嘧菌酯', '井冈霉素a'}"""
    out: set[str] = set()
    for part in (text or "").replace("，", "、").split("、"):
        name = part.strip()
        if not name:
            continue
        # 去掉含量（数字 + % + 单位），只留成分名
        for i, ch in enumerate(name):
            if ch.isdigit():
                name = name[:i].strip()
                break
        if name:
            out.add(name.lower())
    return out


def _consecutive_same(field_logs: list[dict]) -> list[dict]:
    """找出「相邻两次施药有效成分有重叠」的成对记录（用于轮换提醒）。

    抗性不是按产品名看，是按有效成分看：换了个商品名的同一种药照样会加速抗性。
    """
    sprays = [lg for lg in field_logs if lg.get("type") == "spray" and lg.get("date")]
    sprays.sort(key=lambda x: (x["date"], x.get("created_at") or ""))
    pairs = []
    for prev, cur in zip(sprays, sprays[1:]):
        a, b = _ingredients(prev.get("item", "")), _ingredients(cur.get("item", ""))
        # 没有有效成分信息（手填药名）时退回按药名比
        if not a or not b:
            shared = {prev.get("item", "")} & {cur.get("item", "")}
        else:
            shared = a & b
        if shared:
            pairs.append({"prev": prev, "cur": cur, "shared": sorted(shared)})
    return pairs


def build(conn, user_id: int, *, field_id: str = "", period: str = "season",
          date_from: str = "", date_to: str = "", today: str | None = None) -> dict:
    ref = today or spray.today_str()

    all_fields = repo.list_fields(conn, user_id)
    if field_id:
        fields = [f for f in all_fields if f["id"] == field_id]
        if not fields:
            raise repo.NotFound(f"未找到田块：{field_id}")
    else:
        fields = all_fields
    field_by_id = {f["id"]: f for f in fields}

    if date_from or date_to:
        start = date_from or "0000-01-01"
        end = date_to or ref
        label = f"{start} ~ {end}"
    else:
        start, end, label = resolve_range(fields, period, ref)

    logs = repo.list_logs(conn, user_id, limit=1000)
    if field_id:
        logs = [lg for lg in logs if lg.get("field_id") == field_id]
    elif fields:
        ids = set(field_by_id)
        logs = [lg for lg in logs if lg.get("field_id") in ids]
    if start:
        logs = [lg for lg in logs if str(lg.get("date", ""))[:10] >= start]
    if end:
        logs = [lg for lg in logs if str(lg.get("date", ""))[:10] <= end]

    sprays = [lg for lg in logs if lg.get("type") == "spray"]

    # ---- 全量（不受区间限制）的记录：判「还在不在间隔期」必须看全部，
    #      否则把区间调到「本月」就会让一块还在间隔期的田突然显示"可采收"。
    all_logs = repo.list_logs(conn, user_id, limit=1000)
    if field_id:
        all_logs = [lg for lg in all_logs if lg.get("field_id") == field_id]

    # ---- 按地块
    rows = []
    alerts = []
    locked_count = 0
    unknown_count = 0
    for f in fields:
        mine = [lg for lg in all_logs if lg.get("field_id") == f["id"]]
        mine_in_range = [lg for lg in sprays if lg.get("field_id") == f["id"]]
        cons = spray.constraint(mine, ref)
        last = None
        for lg in sorted(mine_in_range, key=lambda x: (x["date"], x.get("created_at") or ""), reverse=True):
            last = lg
            break
        has_unknown = any(
            lg.get("type") == "spray" and lg.get("interval_days") in (None, "")
            for lg in mine_in_range
        )
        if cons and cons["locked"]:
            locked_count += 1
        if has_unknown and not (cons and cons["locked"]):
            unknown_count += 1
        row = {
            "field_id": f["id"],
            "name": f.get("name", ""),
            "crop": f.get("crop", ""),
            "area_mu": f.get("area_mu", 0),
            "spray_count": len(mine_in_range),
            "last_date": (last or {}).get("date", ""),
            "last_item": (last or {}).get("item", ""),
            "last_reg_no": (last or {}).get("reg_no", ""),
            "interval_days": cons["interval_days"] if cons else None,
            "earliest_date": cons["earliest_date"] if cons else "",
            "days_left": cons["days_left"] if cons else None,
            "locked": bool(cons and cons["locked"]),
            "has_unknown_interval": has_unknown and not cons,
            "status": ("locked" if (cons and cons["locked"])
                       else "ok" if cons else "unknown" if has_unknown else "none"),
        }
        rows.append(row)
        if cons and cons["locked"]:
            alerts.append({
                "field_id": f["id"], "name": f.get("name", ""), "crop": f.get("crop", ""),
                "earliest_date": cons["earliest_date"], "days_left": cons["days_left"],
                "item": cons["item"], "reg_no": cons["reg_no"],
                "date": cons["date"], "interval_days": cons["interval_days"],
            })

    rows.sort(key=lambda r: (0 if r["locked"] else 1 if r["status"] == "unknown" else 2,
                             r["earliest_date"] or "9999", r["name"]))
    alerts.sort(key=lambda a: a["earliest_date"])

    # ---- 按药剂
    groups: dict[str, dict] = {}
    for lg in sprays:
        key = (lg.get("reg_no") or "").strip().upper() or f"name:{lg.get('item', '')}"
        g = groups.get(key)
        if g is None:
            reg = (lg.get("reg_no") or "").strip()
            meta = pesticides.get(reg) if reg else None
            g = {
                "key": key,
                "reg_no": reg,
                "product": (meta or {}).get("product") or lg.get("item", "") or "（未填药名）",
                "item": lg.get("item", ""),
                "form": (meta or {}).get("form", ""),
                "toxicity": (meta or {}).get("toxicity", ""),
                "ingredient": (meta or {}).get("ingredient", ""),
                "count": 0,
                "field_ids": [],
                "field_names": [],
                "last_date": "",
                "repeat_fields": [],
                "registered": bool(meta),
            }
            groups[key] = g
        g["count"] += 1
        fid = lg.get("field_id", "")
        if fid and fid not in g["field_ids"]:
            g["field_ids"].append(fid)
            g["field_names"].append(field_by_id.get(fid, {}).get("name", fid))
        if str(lg.get("date", "")) > g["last_date"]:
            g["last_date"] = str(lg.get("date", ""))

    # 轮换提醒：按田看相邻两次是否同一有效成分
    for f in fields:
        mine = [lg for lg in all_logs if lg.get("field_id") == f["id"]]
        for pair in _consecutive_same(mine):
            for lg in (pair["cur"],):
                key = (lg.get("reg_no") or "").strip().upper() or f"name:{lg.get('item', '')}"
                g = groups.get(key)
                if g and f.get("name") not in g["repeat_fields"]:
                    g["repeat_fields"].append(f.get("name", ""))

    products = sorted(groups.values(), key=lambda g: (-g["count"], g["product"]))
    for g in products:
        g["rotate_warning"] = bool(g["repeat_fields"])

    # ---- 用药史（倒序，带每条自己的解禁日）
    history = spray.annotate(sprays, ref)

    next_unlock = alerts[0] if alerts else None
    summary = {
        "spray_count": len(sprays),
        "product_count": len(products),
        "locked_fields": locked_count,
        "unknown_fields": unknown_count,
        "field_count": len(fields),
        "next_unlock": ({k: next_unlock[k] for k in
                         ("field_id", "name", "earliest_date", "days_left", "item")}
                        if next_unlock else None),
    }

    return {
        "today": ref,
        # 键名用 start / end 而不是 from / to：from 是 Python 关键字，
        # pydantic 模型里得用 alias 绕，为一个字段名引入 alias 不值得。
        "period": {"start": start, "end": end, "label": label, "key": period},
        "summary": summary,
        "fields": rows,
        "products": products,
        "history": history,
        "alerts": alerts,
        "note": _note(len(sprays)),
    }


def _note(spray_count: int) -> str:
    if spray_count == 0:
        return ("本区间还没有打药记录。记一笔时把药剂和安全间隔期填上，"
                "这里就会自动算出每块田最早哪天能收。")
    return ("安全间隔期以农药标签为准，由施药人填写；系统只做倒计时，不推断、不给默认值。"
            "台账不记录推荐剂量。")
