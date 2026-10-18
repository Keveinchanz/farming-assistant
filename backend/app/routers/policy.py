"""任务 4.7 —— 惠农政策接口：返回 2026 年政策列表（标题、核心内容、出处、发布时间）。

source_url 是官方原文地址，前端把政策标题渲染成指向它的外链（`target="_blank"`
+ `rel="noopener noreferrer"`），读者可以逐一核对摘要与要点。

points 是**逐字照抄**的原文要点（条目名 + 该条第一句话），summary 是导语加条目清单，
两者都由 src/content/policy_fetch.py 从官方页面自动摘录，不做改写。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Query

from backend.app import deps
from backend.app.core.errors import CODE_BAD_REQUEST, ApiError
from backend.app.schemas.models import ApiResponse, PolicyListData
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api.policy")

router = APIRouter(prefix="/api/policy", tags=["惠农政策"])


def _as_sortable(date_str: str) -> str:
    """把「2026年3月」「2026-03-25」统一成可比较的字符串。"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4})年(\d{1,2})月", date_str or "")
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-00"
    return date_str or ""


@router.get(
    "/list",
    response_model=ApiResponse[PolicyListData],
    summary="2026 年惠农政策列表",
    description=(
        "返回 2026 年官方发布的惠农政策，支持按政策类型、发布时间区间与关键词筛选。\n\n"
        "summary 字段是从官方原文自动提取的核心内容摘要（导语 + 条目清单），可回溯到原始页面。"
    ),
)
async def list_policies(
    type: str = Query("", description="政策类型 key，见响应里的 types 字段"),
    start: str = Query("", description="起始时间，如 2026-01 或 2026-03-01"),
    end: str = Query("", description="结束时间，如 2026-12"),
    q: str = Query("", description="关键词（匹配标题与摘要）"),
):
    raw = deps.load_json_cached("policy_list")
    items = list(raw["items"])

    if type:
        known = {t["key"] for t in raw["types"]}
        if type not in known:
            raise ApiError(f"未知政策类型 {type!r}，可选：{'、'.join(sorted(known))}", CODE_BAD_REQUEST)
        items = [it for it in items if it["type"] == type]

    if start:
        if not re.match(r"^\d{4}(-\d{2}){0,2}$", start):
            raise ApiError("start 格式应为 YYYY 或 YYYY-MM 或 YYYY-MM-DD", CODE_BAD_REQUEST)
        items = [it for it in items if _as_sortable(it.get("publish_date_iso") or it["publish_date"]) >= _pad(start)]
    if end:
        if not re.match(r"^\d{4}(-\d{2}){0,2}$", end):
            raise ApiError("end 格式应为 YYYY 或 YYYY-MM 或 YYYY-MM-DD", CODE_BAD_REQUEST)
        items = [it for it in items if _as_sortable(it.get("publish_date_iso") or it["publish_date"]) <= _pad(end, upper=True)]

    if q.strip():
        toks = q.split()
        items = [it for it in items
                 if all(t in f"{it['title']}{it['summary']}{it['publish_unit']}{it['type_name']}"
                        for t in toks)]

    items.sort(key=lambda it: _as_sortable(it.get("publish_date_iso") or it["publish_date"]),
               reverse=True)

    logger.info("政策列表：type=%r start=%r end=%r q=%r -> %d 条", type, start, end, q, len(items))
    return {
        "code": 0, "msg": "ok",
        "data": {
            "total": len(items),
            "types": raw["types"],
            "items": items,
            "year": raw.get("year", 2026),
            "demo_data": bool(raw.get("demo_data", False)),
        },
    }


def _pad(value: str, *, upper: bool = False) -> str:
    """把 YYYY / YYYY-MM 补齐成可比较的 YYYY-MM-DD。"""
    parts = value.split("-")
    if len(parts) == 1:
        return f"{parts[0]}-12-31" if upper else f"{parts[0]}-01-01"
    if len(parts) == 2:
        return f"{parts[0]}-{parts[1]}-31" if upper else f"{parts[0]}-{parts[1]}-01"
    return value
