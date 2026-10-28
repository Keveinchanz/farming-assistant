#!/usr/bin/env python
"""验证问诊单上的「本地产收提醒」在真实诊断流程里会出来。

流程走的是用户真会走的那条路：
    选一张图 → 诊断 → 归档到水稻那块田 → 生成问诊单 → 看第六段后面那一节

跑法：
    python scripts/verify_sheet_spray.py

产出：shots/sheet_spray.png，以及控制台里的这一节正文。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
SHOTS = ROOT / "shots"
SAMPLE = ROOT / "Sample" / "100" / "DSC05819.JPG"


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not SAMPLE.exists():
        print(f"缺少样本图：{SAMPLE}", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        r = ctx.request.post(f"{BASE}/api/auth/login",
                             data={"username": "user", "password": "nihao123"})
        assert r.status == 200, f"登录失败：{r.status} {r.text()[:200]}"

        page = ctx.new_page()
        page.on("pageerror", lambda e: print("PAGEERR:", str(e)[:200]))

        page.goto(f"{BASE}/diagnose.html", wait_until="networkidle")
        page.set_input_files("#input-image", str(SAMPLE))
        page.wait_for_selector("#image-preview img", timeout=10000)   # 选图只是预览
        page.click("#btn-submit")                                     # 「开始诊断」才跑模型

        # 诊断要跑模型，慢一点；等归档那一格的 select 被填出来。
        # 不能等 option 的 visible —— <option> 在 Playwright 眼里永远不是 visible。
        page.wait_for_function(
            """() => {
                const s = document.getElementById('archive-field');
                return !!s && [...s.options].some((o) => o.value);
            }""",
            timeout=180000)
        fields = page.eval_on_selector_all(
            "#archive-field option", "els => els.map(e => [e.value, e.text])")
        print("可归档的田：", [t for _, t in fields if t])

        # 选水稻那块（只有它还在间隔期里）
        want = next((v for v, t in fields if "水稻" in t), "")
        assert want, "示例数据里应有水稻田"
        page.select_option("#archive-field", want)

        page.click("#btn-archive")
        page.wait_for_function(
            "() => (document.getElementById('archive-status').textContent || '').includes('已归入')",
            timeout=30000)
        print("归档：", page.inner_text("#archive-status"))

        page.locator("button", has_text="生成问诊单").first.click()
        page.wait_for_selector("#sheet-spray", state="visible", timeout=20000)

        body = page.inner_text("#sheet-spray")
        print("\n---- 本地产收提醒 ----\n" + body + "\n---------------------")

        assert "最早" in body or "期满" in body, f"这一节没写出结论：{body}"
        assert "间隔期" in body, "这一节必须说清间隔期是几天"

        SHOTS.mkdir(exist_ok=True)
        page.locator("#sheet-spray").screenshot(path=str(SHOTS / "sheet_spray.png"))
        page.locator("#sheet-modal .sheet").screenshot(path=str(SHOTS / "sheet_full.png"))
        print("已保存 shots/sheet_spray.png 与 shots/sheet_full.png")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
