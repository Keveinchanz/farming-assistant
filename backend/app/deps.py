"""依赖注入 —— 单例装配。

为什么都做成懒加载：
    模型/向量库/语音客户端初始化都可能有副作用（读几百 MB 权重、连 ChromaDB、联网取
    token）。如果在模块导入时就初始化，任何一个不可用都会让整个服务起不来。
    改成懒加载 + 首次调用时才报错，服务始终能启动，前端也能拿到明确的中文错误提示。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from backend.app.core.errors import CODE_MODEL_UNAVAILABLE, ApiError
from src.common.io_utils import read_json
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.deps")


# ---------------------------------------------------------------- 模型与融合

@lru_cache(maxsize=1)
def get_predictor():
    """图像识别器（优先 ONNX，回退 PyTorch 权重）。"""
    from src.infer.predictor import get_shared_predictor

    try:
        return get_shared_predictor()
    except FileNotFoundError as e:
        raise ApiError(
            f"图像识别模型不可用：{e}",
            code=CODE_MODEL_UNAVAILABLE,
            http_status=503,
        ) from e


@lru_cache(maxsize=1)
def get_fusion():
    """多模态诊断编排器。

    实例由 `src.llm.fusion.get_fusion()` 统一持有，HTTP 路由与 skills 技能共用，
    避免同一份 ONNX 在进程里被加载两次。
    """
    from src.llm.fusion import get_fusion as _build

    try:
        return _build()
    except FileNotFoundError as e:
        raise ApiError(
            f"图像识别模型不可用：{e}",
            code=CODE_MODEL_UNAVAILABLE,
            http_status=503,
        ) from e


@lru_cache(maxsize=1)
def get_pest_detector():
    """虫害检测器（IP102 / YOLOv8n ONNX）。

    与 get_predictor 有一个关键区别：**这个是可选的**。
    虫害是后加的第二路证据，它缺席不该让诊断功能整体不可用，
    因此这里不抛 503，只返回 None，由 fusion 侧当作"没有这一路证据"处理。
    （对比 get_predictor：分类模型缺席时整条链路确实没法工作，所以那里必须报错。）
    """
    from src.infer.pest_detector import get_shared_pest_detector

    return get_shared_pest_detector()


# ---------------------------------------------------------------- 语音 / 大模型 / RAG

@lru_cache(maxsize=1)
def get_voice():
    from src.voice.factory import get_voice as _build

    return _build()


@lru_cache(maxsize=1)
def get_llm():
    from src.llm.factory import get_llm as _build

    return _build()


@lru_cache(maxsize=1)
def get_rag():
    from src.rag.factory import get_rag as _build

    return _build()


# ---------------------------------------------------------------- 静态数据文件

@lru_cache(maxsize=8)
def load_json_cached(key: str) -> Any:
    """按 default.yaml 的 paths.<key> 读取并缓存 JSON。

    数据文件由阶段 1 / 4.5 的脚本生成；若还没生成，给出指向具体脚本的报错，
    而不是一个裸的 FileNotFoundError。
    生成脚本对照表见下面的 HINTS。
    """
    HINTS = {
        "label_map": "python scripts/03_build_label_map.py",
        "province_pest": "python scripts/04_gen_province_pest.py",
        "policy_list": "python scripts/05_fetch_policies.py",
    }
    path = path_of(key)
    if not path.exists():
        raise ApiError(
            f"数据文件不存在：{path}；请先运行 {HINTS.get(key, '对应的数据生成脚本')}",
            code=CODE_MODEL_UNAVAILABLE,
            http_status=503,
        )
    return read_json(path)


@lru_cache(maxsize=1)
def load_library() -> dict:
    """知识图书库数据（4.5 产物）。"""
    path = path_of("library_dir") / "library_items.json"
    if not path.exists():
        raise ApiError(
            f"知识图书库数据不存在：{path}；请先运行 python scripts/07_build_library.py",
            code=CODE_MODEL_UNAVAILABLE,
            http_status=503,
        )
    return read_json(path)


def refresh_caches() -> None:
    """清掉全部缓存，让下次请求重新加载（改完数据文件后可调用 /api/admin/reload）。"""
    for fn in (get_predictor, get_pest_detector, get_fusion, get_voice, get_llm, get_rag,
               load_json_cached, load_library):
        fn.cache_clear()
    # src/ 层的共享单例也要清：否则 deps 重建后，fusion 仍拿到旧的 predictor/detector，
    # reload 对模型实例不生效。
    from src.farm import harvest_model
    from src.infer.pest_detector import get_shared_pest_detector
    from src.infer.predictor import get_shared_predictor
    from src.llm.fusion import get_fusion as get_shared_fusion

    for fn in (get_shared_predictor, get_shared_pest_detector, get_shared_fusion):
        fn.cache_clear()
    # 收获期模型与跨省校准量也是 lru_cache 的：训练脚本跑完后如果不点这里，
    # 进程里一直是旧权重，页面上给出的还是训练前的日期，看起来像「训练没生效」。
    harvest_model.clear_model_cache()
    logger.info("已清空后端缓存，下次请求将重新加载模型与数据")
