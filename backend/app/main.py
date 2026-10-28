"""任务 4.1 —— FastAPI 后端服务入口：统一接口规范、跨域配置、静态资源挂载。

路由与静态资源的分工：
    /api/**               后端接口（统一响应包 {"code","msg","data"}）
    /docs                 自动生成的 Swagger 交互文档
    /media/**             → data/processed/dataset/  知识图书库的病害封面与配图
    /media-pest/**        → data/pest/images/       知识图书库的虫害样图（IP102，独立挂载）
    /**                   → frontend/  四个前端页面与静态资源（html=true 会自动映射 index.html）

由于前端与后端同源，页面里一律用相对路径调用 /api/... 即可；
CORSMiddleware 仍然保留，方便你把前端单独部署或本地用 file:// 调试。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app import deps
from backend.app.core.auth_deps import require_admin
from backend.app.core.config import get_settings
from backend.app.core.errors import install_error_handlers
from backend.app.routers import (
    assistant,
    auth,
    chat as chat_router,
    diagnose,
    farm,
    library,
    map as map_router,
    policy,
    share,
    skills as skills_router,
    voice,
)
from backend.app.schemas.models import ApiResponse
from src.common.io_utils import ensure_dir
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.api")

DESCRIPTION = """
**田头农技台** 后端服务。

覆盖任务清单阶段 4 的全部接口：

| 任务 | 接口 | 说明 |
|---|---|---|
| 4.2 | `POST /api/diagnose/image` | 病虫害图像识别（ONNX 推理） |
| 4.3 | `POST /api/diagnose/multimodal` | 图片+语音+文字 多模态融合诊断 |
| 4.4 | （内部）农业 RAG 知识库 | 由 `src/rag/` 提供，供 4.3 检索引用 |
| 4.5 | `GET /api/library`、`GET /api/library/{id}` | 知识图书库列表与详情 |
| 4.6 | `GET /api/map/pest_top3` | 各省高发病虫害 TOP3（按季节） |
| 4.7 | `GET /api/policy/list` | 2026 年惠农政策列表 |

另有方言与语音接口：`GET /api/dialect/list`、`POST /api/voice/tts`、`POST /api/voice/asr`。

