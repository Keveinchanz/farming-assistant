#!/usr/bin/env python
"""阶段 7 / 7.3.4（外部数据）—— 按县质心拉逐日气温，一县一文件。

## 为什么按县质心拉，而不是按田块

ChinaRiceCalendar 的物候是**县均**的（遥感反演后按行政区聚合），所以它的「起点」
天然对应一个县的质心，而不是某一块地。按质心拉气温与它的空间尺度相匹配；
用田块的经纬度反而会引入一个「县均物候 × 点位气温」的错配。
质心取自项目已有的 `data/admin_divisions.json`（`center: [lng, lat]`），
**刻意不用同包的 `xian.shp`** —— 那需要 GDAL/rasterio，而本机没有、Windows + Py3.11
上也不好装，而质心这份数据项目里本来就有。

## 为什么一县一个文件

「存在即跳过」就是断点续传：8000 多天的历史气温一天拉不完，中途断了重跑不会重头再来。
也正因为如此，**失败必须记 null 并继续**，不能悄悄写一个空文件 —— 空文件下次会被当成
「已完成」，那个县就永久缺数据了。缺失清单最后一次性打印出来，人工决定要不要补。

**绝不合成天气。** 某县拉不到就让它缺，缺的那一季在第 31 步会被丢弃并计数。
用气候平均或邻近县插值补出来的「天气」会让模型在假数据上训练，而报告上看不出来。

## 用法

    python scripts/30_fetch_phenology_weather.py                  # 全县全量
    python scripts/30_fetch_phenology_weather.py --provinces 湖北省   # 只拉湖北（最小闭环）
    python scripts/30_fetch_phenology_weather.py --limit 12       # 只拉前 12 个县（试跑）

县表来自 scripts/29 解压出来的那份 xlsx：**没跑过 29 就没有县表**，本脚本会直接报错退出，
不会去猜哪些县需要数据。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 这个脚本一次要 22 年的逐日数据（八千多天），响应体比线上那份「近 90 天」大两个数量级。
# 实测正常 3 秒就回，但连接偶尔会被重置或长时间无响应 —— 超时给 30 秒：
# 比正常耗时宽裕十倍，又不会在真挂住时白等一分半。
# **在 import weather 之前**设好：`_timeout()` 每次调用都读环境变量，所以 setdefault
# 会生效，而线上服务的默认值一个字都不动（网络设置不该被一个离线批处理脚本改掉）。
os.environ.setdefault("FARM_WEATHER_TIMEOUT", "30")

from src.common.io_utils import ensure_dir, read_json, write_json      # noqa: E402
from src.common.logging_utils import setup_logger                      # noqa: E402
from src.common.paths import path_of                                   # noqa: E402
from src.farm import harvest_external                                  # noqa: E402
from src.farm import weather                                           # noqa: E402

logger = setup_logger("agri.phenology.weather")

# 两次请求之间停一下。Open-Meteo 的免费额度是按「呼叫次数」算的，
# 两千多个县一口气打过去会被限流（然后一路 429 到脚本结束）。
# 成功路径的间隔。**放宽到 5 秒不是保守，是必须**：实测 1~2 秒的节奏会在几分钟内
# 撞出 429（见下），而一旦开始 429，每个县就要多花一分多钟去等窗口，
# 反而比慢一点更慢。慢就是快。
REQUEST_GAP = 5.0
RETRIES = 4
RETRY_WAIT = 4.0

# 429（限流）的等待。open-meteo 的限流窗口是**一分钟**：
#   等 30 秒 → 还在窗口里 → 再撞一次 → 再多等 60 秒，一个县白花 96 秒。
# 所以这里给的是**下限**而不是乘数：多大 attempt 都至少等满这个数，
# 一步跨过整个窗口，而不是在窗口边缘反复试。
RATE_LIMIT_WAIT = 65.0

# 分段兜底时每段几年。整段 22 年（八千多天）一旦在传输中途被重置，这一次就白拉了；
# 分成几段之后，每段能各自重试，最坏情况只损失那一段。**正常路径不分段** ——
# 实测整段 3 秒就回，分段只会把请求数翻几倍。
CHUNK_YEARS = 5

# 每次请求覆盖的年份。数据集是 2003–2022，两头各多要一年：
# 早稻的移栽可能落在 1 月，前一年 12 月的气温对它的起点窗口有用。
YEAR_PAD = 1

# 一份完好的缓存该有多少天（2002–2023 约 8035 天）。远低于这个数说明上次拉到一半
# 就被截断了 —— 这种文件必须重拉，不能当成「已完成」。
MIN_CACHED_ROWS = 6000


def county_index() -> dict[str, dict]:
    """{adcode: {name, province, lng, lat}}，来自 data/admin_divisions.json。"""
    data = read_json(path_of("admin_divisions"))
    out: dict[str, dict] = {}
    for prov in data.get("provinces") or []:
        for city in prov.get("cities") or []:
            for county in city.get("counties") or []:
                code = str(county.get("adcode") or "")
                center = county.get("center") or []
                if len(code) != 6 or len(center) != 2:
                    continue
                out[code] = {
                    "name": county.get("name") or "",
                    "province": prov.get("name") or "",
                    "city": city.get("name") or "",
                    "lng": float(center[0]),
                    "lat": float(center[1]),
                }
    return out


def wanted_adcodes(xlsx: Path) -> tuple[set[str], int, int]:
    """从县表里取出「真的用得上的县」，以及需要的年份范围。

    只读县代码就够的话不必解全表；但 `read_calendar` 本来就要读三个 sheet 才能
    确认结构没变（结构变了就该在这里炸，而不是拉到一半才发现），所以直接用它。
    """
    seasons, stats = harvest_external.read_calendar(xlsx)
    if not seasons:
        logger.error("县表里没解析出任何可用的一季 —— 不要继续拉天气。")
        raise SystemExit(1)

    codes = {s["adcode"] for s in seasons}
    years = [s["year"] for s in seasons]
    logger.info("县表：%d 季 / %d 个县 / %d–%d 年（表内 %d 个县，丢弃与序 %d 条、越界 %d 条）",
                stats["kept"], stats["counties"], min(years), max(years),
                stats["counties"], stats["no_order"], stats["outside"])
    return codes, min(years), max(years)


def cache_path(adcode: str) -> Path:
    return path_of("phenology_weather") / f"{adcode}.json"


def _chunks(start: str, end: str, years: int = CHUNK_YEARS) -> list[tuple[str, str]]:
    """把 [start, end] 按年切成若干段（闭区间，段间不重叠、不留缝）。"""
    y0, y1 = int(start[:4]), int(end[:4])
    out = []
    y = y0
    while y <= y1:
        last = min(y + years - 1, y1)
        out.append((f"{y}-01-01", f"{last}-12-31"))
        y = last + 1
    return out


def _next_hour() -> datetime:
    """下一个整点（多给 15 秒，免得卡在窗口边缘又被拒一次）。"""
    return datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=15)


def next_hour_str() -> str:
    """下一个整点的时刻，用于「什么时候可以重跑」这句提示。"""
    return _next_hour().strftime("%H:%M")


def _wait(err: Exception, attempt: int) -> float:
    """这一轮该等多久。两种量级，混成一个数就没法把额度磨完。

    普通网络失败   4/8/12 秒（1/2/3 倍递增）
    429 分钟窗口   至少 65 秒 —— 等 30 秒是穿不过一分钟窗口的，只会在窗口边缘连撞两次

    小时窗口（"Hourly ..."）**不在这里等**：额度按整点恢复，在这个窗口里重试毫无
    意义，见 fetch_one 与 main —— 那边直接停手，等下次重跑。
    """
    if "429" in str(err):
        return max(RATE_LIMIT_WAIT, RATE_LIMIT_WAIT * attempt / 2.0)
    return RETRY_WAIT * attempt


def fetch_one(adcode: str, info: dict, start: str, end: str) -> list[dict]:
    """拉一个县。带重试；整段重试都用完还失败，就退回**分段**再各试一遍。

    分段是兜底而不是默认路径：整段 22 年（八千多天）请求本身很快，
    但一旦在传输中途被重置（实测偶发 ConnectionReset），这一次就前功尽弃；
    切成几段之后每段能独立重试，最坏只损失那一段。全部重试都失败才抛 ——
    调用方记 null 并继续，**不会写一个空文件假装成功**。
    """
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return weather.fetch_history(info["lat"], info["lng"], start, end)
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("  %s 第 %d/%d 次失败：%s", adcode, attempt, RETRIES, e)
            if "Hourly" in str(e):
                # 额度按**整点**恢复，在同一个窗口里怎么重试都没用 —— 立刻停手。
                break
            if attempt < RETRIES:
                time.sleep(_wait(e, attempt))

    # 小时额度用完时，分段兜底只会把请求数翻几倍、把恢复时间推得更远，
    # 所以这一条要在分段之前拦住。用「小时额度已用完」这个措辞是在给 main 发信号。
    if last is not None and "Hourly" in str(last):
        raise RuntimeError(
            f"{adcode} 小时额度已用完（下一个整点 {next_hour_str()} 后可续）：{last}") from last

    chunks = _chunks(start, end)
    logger.warning("  %s 整段失败，改为分 %d 段重试", adcode, len(chunks))
    rows: list[dict] = []
    for c_start, c_end in chunks:
        for attempt in range(1, RETRIES + 1):
            try:
                rows += weather.fetch_history(info["lat"], info["lng"], c_start, c_end)
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("  %s %s~%s 第 %d/%d 次失败：%s",
                               adcode, c_start, c_end, attempt, RETRIES, e)
                if attempt == RETRIES:
                    raise RuntimeError(f"{adcode} 分段 {c_start}~{c_end} 仍失败：{e}") from e
                time.sleep(_wait(e, attempt))
    rows.sort(key=lambda r: str(r.get("date") or ""))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="按县质心拉逐日气温（ChinaRiceCalendar 用）")
    ap.add_argument("--provinces", nargs="*", default=None,
                    help="只拉这些省（按 admin_divisions 的省名，如 湖北省）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个县（试跑用）")
    ap.add_argument("--per-province", type=int, default=0,
                    help="每个省最多取 N 个县，按 adcode 均匀抽样。"
                         "直接截前 N 个只会取到省会城区，那样扩省等于只扩了省会的天气。")
    ap.add_argument("--force", action="store_true", help="已缓存的也重拉")
    ap.add_argument("--gap", type=float, default=REQUEST_GAP, help="两次请求之间的间隔秒数")
    ap.add_argument("--wait-quota", action="store_true",
                    help="撞上小时额度上限时睡到下一个整点自动续拉（无人值守用；"
                         "默认是干净退出，重跑即可续）")
    args = ap.parse_args()

    raw_dir = path_of("phenology_raw")
    xlsx_dir = raw_dir / "_extract"
    found = [p for p in xlsx_dir.rglob("*.xlsx")] if xlsx_dir.exists() else []
    if not found:
        logger.error("找不到解压出来的县表 —— 先跑 python scripts/29_download_phenology_datasets.py")
        raise SystemExit(1)
    xlsx = found[0]

    codes, y_min, y_max = wanted_adcodes(xlsx)
    index = county_index()
    start = f"{y_min - YEAR_PAD}-01-01"
    end = f"{y_max + YEAR_PAD}-12-31"

    # 说明每个县为什么不在列表里：这几类缺失的原因完全不同，混成一个数字就没法排查了。
    missing_centroid = sorted(c for c in codes if c not in index)
    targets = sorted(c for c in codes if c in index)
    if args.provinces:
        wanted = set(args.provinces)
        targets = [c for c in targets if index[c]["province"] in wanted]

    # 每省均匀抽样：把该省的县按 adcode 排好，等间距取 N 个。
    # 一省之内 adcode 的编号大致跟着地市顺序走，等间距取就得到「跨地市」的样本；
    # 直接取前 N 个则会密集落在最小编号那一个市，跨省的意义被削掉大半。
    if args.per_province:
        by_prov: dict[str, list[str]] = {}
        for c in targets:
            by_prov.setdefault(index[c]["province"], []).append(c)
        picked: list[str] = []
        for prov in sorted(by_prov):
            lst = sorted(by_prov[prov])
            n = args.per_province
            if len(lst) <= n:
                picked.extend(lst)
                logger.info("  %s：只有 %d 个县，全取", prov, len(lst))
                continue
            step = len(lst) / n
            chosen = sorted({lst[min(int(i * step + step / 2), len(lst) - 1)] for i in range(n)})
            picked.extend(chosen)
            logger.info("  %s：%d 个县中均匀取 %d 个", prov, len(lst), len(chosen))
        targets = sorted(set(picked))

    if args.limit:
        targets = targets[: args.limit]

    logger.info("质心缺失 %d / %d 个县（第 31 步会按 join 率检查这里）",
                len(missing_centroid), len(codes))
    if missing_centroid[:10]:
        logger.info("  例如：%s", missing_centroid[:10])
    logger.info("本次计划拉取 %d 个县，区间 %s ~ %s", len(targets), start, end)

    ensure_dir(path_of("phenology_weather"))

    done, skipped, failed = 0, 0, []
    quota_out = False
    i = 0
    # 用 while 而不是 for：撞上额度时要把**同一个县**退回来重试，
    # 而不是跳过它（跳过就等于这次没拉成、下次还得再来一遍）。
    while i < len(targets):
        adcode = targets[i]
        i += 1
        info = index[adcode]
        path = cache_path(adcode)
        if path.exists() and not args.force:
            cached = (read_json(path) or {}).get("rows") or []
            if len(cached) >= MIN_CACHED_ROWS:
                skipped += 1
                continue
            # 残缺的缓存比没有更糟：它会被当成「已完成」永久跳过，
            # 于是那个县的季全被判成「天气覆盖不足」而丢弃，而报告上只看到行数少了。
            logger.warning("%s 的缓存只有 %d 天（应有约 8000 天），本次重拉", adcode, len(cached))

        try:
            rows = fetch_one(adcode, info, start, end)
        except Exception as e:  # noqa: BLE001
            if "小时额度已用完" in str(e):
                if not args.wait_quota:
                    # 默认干净退出：已拉的县都已落盘（一县一文件），重跑就是接着拉。
                    quota_out = True
                    logger.warning("[%d/%d] 撞上小时额度上限 —— 先停下，不白等。", i, len(targets))
                    break
                i -= 1                      # 这个县没拉到，下一次仍从它开始
                wait = (_next_hour() - datetime.now()).total_seconds()
                logger.warning("[%d/%d] 撞上小时额度上限 —— 睡 %.0f 秒，%s 自动续拉（已拉的都已落盘）。",
                               i, len(targets), wait, next_hour_str())
                time.sleep(max(wait, 60.0))
                continue
            # 记 null 而不写文件：写了空文件，下次会被当成「已完成」而永久跳过。
            failed.append((adcode, info["name"], str(e)[:120]))
            logger.warning("[%d/%d] %s %s 失败：%s", i, len(targets), adcode, info["name"], e)
            continue

        write_json(path, {
            "adcode": adcode,
            "name": info["name"],
            "province": info["province"],
            "city": info["city"],
            "lng": info["lng"],
            "lat": info["lat"],
            "start": start,
            "end": end,
            "source": "open-meteo archive-api（ERA5 再分析）",
            "note": "县质心取自 data/admin_divisions.json；缺 tmax/tmin 的日子已在拉取时跳过",
            "rows": rows,
        })
        done += 1
        if (done + skipped) % 10 == 0:
            logger.info("进度 %d/%d（已拉 %d，跳过 %d，失败 %d）",
                        i, len(targets), done, skipped, len(failed))
        time.sleep(args.gap)

    logger.info("=" * 70)
    if quota_out:
        left = len(targets) - done - skipped - len(failed)
        logger.warning("本次因**小时额度上限**提前结束：还剩约 %d 个县没拉。", left)
        logger.warning("额度按整点恢复 —— %s 之后重跑同一条命令即可接着拉（已拉的会自动跳过）。",
                       next_hour_str())
        logger.warning("这不是错误，是免费额度的固有节奏；四五百个县要分几个整点窗口拉完。")
    logger.info("完成：新拉 %d 个县，已有缓存跳过 %d 个，失败 %d 个", done, skipped, len(failed))
    if failed:
        logger.warning("以下县**没有天气数据**（第 31 步会丢弃它们对应的季，不会补合成值）：")
        for adcode, name, err in failed[:40]:
            logger.warning("  %s %s  %s", adcode, name, err)
        if len(failed) > 40:
            logger.warning("  …… 其余 %d 个见上方的逐条告警", len(failed) - 40)
        logger.warning("重跑本脚本即可只补这些县（已有缓存会自动跳过）。")
    logger.info("=" * 70)
    logger.info("下一步：python scripts/31_build_harvest_dataset.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
