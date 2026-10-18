"""任务 4.5 —— 知识图书库接口：按分类返回卡片列表、详情内容，支持关键词检索。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend.app import deps
from backend.app.core.errors import CODE_NOT_FOUND, ApiError
from backend.app.schemas.models import ApiResponse, LibraryDetailData, LibraryListData
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api.library")

router = APIRouter(prefix="/api/library", tags=["知识图书库"])

# 列表接口不返回 sections（正文较长），点开详情时才取，避免一次传几百 KB。
# 前端还有一份本地快照（frontend/assets/data/library_preload.js）用的就是这同一组字段，
# 两边必须一致 —— 快照由 src/content/frontend_bundle.py::LIBRARY_BRIEF_FIELDS 生成，
# 有单测钉着这两个元组相等。
_LIST_FIELDS = ("id", "title", "category", "category_name", "crop", "cover",
                "summary", "tags", "index")


def _brief(item: dict) -> dict:
    return {k: item.get(k) for k in _LIST_FIELDS}


def _match(item: dict, q: str) -> bool:
    """关键词检索：标题 / 作物 / 病害 / 标签 / 摘要 / 正文全部参与匹配。"""
    if not q:
        return True
    hay = " ".join([
        item.get("title", ""), item.get("crop", ""), item.get("condition", ""),
        item.get("pathogen_type", ""), item.get("summary", ""),
        " ".join(item.get("tags") or []),
        " ".join(sec.get("text", "") for sec in (item.get("sections") or [])),
    ])
    return all(tok in hay for tok in q.split())


@router.get(
    "",
    response_model=ApiResponse[LibraryListData],
    summary="知识图书库列表",
    description="按分类（病害防治知识 / 虫害防治知识 / 农作物养护知识）返回卡片列表，支持关键词检索与分页。",
)
async def list_library(
    category: str = Query("", description="pest 病害防治知识 / insect 虫害防治知识 / care 农作物养护知识，留空返回全部"),
    q: str = Query("", description="关键词，多个词用空格分隔（需全部命中）"),
    page: int = Query(1, ge=1),
    size: int = Query(12, ge=1, le=100),
):
    data = deps.load_library()
    items = data["items"]

    if category:
        items = [it for it in items if it["category"] == category]
    q = q.strip()
    if q:
        items = [it for it in items if _match(it, q)]

    total = len(items)
    start = (page - 1) * size
    page_items = [_brief(it) for it in items[start:start + size]]

    logger.info("图书库列表：category=%r q=%r -> %d 条（返回第 %d 页 %d 条）",
                category, q, total, page, len(page_items))

    return {
        "code": 0, "msg": "ok",
        "data": {
            "total": total, "page": page, "size": size,
            "categories": data["categories"],
            "items": page_items,
        },
    }


@router.get(
    "/{item_id}",
    response_model=ApiResponse[LibraryDetailData],
    summary="知识图书库详情",
    description="返回卡片的分节正文（发病规律 / 防治方法 / 养护要点等配图），供点击卡片展开时渲染。",
)
async def get_library_item(item_id: str):
    data = deps.load_library()
    item = next((it for it in data["items"] if it["id"] == item_id), None)
    if item is None:
        raise ApiError(f"未找到知识条目：{item_id}", CODE_NOT_FOUND, http_status=404)

    logger.info("图书库详情：%s（%s，%d 个小节）", item_id, item["title"], len(item["sections"]))
    return {"code": 0, "msg": "ok", "data": item}
