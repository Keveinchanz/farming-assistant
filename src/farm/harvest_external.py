"""阶段 7 / 7.3.4（外部数据）—— 用公开物候数据集造训练样本的纯逻辑。

这个模块**不联网、不读数据库、不写文件**，只做三件事，每一件都能在单元测试里
用几行合成数据验完：

    1. 读 ChinaRiceCalendar 的 xlsx（用标准库，不引入 openpyxl）
    2. 把「某县某年某稻型的一季」摊成若干条**训练样本**（一个生长季多个观察截止日）
    3. 切分与上线闸门

## 为什么标签是「剩余天数」，不是「全生育期天数」

`predict_harvest` 把模型输出与 `_gdd_interval` 的返回值放进**同一个列表**取 min/max
（见 harvest_model.py 的 `candidates`），也就是说它把模型输出当成
**「从观察日到成熟还有几天」**。所以训练标签必须同口径：

    y = maturity_doy - cutoff_doy        # 剩余天数

`scripts/24_train_harvest_model.py` 存的是 `days_between(sow_date, harvest_date)`
（全生育期总天数），而它的特征又是在**标记收获那天**算的 ——
`days_since_sow ≈ actual_days`，模型只会学成一个恒等映射，真上线会把收获日
系统性地推晚。这是**既有隐患**，本次不改那一列的含义，只在 24 号脚本里写明
它造不出本模块要的标签，并给它加了一道「不覆盖外部模型」的保护。

## 为什么一个生长季要摊成多条样本

只有「收获当天」一条的话，模型学不到「进度越靠后 → 剩余天数越小」这条映射，
而那正是线上唯一会问它的问题。生长季里每约 15 天取一个截止日，问一次
「今天看这株稻子，还有几天熟」—— 这不是增广，这就是线上那张映射表本身。

## 一处口径差，必须显式记下来

数据集给的是**移栽日**（transplant），本应用 `field["sow_date"]` 的语义是**播种**。
本模块把移栽日当作「作物起点」使用，并在 provenance 里写
`season_start_basis="transplant"`。**不许**凭空减一个「育秧天数」把它折算成播种日
—— 那是编数据。农户若填的是育秧播种日，两者会有一个系统性的偏移，
这正是必须过闸门、且闸门必须用「留出湖北」这一档的原因。
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from src.farm import gdd as gdd_mod
from src.farm import harvest_model
from src.farm.stages import crop_config, maturity_gdd

# 本模块只处理水稻：ChinaRiceCalendar 就是水稻数据集，而 CROP_CODES 只认
# 水稻/玉米/油菜（那张表被单元测试锁着，不能为了这个数据集新增作物编码）。
CROP = "水稻"

RICE_TYPES = ("早稻", "中稻", "晚稻")
CROP_NAME_FOR_RICE_TYPE = "水稻"      # 三种稻型在本应用里都是「水稻」

# 一个生长季里取多少个观察截止日。15 天是「够密」与「别把同一季的样本撑成
# 几千条近似重复」之间的折中；末尾再补成熟前 10 / 5 天各一个，
# 那两档是线上最关心的区间（越接近收获，预测越要准）。
CUTOFF_STEP = 15
CUTOFF_TAIL_OFFSETS = (10, 5)

# 标签小于这个天数的样本丢掉：那时作物已经完熟或即将完熟，
# 线上走的是 `growth["matured"]` 那条分支，**根本不会调用模型**。
MIN_REMAINING_DAYS = 1

# `predict_harvest` 最多往回看 30 天（build_features 的 temps[-30:]）；
# `_gdd_interval` 看最近 14 天。落盘时只存这 30 天，够在离线评估里
# 原样驱动一次真正的 predict_harvest。
TAIL_DAYS = 30

# 季节内天气覆盖率下限：从起点到这个截止日，逐日气温至少要有这么多天。
# 缺太多就不该造样本 —— 补 0 或补均值都是编数据，直接丢掉并计数才是对的。
MIN_SEASON_COVERAGE = 0.6
MIN_SEASON_DAYS = 30


# ==================================================================== XLSX 读取
# 只实现这个小数据集用到的那一小块 OOXML：workbook → rels → sheet、共享字符串、
# 数字/共享串/内联串三种单元格。不引入 openpyxl（项目依赖已经不少了，
# 而这里要读的只是一张规整的表）。

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_COL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def column_index(ref: str) -> int:
    """'A1' → 0，'AC12' → 28。表里就是这种 A1 引用。"""
    m = _COL_RE.match(ref or "")
    if not m:
        raise ValueError(f"看不懂的单元格引用：{ref!r}")
    letters = m.group(1)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall(f"{_NS_MAIN}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{_NS_MAIN}t")))
    return out


def sheet_names(path: str | Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        return [s.get("name") or "" for s in wb.iter(f"{_NS_MAIN}sheet")]


def _sheet_member(z: zipfile.ZipFile, name: str) -> str:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rid = None
    for s in wb.iter(f"{_NS_MAIN}sheet"):
        if s.get("name") == name:
            rid = s.get(f"{_NS_REL}id")
            break
    if rid is None:
        raise KeyError(f"工作簿里没有名为 {name!r} 的 sheet；实际有 {sheet_names(z.filename)}")

    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = None
    for r in rels:
        if r.get("Id") == rid:
            target = r.get("Target")
            break
    if not target:
        raise KeyError(f"sheet {name!r}（{rid}）在 rels 里找不到目标文件")

    target = target.lstrip("/")
    member = target if target.startswith("xl/") else f"xl/{target}"
    if member not in z.namelist():
        raise KeyError(f"sheet {name!r} 指向的 {member} 不在压缩包里")
    return member


def read_sheet(path: str | Path, name: str) -> list[list]:
    """读一个 sheet，返回**按列序号对齐**的行（缺的单元格补 None）。

    为什么不是「按出现顺序」：OOXML 里空单元格是被**省略**的，直接顺序 append
    会把整行左移 —— 县名跑到省代码那一列去，而且不会报错。
    """
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        member = _sheet_member(z, name)
        root = ET.fromstring(z.read(member))

        rows: list[list] = []
        for row in root.iter(f"{_NS_MAIN}row"):
            cells: dict[int, object] = {}
            for c in row.iter(f"{_NS_MAIN}c"):
                idx = column_index(c.get("r") or "")
                cells[idx] = _cell_value(c, shared)
            if not cells:
                rows.append([])
                continue
            width = max(cells) + 1
            rows.append([cells.get(i) for i in range(width)])
        return rows


def _cell_value(c: ET.Element, shared: list[str]):
    t = c.get("t")
    v = c.find(f"{_NS_MAIN}v")
    if t == "inlineStr":
        is_el = c.find(f"{_NS_MAIN}is")
        if is_el is None:
            return None
        return "".join(x.text or "" for x in is_el.iter(f"{_NS_MAIN}t"))
    if v is None:
        return None
    if t == "s":
        i = int(v.text or 0)
        return shared[i] if 0 <= i < len(shared) else None
    if t == "b":
        return v.text == "1"
    return v.text


def _as_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ==================================================================== 物候日历

_HEADER_RE = re.compile(r"^(早稻|中稻|晚稻)_(\d{4})$")

# 前 12 列是固定表头（省/市/县/代码/说明/年度/批复/类型…），后面才是「稻型_年份」
DOY_COLUMN_START = 12


def _season_columns(header: list) -> dict[str, dict[int, int]]:
    """从表头解析 {稻型: {列号: 年份}}。表头不符就报错，**不猜列的位置**。"""
    out: dict[str, dict[int, int]] = {}
    for idx in range(DOY_COLUMN_START, len(header)):
        m = _HEADER_RE.match(str(header[idx] or "").strip())
        if m:
            out.setdefault(m.group(1), {})[idx] = int(m.group(2))
    if not out:
        raise ValueError(
            "表头里没有任何「稻型_年份」列 —— 上游文件的结构可能变了，"
            f"不要按固定的列号硬读。第 13 列起是：{[header[i] for i in range(DOY_COLUMN_START, min(len(header), 20))]}"
        )
    return out


def _doy_to_date(year: int, doy: float) -> date:
    """年内序日 → 日期。闰年/越界都在这里兜住，不让它抛到上层。"""
    d = int(round(doy))
    base = date(year, 1, 1)
    last = (date(year + 1, 1, 1) - base).days          # 365 或 366
    return base + timedelta(days=min(max(d, 1), last) - 1)


def read_calendar(path: str | Path, *, drop_mismatch: bool = True) -> tuple[list[dict], dict]:
    """读三个 sheet 并合成「一季一行」。

    返回 (季节列表, 统计)。一季 = (县代码, 年份, 稻型) 上同时有移栽/抽穗/成熟三个日期的记录。

    空格子（该县该年没提取出来）**一律丢弃，不补** —— 它不等于 0 也不等于缺省值。
    三个日期的顺序不成立（移栽 ≥ 抽穗 ≥ 成熟）的记录也丢弃：那是数据噪声，
    留着会让模型学到「剩余天数为负」这种不可能的情形。
    """
    stats = {
        "sheets": {}, "no_order": 0, "outside": 0, "kept": 0, "counties": 0,
    }

    sheets: dict[str, dict[tuple, dict]] = {}
    for sheet in ("transplant", "heading", "maturity"):
        rows = read_sheet(path, sheet)
        if not rows:
            raise ValueError(f"sheet {sheet!r} 是空的")
        header = rows[0]
        cols = _season_columns(header)

        table: dict[tuple, dict] = {}
        n_rows = 0
        for row in rows[1:]:
            if not row or row[0] is None:
                continue
            adcode = str(row[6] or "").strip()
            if not re.fullmatch(r"\d{6}", adcode):
                continue
            n_rows += 1
            for rice, colmap in cols.items():
                for idx, year in colmap.items():
                    val = _as_float(row[idx] if idx < len(row) else None)
                    if val is None:
                        continue
                    table[(adcode, year, rice)] = {
                        "adcode": adcode,
                        "province": str(row[1] or "").strip(),
                        "city": str(row[3] or "").strip(),
                        "county": str(row[5] or "").strip(),
                        "year": year,
                        "rice_type": rice,
                        sheet: val,
                    }
        sheets[sheet] = table
        stats["sheets"][sheet] = n_rows

    keys = set(sheets["transplant"]) & set(sheets["heading"]) & set(sheets["maturity"])
    seasons: list[dict] = []
    counties: set[str] = set()
    for key in sorted(keys):
        merged = {}
        for sheet in ("transplant", "heading", "maturity"):
            merged.update(sheets[sheet][key])
        t, h, m = merged["transplant"], merged["heading"], merged["maturity"]
        if not (t < h < m):
            stats["no_order"] += 1
            if drop_mismatch:
                continue
        if not (0 < t and m <= 366):
            stats["outside"] += 1
            if drop_mismatch:
                continue
        seasons.append(merged)
        counties.add(merged["adcode"])

    stats["kept"] = len(seasons)
    stats["counties"] = len(counties)
    return seasons, stats


# ==================================================================== 训练样本


def season_cutoffs(start_doy: float, mature_doy: float) -> list[int]:
    """一季里要取哪些观察截止日（年内序日）。"""
    lo = int(round(start_doy))
    hi = int(round(mature_doy))
    out: list[int] = []
    d = lo + CUTOFF_STEP
    while d < hi:
        out.append(d)
        d += CUTOFF_STEP
    for off in CUTOFF_TAIL_OFFSETS:
        d = hi - off
        if lo < d < hi:
            out.append(d)
    return sorted(set(out))


def build_sample(
    *,
    adcode: str,
    year: int,
    rice_type: str,
    start_doy: float,
    mature_doy: float,
    cutoff_doy: int,
    daily: list[dict],
) -> dict | None:
    """造一行样本。返回 None 表示这一季的这个截止日**不可用**（原因见下）。

    `daily` 是该县该年的逐日气温（`date` / `tmax` / `tmin` / `precip`），
    内部裁到 [起点, 截止日] 再交给 `gdd.accumulate` 与 `harvest_model.build_features`。

    **11 个特征一律由 `build_features` 生成**，本模块只负责构造它的三个入参
    （field / growth / temps）。这是「训练与线上不会悄悄飘开」的唯一保证：
    两边各写一套特征，模型不会报错，只会给出越来越离谱的日期。

    返回 None 的三种情形：天气覆盖太差、积温序列为空、以及剩余天数不足
    （最后一种说明这一季的这个截止日已经完熟，线上走的是另一条分支）。
    """
    start = _doy_to_date(year, start_doy)
    cutoff = _doy_to_date(year, cutoff_doy)

    span = (cutoff - start).days + 1
    season = [r for r in daily if start.isoformat() <= str(r.get("date")) <= cutoff.isoformat()]
    if len(season) < MIN_SEASON_DAYS or len(season) < MIN_SEASON_COVERAGE * span:
        return None

    series = gdd_mod.accumulate(season, crop_config(CROP))
    if not series:
        return None

    gdd_cum = float(series[-1]["gdd_cum"])
    mature_gdd = float(maturity_gdd(CROP))
    growth = {
        "gdd_cum": gdd_cum,
        "gdd_remain": max(mature_gdd - gdd_cum, 0.0),
        "days_since_sow": (cutoff - start).days,
    }
    field = {"crop": CROP, "sow_date": start.isoformat()}

    features = harvest_model.build_features(field, growth, series, today=cutoff.isoformat())

    remaining = int(round(mature_doy)) - cutoff_doy
    if remaining < MIN_REMAINING_DAYS:
        return None

    tail = series[-TAIL_DAYS:]
    row = {
        "adcode": adcode,
        "year": year,
        "rice_type": rice_type,
        "group": group_key(adcode=adcode, year=year, rice_type=rice_type),
        "start_doy": int(round(start_doy)),
        "mature_doy": int(round(mature_doy)),
        "cutoff_doy": cutoff_doy,
        "cutoff_date": cutoff.isoformat(),
        "y": remaining,
        # 落盘时只存驱动 predict_harvest 所需的那 30 天，别把整季写进 CSV
        "tail": {
            "g": [float(r["gdd_daily"]) for r in tail],
            "x": [(float(r["tmax"]) if r.get("tmax") is not None else None) for r in tail],
            "n": [(float(r["tmin"]) if r.get("tmin") is not None else None) for r in tail],
            "p": [(float(r["precip"]) if r.get("precip") is not None else None) for r in tail],
        },
    }
    row.update(features)
    return row


def group_key(*, adcode: str, year: int, rice_type: str) -> str:
    """切分单元：`adcode|year|rice_type`。

    **同一季的多条样本必须整体落进同一侧**：它们共享同一个成熟日、
    天气窗口又高度重叠，按行随机切会让「近似孪生」的样本同时出现在训练集与
    验证集里，分数虚高。所以切分单元是「一季」，不是「一行」。

    **组名里不放省名/县名**：训练样本表要过 `scripts/32` 那条
    「不得出现 province/city 之类地区字段」的断言（7.14 的隐私红线），
    而空间留出只需要知道「这个县属于哪个省」—— adcode 前两位本来就是省，
    再用一个省名列纯属多余（见 `province_code`）。少一列就少一次将来有人
    把地区名当特征喂进去的机会。
    """
    return f"{adcode}|{year}|{rice_type}"


def tail_to_temps(tail: dict) -> list[dict]:
    """把落盘的 30 天尾段还原成 `predict_harvest` 认的 temps 列表。

    日期字段不需要：`build_features` 只按位置取 `temps[-30:]` / `gdds[-7:]`，
    `_gdd_interval` 只按位置取 `temps[-14:]`，三处都不看日期。
    """
    g = tail.get("g") or []
    x = tail.get("x") or []
    n = tail.get("n") or []
    p = tail.get("p") or []

    def pick(seq, i):
        return seq[i] if i < len(seq) else None

    out = []
    for i in range(len(g)):
        out.append({
            "date": "",                       # 见 docstring：这三处都不看日期
            "gdd_daily": float(g[i] or 0.0),
            "tmax": pick(x, i),
            "tmin": pick(n, i),
            "precip": pick(p, i),
        })
    return out


def row_to_inputs(row: dict) -> tuple[dict, dict, list[dict]]:
    """(field, growth, temps) —— 直接喂给 `harvest_model.predict_harvest`。"""
    year = int(row["year"])
    start = _doy_to_date(year, float(row["start_doy"]))
    field = {"id": 0, "crop": CROP, "sow_date": start.isoformat()}
    growth = {
        "gdd_cum": float(row["gdd_cum"]),
        "gdd_remain": float(row["gdd_remain"]),
        "days_since_sow": int(row["days_since_sow"]),
        "matured": False,
    }
    return field, growth, tail_to_temps(row["tail"])


# ==================================================================== 切分与闸门

SPLIT_MODES = ("group", "time", "province")

# 空间留出：本项目演示区（crop_stages.yaml 里水稻就是按武汉标定的）。
# 闸门以这一档为准 —— 同分布的那两档只能给个上限参考。
#
# 用**行政区划代码的前两位**而不是省名：训练表里不放地区名（见 group_key），
# 切分靠 `420115 → 42` 推出来。省名只留在报告里给人读。
VAL_PROVINCE_CODES = ("42",)
TIME_SPLIT_YEAR = 2016            # train ≤ 2016 / val ≥ 2017

# 只用于日志与报告的可读性回查，**不参与任何计算**。
PROVINCE_NAMES = {
    "32": "江苏省", "34": "安徽省", "36": "江西省",
    "42": "湖北省", "43": "湖南省",
}


def province_code(adcode: str) -> str:
    """行政区划代码前两位 = 省。空间留出靠它定向，报告里也用它回查省名。"""
    return str(adcode or "")[:2]


def split_by_group(
    rows: list[dict], *, mode: str,
    val_province_codes: tuple[str, ...] = VAL_PROVINCE_CODES,
    split_year: int = TIME_SPLIT_YEAR,
    n_folds: int = 5,
    seed: int = 42,
) -> tuple[list[dict], list[dict], dict]:
    """按 **group** 整组切分，返回 (train, val, meta)。

    mode:
        "group"    —— 把不同的季轮流分进 n_folds 折，取第 0 折作验证集。
                      同分布，给的是「上限参考」。
        "time"     —— train ≤ split_year / val > split_year。
        "province" —— 留出 val_province_codes 这些省（按 adcode 前两位）。**闸门用这一档**。

    三种模式都保证「同一 group 的行整体在同一侧」。
    """
    if mode not in SPLIT_MODES:
        raise ValueError(f"未知切分模式：{mode!r}，只支持 {SPLIT_MODES}")

    groups = sorted({r["group"] for r in rows})

    if mode == "province":
        val_groups = {g for g in groups if province_code(g.split("|", 1)[0]) in val_province_codes}
    elif mode == "time":
        val_groups = {g for g in groups if int(g.split("|")[1]) > split_year}
    else:
        # 用「排序后轮流发牌」而不是 sklearn 的 GroupKFold：结果只取决于
        # 组名的排序，可复现、无依赖，而它要保证的那条性质（同组不跨折）
        # 靠「按组发牌」天然成立。
        import random

        shuffled = list(groups)
        random.Random(seed).shuffle(shuffled)
        val_groups = set(shuffled[::n_folds])

    train = [r for r in rows if r["group"] not in val_groups]
    val = [r for r in rows if r["group"] in val_groups]

    same_side = not ({r["group"] for r in train} & {r["group"] for r in val})
    meta = {
        "mode": mode,
        "holdout": ([PROVINCE_NAMES.get(c, c) for c in val_province_codes]
                    if mode == "province" else None),
        "train_rows": len(train), "val_rows": len(val),
        "train_groups": len(groups) - len(val_groups), "val_groups": len(val_groups),
        "groups_disjoint": same_side,
    }
    return train, val, meta


# 上线闸门。跑不赢基线就不上线 —— 只交付管线与报告，脚本退出码仍然是 0
# （沿用 24 号脚本「等待状态不是错误」的哲学）。
GATE_MIN_MAE_GAIN = 1.0           # 模型 MAE 至少比基线低这么多天
GATE_COVERAGE_RANGE = (0.70, 0.90)  # 留出集原始 q10–q90 覆盖率（名义 0.80）
GATE_MIN_VAL_ROWS = 500
GATE_MIN_VAL_GROUPS = 50


def gate_check(*, model_mae: float, baseline_mae: float, coverage: float,
               val_rows: int, val_groups: int) -> dict:
    """上线闸门。返回 {passed, checks: {名: {ok, detail}}}，每个条件都带中文理由。"""
    lo, hi = GATE_COVERAGE_RANGE
    checks = {
        "mae": {
            "ok": model_mae <= baseline_mae - GATE_MIN_MAE_GAIN,
            "detail": (f"模型 MAE {model_mae:.2f} 天 vs 基线 {baseline_mae:.2f} 天，"
                       f"要求至少低 {GATE_MIN_MAE_GAIN:.1f} 天"),
        },
        "coverage": {
            "ok": lo <= coverage <= hi,
            "detail": (f"留出集 q10–q90 覆盖率 {coverage:.3f}，"
                       f"要求落在 [{lo:.2f}, {hi:.2f}]（名义 0.80）"),
        },
        "rows": {
            "ok": val_rows >= GATE_MIN_VAL_ROWS,
            "detail": f"留出集 {val_rows} 行，要求 ≥ {GATE_MIN_VAL_ROWS} 行",
        },
        "groups": {
            "ok": val_groups >= GATE_MIN_VAL_GROUPS,
            "detail": f"留出集 {val_groups} 组，要求 ≥ {GATE_MIN_VAL_GROUPS} 组",
        },
    }
    return {"passed": all(c["ok"] for c in checks.values()), "checks": checks}


# ==================================================================== 落盘格式

FEATURE_COLUMNS = tuple(harvest_model.FEATURE_NAMES)

# 训练表里**只有**：分组键 + 日期口径 + 标签 + 11 个特征。
# 刻意没有 province / city / county —— 它们是给人看的，留在 `rice_calendar.csv`
# 那张描述性长表里。训练表要过 scripts/32 的 FORBIDDEN_KEYS 断言（7.14 红线），
# 而「这个县属于哪个省」由 adcode 前两位就能推出来（见 province_code），
# 不必为此多带一列地区名。
EXTRA_COLUMNS = (
    "adcode", "year", "rice_type", "group",
    "start_doy", "mature_doy", "cutoff_doy", "cutoff_date", "y",
)
CSV_COLUMNS = EXTRA_COLUMNS + FEATURE_COLUMNS + ("tail",)


def encode_tail(tail: dict) -> str:
    """尾段压成一行文本：`g;x;n;p`，每组内按天用逗号分隔。

    不用 JSON：这三个数组全是定长数字，JSON 的方括号引号加起来能占三成体积，
    而这份表动辄几十万行。格式不通用不要紧 —— 编解码都在本模块里，成对改。
    """
    def fmt(seq, nd):
        return ",".join("" if v is None else f"{float(v):.{nd}f}" for v in (seq or []))

    return ";".join([fmt(tail.get("g"), 2), fmt(tail.get("x"), 1),
                     fmt(tail.get("n"), 1), fmt(tail.get("p"), 1)])


def decode_tail(text: str) -> dict:
    parts = (text or "").split(";")
    if len(parts) != 4:
        raise ValueError(f"尾段格式不对，应有 4 组，实际 {len(parts)} 组：{text[:60]!r}")

    def parse(chunk, nd):
        if not chunk:
            return []
        out = []
        for tok in chunk.split(","):
            if tok == "":
                out.append(None)
                continue
            try:
                out.append(float(tok))
            except ValueError as e:
                raise ValueError(f"尾段里有个不是数字的值：{tok!r}（精度应为 {nd} 位）") from e
        return out

    return {"g": parse(parts[0], 2), "x": parse(parts[1], 1),
            "n": parse(parts[2], 1), "p": parse(parts[3], 1)}


def row_to_csv(row: dict) -> dict:
    out = {k: row.get(k) for k in EXTRA_COLUMNS}
    for name in FEATURE_COLUMNS:
        out[name] = row.get(name)
    out["tail"] = encode_tail(row["tail"])
    return out


def row_from_csv(raw: dict) -> dict:
    out = dict(raw)
    out["year"] = int(raw["year"])
    out["start_doy"] = int(raw["start_doy"])
    out["mature_doy"] = int(raw["mature_doy"])
    out["cutoff_doy"] = int(raw["cutoff_doy"])
    out["y"] = int(raw["y"])
    for name in FEATURE_COLUMNS:
        out[name] = float(raw[name])
    out["tail"] = decode_tail(raw["tail"])
    return out
