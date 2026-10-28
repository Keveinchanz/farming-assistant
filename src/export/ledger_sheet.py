"""用药台账导出：Excel 明细（给合作社/农资店对账）+ A4 一页纸（进档案）。

**为什么不是把页面截图或打印成 PDF**：台账要「能对账」—— 对方要能排序、能求和、
能贴进自己的表里。所以 Excel 是明细表而不是版式复刻；PDF 则是给「签字存档」这个
场景的，一页装下，谁打的、打了什么、哪天能收。

两条与页面一致的硬规矩：
    · 不给推荐剂量，只记用户自己填的用量原文；
    · 没有记录间隔期的行，状态写「未记录间隔期」，不写「可采收」——
      导出件会被当成凭证，这里含糊一次，责任就落到农技员头上。
"""

from __future__ import annotations

import io
from typing import Any

from src.export.sheet_pdf import MUTED, RULE, _ensure_font

STATUS_TEXT = {
    "locked": "间隔期内",
    "ok": "可采收",
    "unknown": "间隔期未记录，需核对",
    "none": "无打药记录",
}


def _stamp(today: str) -> str:
    return (today or "").replace("-", "") or "export"


# ---------------------------------------------------------------- Excel

def build_xlsx(data: dict) -> bytes:
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "default_date_format": "yyyy-mm-dd"})
    wb.set_properties({
        "title": "用药台账",
        "subject": "田头农技台 · 用药台账",
        "comments": data.get("note", ""),
    })

    title_fmt = wb.add_format({"bold": True, "font_size": 15, "font_name": "Microsoft YaHei"})
    sub_fmt = wb.add_format({"font_size": 10, "font_color": "#5b6660",
                             "font_name": "Microsoft YaHei"})
    head_fmt = wb.add_format({"bold": True, "font_size": 10, "font_name": "Microsoft YaHei",
                              "bg_color": "#eef1ef", "border": 1, "border_color": "#d7dcd9",
                              "text_wrap": True, "valign": "vcenter"})
    cell_fmt = wb.add_format({"font_size": 10, "font_name": "Microsoft YaHei", "border": 1,
                              "border_color": "#e4e8e6", "valign": "top"})
    num_fmt = wb.add_format({"font_size": 10, "font_name": "Consolas", "border": 1,
                             "border_color": "#e4e8e6", "align": "left"})
    warn_fmt = wb.add_format({"font_size": 10, "font_name": "Microsoft YaHei", "border": 1,
                              "border_color": "#e4e8e6", "bg_color": "#fdeaea",
                              "font_color": "#991b1b"})
    note_fmt = wb.add_format({"font_size": 9, "font_color": "#667270", "text_wrap": True,
                              "font_name": "Microsoft YaHei", "valign": "top"})

    period = data.get("period", {})
    summary = data.get("summary", {})

    # ---- 按地块
    ws = wb.add_worksheet("按地块")
    ws.set_column("A:A", 22)
    ws.set_column("B:B", 8)
    ws.set_column("C:C", 12)
    ws.set_column("D:D", 26)
    ws.set_column("E:E", 16)
    ws.set_column("F:F", 11)
    ws.set_column("G:G", 12)
    ws.set_column("H:H", 20)

    ws.write("A1", "用药台账 · 按地块", title_fmt)
    ws.write("A2", f"区间：{period.get('label', '')}　导出：{data.get('today', '')}", sub_fmt)
    ws.write("A3", (f"施药 {summary.get('spray_count', 0)} 次 · 药剂 "
                    f"{summary.get('product_count', 0)} 种 · 仍在间隔期 "
                    f"{summary.get('locked_fields', 0)} 块 · 未记录间隔期 "
                    f"{summary.get('unknown_fields', 0)} 块"), sub_fmt)

    heads = ["地块", "作物", "面积(亩)", "最近一次用药", "登记证号", "间隔期(天)",
             "最早可收", "状态"]
    for c, h in enumerate(heads):
        ws.write(3, c, h, head_fmt)

    for i, row in enumerate(data.get("fields", []), start=4):
        st = row.get("status", "none")
        fmt = warn_fmt if st == "locked" else cell_fmt
        ws.write(i, 0, row.get("name", ""), fmt)
        ws.write(i, 1, row.get("crop", ""), fmt)
        ws.write_number(i, 2, float(row.get("area_mu") or 0), num_fmt)
        ws.write(i, 3, row.get("last_item", ""), fmt)
        ws.write(i, 4, row.get("last_reg_no", ""), num_fmt)
        gap = row.get("interval_days")
        if gap is None:
            ws.write(i, 5, "未记录", warn_fmt if st != "none" else cell_fmt)
        else:
            ws.write_number(i, 5, int(gap), num_fmt)
        ws.write(i, 6, row.get("earliest_date", "") or "—", num_fmt)
        text = STATUS_TEXT.get(st, st)
        if row.get("locked") and row.get("days_left") is not None:
            text = f"{text}（还剩 {row['days_left']} 天）"
        ws.write(i, 7, text, warn_fmt if st == "locked" else fmt)

    ws.freeze_panes(4, 0)

    # ---- 按药剂
    ws2 = wb.add_worksheet("按药剂")
    ws2.set_column("A:A", 24)
    ws2.set_column("B:B", 14)
    ws2.set_column("C:C", 8)
    ws2.set_column("D:D", 34)
    ws2.set_column("E:E", 9)
    ws2.set_column("F:F", 22)
    ws2.set_column("G:G", 12)
    ws2.set_column("H:H", 26)

    ws2.write("A1", "用药台账 · 按药剂", title_fmt)
    ws2.write("A2", f"区间：{period.get('label', '')}　导出：{data.get('today', '')}", sub_fmt)

    heads2 = ["药剂", "登记证号", "毒性", "有效成分", "次数", "用在哪些地块",
              "最近施用", "提醒"]
    for c, h in enumerate(heads2):
        ws2.write(3, c, h, head_fmt)

    for i, g in enumerate(data.get("products", []), start=4):
        ws2.write(i, 0, g.get("product", ""), cell_fmt)
        ws2.write(i, 1, g.get("reg_no", ""), num_fmt)
        ws2.write(i, 2, g.get("toxicity", ""), cell_fmt)
        ws2.write(i, 3, g.get("ingredient", ""), cell_fmt)
        ws2.write_number(i, 4, int(g.get("count") or 0), num_fmt)
        ws2.write(i, 5, "、".join(g.get("field_names", [])), cell_fmt)
        ws2.write(i, 6, g.get("last_date", ""), num_fmt)
        tip = ""
        if g.get("rotate_warning"):
            tip = "连续两次同一有效成分，建议换作用机理（" + \
                  "、".join(g.get("repeat_fields", [])) + "）"
        ws2.write(i, 7, tip, warn_fmt if tip else cell_fmt)

    ws2.freeze_panes(4, 0)

    # ---- 用药史
    ws3 = wb.add_worksheet("用药史")
    ws3.set_column("A:A", 12)
    ws3.set_column("B:B", 22)
    ws3.set_column("C:C", 28)
    ws3.set_column("D:D", 14)
    ws3.set_column("E:E", 11)
    ws3.set_column("F:F", 12)
    ws3.set_column("G:G", 18)
    ws3.set_column("H:H", 30)

    ws3.write("A1", "用药台账 · 用药史（倒序）", title_fmt)
    ws3.write("A2", f"区间：{period.get('label', '')}　导出：{data.get('today', '')}", sub_fmt)

    heads3 = ["日期", "地块", "药剂", "登记证号", "用量", "间隔期(天)", "解禁日", "备注"]
    for c, h in enumerate(heads3):
        ws3.write(3, c, h, head_fmt)

    names = {f["field_id"]: f.get("name", "") for f in data.get("fields", [])}
    for i, lg in enumerate(data.get("history", []), start=4):
        ws3.write(i, 0, lg.get("date", ""), num_fmt)
        ws3.write(i, 1, names.get(lg.get("field_id", ""), lg.get("field_id", "")), cell_fmt)
        ws3.write(i, 2, lg.get("item", ""), cell_fmt)
        ws3.write(i, 3, lg.get("reg_no", ""), num_fmt)
        ws3.write(i, 4, lg.get("amount", ""), cell_fmt)
        gap = lg.get("interval_days")
        if gap is None:
            ws3.write(i, 5, "未记录", warn_fmt)
            ws3.write(i, 6, "—", warn_fmt)
        else:
            ws3.write_number(i, 5, int(gap), num_fmt)
            ws3.write(i, 6, lg.get("interval_end", ""), num_fmt)
        ws3.write(i, 7, lg.get("note", ""), cell_fmt)

    ws3.freeze_panes(4, 0)

    # ---- 说明
    ws4 = wb.add_worksheet("说明")
    ws4.set_column("A:A", 100)
    ws4.write("A1", "数据说明", title_fmt)
    lines = [
        data.get("note", ""),
        "",
        "安全间隔期来源：农药标签。公开的农药登记公告里没有这一项，因此本表只记录施药人"
        "填写的天数，并由系统做「施药日 + 间隔期 = 最早可采收日」的加法，不推断、不给默认值。",
        "未记录间隔期 ≠ 可以采收。表中凡是「未记录」的行，都表示这份台账不知道安全间隔期，"
        "请核对瓶身标签后补填。",
        "一块田里既有早已期满的旧施药、又有留空了间隔期的新施药时，状态一律写"
        "「间隔期未记录，需核对」，不写「可采收」—— 旧记录算出的解禁日属于那次施药，"
        "不能代表新药。该行不填「最早可收」，具体日期请看「用药史」里每次施药自己的解禁日。",
        "本表不包含推荐剂量。用量一列是施药人自己填的原文，未做单位换算。",
        "药剂信息（登记证号、有效成分、剂型、毒性、登记作物、防治对象）摘自农业农村部"
        "农药管理司《农药登记信息》，原文照录。",
        f"导出时间：{data.get('today', '')}　导出人：本账号",
    ]
    for i, line in enumerate(lines, start=2):
        ws4.set_row(i - 1, 26)
        ws4.write(i - 1, 0, line, note_fmt)

    wb.close()
    return buf.getvalue()


