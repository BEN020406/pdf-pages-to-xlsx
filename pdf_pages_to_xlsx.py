# -*- coding: utf-8 -*-
"""
pdf_pages_to_xlsx.py — 从多个 PDF 各取指定页，合成一个 Excel，一个 PDF 一个工作表。

用法:
    python pdf_pages_to_xlsx.py --input ./sample --page 2 --out ./out/result.xlsx
    python pdf_pages_to_xlsx.py --input a.pdf b.pdf --page 2 --mode image
    python pdf_pages_to_xlsx.py --input ./sample --pages 1,3,3,5 --mode both
    python pdf_pages_to_xlsx.py --input ./sample --page 2 --mode both --merge

模式:
    both  图片 + 文本都写（默认：看得见原貌，又能复制数字）
    text  只写表格/文本层
    image 只把该页渲染成图片贴进工作表（扫描件、版式复杂时用）

取表引擎 (--engine):
    auto     先 pdfplumber，抽不到再 camelot（默认）
    plumber  只用 pdfplumber
    camelot  只用 camelot（flavor=auto，自动选 lattice/network）

依赖: PyMuPDF(fitz)、pdfplumber、openpyxl；camelot-py 可选（无边框表/更准的框线识别）
"""
import argparse
import io
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

INVALID_SHEET_CHARS = set(r"[]:*?/\\")


def safe_sheet_name(name: str, used: set) -> str:
    base = "".join("_" if c in INVALID_SHEET_CHARS else c for c in name).strip() or "Sheet"
    base = base[:31]
    name, n = base, 2
    while name in used:
        suffix = f"_{n}"
        name = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(name)
    return name


def collect_pdfs(items):
    paths = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            paths += sorted([x for x in p.rglob("*") if x.suffix.lower() == ".pdf"])
        elif p.suffix.lower() == ".pdf" and p.exists():
            paths.append(p)
    # 去重保序
    seen, out = set(), []
    for p in paths:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def tables_pdfplumber(pdf_path: Path, page_index: int):
    """返回该页的表格列表，每张表是二维字符串列表。"""
    tables = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        if page_index >= len(pdf.pages):
            return tables
        page = pdf.pages[page_index]
        for t in page.extract_tables():
            rows = [[(c or "").strip() for c in row] for row in t]
            rows = [r for r in rows if any(r)]
            if rows:
                tables.append(rows)
    return tables


def tables_camelot(pdf_path: Path, page_index: int, flavor: str = "auto"):
    """camelot 抽表，返回 (tables, notes)。flavor=auto 让 camelot 自己选 lattice/network。

    页码越界时必须提前返回 —— 实测 camelot 会抛
    `IndexError: list index out of range`，整个批处理直接中断。
    """
    import warnings

    import camelot  # 只在需要时导入，没装也不影响其它模式

    try:
        with fitz.open(str(pdf_path)) as doc:
            if page_index >= doc.page_count:
                return [], [f"第 {page_index + 1} 页超出范围（共 {doc.page_count} 页）"]
    except Exception as e:  # noqa: BLE001
        return [], [f"无法读取页数：{type(e).__name__}"]

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # camelot 的 "auto-detected per-page flavors" 只是提示
            found = camelot.read_pdf(str(pdf_path), pages=str(page_index + 1), flavor=flavor)
    except Exception as e:  # noqa: BLE001
        return [], [f"camelot 失败：{type(e).__name__}: {e}"]

    tables, notes = [], []
    for t in found:
        rows = [[(c if isinstance(c, str) else str(c)).strip() for c in row] for row in t.df.values.tolist()]
        rows = [r for r in rows if any(r)]
        if rows:
            tables.append(rows)
            rep = t.parsing_report
            notes.append(f"accuracy {rep.get('accuracy')} / whitespace {rep.get('whitespace')}")
    return tables, notes


def extract_tables(pdf_path: Path, page_index: int, engine: str = "auto"):
    """engine: auto(先 pdfplumber，抽不到再 camelot) / plumber / camelot"""
    if engine == "plumber":
        return tables_pdfplumber(pdf_path, page_index), []
    if engine == "camelot":
        return tables_camelot(pdf_path, page_index)
    tables = tables_pdfplumber(pdf_path, page_index)
    if tables:
        return tables, []
    return tables_camelot(pdf_path, page_index)


def extract_text(pdf_path: Path, page_index: int):
    with fitz.open(str(pdf_path)) as doc:
        if page_index >= doc.page_count:
            return ""
        return doc[page_index].get_text("text").strip()


def render_page_png(pdf_path: Path, page_index: int, dpi: int = 150) -> bytes:
    with fitz.open(str(pdf_path)) as doc:
        if page_index >= doc.page_count:
            return b""
        pix = doc[page_index].get_pixmap(dpi=dpi)
        return pix.tobytes("png")


THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="F2F2F2")


