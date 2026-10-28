"""农药登记索引查询（用药台账的数据源）。

数据源是 `data/pesticide_registry.json` —— 农业农村部农药管理司《农药登记信息》
的离线索引（3437 条 / 294 个防治对象），项目里早就有，不是为台账新拉的。

**这里有一条必须记住的事实**：公开登记数据里有登记证号、有效成分、剂型、毒性、
登记企业、登记作物、防治对象，但**没有安全间隔期**。安全间隔期只印在瓶身标签上，
属于「产品标签」而不属于「登记公告」。所以：

    · 本模块只负责「查得到哪几个药、它们的证号与成分是什么」；
    · 间隔期一律由施药人照标签填写（见 migrations/008 与 src/farm/spray.py）；
    · 任何地方都不要按「剂型/毒性」去推算一个间隔期 —— 那不是估算，是编造，
      农户会照着它提前采收。

2.4 MB 的 JSON 不适合每个请求都读一遍，所以按 (路径, mtime, 大小) 缓存一份扁平索引。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.farm.pesticides")

# 缓存。key 带上 mtime，换了数据文件（重新抓取登记信息）会自动失效。
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "records": [], "meta": {}}

# 一条记录对外暴露的字段。**新增字段要同时改 backend/app/schemas/farm.py 的
# PesticideData**，否则会被 response_model 静默裁掉（这个坑踩过三次）。
FIELDS = ("reg_no", "product", "holder", "form", "toxicity", "ingredient",
          "kind", "crop", "target", "source")


def _registry_path():
    # 走 configs/default.yaml 的 paths.pesticide_registry，不在代码里写死路径
    return path_of("pesticide_registry")


def _split_crops(text: str) -> list[str]:
    """登记作物是「大豆、花生、玉米」这种顿号串。"""
    return [c.strip() for c in (text or "").replace(",", "、").split("、") if c.strip()]


def _build() -> tuple[list[dict], dict]:
    path = _registry_path()
    if not path.exists():
        logger.warning("农药登记索引不存在：%s（药剂下拉会退化为手填）", path)
        return [], {"total": 0, "targets": 0, "notice": ""}

    data = json.loads(path.read_text(encoding="utf-8"))
    targets_raw: dict[str, list[dict]] = data.get("targets") or {}

    # 同一个登记证会出现在多个防治对象下（一个药登记了稻瘟病、纹枯病两条），
    # 这里按证号合并成一条，把防治对象并成 targets 列表 —— 否则下拉里会重复出
    # 三行一模一样的「稻瘟灵」，用户不知道该选哪个。
    by_reg: dict[str, dict] = {}
    for target, items in targets_raw.items():
        for it in items or []:
            reg = (it.get("reg_no") or "").strip()
            if not reg:
                continue
            rec = by_reg.get(reg)
            if rec is None:
                rec = {k: it.get(k, "") or "" for k in FIELDS}
                rec["reg_no"] = reg
                rec["targets"] = []
                rec["crops"] = _split_crops(it.get("crop", ""))
                by_reg[reg] = rec
            if target not in rec["targets"]:
                rec["targets"].append(target)

    records = list(by_reg.values())
    meta = {
        "total": len(records),
        "targets": len(targets_raw),
        "notice": data.get("notice", ""),
        "source_name": data.get("source_name", ""),
        "generated_at": data.get("generated_at", ""),
    }
    return records, meta


def _index() -> list[dict]:
    path = _registry_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None

    with _LOCK:
        if key is not None and _CACHE["key"] == key:
            return _CACHE["records"]
        records, meta = _build()
        _CACHE.update({"key": key, "records": records, "meta": meta})
        logger.info("农药登记索引已载入：%d 条登记证 / %d 个防治对象",
                    meta.get("total", 0), meta.get("targets", 0))
        return records


def stats() -> dict:
    _index()
    return dict(_CACHE["meta"])


def _match(rec: dict, target: str, crop: str, q: str) -> bool:
    if crop:
        # 登记作物可能是一串（「大豆、花生、玉米」），按单项比对
        if crop not in rec.get("crops", []):
            return False
    if target:
        # 「玉米大斑病」与「大斑病」都要能命中：库里的键名长短不一
        hit = any(target in t or t in target for t in rec.get("targets", []))
        if not hit:
            return False
    if q:
        needle = q.strip().lower()
        if not needle:
            return True
        hay = " ".join([
            rec.get("product", ""), rec.get("ingredient", ""),
            rec.get("reg_no", ""), rec.get("target", ""),
            " ".join(rec.get("targets", [])),
        ]).lower()
        if needle not in hay:
            return False
    return True


def search(*, target: str = "", crop: str = "", q: str = "", limit: int = 40) -> list[dict]:
    """按防治对象 / 作物 / 关键词查登记药剂。三个条件都是可选的，同时给就是交集。

    没有 q 又没有 target 时**不返回全库**：上千条一次塞进下拉，等于没有筛选。
    这种情况直接返回空列表，让前端提示「输入药名或病害名再搜」。

    先 strip 再判空：用户清空输入框后浏览器可能留下一个空格（或中文输入法的
    全角空格），按「有值」处理就会把全库塞回下拉，与「没筛选」是同一件事。
    """
    target = (target or "").strip()
    q = (q or "").strip()
    if not (target or q):
        return []
    records = _index()
    out = [r for r in records if _match(r, target, crop.strip(), q)]
    return out[: max(1, min(int(limit), 100))]


def get(reg_no: str) -> dict | None:
    reg = (reg_no or "").strip().upper()
    if not reg:
        return None
    for r in _index():
        if r["reg_no"].upper() == reg:
            return r
    return None


def label(rec: dict) -> str:
    """下拉里显示的一行，例如「稻瘟灵 乳油 · PD20110955 · 低毒」。"""
    bits = [rec.get("product", "")]
    if rec.get("form"):
        bits.append(rec["form"])
    text = " ".join(b for b in bits if b)
    if rec.get("reg_no"):
        text += f" · {rec['reg_no']}"
    if rec.get("toxicity"):
        text += f" · {rec['toxicity']}"
    return text