**降级说明**：未配置语音服务（当前默认科大讯飞方言大模型）/ DeepSeek 的 Key 时，语音与大模型会自动降级为 Mock，
接口契约不变，响应里的 `degraded` 字段会如实标注哪些能力走了降级。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时把知识图书库读进内存（用户要求「首次进入网页就要加载」的服务侧一半）。

    load_library() 是懒加载的，而 4.5 的产物有 1.3 MB —— 打开图书库页正好就是
    第一次请求，没人希望那一下还要等文件 IO + JSON 解析。这里先热一遍，
    之后所有请求都直接命中 @lru_cache。

    失败**只告警不阻断启动**：数据没生成不该让整个服务起不来，接口那边会给出
    「请先运行 python scripts/07_build_library.py」的明确报错。
    """
    try:
        data = deps.load_library()
        logger.info("知识图书库已预热：%d 张卡片（%s）", data.get("total"),
                    "，".join(f"{c['name']} {c['count']} 张" for c in data.get("categories", [])))
    except Exception as e:  # noqa: BLE001
        logger.warning("知识图书库预热失败，将在首次请求时重试：%s", e)

    # 阶段 7：把农田库建好（首次启动自动建库并迁移到最新版本）。
    # 也提供 scripts/23_init_farm_db.py 做同样的事，供「不想让服务改文件」的场景使用。
    # 失败不阻断启动 —— 农田功能会有明确报错，但诊断/知识库这些阶段 4 的能力照常可用。
    try:
        from src.farm.db import init_db

        path = init_db(verbose=False)
        logger.info("农田数据库就绪：%s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("农田数据库初始化失败，/api/farm/* 将不可用：%s", e)
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="田头农技台",
        description=DESCRIPTION,
        version="1.0.0",
        contact={"name": "田头农技台"},
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,      # 与 allow_origins=["*"] 同时为 True 会被浏览器拒绝
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    # ---- 业务接口 ----
    app.include_router(diagnose.router)
    app.include_router(library.router)
    app.include_router(map_router.router)
    app.include_router(policy.router)
    app.include_router(voice.router)
    # AI 助手：页面内常驻宠物，公开可用（未登录也能聊，只是读不到私有农田数据）
    app.include_router(assistant.router)
    # 快速版：仿微信聊天框。与宠物共用同一条诊断链路，只是把结果压成一条气泡
    app.include_router(chat_router.router)
    # 阶段 7：账号与我的农田。公开接口（诊断/知识库/地图/政策）不受影响，
    # /api/farm/* 由路由自己的依赖强制登录（7.0.6）。
    app.include_router(auth.router)
    app.include_router(farm.router)
    # 模型技能：两个按功能封装的统一接口（病虫害诊断 / 农田生长与 3D）
    app.include_router(skills_router.router)
    # 问诊单交付：只读分享页 / 二维码 / PDF（公开读接口 + 登录后写的接口）
    app.include_router(share.router)

    # ---- 健康检查：一眼看清哪个能力可用、哪个在降级 ----
    @app.get("/api/health", response_model=ApiResponse[dict], tags=["服务状态"],
             summary="服务与各能力可用状态")
    async def health():
        return {"code": 0, "msg": "ok", "data": _health_payload()}

    @app.post("/api/admin/reload", response_model=ApiResponse[dict], tags=["服务状态"],
              summary="清空缓存并重新加载模型与数据",
              description="重新训练模型、重建知识库或修改数据文件后调用，无需重启服务。"
                          "阶段 7 起**仅管理员（id=1）可用** —— 它会清掉所有模型缓存，"
                          "重新加载要几十秒，不该由普通用户触发（7.0.6）。")
    async def reload_caches(_admin: dict = Depends(require_admin)):
        deps.refresh_caches()
        return {"code": 0, "msg": "缓存已清空，下次请求将重新加载", "data": {"reloaded": True}}

    # ---- 静态资源 ----
    # 知识图书库的封面/配图直接指向 data/processed/dataset，不复制一份到前端
    media_dir = path_of("processed_dataset")
    if media_dir.exists():
        app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")
    else:
        logger.warning("归档数据目录不存在，/media 未挂载：%s（请先运行 scripts/01_organize_data.py）",
                       media_dir)

    # 虫害样图（IP102）单独挂一条：它是另一套标签空间的数据集，目录也刻意分开，
    # 不往 /media 那个 271 类病害归档里混。
    pest_media_dir = path_of("pest_dataset") / "images"
    if pest_media_dir.exists():
        app.mount("/media-pest", StaticFiles(directory=str(pest_media_dir)), name="media-pest")
    else:
        logger.warning("虫害数据集不存在，/media-pest 未挂载：%s（虫害知识卡片将没有封面）",
                       pest_media_dir)

    # 阶段 7：病虫害入档的图片（data/farm_images/{user_id}/{field_id}/…）。
    # 挂成静态目录是为了让「历史卡片里的诊断缩略图」能直接 <img src>，
    # 而不是每张图都过一次 base64 接口。注意它**不按 user_id 做访问控制** ——
    # 路径里的 user_id 是可见的，所以这里只放「用户自己上传的田间照片」，
    # 不放任何私密内容。真要严格隔离得走带鉴权的接口，那是另一个量级的事，
    # 本项目（演示性质、单机部署）选择把这条路标注清楚而不是假装它安全。
    farm_images_dir = path_of("farm_images")
    ensure_dir(farm_images_dir)
    app.mount("/farm-images", StaticFiles(directory=str(farm_images_dir)), name="farm-images")

    frontend_dir = ensure_dir(path_of("frontend_dir"))
    ensure_dir(frontend_dir / "audio")
    # html=True 让 "/" 自动映射到 index.html；这个挂载放在最后，因此不会抢走 /api 与 /docs
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    logger.info("服务已装配：host=%s port=%s，前端目录 %s", settings.host, settings.port, frontend_dir)
    return app


def _health_payload() -> dict:
    """汇总各能力的当前状态。任何一项失败都不影响整体返回。"""
    payload: dict = {"status": "ok"}

    # 模型
    try:
        predictor = deps.get_predictor()
        payload["model"] = {
            "available": True,
            "backend": predictor.backend,
            "num_classes": predictor.num_classes,
            "input_size": predictor.input_size,
            "confidence_threshold": predictor.confidence_threshold,
        }
    except Exception as e:  # noqa: BLE001
        payload["model"] = {"available": False, "error": str(e)}
        payload["status"] = "degraded"

    # 语音
    try:
        from src.voice.factory import current_mode

        payload["voice"] = {"mode": current_mode(), **deps.get_voice().describe()}
    except Exception as e:  # noqa: BLE001
        payload["voice"] = {"error": str(e)}

    # 大模型
    try:
        from src.llm.factory import current_mode as llm_mode

        payload["llm"] = {"mode": llm_mode(), **deps.get_llm().describe()}
    except Exception as e:  # noqa: BLE001
        payload["llm"] = {"error": str(e)}

    # RAG
    try:
        payload["rag"] = deps.get_rag().stats()
    except Exception as e:  # noqa: BLE001
        payload["rag"] = {"error": str(e)}

    # 虫害检测
    #
    # 刻意放在**独立的 pest_detector 子对象**里，而不是并进上面的 model：
    # payload["model"] 的 backend / num_classes / input_size 说的是**分类**模型
    # （onnxruntime-cpu / 271 / 224），把 102 类、640 输入的检测模型混进去，
    # 这三个字段立刻就变成自相矛盾的值。宁可多一层嵌套，也不要让字段含义变模糊。
    try:
        detector = deps.get_pest_detector()
        if detector is None:
            payload["pest_detector"] = {
                "available": False,
                "hint": "未找到虫害检测模型，请先运行 scripts/17_train_pest_yolo.py "
                        "与 scripts/19_export_pest_onnx.py",
            }
        else:
            # 属性名照抄 OnnxPestDetector.__init__ 里挂的那几个（conf_threshold /
            # iou_threshold，**不是** confidence_threshold）。曾经这里写成了后者，
            # 于是这一整块直接抛 AttributeError 落进下面的 except，/api/health 对
            # 一个明明装好、也跑得动的检测模型报 available=false —— 而同一时刻
            # /api/diagnose/multimodal 正在用它出检测框。两处自相矛盾。
            payload["pest_detector"] = {
                "available": True,
                "backend": detector.backend,
                "num_classes": detector.num_classes,
                "input_size": detector.input_size,
                "confidence_threshold": detector.conf_threshold,
                "iou_threshold": detector.iou_threshold,
                "model_path": str(detector.onnx_path),
            }
    except Exception as e:  # noqa: BLE001
        payload["pest_detector"] = {"available": False, "error": str(e)}

    # 数据文件
    files = {
        "label_map": path_of("label_map"),
        "splits": path_of("splits_dir") / "train.csv",
        "province_pest": path_of("province_pest"),
        "policy_list": path_of("policy_list"),
        "library": path_of("library_dir") / "library_items.json",
        "onnx": path_of("onnx_dir") / "inference_model.onnx",
        "pest_onnx": path_of("pest_onnx"),
        "pest_yaml": path_of("pest_yaml"),
    }
    payload["data"] = {k: {"exists": Path(v).exists(), "path": str(v)} for k, v in files.items()}

    # 阶段 7：我的农田（7.10 要求 /api/health 增加 farm / weather / farm_llm 三状态）
    try:
        from src.farm.db import applied_version_of, farm_db_path

        path = farm_db_path()
        from src.farm.db import connect

        with connect(path) as conn:
            from src.farm import repo

            payload["farm"] = {
                "available": path.exists(),
                "db": str(path),
                "schema_version": applied_version_of(path),
                "fields": conn.execute("SELECT COUNT(*) AS n FROM fields").fetchone()["n"],
                "harvest_samples": repo.count_harvests(conn),
                "harvest_samples_needed": 30,
            }
    except Exception as e:  # noqa: BLE001
        payload["farm"] = {
            "available": False,
            "error": str(e),
            "hint": "请先运行 python scripts/23_init_farm_db.py 建库",
        }

    try:
        from src.farm import weather as farm_weather

        payload["weather"] = farm_weather.probe()
    except Exception as e:  # noqa: BLE001
        payload["weather"] = {"reachable": False, "detail": str(e)}

    try:
        from src.llm.factory import current_mode as llm_mode

        # 叙述层与大模型同源：没有 Key 时叙述层走模板，字段一致（7.5）
        payload["farm_llm"] = {
            "mode": llm_mode(),
            "narrative_source": "template" if llm_mode() == "mock" else "llm",
        }
    except Exception as e:  # noqa: BLE001
        payload["farm_llm"] = {"error": str(e)}

    if not payload["data"]["province_pest"]["exists"] or not payload["data"]["policy_list"]["exists"]:
        payload["status"] = "degraded"

    return payload


# uvicorn 的工厂入口：uvicorn backend.app.main:app --reload
app = create_app()
