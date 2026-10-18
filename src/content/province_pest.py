"""任务 1.5 —— 整理全国各省病虫害 TOP3 数据，按省份、季节维度分类。

⚠ 数据性质说明（重要）
    官方公开资料（全国农技中心《2026 年全国农作物病虫害发生趋势预报》）只给出
    **全国维度**的趋势，没有「逐省 × 逐季 × TOP3」的明细表。因此逐省部分由
    「各省主要作物 + 病原类型季节性规律」细化得到，输出的 JSON 里带 demo_data 与
    disclaimer 字段留档。拿到真实逐省数据后，替换 data/province_pest_top3.json 即可，
    接口与前端无需改动。

生成规则（可复核、非随机）：
    1. 每个省份列出其主要作物（PROVINCE_CROPS，依据实际种植分布整理）
    2. 候选病害 = 这些作物在 271 类数据集里真实存在的病害类别（保证点击地图能跳到知识库详情）
    3. 按病原类型的季节性亲和度打分排序（真菌/细菌夏季高发、病毒春夏、生理性与药害与季节相关）
    4. 同分时按该类别的样本量降序（样本多≈记录充分≈更常见），最后按名称稳定排序
    5. 取前 3 名，标注高发时段与风险等级

省份名严格使用 GeoJSON（frontend/assets/geo/china.json）里的官方全称，
否则前端 ECharts 上色与弹窗匹配不上。
"""

from __future__ import annotations

from src.common.io_utils import write_json
from src.common.logging_utils import setup_logger
from src.common.paths import path_of

logger = setup_logger("agri.province_pest")

# 省份 -> 行政编码（取自 GeoJSON properties.adcode，用于前端定位）
PROVINCE_ADCODE: dict[str, str] = {
    "北京市": "110000", "天津市": "120000", "河北省": "130000", "山西省": "140000",
    "内蒙古自治区": "150000", "辽宁省": "210000", "吉林省": "220000", "黑龙江省": "230000",
    "上海市": "310000", "江苏省": "320000", "浙江省": "330000", "安徽省": "340000",
    "福建省": "350000", "江西省": "360000", "山东省": "370000", "河南省": "410000",
    "湖北省": "420000", "湖南省": "430000", "广东省": "440000", "广西壮族自治区": "450000",
    "海南省": "460000", "重庆市": "500000", "四川省": "510000", "贵州省": "520000",
    "云南省": "530000", "西藏自治区": "540000", "陕西省": "610000", "甘肃省": "620000",
    "青海省": "630000", "宁夏回族自治区": "640000", "新疆维吾尔自治区": "650000",
    "台湾省": "710000", "香港特别行政区": "810000", "澳门特别行政区": "820000",
}

# 省份 -> 主要作物（须为本数据集 47 个作物大类中的名称，否则候选病害为空）
PROVINCE_CROPS: dict[str, list[str]] = {
    "北京市": ["大白菜", "番茄", "黄瓜", "苹果"],
    "天津市": ["大白菜", "黄瓜", "番茄", "辣椒"],
    "河北省": ["小麦", "玉米", "大白菜", "苹果", "棉花"],
    "山西省": ["玉米", "小麦", "谷子", "苹果", "马铃薯"],
    "内蒙古自治区": ["马铃薯", "向日葵", "玉米", "小麦", "谷子"],
    "辽宁省": ["玉米", "大豆", "花生", "苹果", "马铃薯"],
    "吉林省": ["玉米", "大豆", "马铃薯", "高粱"],
    "黑龙江省": ["大豆", "玉米", "马铃薯", "小麦"],
    "上海市": ["大白菜", "菠菜", "黄瓜", "番茄"],
    "江苏省": ["小麦", "玉米", "大白菜", "黄瓜", "番茄"],
    "浙江省": ["番茄", "黄瓜", "辣椒", "草莓", "桑树"],
    "安徽省": ["小麦", "玉米", "大豆", "辣椒", "大白菜"],
    "福建省": ["甘薯", "花生", "辣椒", "香椿", "蓝莓"],
    "江西省": ["辣椒", "甘薯", "花生", "桑树", "芝麻"],
    "山东省": ["小麦", "玉米", "花生", "苹果", "大白菜", "马铃薯"],
    "河南省": ["小麦", "玉米", "花生", "大豆", "辣椒"],
    "湖北省": ["小麦", "玉米", "棉花", "芝麻", "辣椒"],
    "湖南省": ["辣椒", "甘薯", "玉米", "桑树", "芝麻"],
    "广东省": ["甘薯", "花生", "辣椒", "豇豆", "桑树"],
    "广西壮族自治区": ["甘薯", "花生", "豇豆", "桑树", "萝卜"],
    "海南省": ["甘薯", "豇豆", "辣椒", "甜瓜", "萝卜"],
    "重庆市": ["辣椒", "甘薯", "玉米", "生姜", "萝卜"],
    "四川省": ["辣椒", "玉米", "甘薯", "生姜", "萝卜"],
    "贵州省": ["辣椒", "玉米", "马铃薯", "生姜", "甘薯"],
    "云南省": ["辣椒", "玉米", "马铃薯", "烟草", "生姜"],
    "西藏自治区": ["小麦", "马铃薯", "甘蓝", "萝卜"],
    "陕西省": ["小麦", "玉米", "苹果", "猕猴桃", "辣椒"],
    "甘肃省": ["马铃薯", "小麦", "玉米", "苹果", "向日葵"],
    "青海省": ["小麦", "马铃薯", "甘蓝", "萝卜"],
    "宁夏回族自治区": ["小麦", "玉米", "马铃薯", "向日葵"],
    "新疆维吾尔自治区": ["棉花", "番茄", "小麦", "向日葵", "甜瓜", "葡萄", "枣树"],
    "台湾省": ["甘薯", "花生", "豇豆", "甜瓜", "蓝莓"],
    "香港特别行政区": ["大白菜", "菠菜", "黄瓜", "番茄"],
    "澳门特别行政区": ["大白菜", "菠菜", "番茄", "黄瓜"],
}

