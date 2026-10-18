"""阶段 7 / 7.3.1 —— 生育期配置的读取与校验。

配置本体在 configs/crop_stages.yaml（阈值怎么定出来的，那个文件的头部注释里写了）。
这里做三件事：读、**校验**、给出统一的查询接口。

为什么要校验而不是直接用：配置是手写的 YAML，一个 `Tu <= Topt` 的笔误会让
7.3.2 的四段式**除零**，而那行代码在积温累加的循环里 —— 表现为整个生长引擎崩掉，
却很难一眼看出是配置写错了。在这里一次性拦住，报错直接说清哪个作物哪个字段。
"""

from __future__ import annotations

from functools import lru_cache

from src.common.config import load_crop_stages_config
from src.common.logging_utils import setup_logger

logger = setup_logger("agri.farm.stages")

REQUIRED_KEYS = ("source", "Tbase", "Topt", "Tu", "maturity_gdd", "stages")


@lru_cache(maxsize=1)
def all_stages() -> dict:
    """全部作物配置（带校验，结果缓存）。"""
    raw = load_crop_stages_config()
    if not raw:
        raise ValueError("configs/crop_stages.yaml 是空的")

    for crop, cfg in raw.items():
        _validate(crop, cfg)
    return raw


def _validate(crop: str, cfg: dict) -> None:
    """把配置错误在加载时一次性报清楚。"""
    if not isinstance(cfg, dict):
        raise ValueError(f"作物 {crop} 的配置应为映射，实际是 {type(cfg).__name__}")

    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ValueError(f"作物 {crop} 的配置缺字段：{'、'.join(missing)}")

    tbase, topt, tu = float(cfg["Tbase"]), float(cfg["Topt"]), float(cfg["Tu"])
    if not (tbase < topt < tu):
        # Tu == Topt 时四段式的第三段会除以 0；Tbase >= Topt 则第二段没有意义
        raise ValueError(
            f"作物 {crop} 的温度基点不合法：需要 Tbase < Topt < Tu，"
            f"实际 {tbase} / {topt} / {tu}"
        )

    stages = cfg["stages"]
    if not isinstance(stages, list) or len(stages) < 2:
        raise ValueError(f"作物 {crop} 至少需要 2 个生育阶段")

    last = 0.0
    names: set[str] = set()
    for st in stages:
        for key in ("name", "gdd", "actions"):
            if key not in st:
                raise ValueError(f"作物 {crop} 的阶段缺字段 {key}：{st}")
        if st["name"] in names:
            raise ValueError(f"作物 {crop} 有重名的阶段：{st['name']}")
        names.add(st["name"])
        # 阶段阈值必须**严格递增**：相等或倒序会让「取最后一个 gdd <= 当前积温」
        # 的结果变成随机（取决于遍历顺序），阶段就会跳来跳去。
        if float(st["gdd"]) <= last:
            raise ValueError(
                f"作物 {crop} 的阶段阈值必须严格递增，"
                f"「{st['name']}」{st['gdd']} 不大于前一个阶段"
            )
        last = float(st["gdd"])

    if abs(float(cfg["maturity_gdd"]) - last) > 1e-6:
        raise ValueError(
            f"作物 {crop} 的 maturity_gdd({cfg['maturity_gdd']}) 应等于最后一个阶段的"
            f"阈值({last}) —— 否则「进度」与「阶段」会互相矛盾"
        )

    # 推荐施肥量（可选字段，但写了就必须是三个正数）：
    # 它是折算施肥影响的**分母**，为 0 会直接除零；负数的含义更是无从解释。
    # 与温度基点同样的道理：配置写错要在加载时炸掉，不要等到某个用户施肥时才崩。
    nutrition = cfg.get("nutrition")
    if nutrition is not None:
        if not isinstance(nutrition, dict):
            raise ValueError(f"作物 {crop} 的 nutrition 应为映射，实际是 {type(nutrition).__name__}")
        missing = [k for k in ("n", "p2o5", "k2o") if k not in nutrition]
        if missing:
            raise ValueError(f"作物 {crop} 的 nutrition 缺字段：{'、'.join(missing)}")
        for key in ("n", "p2o5", "k2o"):
            if float(nutrition[key]) <= 0:
                raise ValueError(
                    f"作物 {crop} 的 nutrition.{key} 必须大于 0，实际 {nutrition[key]}"
                )


def supported_crops() -> tuple[str, ...]:
    """支持的作物名。7.17 明确只做这三个，别悄悄加。"""
    return tuple(all_stages().keys())


def is_supported(crop: str) -> bool:
    return (crop or "").strip() in all_stages()


def crop_config(crop: str) -> dict:
    """取某作物的配置；不支持的作物抛 ValueError（中文说明，路由层转 400）。"""
    name = (crop or "").strip()
    data = all_stages()
    if name not in data:
        raise ValueError(f"暂不支持作物「{crop}」，可选：{'、'.join(data)}")
    return data[name]


def stage_names(crop: str) -> list[str]:
    return [s["name"] for s in crop_config(crop)["stages"]]


def maturity_gdd(crop: str) -> float:
    return float(crop_config(crop)["maturity_gdd"])


def stage_actions(crop: str, stage_name: str) -> list[str]:
    """某阶段该干的农活（7.4.1「积温跨阶段阈值 → 该阶段 actions」要用）。"""
    for st in crop_config(crop)["stages"]:
        if st["name"] == stage_name:
            return list(st.get("actions") or [])
    return []
