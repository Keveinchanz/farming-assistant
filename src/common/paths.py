"""路径与配置的统一入口。

项目根目录 = 本文件上溯三级（src/common/paths.py -> src/common -> src -> 项目根）。
所有配置里的相对路径都相对项目根解析，因此脚本从任何工作目录运行都一致。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------- 项目根

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
CONFIGS_DIR: Path = PROJECT_ROOT / "configs"


def project_path(*parts: str | os.PathLike[str]) -> Path:
    """相对项目根拼出绝对路径。"""
    return PROJECT_ROOT.joinpath(*[os.fspath(p) for p in parts])


def resolve(path_like: str | os.PathLike[str] | None) -> Path | None:
    """把配置里的路径解析成绝对路径；已是绝对路径则原样返回。"""
    if path_like is None:
        return None
    p = Path(path_like)
    return p if p.is_absolute() else PROJECT_ROOT / p


# ---------------------------------------------------------------- 常用目录

def raw_dataset_dir() -> Path:
    return project_path("Sample")


def data_dir() -> Path:
    return project_path("data")


def outputs_dir() -> Path:
    return project_path("outputs")


def models_dir() -> Path:
    return project_path("models")


def knowledge_dir() -> Path:
    return project_path("knowledge")


def frontend_dir() -> Path:
    return project_path("frontend")


# ---------------------------------------------------------------- 缓存

@lru_cache(maxsize=1)
def default_config() -> dict:
    """读取 configs/default.yaml（带缓存）。"""
    from src.common.config import load_yaml

    return load_yaml(CONFIGS_DIR / "default.yaml")


def path_of(key: str) -> Path:
    """按 configs/default.yaml 的 paths.<key> 取绝对路径。

    例：path_of("label_map") -> D:\\code file\\insert\\data\\label_map.json
    """
    cfg = default_config().get("paths", {})
    if key not in cfg:
        raise KeyError(f"configs/default.yaml 的 paths 段里没有 {key!r}；" f"现有键：{sorted(cfg)}")
    resolved = resolve(cfg[key])
    assert resolved is not None
    return resolved
