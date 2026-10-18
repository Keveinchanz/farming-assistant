"""任务 4.6 —— 地图数据接口：返回各省 TOP3 病虫害，支持按季节筛选。

数据来源为 data/province_pest_top3.json（1.5 产物，已标注 demo_data=true）。
接口会把「省份 × 季节」的嵌套结构按请求的季节**拍平成 top3 数组**，
前端 hover 时直接读 province.top3，切换季节只需重新请求一次并整体 setOption。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend.app import deps
from backend.app.core.errors import CODE_BAD_REQUEST, CODE_NOT_FOUND, ApiError
from backend.app.schemas.models import ApiResponse, MapPestTop3Data
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api.map")

router = APIRouter(prefix="/api/map", tags=["地图数据"])


@router.get(
    "/pest_top3",
    response_model=ApiResponse[MapPestTop3Data],
    summary="各省高发病虫害 TOP3",
    description=(
        "按季节返回各省高发的 TOP3 病虫害。省份名为官方全称（如「内蒙古自治区」），"
        "与前端地图 GeoJSON 的 properties.name 一致，可直接用于上色与弹窗。\n\n"
        "响应中的 demo_data=true 表示这是由「各省主要作物 + 病原季节性规律」推导的演示数据。"
    ),
)
async def pest_top3(
    season: str = Query("spring", description="spring / summer / autumn / winter"),
    province: str = Query("", description="只取某个省（官方全称），留空返回全部"),
):
    raw = deps.load_json_cached("province_pest")

    seasons = raw["seasons"]
    valid = {s["key"] for s in seasons}
    if season not in valid:
        raise ApiError(f"未知季节 {season!r}，可选：{'、'.join(sorted(valid))}", CODE_BAD_REQUEST)

    provinces = raw["provinces"]
    if province:
        provinces = [p for p in provinces if p["name"] == province or p["short_name"] == province]
        if not provinces:
            raise ApiError(f"未找到省份：{province}", CODE_NOT_FOUND, http_status=404)

    flattened = []
    for p in provinces:
        flattened.append({
            "name": p["name"],
            "short_name": p.get("short_name", ""),
            "adcode": p.get("adcode", ""),
            "crops": p.get("crops", []),
            "top3": (p.get("seasons") or {}).get(season, []),
            "threat_scope": int(p.get("threat_scope", 0)),
        })

    logger.info("地图数据：season=%s province=%r -> %d 个省份", season, province, len(flattened))
    return {
        "code": 0, "msg": "ok",
        "data": {
            "season": season,
            "seasons": seasons,
            "provinces": flattened,
            "demo_data": bool(raw.get("demo_data", True)),
            "disclaimer": raw.get("disclaimer", ""),
        },
    }
