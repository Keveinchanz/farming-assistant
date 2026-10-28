"""技能可用性探测。

只查文件与工厂的配置模式，**不加载模型、不联网** —— `GET /api/skills` 应该是
毫秒级请求，不能因为查询可用性就把几百 MB 的 ONNX 读进内存。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.common.logging_utils import setup_logger

logger = setup_logger("agri.skills.availability")


def _mode(module_name: str) -> str:
    try:
        module = __import__(module_name, fromlist=["current_mode"])
        return str(module.current_mode())
    except Exception as e:  # noqa: BLE001
        logger.debug("%s 模式探测失败：%s", module_name, e)
        return "unknown"


def crop_diagnosis_availability() -> dict[str, Any]:
    """技能 A：分类器是硬依赖；检测器、语音、RAG、LLM 缺席都能降级。"""
    from src.common.paths import path_of

    onnx_dir = Path(path_of("onnx_dir"))
    classifier = (onnx_dir / "inference_model.onnx").exists()
    if not classifier:
        # ONNX 没导出时，PyTorch 权重也能让服务跑起来
        try:
            classifier = Path(path_of("checkpoints_dir") / "best_resnet50.pth").exists()
        except Exception:  # noqa: BLE001
            classifier = False

    try:
        pest = Path(path_of("pest_onnx")).exists()
    except Exception:  # noqa: BLE001
        pest = False

    try:
        registry = Path(path_of("pesticide_registry")).exists()
    except Exception:  # noqa: BLE001
        registry = False

    return {
        "disease_classifier": classifier,
        "pest_detector": pest,
        "llm": _mode("src.llm.factory"),
        "rag": _mode("src.rag.factory"),
        "voice": _mode("src.voice.factory"),
        "pesticide_registry": registry,
    }


def farm_growth_availability() -> dict[str, Any]:
    """技能 B：始终可执行（可注入 history），收获模型只是可选增强。"""
    try:
        from src.farm.stages import supported_crops

        crops = len(supported_crops())
    except Exception:  # noqa: BLE001
        crops = 0

    try:
        from src.farm.harvest_model import models_available

        harvest = bool(models_available())
    except Exception:  # noqa: BLE001
        harvest = False

    return {
        "gdd": crops,
        "harvest_model": harvest,
        "weather": "on_demand",
        "crops_supported": crops,
    }


def availability(name: str) -> dict[str, Any]:
    if name == "crop-diagnosis":
        return crop_diagnosis_availability()
    if name == "farm-growth":
        return farm_growth_availability()
    return {}


def is_available(name: str) -> bool:
    info = availability(name)
    if name == "crop-diagnosis":
        return bool(info.get("disease_classifier"))
    return bool(info)


__all__ = [
    "availability", "is_available",
    "crop_diagnosis_availability", "farm_growth_availability",
]
