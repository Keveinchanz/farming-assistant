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
    logger.info("已创建通用演示账号：%s", username)
    return user
