# -*- coding: utf-8 -*-
"""core.writer —— 把 PageResult 写成 xlsx，尽量还原 PDF 原表的样子。

还原三件事：
    1. **黑框加粗**：外框 medium、内框 thin，颜色纯黑
       （原来用 BFBFBF 细灰线，打印出来几乎看不见）
    2. **行列尺寸 1:1**：按 PDF 的 pt 值换算成 Excel 的列宽/行高
    3. **表头/合计行加粗分隔线**

单位换算：
    Excel 列宽 1 单位 ≈ 5.25 pt（默认字体下）
    Excel 行高 1 单位 = 1 pt
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .pipeline import PageResult

INVALID_SHEET_CHARS = set(r"[]:*?/\\")

# ---------------------------------------------------------------- 样式常量
# 注意：openpyxl 的 Side(color=...) 传 "000000" 会被当成 ARGB 解析成
# 00000000（全透明黑），打印出来是看不见的线。必须写全 8 位 ARGB。
BLACK = "FF000000"
SIDE_OUTER = Side(style="medium", color=BLACK)   # 外框：加粗
SIDE_INNER = Side(style="thin", color=BLACK)     # 内框：细黑
SIDE_HEAVY = Side(style="medium", color=BLACK)   # 表头下 / 合计行上

FILL_HEADER = PatternFill("solid", fgColor="F2F2F2")
FILL_MUTED = PatternFill("solid", fgColor="FAFAFA")

# 状态底色统一调淡 —— 原来用的是饱和色（C6EFCE/BDD7EE/FCE4D6 等），
# 实测会盖住黑色框线，导致表格看起来「框断了」。
# 这里用同样的色相但更浅，既保留信息又不抢框线。
STATUS_FILL = {
    "C6EFCE": "EAF6EF",   # agree      绿
    "FFEB9C": "FDF6DC",   # local      黄
    "BDD7EE": "E8F1FA",   # model      蓝
    "FCE4D6": "FDF0E9",   # precision  橙
    "FFC7CE": "FDEAEC",   # conflict   红
    "E4DFEC": "F1EEF6",   # manual     紫
}


def _status_fill(color_hex: str) -> PatternFill | None:
    """把对账状态色转成淡色填充，避免压住框线。"""
    if not color_hex:
        return None
    key = str(color_hex).upper().lstrip("#")
    pale = STATUS_FILL.get(key)
    if pale is None:
        # 未知颜色：自动调淡（与白色混合 65%）
        try:
            r, g, b = int(key[0:2], 16), int(key[2:4], 16), int(key[4:6], 16)
            r = int(r + (255 - r) * 0.65)
            g = int(g + (255 - g) * 0.65)
            b = int(b + (255 - b) * 0.65)
            pale = f"{r:02X}{g:02X}{b:02X}"
        except Exception:  # noqa: BLE001
            return None
    return PatternFill("solid", fgColor=pale)
TITLE_FONT = Font(name="宋体", size=12, bold=True, color=BLACK)
HEADER_FONT = Font(name="宋体", size=10, bold=True, color=BLACK)
BODY_FONT = Font(name="宋体", size=10, color=BLACK)
MONO_FONT = Font(name="Consolas", size=10, color=BLACK)


# ---------------------------------------------------------------- 版式还原
# Excel 的列宽是「按工作表」的，同一张表里没法给三个子表不同的列宽 ——
# 所以一张子表一个 sheet，列宽/字号/合并才不会被别的子表拉歪。

def _px_to_pt(px: float, dpi: int) -> float:
    return px * 72.0 / max(1, dpi)


def _px_to_chars(px: float, dpi: int) -> float:
    """Excel 列宽单位 ≈ 默认字体下几个字符宽（96dpi 下 1 字符 ≈ 7px）。"""
    return max(2.0, px * 96.0 / max(1, dpi) / 7.0)


def _shown_text(cell) -> str:
    """这个格子最终在 Excel 里显示成什么（数字按格式化后的样子算宽度）。"""
    if cell.kind == "number" and cell.value is not None:
        return f"{cell.value:,.0f}" if _is_integral(cell.value) else f"{cell.value:,.2f}"
    return cell.text or ""


def _is_integral(v: float) -> bool:
    return abs(v - round(v)) < 1e-9


def _ensure_widths(ws, blk) -> None:
    """按内容反算列宽，保证数字不会显示成 ### 。

    实测坑：列宽是按原页像素比例算的，但字号是从 OCR 框高度估的；
    字号一大，`101,325,990` 就撑破格子，Excel 显示成 ###（不是科学计数法）。

    只给两类格加宽：
        · 数字格 —— 必须完整显示，不然就是 ###；
        · 短文本（<= 20 字）—— 通常是列标题，撑宽一点更像原表。
    长文本（标签、脚注）不加宽，交给 wrap / 溢出，否则整张表会被拉到 90 列宽。
    """
    for cell in blk.cells:
        if getattr(cell, "is_footnote", False):
            continue
        text = _shown_text(cell)
        if not text:
            continue
        if cell.kind != "number" and len(text) > 20:
            continue
        size = max(8.0, min(20.0, cell.font_size))
        em = sum(1.0 if ord(ch) > 127 else 0.55 for ch in text)
        need_pt = em * size
        per_col = (need_pt / PT_PER_COL + 1.4) / max(1, cell.colspan)
        for k in range(max(1, cell.colspan)):
            letter = get_column_letter(cell.col + k + 1)
            cur = ws.column_dimensions[letter].width or 8.0
            ws.column_dimensions[letter].width = max(cur, min(per_col, 60.0))


def _number_format_for(value: float) -> str:
    """整数金额就别显示 .00 —— 原表本来就是整数（实测会显得多出两位）。"""
    return "#,##0;[Red]-#,##0" if _is_integral(value) else "#,##0.00;[Red]-#,##0.00"


def _block_name(res, blk, index: int) -> str:
    """用子表里那句表名当 sheet 名，找不到就退回 页码+序号。"""
    from pathlib import Path
    import re

    title = ""
    for c in sorted(blk.cells, key=lambda c: (c.row, c.col)):
        if c.colspan >= 3 and c.text and len(c.text) <= 80:
            title = c.text
            break
    if title:
        if len(title) > 31:
            # 这类表名是「中文 + 葡语」，超长时留中文那半
            cut = re.split(r"\s+[A-Za-zÀ-ÿ]", title)[0].strip()
            title = (cut or title)[:31]
        return title
    return f"{Path(res.path).stem[:22]}_p{res.page_no}_{index + 1}"


def _block_border(ri: int, ci: int, blk) -> Border:
    """外圈粗、内部细 —— 和原表的印刷线一致。"""
    return Border(
        left=SIDE_OUTER if ci == 0 else SIDE_INNER,
        right=SIDE_OUTER if ci == blk.n_cols - 1 else SIDE_INNER,
        top=SIDE_OUTER if ri == 0 else SIDE_INNER,
        bottom=SIDE_OUTER if ri == blk.n_rows - 1 else SIDE_INNER,
    )


def _write_block_sheet(ws, res, blk, opts: ExportOptions, loose=None) -> None:
    from pathlib import Path

    header_row = 1
    c = ws.cell(row=header_row, column=1,
                value=f"{Path(res.path).name} · 第 {res.page_no} 页 · 按表格线还原")
    c.font = TITLE_FONT
    ws.row_dimensions[header_row].height = 22

    start = 3
    # 表格线之外的页眉文字：原样放在表格上方、跨列居中
    for line in (loose or []):
        cell = ws.cell(row=start, column=1, value=line.text)
        cell.font = Font(name="宋体", size=max(7.0, min(16.0, line.font_size)),
                         bold=False, color=BLACK)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.merge_cells(start_row=start, start_column=1,
                       end_row=start, end_column=max(1, blk.n_cols))
        if ws.row_dimensions[start].height is None:
            ws.row_dimensions[start].height = max(14.0, line.font_size * 1.6)
        start += 1
    if loose:
        start += 1

    for i, w in enumerate(blk.col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _px_to_chars(w, blk.dpi)
    for i, h in enumerate(blk.row_heights):
        ws.row_dimensions[start + i].height = max(12.0, _px_to_pt(h, blk.dpi))

    occupied: set[tuple[int, int]] = set()
    for cell in sorted(blk.cells, key=lambda c: (c.row, c.col)):
        r0, c0 = start + cell.row, 1 + cell.col
        r1, c1 = r0 + cell.rowspan - 1, c0 + cell.colspan - 1
        if (r0, c0) in occupied:
            continue

        target = ws.cell(row=r0, column=c0)
        if getattr(cell, "is_footnote", False):
            # 脚注：跨整行、灰字、不加框线 —— 跟表格数据区分开
            target.value = cell.text
            target.font = Font(name="宋体", size=9, color="595959")
            target.alignment = Alignment(horizontal="left", vertical="center",
                                         wrap_text=True)
            for rr in range(r0, r1 + 1):
                for cc in range(c0, c1 + 1):
                    occupied.add((rr, cc))
            if c1 > c0:
                try:
                    ws.merge_cells(start_row=r0, start_column=c0,
                                   end_row=r1, end_column=c1)
                except Exception:  # noqa: BLE001
                    pass
            continue

        border = _block_border(cell.row, cell.col, blk)
        if cell.kind == "number" and cell.value is not None:
            target.value = cell.value
            target.number_format = _number_format_for(cell.value)
        elif cell.kind != "empty":
            target.value = cell.text
        target.font = Font(name="宋体", size=max(8.0, min(20.0, cell.font_size)),
                           bold=cell.bold, color=BLACK)
        target.alignment = Alignment(horizontal=cell.align, vertical="center",
                                     wrap_text=bool(getattr(cell, "wrap", False)))
        target.border = border
        if cell.kind == "empty" and not cell.merged:
            continue

        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                occupied.add((rr, cc))
                ws.cell(row=rr, column=cc).border = border
        if cell.merged:
            try:
                ws.merge_cells(start_row=r0, start_column=c0,
                               end_row=r1, end_column=c1)
            except Exception:  # noqa: BLE001
                pass

    # 写完之后按真实内容再把列宽撑一遍，杜绝 ###
    _ensure_widths(ws, blk)

SMALL_FONT = Font(name="宋体", size=9, color="595959")

NUMBER_FMT = "#,##0.00;[Red]-#,##0.00"
PT_PER_COL = 5.25

INDENT_STEP = "    "


@dataclass
class ExportOptions:
    include_image: bool = True
    include_table: bool = True
    include_audit_sheet: bool = True
    include_summary_sheet: bool = True
    colorize: bool = True
    image_width_px: int = 900
    indent_text_columns: bool = True
    # 新增：还原 PDF 版式
    restore_layout: bool = True       # 行列宽高按 PDF pt 还原
    bold_borders: bool = True         # 黑框加粗
    draw_borders_when_absent: bool = True   # 原表无框线时也画框（表格更好读）


@dataclass
class ExportReport:
    out_path: Path
    sheets: list[tuple[str, int, int, int]] = field(default_factory=list)
    total_cost: float = 0.0
    total_review: int = 0


def safe_sheet_name(name: str, used: set) -> str:
    base = "".join("_" if c in INVALID_SHEET_CHARS else c for c in name).strip() or "Sheet"
    base = base[:31]
    out, n = base, 2
    while out in used:
        suffix = f"_{n}"
        out = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(out)
    return out


def _is_numeric_text(s: str) -> bool:
    t = re.sub(r"[\s,，.%（）()]", "", str(s or ""))
    return bool(t) and all(ch in "0123456789-" for ch in t)


def _looks_like_header(row: list) -> bool:
    """表头：有文字、没数字、且至少两列有内容。"""
    filled = [c for c in row if c and getattr(c, "kind", "") != "empty"]
    if len(filled) < 2:
        return False
    return not any(c.kind == "number" and c.value is not None for c in filled)


def _border_for(ri: int, ci: int, n_rows: int, n_cols: int,
                header_rows: int, opts: ExportOptions) -> Border | None:
    """按位置给边框：外圈粗、内部细。"""
    if not opts.bold_borders:
        thin = Side(style="thin", color="BFBFBF")
        return Border(left=thin, right=thin, top=thin, bottom=thin)

    top = SIDE_OUTER if ri == 0 else SIDE_INNER
    bottom = SIDE_OUTER if ri == n_rows - 1 else SIDE_INNER
    left = SIDE_OUTER if ci == 0 else SIDE_INNER
    right = SIDE_OUTER if ci == n_cols - 1 else SIDE_INNER

    # 表头最后一行下方加粗
    if header_rows and ri == header_rows - 1 and ri < n_rows - 1:
        bottom = SIDE_HEAVY

    return Border(left=left, right=right, top=top, bottom=bottom)


def _write_cell(ws, row: int, col: int, cell, opts: ExportOptions,
                is_header: bool, border: Border | None):
    if cell is None or cell.kind == "empty":
        c = ws.cell(row=row, column=col, value=None)
        if border:
            c.border = border
        return

    if cell.kind == "number" and cell.value is not None:
        c = ws.cell(row=row, column=col, value=cell.value)
        c.number_format = NUMBER_FMT
        c.font = MONO_FONT if not is_header else HEADER_FONT
        c.alignment = Alignment(horizontal="right", vertical="center")
    else:
        text = cell.text
        if opts.indent_text_columns and cell.indent and col == 1:
            text = INDENT_STEP * cell.indent + text
        c = ws.cell(row=row, column=col, value=text)
        c.font = HEADER_FONT if is_header else BODY_FONT
        c.alignment = (Alignment(horizontal="center", vertical="center")
                       if is_header
                       else Alignment(horizontal="left", vertical="center"))

    if border:
        c.border = border
    if is_header:
        c.fill = FILL_HEADER
    elif opts.colorize and cell.color:
        f = _status_fill(cell.color)
        if f:
            c.fill = f


def _apply_layout(ws, res: PageResult, start_row: int, n_rows: int,
                  opts: ExportOptions):
    """按 PDF 的 pt 还原行高列宽。

    没有 layout（扫描件走 OCR 路径）时，退回到一组舒适的默认值 ——
    否则行高是 Excel 默认的 15pt，中文挤在一起很难看。
    """
    layout = res.layout
    if layout is None or not opts.restore_layout:
        # 兜底：给个适合中文的默认行高与列宽
        for i in range(n_rows):
            ws.row_dimensions[start_row + i].height = 22.0
        n_cols = max((len(r) for r in res.cells), default=1)
        for j in range(n_cols):
            ws.column_dimensions[get_column_letter(j + 1)].width = 22.0
        return
    try:
        for i, h in enumerate(layout.row_heights[:n_rows]):
            ws.row_dimensions[start_row + i].height = max(14.0, round(float(h), 1))
        for j, w in enumerate(layout.col_widths):
            ws.column_dimensions[get_column_letter(j + 1)].width = \
                max(6.0, round(float(w) / PT_PER_COL, 2))
    except Exception:  # noqa: BLE001
        for i in range(n_rows):
            ws.row_dimensions[start_row + i].height = 22.0


def _autofit(ws, max_width: int = 60):
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            s = str(c.value)
            w = sum(2 if ord(ch) > 127 else 1 for ch in s)
            widths[c.column] = max(widths.get(c.column, 8), min(w + 2, max_width))
    for col, w in widths.items():
        cur = ws.column_dimensions[get_column_letter(col)].width
        if cur is None:
            ws.column_dimensions[get_column_letter(col)].width = w


def write_workbook(
    results: list[PageResult],
    out_path: Path,
    opts: ExportOptions | None = None,
    sheet_name_of=None,
) -> ExportReport:
    opts = opts or ExportOptions()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    report = ExportReport(out_path=out_path)
    all_verdicts = []

    for res in results:
        # 按表格线还原的页：一个子表一个工作表（列宽是工作表级的，
        # 同一张表里放不下三种列宽）
        if getattr(res, "blocks", None):
            for bi, blk in enumerate(res.blocks):
                bname = safe_sheet_name(_block_name(res, blk, bi), used)
                bws = wb.create_sheet(bname)
                # 页眉（纳税人/年度/附件号）只写在第一张表上方
                loose = getattr(res, "loose", []) if bi == 0 else []
                _write_block_sheet(bws, res, blk, opts, loose=loose)
                bws.sheet_view.showGridLines = False
                bws.freeze_panes = "A3"
                report.sheets.append((bname, blk.n_rows, blk.n_cols, 0))
            report.total_cost += res.cost_cny
            continue

        default_name = Path(res.path).stem
        if sheet_name_of:
            default_name = sheet_name_of(res) or default_name
        name = safe_sheet_name(default_name, used)
        ws = wb.create_sheet(name)

        title = f"{Path(res.path).name} · 第 {res.page_no} 页 · 来源 {res.source or '未知'}"
        if res.error:
            title += f" · 错误：{res.error}"
        c = ws.cell(row=1, column=1, value=title)
        c.font = TITLE_FONT
        ws.row_dimensions[1].height = 22

        row_cursor = 3

        if opts.include_table and res.cells:
            n_rows = len(res.cells)
            n_cols = max((len(r) for r in res.cells), default=1)
            header = _looks_like_header(res.cells[0])
            header_rows = 1 if header else 0

            for ri, row in enumerate(res.cells):
                for ci in range(n_cols):
                    cell = row[ci] if ci < len(row) else None
                    border = _border_for(ri, ci, n_rows, n_cols, header_rows, opts)
                    _write_cell(ws, row_cursor + ri, ci + 1, cell, opts,
                                is_header=(header and ri == 0), border=border)

            _apply_layout(ws, res, row_cursor, n_rows, opts)
            row_cursor += n_rows + 2
            all_verdicts.extend(cv for cv in res.report.cells)
        else:
            _autofit(ws)

        if opts.include_image and res.png:
            try:
                img = XLImage(io.BytesIO(res.png))
                scale = min(1.0, opts.image_width_px / img.width)
                img.width, img.height = int(img.width * scale), int(img.height * scale)
                t = ws.cell(row=row_cursor, column=1, value="原始页面")
                t.font = Font(name="宋体", size=10, bold=True, color="595959")
                row_cursor += 1
                ws.add_image(img, f"A{row_cursor}")
                row_cursor += int(img.height / 20) + 2
            except Exception:  # noqa: BLE001
                pass

        if res.notes:
            n = ws.cell(row=row_cursor, column=1, value="备注")
            n.font = Font(name="宋体", size=10, bold=True, color="595959")
            row_cursor += 1
            for note in res.notes:
                nc = ws.cell(row=row_cursor, column=1, value=str(note))
                nc.font = SMALL_FONT
                row_cursor += 1

        ws.sheet_view.showGridLines = False
        ws.freeze_panes = f"A{3}"
        report.sheets.append((name, res.n_rows, res.n_cols, res.review_count))
        report.total_review += res.review_count
        report.total_cost += res.cost_cny

    # OCR 校验页
    if opts.include_audit_sheet and all_verdicts:
        from .reconcile import ReconcileReport
        ws = wb.create_sheet(safe_sheet_name("OCR校验", used))
        rep = ReconcileReport(cells=all_verdicts)
        rows = rep.rows_for_sheet()
        n_cols = len(rows[0]) if rows else 1
        for ri, row in enumerate(rows):
            for ci, v in enumerate(row):
                c = ws.cell(row=ri + 1, column=ci + 1, value=v)
                c.border = _border_for(ri, ci, len(rows), n_cols,
                                       1 if ri == 0 else 0, opts)
                c.font = HEADER_FONT if ri == 0 else BODY_FONT
                if ri == 0:
                    c.fill = FILL_HEADER
                c.alignment = Alignment(vertical="center")
        _autofit(ws)
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "A2"

    # 汇总页
    if opts.include_summary_sheet:
        ws = wb.create_sheet(safe_sheet_name("汇总", used))
        headers = ["工作表", "行数", "列数", "需复核单元格"]
        for ci, h in enumerate(headers):
            c = ws.cell(row=1, column=ci + 1, value=h)
            c.font = HEADER_FONT
            c.fill = FILL_HEADER
            c.border = _border_for(0, ci, 1, len(headers), 1, opts)
        for ri, (nm, nr, nc, rv) in enumerate(report.sheets, start=2):
            for ci, v in enumerate([nm, nr, nc, rv]):
                c = ws.cell(row=ri, column=ci + 1, value=v)
                c.font = BODY_FONT
                c.border = _border_for(ri - 1, ci, len(report.sheets) + 1,
                                       len(headers), 1, opts)
        last = len(report.sheets) + 3
        ws.cell(row=last, column=1, value="合计").font = HEADER_FONT
        ws.cell(row=last, column=4, value=report.total_review).font = HEADER_FONT
        ws.cell(row=last + 1, column=1, value="大模型花费（元）").font = BODY_FONT
        ws.cell(row=last + 1, column=2, value=round(report.total_cost, 6)).font = BODY_FONT
        note = ws.cell(row=last + 2, column=1,
                       value="需复核 = 红(conflict) + 蓝(model) + 橙(precision)，详见「OCR校验」页")
        note.font = SMALL_FONT
        _autofit(ws)
        ws.sheet_view.showGridLines = False

    wb.save(str(out_path))
    return report