# ---------------------------------------------------------------- PDF

def build_pdf(data: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)
    from xml.sax.saxutils import escape

    _ensure_font()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title="用药台账", author="田头农技台")
    ink = colors.HexColor("#1c2620")
    muted = MUTED
    head_bg = colors.HexColor("#eef1ef")
    danger_bg = colors.HexColor("#fdeaea")

    h1 = ParagraphStyle("h1", fontName="STSong-Light", fontSize=17, leading=24, textColor=ink)
    sub = ParagraphStyle("sub", fontName="STSong-Light", fontSize=9.5, leading=15, textColor=muted)
    h2 = ParagraphStyle("h2", fontName="STSong-Light", fontSize=11.5, leading=18, textColor=ink,
                        spaceBefore=8, spaceAfter=4)
    th = ParagraphStyle("th", fontName="STSong-Light", fontSize=9, leading=13, textColor=ink)
    td = ParagraphStyle("td", fontName="STSong-Light", fontSize=9, leading=13, textColor=ink)
    foot = ParagraphStyle("foot", fontName="STSong-Light", fontSize=8, leading=12, textColor=muted)

    def P(text: Any, style) -> Paragraph:
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)

    period = data.get("period", {})
    summary = data.get("summary", {})
    story = [
        P("用药台账", h1),
        P(f"田头农技台　　区间：{period.get('label', '')}　　"
          f"导出：{data.get('today', '')}", sub),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=0.8, color=RULE),
        Spacer(1, 6),
    ]

    s = (f"施药 {summary.get('spray_count', 0)} 次　·　药剂 {summary.get('product_count', 0)} 种　"
         f"·　仍在间隔期 {summary.get('locked_fields', 0)} 块　·　未记录间隔期 "
         f"{summary.get('unknown_fields', 0)} 块")
    story.append(P(s, sub))

    nxt = summary.get("next_unlock")
    if nxt:
        story.append(Spacer(1, 6))
        warn = Table([[P(f"最近一块解禁：{nxt.get('name', '')}　{nxt.get('earliest_date', '')}"
                         f"（还剩 {nxt.get('days_left', 0)} 天，{nxt.get('item', '')}）", td)]],
                     colWidths=[doc.width])
        warn.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff7e6")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#e0b25c")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(warn)

    # 按地块
    story.append(P("一、按地块", h2))
    rows = [[P(x, th) for x in ("地块", "作物", "最近用药", "间隔期", "最早可收", "状态")]]
    styles = []
    for i, r in enumerate(data.get("fields", []), start=1):
        st = r.get("status", "none")
        status = STATUS_TEXT.get(st, st)
        if r.get("locked") and r.get("days_left") is not None:
            status = f"{status} 还剩 {r['days_left']} 天"
        gap = "未记录" if r.get("interval_days") is None else f"{r['interval_days']} 天"
        rows.append([
            P(r.get("name", ""), td), P(r.get("crop", ""), td),
            P(r.get("last_item", "") or "—", td), P(gap, td),
            P(r.get("earliest_date", "") or "—", td), P(status, td),
        ])
        if st == "locked":
            styles.append(("BACKGROUND", (0, i), (-1, i), danger_bg))
    if len(rows) == 1:
        rows.append([P("本区间没有记录", td)] + [P("", td)] * 5)
    t = Table(rows, colWidths=[doc.width * 0.25, doc.width * 0.08, doc.width * 0.24,
                               doc.width * 0.11, doc.width * 0.13, doc.width * 0.19],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ] + styles))
    story.append(t)

    # 按药剂
    story.append(P("二、按药剂", h2))
    rows2 = [[P(x, th) for x in ("药剂", "登记证号", "毒性", "次数", "用在哪块", "提醒")]]
    styles2 = []
    for i, g in enumerate(data.get("products", []), start=1):
        tip = "建议换作用机理" if g.get("rotate_warning") else "—"
        rows2.append([
            P(g.get("product", ""), td), P(g.get("reg_no", "") or "手填", td),
            P(g.get("toxicity", "") or "—", td), P(str(g.get("count", 0)), td),
            P("、".join(g.get("field_names", [])), td), P(tip, td),
        ])
        if g.get("rotate_warning"):
            styles2.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fef3c7")))
    if len(rows2) == 1:
        rows2.append([P("本区间没有记录", td)] + [P("", td)] * 5)
    t2 = Table(rows2, colWidths=[doc.width * 0.24, doc.width * 0.15, doc.width * 0.08,
                                 doc.width * 0.07, doc.width * 0.26, doc.width * 0.20],
               repeatRows=1)
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ] + styles2))
    story.append(t2)

    # 用药史
    story.append(P("三、用药史（最近 15 条）", h2))
    names = {f["field_id"]: f.get("name", "") for f in data.get("fields", [])}
    rows3 = [[P(x, th) for x in ("日期", "地块", "药剂", "间隔期", "解禁日")]]
    for lg in (data.get("history", []) or [])[:15]:
        gap = "未记录" if lg.get("interval_days") is None else f"{lg['interval_days']} 天"
        rows3.append([
            P(lg.get("date", ""), td),
            P(names.get(lg.get("field_id", ""), ""), td),
            P(lg.get("item", ""), td), P(gap, td),
            P(lg.get("interval_end", "") or "—", td),
        ])
    if len(rows3) == 1:
        rows3.append([P("本区间没有记录", td)] + [P("", td)] * 4)
    t3 = Table(rows3, colWidths=[doc.width * 0.14, doc.width * 0.24, doc.width * 0.30,
                                 doc.width * 0.14, doc.width * 0.18], repeatRows=1)
    t3.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t3)

    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.6, color=RULE))
    story.append(Spacer(1, 4))
    story.append(P("安全间隔期以农药标签为准，由施药人填写；本表只做「施药日 + 间隔期」的加法，"
                   "不推断、不给默认值。未记录间隔期不等于可以采收：旧施药已期满、新施药留空时，"
                   "状态写「间隔期未记录，需核对」，不填最早可收。本表不含推荐剂量，"
                   "用量为施药人填写的原文。药剂登记信息摘自农业农村部农药管理司"
                   "《农药登记信息》。", foot))
    story.append(Spacer(1, 6))
    story.append(P("记录人（签字）：________________　　日期：____________", foot))

    doc.build(story)
    return buf.getvalue()