def write_table(ws, start_row: int, rows, title: str | None = None) -> int:
    r = start_row
    if title:
        c = ws.cell(row=r, column=1, value=title)
        c.font = Font(bold=True, size=11)
        r += 1
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            cell = ws.cell(row=r, column=j + 1, value=val)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=False)
            if i == 0:
                cell.font = Font(bold=True)
                cell.fill = HEAD_FILL
        r += 1
    return r + 1


def autofit(ws, max_width=60):
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None or isinstance(cell.value, str) is False:
                continue
            # 中文按 2 个字符宽度估算
            w = sum(2 if ord(ch) > 127 else 1 for ch in str(cell.value))
            widths[cell.column] = max(widths.get(cell.column, 8), min(w + 2, max_width))
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def build(pdfs, page_spec, mode: str, out_path: Path, merge: bool, engine: str = "auto"):
    wb = Workbook()
    wb.remove(wb.active)
    used = set()
    report = []

    for idx, pdf_path in enumerate(pdfs):
        page_no = page_spec[idx] if isinstance(page_spec, list) else page_spec
        page_index = page_no - 1
        sheet_name = safe_sheet_name(pdf_path.stem, used)
        ws = wb.create_sheet(sheet_name)

        row_cursor = 1
        c = ws.cell(row=row_cursor, column=1, value=f"{pdf_path.name} · 第 {page_no} 页")
        c.font = Font(bold=True, size=12)
        row_cursor += 2

        n_tables = 0
        if mode in ("image", "both"):
            png = render_page_png(pdf_path, page_index)
            if png:
                img = XLImage(io.BytesIO(png))
                # 缩放至合适宽度（约 900px）
                scale = min(1.0, 900 / img.width)
                img.width, img.height = int(img.width * scale), int(img.height * scale)
                ws.add_image(img, f"A{row_cursor}")
                # 预留图片占用的行
                row_cursor += int(img.height / 20) + 2

        if mode in ("text", "both"):
            tables, notes = extract_tables(pdf_path, page_index, engine)
            n_tables = len(tables)
            if tables:
                if merge:
                    all_rows = []
                    for t in tables:
                        all_rows += t
                        all_rows.append([""])
                    row_cursor = write_table(ws, row_cursor, all_rows, "表格（已合并本页全部表）")
                else:
                    for k, t in enumerate(tables, 1):
                        row_cursor = write_table(ws, row_cursor, t, f"表格 {k}")
            # 有表格时不再重复写文本层（文本层会把单元格内容按行拆散，噪声大）
            text = "" if tables else extract_text(pdf_path, page_index)
            if text:
                c = ws.cell(row=row_cursor, column=1, value="文本层")
                c.font = Font(bold=True, size=11)
                row_cursor += 1
                for line in text.splitlines():
                    if line.strip():
                        ws.cell(row=row_cursor, column=1, value=line.strip())
                        row_cursor += 1
            if not tables and not text:
                ws.cell(row=row_cursor, column=1, value="（该页无文本层，可能是扫描件，建议用 --mode image）")

        autofit(ws)
        ws.freeze_panes = "A1"
        report.append((pdf_path.name, sheet_name, n_tables, notes))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", nargs="+", required=True, help="PDF 文件或目录（可多个）")
    ap.add_argument("--page", type=int, default=2, help="要提取的页码，从 1 开始，默认 2")
    ap.add_argument("--pages", default="", help="按 PDF 顺序逐个指定页码，逗号分隔，如 1,3,3,5；给了它就用它")
    ap.add_argument("--out", default="./out/result.xlsx")
    ap.add_argument("--mode", choices=["text", "image", "both"], default="both")
    ap.add_argument("--engine", choices=["auto", "plumber", "camelot"], default="auto",
                    help="取表引擎：auto=先 pdfplumber，抽不到再用 camelot（默认）")
    ap.add_argument("--merge", action="store_true", help="把本页多张表拼成一张")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.input)
    if not pdfs:
        print("ERROR: no PDF found", file=sys.stderr)
        return 1

    page_spec = args.page
    if args.pages.strip():
        try:
            pages = [int(x) for x in args.pages.replace("，", ",").split(",") if x.strip()]
        except ValueError:
            print("ERROR: --pages 只接受逗号分隔的整数", file=sys.stderr)
            return 1
        if len(pages) != len(pdfs):
            print(f"ERROR: --pages 给了 {len(pages)} 个页码，但找到 {len(pdfs)} 个 PDF", file=sys.stderr)
            return 1
        page_spec = pages
        print(f"PDFs: {len(pdfs)}  pages: {pages}  mode: {args.mode}  engine: {args.engine}")
    else:
        print(f"PDFs: {len(pdfs)}  page: {args.page}  mode: {args.mode}  engine: {args.engine}")

    report = build(pdfs, page_spec, args.mode, Path(args.out), args.merge, args.engine)
    for name, sheet, nt, notes in report:
        extra = f"  ({'; '.join(notes)})" if notes else ""
        print(f"  {name} -> sheet [{sheet}]  tables: {nt}{extra}")
    print(f"OUT: {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
