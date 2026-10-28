#!/usr/bin/env python
"""用药台账的截图脚本（README 用图 + 视觉回归）。

跑法：
    python scripts/shot_ledger.py

依赖：后端已在 8000 端口运行，shots/auth.json 是有效的登录态。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "shots"
BASE = "http://127.0.0.1:8000"

# 演示账号，与 src/farm/seed.py 的 DEMO_ACCOUNT 同源
DEMO_USER = "user"
DEMO_PASS = "nihao123"


def _ensure_login(ctx, page) -> None:
    """登录态会过期（Cookie 7 天）。先用 /api/me 探一下，过期就走接口重新登。

    走接口而不是填表单：ctx.request 与页面共享同一份 Cookie，登录成功后浏览器
    里立刻就是登录态，不用等页面跳转（省掉「跳转过程中 locator 被销毁」这类偶发超时）。
    """
    resp = ctx.request.get(f"{BASE}/api/auth/me")
    if resp.ok and (resp.json().get("data") or {}).get("username"):
        print("登录态有效：", resp.json()["data"]["username"])
        return

    r = ctx.request.post(f"{BASE}/api/auth/login",
                         data={"username": DEMO_USER, "password": DEMO_PASS})
    if not r.ok or r.json().get("code") != 0:
        raise SystemExit(f"演示账号登录失败：{r.status} {r.text()[:200]}")
    print("已用演示账号登录：", DEMO_USER)


def _shot_main(page, path) -> None:
    """截正文区域（main.page），跳过站点页脚与悬浮气泡。

    顶栏是 sticky 的：正文比视口高时，元素截图会边滚边拼，顶栏就压在标题上。
    截之前把它临时改成 static，截完还原 —— 只影响这一张图。
    """
    page.evaluate("window.scrollTo(0, 0)")
    page.evaluate(
        """() => {
            const h = document.querySelector('.site-header');
            if (h) h.dataset.prevPos = h.style.position, h.style.position = 'static';
        }""")
    try:
        page.locator("main.page").screenshot(path=str(path))
    finally:
        page.evaluate(
            """() => {
                const h = document.querySelector('.site-header');
                if (h) h.style.position = h.dataset.prevPos || '';
            }""")


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(exist_ok=True)
    state = OUT / "auth.json"
    state_arg = str(state) if state.exists() else None

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  storage_state=state_arg, locale="zh-CN")
        page = ctx.new_page()
        _ensure_login(ctx, page)
        ctx.storage_state(path=str(state))          # 顺手把新鲜登录态写回去

        # ---- 台账页（按地块）
        page.goto(f"{BASE}/ledger.html", wait_until="networkidle")
        page.wait_for_selector(".ledger-table tbody tr", timeout=15000)
        # 截 <main> 而不是整页：小禾的问候气泡是 position:fixed，
        # 整页截图会把它重绘到页面最底部，变成一行没有样式的裸文字。
        _shot_main(page, OUT / "ledger_desktop.png")
        print("ledger_desktop.png", page.locator(".ledger-table tbody tr").count(), "行")

        # ---- 按药剂
        page.locator(".ledger-tabs button", has_text="按药剂").click()
        page.wait_for_timeout(400)
        _shot_main(page, OUT / "ledger_product.png")

        # ---- 用药史
        page.locator(".ledger-tabs button", has_text="用药史").click()
        page.wait_for_timeout(400)
        _shot_main(page, OUT / "ledger_history.png")

        # ---- 窄屏
        narrow = browser.new_context(viewport={"width": 390, "height": 844},
                                     storage_state=str(state), locale="zh-CN",
                                     is_mobile=True, has_touch=True, device_scale_factor=2)
        mp = narrow.new_page()
        mp.goto(f"{BASE}/ledger.html", wait_until="networkidle")
        mp.wait_for_selector(".ledger-card, .ledger-table tbody tr", timeout=15000)
        mp.screenshot(path=str(OUT / "ledger_mobile.png"), full_page=True)

        # ---- 农田页的打药表单：药剂下拉 + 最早可采收
        # 「记一笔」和麦克风按钮都在进入田块之后才显形，所以先点一块田。
        page.goto(f"{BASE}/farm.html", wait_until="networkidle")
        page.wait_for_selector(".plot-card[data-field-id]", timeout=20000)
        # 进水稻那块：登记库按作物登记，「稻瘟灵」在油菜上查不到（这是对的）
        page.locator(".plot-card[data-field-id]", has_text="水稻").first.click()
        page.wait_for_selector("#btn-add-log", timeout=20000)
        page.click("#btn-add-log")
        page.wait_for_selector("#voice-text", state="visible", timeout=8000)

        # 不解析出一句话，下面那组字段是不显示的 —— 这里照用户的真实动作走一遍
        page.fill("#voice-text", "今天给东头那块打了一遍稻瘟灵")
        page.click("#voice-reparse")
        page.wait_for_selector("#voice-fields", state="visible", timeout=15000)

        page.select_option("#voice-type", "spray")
        page.wait_for_selector("#voice-spray-box", state="visible", timeout=8000)

        page.fill("#voice-pesticide-q", "稻瘟灵")
        page.wait_for_timeout(1200)          # 320ms 防抖 + 一次请求
        opts = page.locator("#voice-pesticide option").count()
        print("药剂下拉候选：", opts)
        assert opts > 1, f"水稻上搜「稻瘟灵」应能出候选，实际只有 {opts} 个 option"

        page.fill("#voice-interval", "21")
        page.wait_for_timeout(400)
        earliest = page.inner_text("#voice-earliest")
        print("最早可采收：", earliest)
        assert "—" != earliest.strip(), "填了间隔期就该算出解禁日"
        page.screenshot(path=str(OUT / "farm_spray_form.png"), full_page=False)

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
