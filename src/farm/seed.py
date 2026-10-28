"""阶段 7 —— 注册时预置的示例田。

**为什么要有这个**：新账号进「我的农田」是一页空白，而这一页几乎全部内容
（天气、积温、生育阶段、3D 田块形态）都挂在具体田块上。没有田就什么都看不到，
演示时第一眼看到的是一个空页面。所以注册时预置三条**示例田**，名字里带「示例」
二字、可以随时改名或删掉。

三条田覆盖三个作物 × 三个生长阶段，正好把三套成熟形态都摆出来：

    示例田 · 水稻   播种回推  30 天  → 分蘖期（绿油油）
    示例田 · 玉米   播种回推  70 天  → 抽穗期（高挺）
    示例田 · 油菜   播种回推 200 天  → 成熟期（金黄低头，展示完熟形态）

播种回推的天数是按武汉的实际气温标定的（见 configs/crop_stages.yaml 头部：
播后 30 天的水稻累积 400 度、70 天的玉米 1072 度、200 天的油菜 2754 度，
分别落在各自的预期阶段里）。

**为什么必须带经纬度**：天气、积温、3D 参数全部依赖坐标。位置留空的话，
示例田只是一行没有内容的记录 —— 预置就白做了。因此这里填了武汉（114.30, 30.58），
并写进 user_settings 作为该用户新建田时的默认省市，用户随时可以改。
这是**演示数据**，和 province_pest_top3.json 一样属于演示性质，不冒充真实地块。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from src.common.config import env
from src.common.logging_utils import setup_logger
from src.farm import repo, users
from src.farm.timeutil import today_local

logger = setup_logger("agri.farm.seed")

# 演示坐标（武汉）。三个作物在湖北都种得开，且已实测该点的
# Open-Meteo 历史与预报接口都可用。
DEMO_PROVINCE = "湖北省"
DEMO_CITY = "武汉市"
DEMO_LNG = 114.30
DEMO_LAT = 30.58

DEMO_FIELDS: tuple[dict, ...] = (
    {"name": "示例田 · 水稻", "crop": "水稻", "back_days": 30, "area_mu": 3.0},
    {"name": "示例田 · 玉米", "crop": "玉米", "back_days": 70, "area_mu": 2.5},
    {"name": "示例田 · 油菜", "crop": "油菜", "back_days": 200, "area_mu": 2.0},
)


def demo_sow_date(back_days: int, *, today: str | None = None) -> str:
    """示例田的播种日 = 今天往前推 N 天。"""
    base = today_local() if today is None else date.fromisoformat(today)
    return (base - timedelta(days=int(back_days))).isoformat()


def seed_demo_fields(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """给新用户建三条示例田，并把默认省市写进 user_settings。

    整体在一个事务里（连接上下文负责提交）：注册流程中途失败不该留下
    「有账号但只有两条示例田」的半截状态。
    """
    created: list[dict] = []
    for item in DEMO_FIELDS:
        created.append(repo.create_field(
            conn,
            user_id,
            name=item["name"],
            crop=item["crop"],
            area_mu=item["area_mu"],
            province=DEMO_PROVINCE,
            city=DEMO_CITY,
            lng=DEMO_LNG,
            lat=DEMO_LAT,
            sow_date=demo_sow_date(item["back_days"]),
        ))

    users.update_settings(conn, user_id, {
        "default_province": DEMO_PROVINCE,
        "default_city": DEMO_CITY,
    })

    logger.info("已为用户 %s 预置 %d 条示例田（%s %s）",
                user_id, len(created), DEMO_PROVINCE, DEMO_CITY)
    return created


# ---------------------------------------------------------------- 通用演示账号

# 每个部署实例都预置一个演示账号，方便访问者直接登录体验。
# 它是**普通账号**：没有管理员权限，也不会绕过登录校验；页面照样要求登录或注册。
DEMO_ACCOUNT: dict[str, str] = {
    # 用户名与 users.DEMO_ACCOUNT_USERNAME 同源：权限层要靠它把演示账号挡在管理员之外
    "username": users.DEMO_ACCOUNT_USERNAME,
    "password": "nihao123",
    "nickname": "张大叔",
}

_OFF_VALUES = {"off", "0", "false", "no"}


def seed_demo_account(conn: sqlite3.Connection) -> dict | None:
    """预置通用演示账号：不存在才创建，已存在则原样保留。

    - **幂等**：同名账号已存在时返回 None，不覆盖密码、昵称与数据
    - **可重建**：每次服务启动都会走一遍，账号被删掉后下次启动会补回来
    - **可关闭**：环境变量 DEMO_SEED_ACCOUNT=off 时跳过（留给不希望公开账号的部署）

    创建成功后一并预置三条示例田，与正常注册后的初始状态保持一致。
    """
    if str(env("DEMO_SEED_ACCOUNT", "on")).strip().lower() in _OFF_VALUES:
        return None

    username = DEMO_ACCOUNT["username"]
    if users.find_by_username(conn, username):
        return None

    user = users.create_user(
        conn,
        username=username,
        email="",
        password=DEMO_ACCOUNT["password"],
        nickname=DEMO_ACCOUNT["nickname"],
    )
    seed_demo_fields(conn, user["id"])
    seed_demo_followups(conn, user["id"])
    seed_demo_sprays(conn, user["id"])
    logger.info("已创建通用演示账号：%s", username)
    return user


# ---------------------------------------------------------------- 演示回访记录

# 回访队列是「待办」而不是「历史」：一条都没有时整块卡片是隐藏的（见 farm.js），
# 演示时就等于这个功能不存在。所以给演示账号预置几条**带真实时间关系**的记录：
#
#   · 两条已过期（拖了 9 天、3 天）—— 队列里最该被看见的就是它们；
#   · 一条今天到期 —— 演示「今天该回访谁」；
#   · 三条未来 7 天内 —— 让队列有续，不是清一色红色。
#
# 总数正好等于 farm.js 的 FOLLOWUP_SHOW（6）：演示时六条全露出来，
# 不会出现「另有 2 条未列出」把最该展示的那条藏掉。
#
# 日期是**相对今天**算的，不是写死的绝对日期：写死的话过两周整套演示就全变成
# 「已过期 400 天」，越演示越像坏掉了。作物与病害的搭配也在各自的真实范围内
# （水稻配螟虫/稻瘟、油菜配菌核/霜霉、玉米配螟虫/大斑），不能张冠李戴。
DEMO_FOLLOWUPS: tuple[dict, ...] = (
    {"id": "dgn_demo_fu01", "crop": "水稻", "label": "二化螟", "subject": "陈秀兰",
     "confidence": 0.86, "diag_back": 12, "recheck_offset": -9},
    {"id": "dgn_demo_fu02", "crop": "油菜", "label": "菌核病", "subject": "刘志远",
     "confidence": 0.79, "diag_back": 8, "recheck_offset": -3},
    {"id": "dgn_demo_fu03", "crop": "水稻", "label": "稻瘟病", "subject": "王守田",
     "confidence": 0.91, "diag_back": 3, "recheck_offset": 0},
    {"id": "dgn_demo_fu04", "crop": "玉米", "label": "玉米螟", "subject": "周德海",
     "confidence": 0.72, "diag_back": 4, "recheck_offset": 2},
    {"id": "dgn_demo_fu05", "crop": "玉米", "label": "玉米大斑病", "subject": "赵老四",
     "confidence": 0.88, "diag_back": 2, "recheck_offset": 5},
    {"id": "dgn_demo_fu06", "crop": "油菜", "label": "霜霉病", "subject": "孙月娥",
     "confidence": 0.68, "diag_back": 5, "recheck_offset": 6},
)


def seed_demo_followups(
    conn: sqlite3.Connection, user_id: int, *, today: str | None = None
) -> list[dict]:
    """给演示账号补几条待回访的诊断。

    **幂等**：按固定 id（`dgn_demo_fu01`…）插入，重复调用只补缺的那几条，
    不会越跑越多。田块按作物找现成的示例田，找不到就跳过 —— 用户把示例田
    改名/删掉之后，这里不该硬塞一条挂不上田的记录。

    `report_json` 留 NULL：这条记录只服务于「队列里显示谁、哪天、查出了什么」，
    编一份假报告反而会让「看结论」点开是空的。要演示完整报告就走一次真诊断。
    """
    base = date.fromisoformat(today) if today else today_local()
    by_crop = {f["crop"]: f for f in repo.list_fields(conn, user_id)}

    created: list[dict] = []
    for item in DEMO_FOLLOWUPS:
        field = by_crop.get(item["crop"])
        if field is None:
            logger.warning("演示回访跳过 %s：没有 %s 的田块", item["id"], item["crop"])
            continue
        exists = conn.execute(
            "SELECT 1 FROM diagnoses WHERE user_id = ? AND id = ?", (user_id, item["id"])
        ).fetchone()
        if exists:
            continue
        created.append(repo.create_diagnosis(
            conn,
            user_id,
            field_id=field["id"],
            date=(base - timedelta(days=item["diag_back"])).isoformat(),
            label=item["label"],
            confidence=item["confidence"],
            subject=item["subject"],
            recheck_date=(base + timedelta(days=item["recheck_offset"])).isoformat(),
            diagnosis_id=item["id"],
        ))

    if created:
        logger.info("已为演示账号 %s 预置 %d 条待回访诊断", user_id, len(created))
    return created


# ---------------------------------------------------------------- 演示用药记录
#
# 用药台账靠「打药」这一类农事记录活着，一条都没有时整页是空态，演示就等于
# 这个功能不存在。所以给三块示例田预置七条施药记录，刻意覆盖三种状态：
#
#   · 水稻 10-20 那次解禁 11-10 → **还在间隔期**（台账顶上那条红条就靠它）；
#   · 玉米 10-08 那次解禁 10-22 → **已过、可采收**；
#   · 油菜两条都**没填间隔期** → **未记录**（这是「不知道」，不是「可以摘」）。
#
# 水稻 10-05 与 10-20 是同一个药（稻瘟灵），正好触发「连续两次同一有效成分，
# 建议换作用机理」那条提醒 —— 抗性是农技员真的会管的事，不是凑数的。
#
# 药剂与证号都是真的（data/pesticide_registry.json 里能查到），作物与防治对象
# 也对得上（水稻→稻瘟病、玉米→大斑病、油菜→菌核病）。
# 日期相对今天算，写死的话过两个月整套演示就都变成"已过期 300 天"。
DEMO_SPRAYS: tuple[dict, ...] = (
    {"id": "log_demo_sp01", "crop": "水稻", "back": 29, "item": "井冈·嘧菌酯 悬浮剂",
     "reg_no": "PD20211411", "days": 14, "amount": "每桶水 30 毫升"},
    {"id": "log_demo_sp02", "crop": "水稻", "back": 19, "item": "稻瘟灵 乳油",
     "reg_no": "PD20110955", "days": 21, "amount": "每桶水 40 毫升"},
    {"id": "log_demo_sp03", "crop": "水稻", "back": 4, "item": "稻瘟灵 乳油",
     "reg_no": "PD20110955", "days": 21, "amount": "每桶水 40 毫升"},
    {"id": "log_demo_sp04", "crop": "玉米", "back": 65, "item": "吡唑醚菌酯 悬浮剂",
     "reg_no": "PD20161186", "days": 14, "amount": "每桶水 25 毫升"},
    {"id": "log_demo_sp05", "crop": "玉米", "back": 16, "item": "唑醚·戊唑醇 悬浮剂",
     "reg_no": "PD20211403", "days": 14, "amount": "每桶水 20 毫升"},
    {"id": "log_demo_sp06", "crop": "油菜", "back": 40, "item": "多菌灵 悬浮剂",
     "reg_no": "PD86134-3", "days": None, "amount": "每桶水 50 克"},
    {"id": "log_demo_sp07", "crop": "油菜", "back": 24, "item": "菌核净 悬浮剂",
     "reg_no": "PD20211915", "days": None, "amount": "每桶水 40 克"},
)


def seed_demo_sprays(
    conn: sqlite3.Connection, user_id: int, *, today: str | None = None
) -> list[dict]:
    """给演示账号补几条施药记录（用药台账的演示数据）。

    幂等：按固定 id（`log_demo_spXX`）插入，重复调用只补缺的。田块按作物找，
    找不到就跳过 —— 示例田被改名/删掉之后不该硬塞一条挂不上田的记录。
    """
    base = date.fromisoformat(today) if today else today_local()
    by_crop: dict[str, dict] = {}
    for f in repo.list_fields(conn, user_id):
        by_crop.setdefault(f["crop"], f)

    created: list[dict] = []
    for item in DEMO_SPRAYS:
        field = by_crop.get(item["crop"])
        if field is None:
            logger.warning("演示用药跳过 %s：没有 %s 的田块", item["id"], item["crop"])
            continue
        exists = conn.execute(
            "SELECT 1 FROM logs WHERE user_id = ? AND id = ?", (user_id, item["id"])
        ).fetchone()
        if exists:
            continue
        created.append(repo.create_log(
            conn, user_id,
            field_id=field["id"],
            type="spray",
            date=(base - timedelta(days=item["back"])).isoformat(),
            item=item["item"],
            amount=item["amount"],
            note="（演示数据）",
            source="manual",
            reg_no=item["reg_no"],
            interval_days=item["days"],
            log_id=item["id"],
        ))

    if created:
        logger.info("已为演示账号 %s 预置 %d 条施药记录", user_id, len(created))
    return created
