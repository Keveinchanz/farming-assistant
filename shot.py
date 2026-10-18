#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shot.py -- drive a real browser and save a screenshot, so Claude Code can see it.

One-shot rendering (shot.ps1) is enough for public pages. This script adds what
that cannot do: log in, click, type, wait for a selector, capture console errors
and failed requests, and save/reuse a login session.

Run it with the project's conda env:
    & "C:\\Users\\Jack Tom\\.conda\\envs\\az\\python.exe" shot.py <url> [options]

Examples:
    # authenticated page, reusing a saved session
    python shot.py http://127.0.0.1:8000/farm.html --state outputs/shots/auth.json -o farm.png

    # log in once, save the session for later runs
    python shot.py http://127.0.0.1:8000/login.html \
        --fill "#username=sunny" --fill "#password=secret" \
        --click "button[type=submit]" --wait-for "text=我的农田" \
        --save-state outputs/shots/auth.json -o after_login.png

    # watch console + failed requests while a page runs
    python shot.py http://127.0.0.1:8000/diagnose.html --console
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def parse_fill(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--fill expects SELECTOR=VALUE, got: {raw}")
    sel, val = raw.split("=", 1)
    return sel, val


def parse_setfile(raw: str) -> tuple[str, str]:
    """--set-file SELECTOR=PATH —— 给 <input type=file> 塞一个文件。

    没有这个就没法给诊断页喂叶子照片：page.fill() 对文件输入框无效，
    必须先提交一张真图，页面才会渲染出诊断结果卡片。
    """
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--set-file expects SELECTOR=PATH, got: {raw}")
    sel, path = raw.split("=", 1)
    return sel, path


def main() -> int:
    ap = argparse.ArgumentParser(description="Screenshot / drive a page for Claude Code.")
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default=None, help="PNG path (default: outputs/shots/<slug>.png)")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=2000)
    ap.add_argument("--full-page", action="store_true", help="capture the whole scrollable page")
    ap.add_argument("--settle-ms", type=int, default=1800, help="wait after load before shooting")
    ap.add_argument("--wait-for", default=None, help="CSS/text selector to wait for")
    ap.add_argument("--wait-timeout", type=int, default=20000)
    ap.add_argument("--click", action="append", default=[], help="selector to click (repeatable)")
    ap.add_argument("--fill", action="append", type=parse_fill, default=[], help="SELECTOR=VALUE (repeatable)")
    ap.add_argument("--set-file", action="append", type=parse_setfile, default=[],
                    help="SELECTOR=PATH -- set a file on <input type=file> (repeatable)")
    ap.add_argument("--selector", default=None,
                    help="screenshot just this element instead of the viewport "
                         "(e.g. '#result-root') -- gives a tight, information-dense image")
    ap.add_argument("--state", default=None, help="load a saved storage state (cookies/localStorage)")
    ap.add_argument("--save-state", default=None, help="save storage state after the run")
    ap.add_argument("--console", action="store_true", help="print console messages / page errors / failed requests")
    ap.add_argument("--dump-text", action="store_true", help="print document.body.innerText")
    ap.add_argument("--browser", default="chrome", choices=["chrome", "msedge", "chromium"],
                    help="chrome/msedge use the installed system browser (no download); chromium needs `playwright install chromium`")
    ap.add_argument("--headed", action="store_true", help="show the window (debugging)")
    ap.add_argument("--arg", action="append", default=[],
                    help="extra browser launch flag, e.g. --arg=--enable-unsafe-swiftshader "
                         "(repeatable) -- needed for WebGL in headless")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    slug = "".join(c if c.isalnum() else "_" for c in args.url.split("://")[-1]).strip("_") or "page"
    out = Path(args.out) if args.out else root / "outputs" / "shots" / f"{slug}.png"
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)

    logs: list[str] = []

    with sync_playwright() as pw:
        if args.browser == "chromium":
            browser = pw.chromium.launch(headless=not args.headed, args=args.arg)
        else:
            # Drive the already-installed Chrome/Edge -- no 150 MB download needed.
            browser = pw.chromium.launch(channel=args.browser, headless=not args.headed, args=args.arg)

        ctx_kwargs: dict = {"viewport": {"width": args.width, "height": args.height}}
        if args.state:
            ctx_kwargs["storage_state"] = str(Path(args.state) if Path(args.state).is_absolute() else root / args.state)
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()

        if args.console:
            page.on("console", lambda m: logs.append(f"[console:{m.type}] {m.text}"))
            page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
            page.on("requestfailed", lambda r: logs.append(f"[requestfailed] {r.url} -- {r.failure}"))
            page.on("response", lambda r: logs.append(f"[http {r.status}] {r.url}") if r.status >= 400 else None)

        page.goto(args.url, wait_until="load", timeout=45000)

        for sel, val in args.fill:
            page.fill(sel, val)
        # 文件上传要在点击之前：填完文件输入框才会触发 change 事件、页面才会显示预览。
        for sel, path in args.set_file:
            page.set_input_files(sel, str(Path(path) if Path(path).is_absolute() else root / path))
        for sel in args.click:
            page.click(sel)

        # --wait-for 必须放在交互之后。原来的顺序是先等、再填再点，于是
        # 「提交后才会出现的结果元素」永远等不到，必然超时 —— 而文档里的示例
        # （--click 提交 --wait-for 结果）本来就是按「点完再等」写的。
        if args.wait_for:
            page.wait_for_selector(args.wait_for, timeout=args.wait_timeout)

        page.wait_for_timeout(args.settle_ms)

        if args.selector:
            loc = page.locator(args.selector).first
            box = loc.bounding_box()
            if box:
                print(f"SELECTOR {args.selector}  {box['width']:.0f}x{box['height']:.0f} px")
            loc.screenshot(path=str(out))
        else:
            page.screenshot(path=str(out), full_page=args.full_page)
        print(f"PNG -> {out}")

        if args.dump_text:
            print("\n--- innerText ---")
            print(page.inner_text("body"))

        if args.save_state:
            sp = Path(args.save_state)
            if not sp.is_absolute():
                sp = root / sp
            sp.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(sp))
            print(f"STATE -> {sp}")

        ctx.close()
        browser.close()

    if logs:
        print("\n--- console / network ---")
        for line in logs:
            print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
