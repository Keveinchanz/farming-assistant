"""农田生长 + 收获预测 + 3D 参数技能。

**纯函数路径**：不登录、不读 SQLite。气温可以直接注入 `history`；没注入但给了
经纬度就联网取 Open-Meteo；两者都没有则返回完整结构 + 降级标记，不编造收获日期。

3D 参数就是前端 `Farm3D.setModel(model)` 直接消费的那一组字段。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from skills.errors import SkillInputError
from skills.schemas import FarmGrowthInput

MISSING_WEATHER = "未提供经纬度，且没有历史气温"


def _rows_from_models(rows: list[Any]) -> list[dict]:
    out = []
    for row in rows:
        item = row.model_dump() if hasattr(row, "model_dump") else dict(row)
        if item.get("tmean") is None and item.get("tmax") is not None and item.get("tmin") is not None:
            item["tmean"] = (float(item["tmax"]) + float(item["tmin"])) / 2
        out.append(item)
    return out


def _history_rows(req: FarmGrowthInput, field: dict, today: str) -> tuple[list[dict], str, str]:
    """返回 (气温行, 来源标记, 降级原因)。"""
    if req.history:
        return _rows_from_models(req.history), "provided", ""

    lat, lng = field.get("lat"), field.get("lng")
    if lat is None or lng is None:
        return [], "none", MISSING_WEATHER

    from src.farm import weather
    from src.farm.timeutil import days_between, today_local

    try:
        sow = field["sow_date"]
        # 与 ensure_history() 一致：archive 接口有约 5 天延迟，最近几天用预报补
        archive_end = (today_local() - timedelta(days=5)).isoformat()
        archive = (
            weather.fetch_history(float(lat), float(lng), sow, archive_end)
            if days_between(sow, archive_end) >= 0 else []
        )
        forecast = weather.fetch_forecast(
            float(lat), float(lng), past_days=7, forecast_days=1
        )
        merged = weather.merge_history(archive, forecast, until=today)
        return _rows_from_models(merged), "fetched", ""
    except Exception as e:  # noqa: BLE001 —— 天气失败是降级，不是技能失败
        return [], "none", f"天气取数失败：{e}"


def _forecast_block(req: FarmGrowthInput, field: dict, today: str) -> dict:
    if req.forecast:
        days = [r for r in _rows_from_models(req.forecast) if r.get("date", "") >= today]
        return {
            "days": days,
            "source": "forecast" if days else "none",
            "source_note": "使用请求中的天气预报",
            "degraded": not bool(days),
        }

    lat, lng = field.get("lat"), field.get("lng")
    if lat is None or lng is None:
        return {
            "days": [], "source": "none", "degraded": True,
            "source_note": "未提供经纬度，且没有天气预报",
        }

    from src.farm import weather

    try:
        days = weather.fetch_forecast(float(lat), float(lng), past_days=0, forecast_days=3)
        days = [d for d in days if str(d.get("date", "")) >= today]
        return {
            "days": days, "source": "forecast" if days else "none",
            "source_note": "Open-Meteo 预报", "degraded": not bool(days),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "days": [], "source": "none", "degraded": True,
            "source_note": f"天气预报不可用：{e}",
        }


def _supported_crops_hint(limit: int = 24) -> str:
    try:
        from src.farm.stages import supported_crops

        crops = list(supported_crops())
        return "、".join(crops[:limit]) + ("…" if len(crops) > limit else "")
    except Exception:  # noqa: BLE001
        return ""


def run(payload: dict, *, allow_paths: bool = True) -> dict:  # noqa: ARG001
    req = FarmGrowthInput.model_validate(payload)
    field = req.field.model_dump()

    from src.farm import advisor, gdd, growth, harvest_model
    from src.farm.stages import crop_config
    from src.farm.timeutil import days_between, today_str

    today = req.today or today_str()
    if days_between(field["sow_date"], today) < 0:
        raise SkillInputError("today 不能早于播种日")

    try:
        cfg = crop_config(field["crop"])
    except Exception as e:  # noqa: BLE001
        hint = _supported_crops_hint()
        raise SkillInputError(
            f"不支持的作物「{field['crop']}」" + (f"；可选：{hint}" if hint else "")
        ) from e

    rows, source_label, reason = _history_rows(req, field, today)
    status = growth.status_from_rows(
        rows, field["sow_date"], today, reason=reason, source=source_label
    )

    growth_data = growth.compute_growth(field, status, today=today)
    model_data = growth.compute_model(
        field, growth_data, status, degraded=bool(status.get("degraded"))
    )
    series = gdd.accumulate(status["rows"], cfg)
    harvest = harvest_model.predict_harvest(
        field, growth_data, series,
        today=today,
        management=req.management.model_dump() if req.management else None,
        # 技能是无状态纯路径：不传 sample_count 时也不要去打开数据库
        sample_count=req.sample_count if req.sample_count is not None else 0,
    )

    weather_block = (
        _forecast_block(req, field, today)
        if {"weather", "advice"} & set(req.include)
        else {
            "days": [], "source": "none", "degraded": True,
            "source_note": "本次未请求天气数据",
        }
    )
    advice = advisor.harvest_advice(
        growth_data, model_data, harvest, weather_block, today=today
    )
    window = advisor.harvest_window(
        series, harvest, weather_block,
        maturity_gdd=growth_data["maturity_gdd"], today=today,
    )

    history_meta = {
        "rows": len(status["rows"]),
        "source": status.get("source", source_label),
        "degraded": bool(status.get("degraded")),
        "degraded_reason": status.get("degraded_reason", ""),
        "source_note": status.get("source_note", ""),
        "as_of": status.get("as_of", ""),
        "confidence": status.get("confidence", ""),
    }

    result: dict[str, Any] = {
        "field": field,
        "today": today,
        "weather": {"history": history_meta, "forecast": weather_block},
        "growth": growth_data,
        "model": model_data,
        "harvest": harvest,
        "harvest_advice": advice,
        "harvest_window": window,
        "degraded": {
            "weather": bool(weather_block.get("degraded")) if "weather" in set(req.include) else False,
            "history": bool(status.get("degraded")),
            "harvest_model": harvest.get("method") != "xgboost",
        },
        "input_summary": {
            "history_provided": bool(req.history),
            "forecast_provided": bool(req.forecast),
            "has_coordinates": field.get("lat") is not None and field.get("lng") is not None,
        },
    }

    selected = set(req.include)
    if "growth" not in selected:
        result.pop("growth", None)
    if "model" not in selected:
        result.pop("model", None)
    if "harvest" not in selected:
        result.pop("harvest", None)
    if "advice" not in selected:
        result.pop("harvest_advice", None)
        result.pop("harvest_window", None)
    if "weather" not in selected:
        result.pop("weather", None)
    return result


__all__ = ["run"]