SEASONS: list[dict] = [
    {"key": "spring", "name": "春"},
    {"key": "summer", "name": "夏"},
    {"key": "autumn", "name": "秋"},
    {"key": "winter", "name": "冬"},
]
SEASON_MONTHS = {"spring": "3～5月", "summer": "6～8月", "autumn": "9～11月", "winter": "12～2月"}

# 病原类型 × 季节的亲和度（分值越高越容易在该季节流行）
SEASON_AFFINITY: dict[str, dict[str, int]] = {
    "spring": {"真菌": 3, "病毒": 2, "细菌": 1, "生理性": 2, "药害肥害": 2, "其他": 1},
    "summer": {"真菌": 3, "细菌": 3, "病毒": 2, "生理性": 2, "药害肥害": 3, "其他": 1},
    "autumn": {"真菌": 3, "细菌": 2, "病毒": 1, "生理性": 1, "药害肥害": 1, "其他": 1},
    "winter": {"生理性": 3, "真菌": 2, "细菌": 1, "病毒": 1, "药害肥害": 1, "其他": 1},
}

# 病原类型 -> 高发时段补充说明
PEAK_HINT = {
    "真菌": "高湿连阴雨后 5～10 天为发病高峰",
    "细菌": "高温高湿、暴雨或灌溉后伤口易侵染",
    "病毒": "介体（蚜虫/粉虱）迁飞高峰期同步高发",
    "生理性": "苗期与逆境（低温/干旱/缺素）阶段集中显现",
    "药害肥害": "施药施肥后 3～7 天集中显现",
    "其他": "田间管理关键期需重点巡查",
}

DISCLAIMER = (
    "全国维度趋势取自全国农技中心《2026 年全国农作物病虫害发生趋势预报》，"
    "逐省分布按各省主要作物与病原类型的季节性规律细化。"
)


def _candidates_by_crop(label_map: dict) -> dict[str, list[dict]]:
    """作物 -> 该作物在本数据集中的病害类别列表（排除健康类样本）。"""
    by_crop: dict[str, list[dict]] = {}
    for entry in label_map["index_to_label"].values():
        if entry["is_healthy"]:
            continue
        by_crop.setdefault(entry["crop"], []).append(entry)
    return by_crop


def _rank(candidates: list[tuple[dict, int]], season: str) -> list[dict]:
    """按季节亲和度 + 作物重要度 + 样本量排序。candidates 为 (类别, 作物在省内列表中的序号)。"""
    affinity = SEASON_AFFINITY[season]
    return [
        e
        for e, _ in sorted(
            candidates,
            key=lambda pair: (
                -affinity.get(pair[0]["pathogen_type"], 1),   # ① 季节亲和度
                pair[1],                                        # ② 省内越主要的作物越靠前
                -min(pair[0]["num_images"], 300),               # ③ 样本多≈记录充分≈更常见
                pair[0]["label_cn"],                            # ④ 稳定排序
            ),
        )
    ]


def _top3_diverse(ranked: list[dict], limit: int = 3) -> list[dict]:
    """取前 N 名，并尽量让 TOP3 覆盖不同作物。

    地图弹窗里给出同一作物的三个病害对农户没用；优先每作物取一条，
    作物数不足时才允许同作物重复补位。
    """
    picked: list[dict] = []
    seen_crops: set[str] = set()

    for entry in ranked:                       # 第一轮：每作物最多一条
        if entry["crop"] not in seen_crops:
            picked.append(entry)
            seen_crops.add(entry["crop"])
        if len(picked) == limit:
            return picked

    for entry in ranked:                       # 第二轮：作物不够时补位
        if entry not in picked:
            picked.append(entry)
        if len(picked) == limit:
            break
    return picked


