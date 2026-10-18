"""任务 1.4 —— 建立标签映射字典：中文名称 ↔ 数字索引 ↔ 病虫害基础信息。

产出 data/label_map.json，它是全项目的**单一事实源**，被四处消费：
    · src/data/dataset.py        训练时 索引 -> 标签
    · src/infer/predictor.py     推理时 索引 -> 中文名（后端 4.2）
    · src/content/library_build.py  知识图书库按 作物/病虫害 组织（4.5）
    · src/llm/fusion.py          把候选病害名作为约束喂给大模型，抑制幻觉（3.3）

结构（jsonc）：
{
  "version": 1,
  "num_classes": 271,
  "num_crops": 47,
  "crop_classes": [{"name":"玉米","category":"粮食作物","num_classes":23,"num_images":230}, ...],
  "index_to_label": {"0": {"index":0,"dir_id":"100","label_cn":"南瓜病毒病", ...}},
  "label_to_index": {"南瓜病毒病": 0, ...},
  "dir_to_index":  {"100": 0, ...},
  "base_info": {"南瓜病毒病": {"pathogen_type":"病毒","condition":"病毒病","search_terms":[...]}}
}

注意 index 是 0..270 的连续整数，不是原始目录号 100..370 —— 见 src/data/split.py 的说明。
"""

from __future__ import annotations

from pathlib import Path

from src.common.io_utils import list_images, write_json
from src.common.logging_utils import setup_logger
from src.common.paths import PROJECT_ROOT, path_of
from src.data.split import load_classes

logger = setup_logger("agri.label_map")

HEALTHY_PATHOGEN = "健康"


def _cover_for(out_dir: Path) -> str | None:
    """挑一张代表图作为知识图书库卡片的封面。"""
    images = list_images(out_dir, recursive=False)
    if not images:
        return None
    return images[0].relative_to(PROJECT_ROOT).as_posix()


def build_label_map() -> dict:
    classes = load_classes()

    index_to_label: dict[str, dict] = {}
    label_to_index: dict[str, int] = {}
    dir_to_index: dict[str, int] = {}
    base_info: dict[str, dict] = {}
    crops: dict[str, dict] = {}

    for c in classes:
        idx = c["index"]
        out_dir = PROJECT_ROOT / c["out_dir"]
        cover = _cover_for(out_dir)

        entry = {
            "index": idx,
            "dir_id": c["dir_id"],
            "label_cn": c["label_cn"],
            "crop": c["crop"],
            "crop_category": c["crop_category"],
            "condition": c["condition"],
            "pathogen_type": c["pathogen_type"],
            "is_healthy": bool(c["is_healthy"]),
            "is_mixed": bool(c["is_mixed"]),
            "num_images": int(c["num_images"]),
            "cover": cover,
            "out_dir": c["out_dir"],
        }
        index_to_label[str(idx)] = entry
        label_to_index[c["label_cn"]] = idx
        dir_to_index[str(c["dir_id"])] = idx

        # 检索用词全部来自已有字段的组合，不做无依据的医学推断
        base_info[c["label_cn"]] = {
            "crop": c["crop"],
            "crop_category": c["crop_category"],
            "condition": c["condition"],
            "pathogen_type": c["pathogen_type"],
            "is_healthy": bool(c["is_healthy"]),
            "is_mixed": bool(c["is_mixed"]),
            "search_terms": sorted(
                {c["crop"], c["condition"], c["pathogen_type"], c["label_cn"]} - {""}
            ),
        }

        crop = crops.setdefault(
            c["crop"],
            {"name": c["crop"], "category": c["crop_category"],
             "num_classes": 0, "num_images": 0, "classes": []},
        )
        crop["num_classes"] += 1
        crop["num_images"] += int(c["num_images"])
        crop["classes"].append(c["label_cn"])

    label_map = {
        "version": 1,
        "source": "PDD271 公开样本（Sample/），类别名来自 label_rename.txt",
        "num_classes": len(classes),
        "num_crops": len(crops),
        "num_images": sum(int(c["num_images"]) for c in classes),
        "healthy_pathogen": HEALTHY_PATHOGEN,
        "crop_classes": sorted(crops.values(), key=lambda x: (-x["num_images"], x["name"])),
        "index_to_label": index_to_label,
        "label_to_index": label_to_index,
        "dir_to_index": dir_to_index,
        "base_info": base_info,
    }
    return label_map


def build_and_save() -> dict:
    label_map = build_label_map()
    out = path_of("label_map")
    write_json(out, label_map)
    _report(label_map, out)
    return label_map


def _report(lm: dict, out: Path) -> None:
    logger.info("已写出标签映射：%s", out)
    logger.info("类别数 %d / 作物大类 %d / 图片总数 %d",
                lm["num_classes"], lm["num_crops"], lm["num_images"])

    indices = sorted(int(k) for k in lm["index_to_label"])
    expected = list(range(len(indices)))
    if indices != expected:
        logger.error("索引不是 0..%d 的连续整数，训练会出错！", len(indices) - 1)
    else:
        logger.info("索引连续性校验通过：0..%d ✓", indices[-1])

    healthy = [v["label_cn"] for v in lm["index_to_label"].values() if v["is_healthy"]]
    mixed = [v["label_cn"] for v in lm["index_to_label"].values() if v["is_mixed"]]
    logger.info("健康类样本 %d 个：%s", len(healthy), "、".join(healthy))
    logger.info("混合型病害 %d 个：%s", len(mixed), "、".join(mixed))

    by_pathogen: dict[str, int] = {}
    for v in lm["index_to_label"].values():
        by_pathogen[v["pathogen_type"]] = by_pathogen.get(v["pathogen_type"], 0) + 1
    logger.info("按病原类型分布：%s",
                "，".join(f"{k} {v}" for k, v in sorted(by_pathogen.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    build_and_save()
