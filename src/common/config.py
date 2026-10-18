"""配置文件（YAML）与环境变量（.env）的统一加载。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.common.paths import CONFIGS_DIR, PROJECT_ROOT


# ---------------------------------------------------------------- YAML

def load_yaml(path: str | os.PathLike[str]) -> dict:
    """读取 YAML；文件不存在时抛 FileNotFoundError（不静默返回空字典）。"""
    p = Path(path)
    if not p.is_absolute():
        p = CONFIGS_DIR / p.name if p.parent == Path(".") else PROJECT_ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在：{p}")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层应为映射：{p}")
    return data


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 覆盖 base；返回新字典，不修改入参。"""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=32)
def load_config(name: str = "default", override: dict | None = None) -> dict:
    """载入 configs/<name>.yaml；name='default' 时直接读全局配置。

    `override` 需为可哈希的调用约定：外部若需合并，请自行调用 deep_merge。
    """
    if name == "default":
        return load_yaml(CONFIGS_DIR / "default.yaml")
    return load_yaml(CONFIGS_DIR / f"{name}.yaml")


def load_training_config(name: str = "train_resnet50") -> dict:
    """训练配置 = default.yaml 深合并 <name>.yaml，保证公共段（runtime 等）始终可用。"""
    return deep_merge(load_config("default"), load_config(name))


def load_crops_config() -> dict:
    return load_config("crops")


def load_dialect_config() -> dict:
    return load_config("dialect")


def load_crop_stages_config() -> dict:
    """阶段 7 的生育期配置（积温阈值 / 各阶段农事提示）。

    与 crops.yaml（47 种作物的词表，供作物名最长前缀匹配用）是**两回事**：
    那个是「文本里出现的作物名要归到哪一类」的词典，这个是「三个作物各自的
    温度基点与阶段积温阈值」。两者不要合并，也不要互相覆盖。
    """
    return load_config("crop_stages")


# ---------------------------------------------------------------- .env

@lru_cache(maxsize=1)
def load_env() -> None:
    """把项目根的 .env 载入进程环境（已存在的真实环境变量优先，不被覆盖）。"""
    env_path = PROJECT_ROOT / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv 未装时退化为手写解析
        if env_path.exists():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
        return

    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)


def env(key: str, default: Any = None, *, cast: str = "str") -> Any:
    """读环境变量并做类型转换；load_env() 会先确保 .env 已载入。"""
    load_env()
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    if cast == "int":
        try:
            return int(raw)
        except ValueError:
            return default
    if cast == "float":
        try:
            return float(raw)
        except ValueError:
            return default
    if cast == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return raw


def env_mode(key: str, *, auto_when: bool, real: str, mock: str = "mock") -> str:
    """解析 `auto|real|mock` 三态开关 —— 语音/大模型/RAG 三处降级决策共用同一逻辑。

    例：env_mode("LLM_PROVIDER", auto_when=bool(env("DEEPSEEK_API_KEY")), real="deepseek")
        -> 有 Key 返回 'deepseek'，无 Key 返回 'mock'；显式写入 'deepseek'/'mock' 时以显式值为准。
    """
    mode = (env(key, "auto") or "auto").strip().lower()
    if mode == "auto":
        return real if auto_when else mock
    return mode