def build_province_pest(label_map: dict) -> dict:
    by_crop = _candidates_by_crop(label_map)
    missing_crops = {
        crop for crops in PROVINCE_CROPS.values() for crop in crops if crop not in by_crop
    }
    if missing_crops:
        logger.warning("以下作物在数据集中没有病害类别，将只贡献健康样本：%s", sorted(missing_crops))

    provinces = []
    for prov, crops in PROVINCE_CROPS.items():
        # (类别, 该作物在省内作物清单里的序号) —— 序号用于给省内主导作物加权
        candidates = [
            (e, idx) for idx, crop in enumerate(crops) for e in by_crop.get(crop, [])
        ]
        seasons: dict[str, list[dict]] = {}

        for season in SEASON_AFFINITY:
            ranked = _rank(candidates, season)
            top3 = []
            for rank_i, entry in enumerate(_top3_diverse(ranked), start=1):
                hint = PEAK_HINT.get(entry["pathogen_type"], PEAK_HINT["其他"])
                top3.append(
                    {
                        "rank": rank_i,
                        "pest": entry["label_cn"],
                        "crop": entry["crop"],
                        "pathogen_type": entry["pathogen_type"],
                        "risk": "高" if rank_i == 1 else "中",
                        "peak_window": SEASON_MONTHS[season],
                        "note": hint,
                        "library_id": f"lib_pest_{entry['index']}",   # 供地图点击跳转知识库详情
                    }
                )
            seasons[season] = top3

        provinces.append(
            {
                "name": prov,
                "adcode": PROVINCE_ADCODE.get(prov, ""),
                "short_name": prov.replace("省", "").replace("市", "")
                                     .replace("自治区", "").replace("特别行政区", "")
                                     .replace("壮族", "").replace("回族", "").replace("维吾尔", ""),
                "crops": crops,
                # 病害种类基数：本省主要作物在本数据集中涉及的全部病害类别数（健康类不计）。
                # 这是本文件里**真正有地域区分度**的指标（实测 34 个省份在 21~74 之间）。
                # 曾尝试用「TOP3 病原季节亲和度之和」当地图分级依据，但因为它由排序规则本身
                # 决定（总是优先高亲和度病原），实测 33/34 个省都是 8~9，完全退化；
                # 「首要病害病原类型」同理 —— 真菌病占 250 类里的 133 类，春夏秋三季全国都是真菌。
                # 因此地图上色改用这个基数，含义是「该省需要重点防范的病虫害种类规模」。
                "threat_scope": len(candidates),
                "seasons": seasons,
            }
        )

    logger.info("生成 %d 个省份 × %d 个季节的 TOP3",
                len(provinces), len(SEASON_AFFINITY))
    return {
        "version": 1,
        "demo_data": True,
        "disclaimer": DISCLAIMER,
        "source": "全国农技中心《2026 年全国农作物病虫害发生趋势预报》(全国维度) + 各省作物分布推导",
        "generated_from": "data/label_map.json（病害名称与知识库条目一一对应）",
        "seasons": SEASONS,
        "provinces": provinces,
    }


def build_and_save(label_map: dict | None = None) -> dict:
    from src.common.io_utils import read_json
    from src.content.frontend_bundle import build_home_preload

    if label_map is None:
        label_map = read_json(path_of("label_map"))
    data = build_province_pest(label_map)
    out = path_of("province_pest")
    write_json(out, data)
    _report(data, out)
    # 顺手刷新前端的本地预加载快照，省得"改了数据忘了重新打包"，首页显示的还是旧省份。
    build_home_preload(data)
    return data


def _report(data: dict, out) -> None:
    logger.info("已写出各省 TOP3 数据：%s（demo_data=%s）", out, data["demo_data"])
    empty = [p["name"] for p in data["provinces"] if not p["seasons"]["spring"]]
    if empty:
        logger.warning("以下省份春季无可选病害（作物未覆盖）：%s", empty)

    sample = data["provinces"][15]        # 河南省
    logger.info("示例 —— %s（%s）春季 TOP3（病害种类基数 %d）：",
                sample["name"], "、".join(sample["crops"]), sample["threat_scope"])
    for item in sample["seasons"]["spring"]:
        logger.info("  %d. %s（%s，风险%s，%s）",
                    item["rank"], item["pest"], item["crop"], item["risk"], item["peak_window"])

    scopes = sorted(p["threat_scope"] for p in data["provinces"])
    logger.info("病害种类基数分布（地图上色依据）：min=%d max=%d 中位=%d",
                scopes[0], scopes[-1], scopes[len(scopes) // 2])
    logger.info("  基数最高：%s", "、".join(
        f"{p['name']}({p['threat_scope']})"
        for p in sorted(data["provinces"], key=lambda x: -x["threat_scope"])[:5]))
    logger.info("  基数最低：%s", "、".join(
        f"{p['name']}({p['threat_scope']})"
        for p in sorted(data["provinces"], key=lambda x: x["threat_scope"])[:5]))


if __name__ == "__main__":
    build_and_save()
