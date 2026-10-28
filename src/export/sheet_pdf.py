"""一页纸问诊单 → PDF（服务端生成，供「下载 PDF」按钮直接用）。

**为什么还要服务端出一份 PDF**：前端的「打印 / 存 PDF」走的是系统打印对话框，
在手机微信内置浏览器、部分国产浏览器里这个入口要么没有、要么只能打不能存。
农技员真正要的是一份能发到群里、能进档案的文件 —— 所以补一条不依赖浏览器菜单的路。

三点取舍：

    · **不复刻前端的 HTML 版式**：那是给屏幕和 A4 打印看的，套到 PDF 里要重排一次，
      两边一改就分叉。这里只保证「同样的六段、同样的顺序、同样的免责话术」，
      版式按 A4 重新排。
    · **不写用药剂量**：与问诊单同一条硬规矩（见 diagnose.js 的注释）。剂量要按
      标签和当地指导来，AI 写一个数字上去，农户就照着打了。
    · **字体用 reportlab 自带的 STSong-Light（CID）**：不引入字体文件、不做子集化，
      中文直接可读。代价是 PDF 不内嵌字体（阅读器要用系统字体渲染），
      这对「打印/发微信」这个用途是够的。
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape

FONT = "STSong-Light"
_FONT_READY = False

# 版心内使用的固定色，不跟屏幕主题走：这张纸永远是白底黑字
INK = colors.HexColor("#1c2620")
MUTED = colors.HexColor("#5b6660")
RULE = colors.HexColor("#d7dcd9")
WARN_BG = colors.HexColor("#fff7e6")
WARN_LINE = colors.HexColor("#e0b25c")


def _ensure_font() -> None:
    global _FONT_READY
    if not _FONT_READY:
        pdfmetrics.registerFont(UnicodeCIDFont(FONT))
        _FONT_READY = True


def _p(text: Any, style: ParagraphStyle) -> Paragraph:
    """把任意文本变成 Paragraph：先转义，再把换行变成 <br/>。

    不转义的话，农户名里带一个「<」整页就渲染失败；而这段文字来自用户输入，
    必须当成纯文本对待。
    """
    raw = "" if text is None else str(text)
    safe = escape(raw).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
    return Paragraph(safe, style)


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "base", fontName=FONT, fontSize=10, leading=15.5, textColor=INK, wordWrap="CJK",
    )
    return {
        "title": ParagraphStyle("title", parent=base, fontSize=17, leading=22),
        "sub": ParagraphStyle("sub", parent=base, fontSize=8.2, leading=11, textColor=MUTED),
        "meta": ParagraphStyle("meta", parent=base, fontSize=8.4, leading=11, textColor=MUTED),
        "h": ParagraphStyle("h", parent=base, fontSize=10.8, leading=16, spaceBefore=2),
        "body": base,
        "note": ParagraphStyle("note", parent=base, fontSize=8.2, leading=12, textColor=MUTED),
        "result": ParagraphStyle("result", parent=base, fontSize=12.5, leading=18),
        "sign": ParagraphStyle("sign", parent=base, fontSize=9, leading=14, textColor=MUTED,
                               alignment=TA_CENTER),
    }


def _section(title: str, body: list, st: dict[str, ParagraphStyle]) -> list:
    """一段 = 小标题 + 正文。KeepTogether 避免标题落在页底、正文翻页。"""
    return [KeepTogether([_p(title, st["h"]), Spacer(1, 2 * mm)] + body + [Spacer(1, 4 * mm)])]


def build_sheet_pdf(snapshot: dict[str, Any]) -> bytes:
    """按快照生成 A4 一页纸，返回 PDF 字节。"""
    _ensure_font()
    st = _styles()

    snap = snapshot or {}
    result = snap.get("result") or {}
    region = snap.get("region") or {}
    sections = snap.get("sections") or []

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title="田间问诊单", author="田头农技台",
        subject=str(snap.get("subject") or ""),
    )

    story: list = []

    # ---- 抬头：品牌 / 编号日期
    head = Table(
        [[
            [_p("田头农技台", st["title"]),
             _p("田间问诊单", st["sub"])],
            [_p(f"编号：{snap.get('no') or '现场未归档'}", st["meta"]),
             _p(f"日期：{snap.get('date') or '—'}", st["meta"])],
        ]],
        colWidths=[112 * mm, 62 * mm],
    )
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [head, Spacer(1, 3 * mm),
              HRFlowable(width="100%", thickness=0.8, color=RULE), Spacer(1, 4 * mm)]

    # ---- 元信息（与屏幕版同四项，不改字段名）
    def cell(text: str) -> Paragraph:
        return _p(text, st["body"])

    meta_rows = [
        [_p("服务对象", st["meta"]), cell(snap.get("subject") or "—"),
         _p("地块", st["meta"]), cell(snap.get("field") or "—")],
        [_p("作物", st["meta"]), cell(snap.get("crop") or "—"),
         _p("送检方式", st["meta"]), cell(" + ".join(snap.get("sources") or []) or "—")],
    ]
    meta = Table(meta_rows, colWidths=[20 * mm, 67 * mm, 20 * mm, 67 * mm])
    meta.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f6f8f7")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f6f8f7")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [meta, Spacer(1, 5 * mm)]

    # ---- 一、现场情况
    story += _section("一、现场情况",
                      [_p(snap.get("scene") or "（未填写文字或语音，仅提交照片）", st["body"])], st)

    # ---- 二、诊断结论
    name = result.get("name") or ""
    healthy = bool(result.get("healthy"))
    main = "未见明显病斑" if healthy else (name or "未能确定")
    tail = []
    if result.get("pest"):
        tail.append("虫害")
    if isinstance(result.get("confidence"), int):
        tail.append(f"置信度 {result['confidence']}%")
    conclusion = main + ("（" + "，".join(tail) + "）" if tail else "")
    concl_body = [_p(conclusion, st["result"])]
    if snap.get("summary"):
        concl_body.append(_p(snap["summary"], st["body"]))
    if snap.get("basis"):
        concl_body.append(_p(f"模型候选：{snap['basis']}", st["note"]))
    story += _section("二、诊断结论", concl_body, st)

    # ---- 三、处置建议
    advice: list = []
    if sections:
        first = sections[0]
        advice.append(_p(f"先做：{first.get('title') or ''}", st["body"]))
        if first.get("content"):
            advice.append(_p(first["content"], st["body"]))
        for item in sections[1:]:
            advice.append(Spacer(1, 2 * mm))
            advice.append(_p(f"{item.get('title') or ''}", st["note"]))
            if item.get("content"):
                advice.append(_p(item["content"], st["body"]))
    else:
        advice.append(_p("本次没有可执行的处置建议。", st["body"]))
    story += _section("三、处置建议", advice, st)

    # ---- 四、用药提醒（浅色框：这一段是最容易出事的一段）
    if snap.get("medication"):
        med = Table([[_p("四、用药提醒", st["h"])], [_p(snap["medication"], st["body"])]],
                    colWidths=[174 * mm])
        med.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
            ("BOX", (0, 0), (-1, -1), 0.6, WARN_LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story += [KeepTogether([med, Spacer(1, 4 * mm)])]

    # ---- 五、回访安排
    recheck = snap.get("recheck") or ""
    visit = (f"建议 {recheck} 回到同一块地复查" if recheck else "本次未排回访")
    if healthy:
        visit += "；同一片叶、同一角度再拍一张对比即可。"
    else:
        visit += "；拍一张同部位照片，对比用药前后的变化。"
    story += _section("五、回访安排", [_p(visit, st["body"])], st)

    # ---- 六、本省当前高发
    items = region.get("items") or []
    if items:
        region_body = [_p("；".join(items), st["body"])]
        src = "来源：全国农技中心《2026 年全国农作物病虫害发生趋势预报》+ 各省作物分布推导"
        if region.get("demo"):
            src += "　［演示数据，非实时测报］"
        region_body.append(_p(src, st["note"]))
    else:
        region_body = [_p("未设置所在省份，无法给出本省预警。", st["note"])]
    story += _section("六、本省当前高发（供参考）", region_body, st)

    # ---- 本地产收提醒：只在快照带 harvest 时出现（旧链接没有这一段）。
    # unknown 一律不印日期：这张纸上唯一写「哪天能吃」的地方就是这里。
    hv = snap.get("harvest") or None
    if hv and hv.get("status") not in (None, "", "none"):
        status = hv.get("status")
        if status == "unknown":
            harvest_body = [
                _p(f"这块田 {hv.get('date') or '—'} 施过 {hv.get('item') or '药剂'}，"
                   f"但这次记录没有填安全间隔期，无法判断哪天可以采收。", st["body"]),
                _p("请按瓶身标签补填间隔期后再看这张单。", st["note"]),
            ]
        elif status == "locked":
            tail = (f"（距今 {hv['days_left']} 天）"
                    if isinstance(hv.get("days_left"), int) else "")
            harvest_body = [_p(
                f"这块田 {hv.get('date') or '—'} 施过 {hv.get('item') or '药剂'}，"
                f"标签安全间隔期 {hv.get('interval_days') if hv.get('interval_days') is not None else '—'} 天，"
                f"最早可收 {hv.get('earliest_date') or '—'}{tail}。在这之前不要采收。",
                st["body"])]
        else:
            harvest_body = [_p(
                f"这块田 {hv.get('date') or '—'} 施过 {hv.get('item') or '药剂'}，"
                f"间隔期 {hv.get('interval_days') if hv.get('interval_days') is not None else '—'} 天，"
                f"已于 {hv.get('earliest_date') or '—'} 期满，可以采收。", st["body"])]
        story += _section("采收提醒", harvest_body, st)

    # ---- 签字与免责
    story += [
        Spacer(1, 2 * mm),
        HRFlowable(width="100%", thickness=0.5, color=RULE),
        Spacer(1, 3 * mm),
        _p("农技员签字：______________　　联系方式：______________　　农户确认：______________",
           st["sign"]),
        Spacer(1, 3 * mm),
        _p("本单由「田头农技台」辅助生成：结论基于" +
           ("、".join(snap.get("sources") or []) or "提供的材料") +
           "，不构成全田诊断；用药请以农药登记标签和当地植保部门指导为准。", st["note"]),
    ]

    def _decorate(canvas, doc_):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"第 {doc_.page} 页")
        canvas.drawString(18 * mm, 10 * mm, "田头农技台")
        canvas.restoreState()

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buf.getvalue()
