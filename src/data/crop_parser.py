"""作物大类解析 —— 把数据集的类别中文名切成「作物大类 + 病害描述」。

为什么不用正则/首字符切分：类别名里有大量互相包含的作物名
    「大白菜」vs 白菜   「甘薯」/「紫薯」/「马铃薯」   「桃树」vs「油桃」vs「樱桃」vs「猕猴桃」
按首字符或短前缀切会把「大白菜霜霉病」切成「大」+「白菜霜霉病」，
把「猕猴桃叶斑病」切到「桃」上（若词表里存在「桃」）。

因此采用【闭合词表 + 按长度降序的最长前缀匹配】：
    configs/crops.yaml 显式列出 47 个作物名，按名字长度降序排序后逐一 startswith，
    命中即取，剩余部分即病害描述。
该词表已对 Sample/label_rename.txt 全部 271 个类别名实测，覆盖率 100%
（0 个未命中、0 个闲置作物）。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from functools import lru_cache

from src.common.config import load_crops_config

UNKNOWN_CROP = "未知"
DEFAULT_PATHOGEN = "其他"


@dataclass(frozen=True)
class ParsedLabel:
    """一个类别中文名的结构化解析结果。"""

    label_cn: str          # 原始中文类别名，如「大白菜霜霉病」
    dir_id: str            # 原始数字目录号，如「115」
    crop: str              # 作物大类，如「大白菜」
    crop_category: str      # 作物类别，如「蔬菜」
    condition: str         # 病害/状态描述，如「霜霉病」
    is_healthy: bool       # 是否为健康样本
    is_mixed: bool         # 是否为「A与B混合」型复合病害
    pathogen_type: str     # 病原/病因推断：病毒/细菌/真菌/生理性/药害肥害/健康/其他

    def as_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=1)
def _vocab() -> tuple[list[tuple[str, str]], list[str], list[tuple[str, tuple[str, ...]]]]:
    """载入词表，返回 (按长度降序的(作物,类别)列表, 健康标记, 病原规则)。"""
    cfg = load_crops_config()

    crops = [(c["name"], c.get("category", "其他")) for c in cfg.get("crops", [])]
    crops.sort(key=lambda item: len(item[0]), reverse=True)   # 最长优先，保证包含关系正确

    healthy = list(cfg.get("healthy_markers", ["健康"]))

    rules = [(r["type"], tuple(r.get("keywords", []))) for r in cfg.get("pathogen_rules", [])]

    return crops, healthy, rules


def crop_names() -> list[str]:
    """全部作物大类名（供前端筛选栏 / 知识库分组使用）。"""
    return [name for name, _ in _vocab()[0]]


def crop_categories() -> dict[str, str]:
    """作物名 -> 类别（粮食作物/经济作物/蔬菜/果树）。"""
    return {name: cat for name, cat in _vocab()[0]}


def _match_crop(label_cn: str) -> tuple[str, str]:
    crops, _, _ = _vocab()
    for name, category in crops:      # 已按长度降序
        if label_cn.startswith(name):
            return name, category
    return UNKNOWN_CROP, "其他"


def _infer_pathogen(label_cn: str) -> str:
    _, _, rules = _vocab()
    for ptype, keywords in rules:
        if any(kw in label_cn for kw in keywords):
            return ptype
    return DEFAULT_PATHOGEN


def parse_label(label_cn: str, dir_id: str | int = "") -> ParsedLabel:
    """解析单个类别名。"""
    label_cn = label_cn.strip()
    if not label_cn:
        raise ValueError("类别名为空")

    crop, category = _match_crop(label_cn)
    condition = label_cn[len(crop):] if crop != UNKNOWN_CROP else label_cn

    _, healthy_markers, _ = _vocab()
    # 源数据含错别字：键康/健株/键叶 —— 一并按健康态处理，避免误判成病害类别
    is_healthy = any(m in label_cn for m in healthy_markers)

    return ParsedLabel(
        label_cn=label_cn,
        dir_id=str(dir_id),
        crop=crop,
        crop_category=category,
        condition=condition,
        is_healthy=is_healthy,
        is_mixed="混合" in label_cn,
        pathogen_type="健康" if is_healthy else _infer_pathogen(label_cn),
    )


def parse_many(pairs: list[tuple[str, str | int]]) -> tuple[list[ParsedLabel], list[ParsedLabel]]:
    """批量解析。返回 (解析成功列表, 作物未命中列表)。

    未命中的条目不会静默丢弃 —— 调用方需把它们写入 outputs/reports/unparsed_crops.txt。
    """
    ok: list[ParsedLabel] = []
    unparsed: list[ParsedLabel] = []
    for label_cn, dir_id in pairs:
        item = parse_label(label_cn, dir_id)
        (unparsed if item.crop == UNKNOWN_CROP else ok).append(item)
    return ok, unparsed


if __name__ == "__main__":   # 自检：跑通全部 271 个类别名
    import io
    from src.common.paths import path_of

    rename_file = path_of("label_rename")
    rows = [
        line.strip().rsplit(" ", 1)
        for line in io.open(rename_file, encoding="utf-8-sig")
        if line.strip()
    ]
    ok, bad = parse_many([(n, d) for n, d in rows])
    crops = {p.crop for p in ok}
    print(f"解析 {len(ok) + len(bad)} 个类别，未命中作物 {len(bad)} 个，作物大类 {len(crops)} 个")
    if bad:
        for b in bad:
            print("  未命中:", b.label_cn, b.dir_id)
    else:
        print("全部命中，覆盖校验通过")
