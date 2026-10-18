"""文本向量化 —— sentence-transformers 封装。

模型默认 shibing624/text2vec-base-chinese（中文语义相似度，约 400MB），
下载后缓存在 models/pretrained/hf/，跟着项目走，重装环境不用重下。

懒加载：只有真正用到 RAG 时才 import sentence_transformers 并加载模型 ——
否则后端每次启动都要多花十几秒加载一个可能根本用不到的模型。
"""

from __future__ import annotations

import os
from functools import lru_cache

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.rag.embed")


@lru_cache(maxsize=4)
def get_encoder(model_name: str | None = None):
    """返回一个可调用的编码器（list[str] -> list[list[float]]）。"""
    model_name = model_name or str(env("RAG_EMBEDDING_MODEL", "shibing624/text2vec-base-chinese"))

    cache = path_of("pretrained_dir") / "hf"
    cache.mkdir(parents=True, exist_ok=True)

    # HuggingFace 官方站在国内经常连不上（实测 HEAD 请求超时 WinError 10060），
    # 而向量模型动辄几百 MB，没有镜像基本下不动。因此默认走 hf-mirror.com，
    # 境外部署可用 .env 里的 HF_ENDPOINT 覆盖回官方站。
    endpoint = env("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ["HF_ENDPOINT"] = str(endpoint)

    # HuggingFace 新的 Xet 存储后端域名（cas-bridge.xethub.hf.co）在国内不通，
    # 开着它会在超时里反复重试（实测只到 "Trying to resume download..." 就卡住）。
    # 禁用后走普通 CDN，镜像站可以正常代理。
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    # Windows 默认不支持符号链接，huggingface_hub 会打印一大段告警；用副本即可，关掉噪音
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    os.environ.setdefault("HF_HOME", str(cache))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(cache / "sentence_transformers"))

    from sentence_transformers import SentenceTransformer

    logger.info("加载文本向量模型：%s", model_name)
    logger.info("  下载源 %s（可用 .env 的 HF_ENDPOINT 覆盖）", endpoint)
    logger.info("  缓存目录 %s；首次使用需下载约 400MB，请耐心等待……", cache)

    try:
        model = SentenceTransformer(model_name, cache_folder=str(cache / "sentence_transformers"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"向量模型加载失败：{e}\n"
            f"　常见原因：网络无法访问 {endpoint}。\n"
            "　可尝试：① 在 .env 里换一个 HF_ENDPOINT（如 https://huggingface.co）；\n"
            "　　　　　② 手动下载模型放到 models/pretrained/hf/ 后重试；\n"
            "　　　　　③ 直接跳过 RAG —— 置 RAG_ENABLED=off，诊断功能不受影响。"
        ) from e

    # sentence-transformers 6.x 把 get_sentence_embedding_dimension 改名为 get_embedding_dimension，
    # 这里优先用新名字，老版本回退，避免告警噪音
    get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    dim = get_dim()
    logger.info("向量模型就绪：维度 %d", dim)

    def encode(texts: list[str]) -> list[list[float]]:
        return model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False, batch_size=32,
        ).tolist()

    encode.dimension = dim          # type: ignore[attr-defined]
    return encode


def embedding_dimension() -> int:
    return int(getattr(get_encoder(), "dimension", 0))
