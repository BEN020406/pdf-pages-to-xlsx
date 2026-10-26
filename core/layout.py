# -*- coding: utf-8 -*-
"""core.layout —— 按「表格线」还原版式：单元格、合并、列宽、字号。

为什么需要这个模块（实测依据）：
    现在这条链路（OCR 聚类 / 大模型返回 rows）只能给出「一行一行的文本」，
    写进 Excel 就是一列一列的值，原表的合并标题、居中、列宽、字号全丢。
    客户那张税表第 4 页有三个子表、跨列居中的表名、$ 单独一列，
    用文本流表达必然走样。

做法：
    1. img2table（OpenCV 线检测）切出单元格 bbox —— 精度来自印刷线，不靠猜；
    2. 把所有 cell 的 x/y 边缘聚成全局行列边界，得到严整的网格；
    3. bbox 跨多列/多行的 cell 就是合并单元格（img2table 会把同一合并区
       重复成多个同 bbox 的 cell，这里先去重）；
    4. 文字用现有的 RapidOCR 盒子，按「框中心落在哪个单元格」归位；
    5. 字号由文字框高度换算（px → pt = h * 72 / dpi），不认得字体族，
       只还原字号/粗细/对齐/边框。

只依赖 CPU，不联网。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# 合并同一行的重复 cell 时允许的像素误差
_BBOX_TOL = 3.0


@dataclass
class CellBox:
    """一个单元格（合并区只保留左上角那个）。"""
    text: str = ""
    value: float | None = None
    kind: str = "empty"            # "text" | "number" | "empty"
    row: int = 0
    col: int = 0
    rowspan: int = 1
    colspan: int = 1
    align: str = "left"            # "left" | "center" | "right"
    font_size: float = 10.0
    bold: bool = False
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    text_h: float = 0.0            # 文字框高度（px），用来换算字号
    wrap: bool = False             # 一行放不下时让 Excel 换行，避免溢出到隔壁格
    is_footnote: bool = False      # 表格下方的脚注行（a) b) c) 那种说明）
    parts: list = field(default_factory=list)   # 归到这个格子的 OCR 框

    @property
    def merged(self) -> bool:
        return self.rowspan > 1 or self.colspan > 1


@dataclass
class TableBlock:
    """页面上一个独立的表格块（一页可能有多个，比如这张税表有三个子表）。"""
    x1: float
    y1: float
    x2: float
    y2: float
    n_rows: int
    n_cols: int
    dpi: int = 200
    col_widths: list[float] = field(default_factory=list)   # px
    row_heights: list[float] = field(default_factory=list)  # px
    cells: list[CellBox] = field(default_factory=list)      # 只含左上角锚点

    @property
    def n_merged(self) -> int:
        return sum(1 for c in self.cells if c.merged)


def _cluster_edges(values: list[float], tol: float = _BBOX_TOL) -> list[float]:
    """把近似相等的边界值合成一条线（保留中位数）。"""
    if not values:
        return []
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [statistics.median(g) for g in out]


def _index_of(edges: list[float], v: float) -> int:
    """v 落在第几条边界上（取最近的一条）。"""
    if not edges:
        return 0
    best, best_d = 0, float("inf")
    for i, e in enumerate(edges):
        d = abs(e - v)
        if d < best_d:
            best, best_d = i, d
    return best


def _raw_boxes(png: bytes) -> list[tuple[float, float, float, float]]:
    """img2table 切出来的所有 cell bbox（同一合并区会有重复）。"""
    import io

    from img2table.document import Image

    tables = Image(io.BytesIO(png)).extract_tables(ocr=None)
    out: list[tuple[float, float, float, float]] = []
    for t in tables:
        for cells in t.content.values():
            for c in cells:
                out.append((float(c.bbox.x1), float(c.bbox.y1),
                            float(c.bbox.x2), float(c.bbox.y2)))
    return out


def detect_blocks(png: bytes, dpi: int, *, ocr_boxes=None) -> list[TableBlock]:
    """把一页的 PNG 拆成若干表格块，并把 OCR 文字填进单元格。

    ocr_boxes: 可传入已经跑过的 OCR 结果（core.ocr.Box 列表），避免重复识别。
    """
    try:
        rects = _raw_boxes(png)
    except Exception:  # noqa: BLE001
        return []
    if not rects or ocr_boxes is None:
        return []

    # 同一个 bbox 重复出现（img2table 表示合并区的方式）只留一个
    uniq: list[tuple[float, float, float, float]] = []
    for r in rects:
        if not any(all(abs(a - b) <= _BBOX_TOL for a, b in zip(r, u)) for u in uniq):
            uniq.append(r)

    # 同一个表内的 cell 共享边界；用连通块把「挤在一起」的 cell 归为一个表
    uniq.sort(key=lambda r: (r[1], r[0]))
    blocks: list[list[tuple[float, float, float, float]]] = []
    for r in uniq:
        for b in blocks:
            if any(_close(r, o) for o in b):
                b.append(r)
                break
        else:
            blocks.append([r])

    out: list[TableBlock] = []
    for cells in blocks:
        blk = _build_block(cells, dpi, ocr_boxes)
        if blk is not None and blk.n_rows >= 2 and blk.n_cols >= 2:
            out.append(blk)
    out.sort(key=lambda b: b.y1)
    # 整页统一字号（表名/表头/正文各一档），必须等所有块都建好再算
    _unify_fonts(out)
    return out


@dataclass
class LooseLine:
    """表格线之外的整行文字（页眉的纳税人/年度/附件号之类）。"""
    text: str
    y: float
    x1: float
    x2: float
    font_size: float = 10.0


@dataclass
class PageLayout:
    blocks: list[TableBlock] = field(default_factory=list)
    loose: list[LooseLine] = field(default_factory=list)


def analyze_page(png: bytes, dpi: int, *, ocr_boxes=None) -> PageLayout:
    """检测页面版式：表格块 + 表格外的整行文字。"""
    blocks = detect_blocks(png, dpi, ocr_boxes=ocr_boxes)
    if not blocks or ocr_boxes is None:
        return PageLayout(blocks=list(blocks))

    top = min(b.y1 for b in blocks)
    loose = _loose_lines(ocr_boxes, blocks, top, dpi)
    return PageLayout(blocks=list(blocks), loose=loose)


def _loose_lines(ocr_boxes, blocks, top: float, dpi: int) -> list[LooseLine]:
    """取第一张表格上方、且不在任何表格块里的文字，按行合并。"""
    outside = []
    for b in ocr_boxes:
        cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
        if cy > top - 2:
            continue
        if any(blk.x1 - 2 <= cx <= blk.x2 + 2 and blk.y1 - 2 <= cy <= blk.y2 + 2
               for blk in blocks):
            continue
        outside.append(b)
    if not outside:
        return []

    heights = sorted(b.y1 - b.y0 for b in outside)
    tol = max(6.0, heights[len(heights) // 2] * 0.6)

    rows: list[list] = []
    for b in sorted(outside, key=lambda b: b.y0):
        if rows and abs(b.y0 - rows[-1][0].y0) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])

    out: list[LooseLine] = []
    for group in rows:
        group.sort(key=lambda b: b.x0)
        text = " ".join(b.text for b in group).strip()
        if not text:
            continue
        h = max(b.y1 - b.y0 for b in group)
        out.append(LooseLine(text=text, y=min(b.y0 for b in group),
                             x1=min(b.x0 for b in group),
                             x2=max(b.x1 for b in group),
                             font_size=max(7.0, min(16.0, h * 80.0 / max(1, dpi)))))
    return out


def blocks_to_cells(blocks: list[TableBlock]) -> list[list]:
    """把若干表格块摊平成二维 CellOut，供校对页/汇总使用（导出走 blocks）。"""
    from .pipeline import CellOut

    rows: list[list] = []
    for bi, blk in enumerate(blocks):
        if bi:
            rows.append([CellOut("", None, "empty")])
        grid = [[CellOut("", None, "empty") for _ in range(blk.n_cols)]
                for _ in range(blk.n_rows)]
        for cell in blk.cells:
            note = ""
            if cell.merged:
                note = f"合并 {cell.rowspan}×{cell.colspan}"
            grid[cell.row][cell.col] = CellOut(
                text=cell.text, value=cell.value, kind=cell.kind, note=note)
        rows.extend(grid)
    return rows


def _close(a, b) -> bool:
    """两个矩形是否相邻/重叠（行或列边界相差不超过 2px 即视为同一张表）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(bx1 - ax2, ax1 - bx2)
    dy = max(by1 - ay2, ay1 - by2)
    return dx <= 2 and dy <= 2 or (dx < 0 and dy < 0)


_CURRENCY = None


def _currency_re():
    global _CURRENCY
    if _CURRENCY is None:
        import re
        _CURRENCY = re.compile(r"^(?:US\$|HK\$|MOP|RMB|CNY|\$|¥|€|£)$", re.I)
    return _CURRENCY


def _attach_currency(boxes):
    """把单独的「$」并到它右边紧邻的数字上。

    实测：表格里 $ 常常被识别成一个独立小框，位置略低于数字，
    拼进单元格就变成「101,325,990 $」，货币符号在后 —— 解析必然失败。
    这里先按几何关系把 $ 贴到右边的数字前面，再谈归格。
    """
    cur = _currency_re()
    symbols, rest = [], []
    for b in boxes:
        (symbols if cur.match(b.text.strip()) else rest).append(b)
    if not symbols:
        return boxes

    used: set[int] = set()
    leftover = []
    for s in symbols:
        best, best_d = None, float("inf")
        for i, b in enumerate(rest):
            if i in used or b.x0 < s.x1 - 2:
                continue
            overlap = min(s.y1, b.y1) - max(s.y0, b.y0)
            if overlap < 0.4 * min(s.y1 - s.y0, b.y1 - b.y0):
                continue
            d = b.x0 - s.x1
            if d < best_d and d <= 4 * max(s.y1 - s.y0, 1):
                best, best_d = i, d
        if best is None:
            leftover.append(s)
            continue
        b = rest[best]
        b.text = f"{s.text.strip()} {b.text}".strip()
        used.add(best)

    return rest + leftover


def _best_cell_for_box(box, grid: dict):
    """文字框归格：取「重叠面积最大」的单元格，而不是看中心点。

    实测：一行标签被 OCR 认出很高的框（跨了两行）时，按中心点会整段掉进
    下一行，于是「2. 購入…」的标签和它的金额分家、整表错行。
    """
    best, best_area = None, 0.0
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    for cell in grid.values():
        ix = max(0.0, min(cell.x2, box.x1) - max(cell.x1, box.x0))
        iy = max(0.0, min(cell.y2, box.y1) - max(cell.y1, box.y0))
        area = ix * iy
        if area > best_area:
            best, best_area = cell, area
    box_area = max(1.0, (box.x1 - box.x0) * (box.y1 - box.y0))
    if best is not None and best_area >= 0.5 * box_area:
        return best
    # 面积不到一半（框压在两行之间）时退回中心点
    for cell in grid.values():
        if cell.x1 - 1 <= cx <= cell.x2 + 1 and cell.y1 - 1 <= cy <= cell.y2 + 1:
            return cell
    return best


def _fill_grid(rects, xs: list[float], ys: list[float], ocr_boxes) -> dict:
    """按给定的行列边界把矩形和文字装进格子。"""
    grid: dict[tuple[int, int], CellBox] = {}
    for x1, y1, x2, y2 in rects:
        c0, c1 = _index_of(xs, x1), _index_of(xs, x2) - 1
        r0, r1 = _index_of(ys, y1), _index_of(ys, y2) - 1
        c1 = max(c0, c1)
        r1 = max(r0, r1)
        if (r0, c0) in grid:
            continue
        grid[(r0, c0)] = CellBox(row=r0, col=c0, rowspan=r1 - r0 + 1,
                                 colspan=c1 - c0 + 1, x1=x1, y1=y1, x2=x2, y2=y2)

    for box in ocr_boxes:
        hit = _best_cell_for_box(box, grid)
        if hit is None:
            continue
        hit.parts.append(box)

    # 同一格里的文字按「先上后下、再左到右」拼起来。
    # 不能直接用 OCR 的返回顺序：实测「$ 101,325,990」里 $ 的中心点略低，
    # 会被排到数字后面，拼成「101,325,990 $」，货币符号就解析不掉了。
    for cell in grid.values():
        if not cell.parts:
            continue
        cell.parts.sort(key=lambda b: (round(b.y0 / 6.0), b.x0))
        cell.text = _tidy(" ".join(p.text for p in cell.parts))
        cell.text_h = max(p.y1 - p.y0 for p in cell.parts)
    return grid


def _symbolic_columns(grid: dict, n_cols: int, col_widths: list[float]) -> list[int]:
    """找出「只有货币符号」的窄列。

    实测这张税表：金额列和右侧 a)/b) 注释列之间多切了一条竖线，
    $ 的中心点落进那条 49px 的窄列，于是 $ 和数字被拆到两格，
    导出后看起来像「$ 单独一列、金额另起一列」。这里把这种列并回左邻列。
    """
    out: list[int] = []
    cur = _currency_re()
    for c in range(n_cols):
        if col_widths[c] > 120:      # 只有窄列才可能是「符号列」
            continue
        texts = [cell.text.strip() for (r, cc), cell in grid.items()
                 if cc == c and cell.text.strip()]
        if len(texts) < 2:
            continue
        sym = sum(1 for t in texts if cur.match(t))
        if sym / len(texts) >= 0.6:
            out.append(c)
    return out


def _rows_needing_more_height(block: TableBlock) -> None:
    """字被 wrap 成多行时，把行高抬到放得下 —— 否则 Excel 里文字被裁掉。"""
    import math

    need = [0.0] * block.n_rows
    for cell in block.cells:
        if not cell.wrap or not cell.text or cell.rowspan > 1:
            continue
        width_px = sum(block.col_widths[cell.col:cell.col + cell.colspan])
        avail_pt = max(1.0, (width_px - 6) * 72.0 / max(1, block.dpi))
        em = sum(1.0 if ord(ch) > 127 else 0.55 for ch in cell.text)
        lines = max(1, math.ceil(cell.font_size * em / avail_pt))
        need[cell.row] = max(need[cell.row], lines * cell.font_size * 1.3 + 2)
    for i, h_pt in enumerate(need):
        if h_pt:
            block.row_heights[i] = max(block.row_heights[i],
                                       h_pt * block.dpi / 72.0)


def _unify_fonts(blocks: list[TableBlock]) -> None:
    """整页统一字号：表名 / 表头 / 正文三档，一页一个值。

    实测两个坑：
        1. 字号原来是从每个 OCR 文字框的高度估的，框高有抖动（同一行能差 20%），
           导出后一张表里字大大小小；
        2. 只按「每张表」统一时，同一页的三个子表正文变成 11.8 / 11.3 / 11.8，
           三张表并排看还是不一致。
    所以先按整页合并取中位数，再回填到所有块。
    """
    groups: dict[str, list[float]] = {"title": [], "header": [], "body": []}
    for blk in blocks:
        for cell in blk.cells:
            if cell.kind == "empty" or not cell.text or cell.is_footnote:
                continue
            if _is_title(cell):
                groups["title"].append(cell.font_size)
            elif _is_header(cell):
                groups["header"].append(cell.font_size)
            else:
                groups["body"].append(cell.font_size)

    body = statistics.median(groups["body"]) if groups["body"] else 10.0
    header = max(statistics.median(groups["header"]) if groups["header"] else 0.0,
                 body * 1.1)
    title = max(statistics.median(groups["title"]) if groups["title"] else 0.0,
                body * 1.3)

    for blk in blocks:
        for cell in blk.cells:
            if cell.kind == "empty" or not cell.text:
                continue
            if cell.is_footnote:
                cell.font_size = min(cell.font_size, 9.0)
            elif _is_title(cell):
                cell.font_size = title
            elif _is_header(cell):
                cell.font_size = header
            else:
                cell.font_size = body


def _is_title(cell: CellBox) -> bool:
    return cell.row == 0 or (cell.bold and cell.align == "center" and cell.colspan >= 2)


def _is_header(cell: CellBox) -> bool:
    return not _is_title(cell) and (cell.bold or cell.row == 1)


def _mark_wrap_if_needed(cell: CellBox, dpi: int) -> None:
    """统一字号之后，放不下的格子只标 wrap，**不再改字号**。

    这一点很关键：字号一动，同一张表里又会出现大小不一。
    放不下就让 Excel 换行，行高由 _rows_needing_more_height 按需加高 ——
    框（行高/列宽）本来就该由原表的线决定，不能为了塞字把格子改歪。
    """
    if cell.kind == "empty" or not cell.text:
        return
    em = sum(1.0 if ord(ch) > 127 else 0.55 for ch in cell.text)
    if em <= 0:
        return
    avail_pt = max(0.0, (cell.x2 - cell.x1) - 6) * 72.0 / max(1, dpi)
    if cell.font_size * em > avail_pt:
        cell.wrap = True


def _widen_header_cells(grid: dict, n_cols: int, dpi: int) -> None:
    """表头跨列不够就向右扩一格。

    实测：img2table 在金额列右侧多切了一条很窄的 a)/b) 注释列，表头
    「本期 Do periodo 澳門幣（元）MOP」被切断，Excel 里折成 3 行还把字挤出格。
    规则很克制：只动表头行（第 0 行或加粗居中的格），只在右邻格完全空着时扩。
    """
    for cell in sorted(grid.values(), key=lambda c: (c.row, c.col)):
        if cell.kind == "empty" or not (cell.row == 0 or (cell.bold and cell.align == "center")):
            continue
        while cell.col + cell.colspan < n_cols:
            em = sum(1.0 if ord(ch) > 127 else 0.55 for ch in cell.text)
            span = cell.colspan
            width_pt = max(1.0, (cell.x2 - cell.x1) * 72.0 / max(1, dpi))
            if cell.font_size * em <= width_pt:
                break
            right = grid.get((cell.row, cell.col + span))
            if right is None or right.text.strip() or right.merged:
                break
            del grid[(cell.row, cell.col + span)]
            cell.colspan = span + 1
            cell.x2 = right.x2


def _mark_footnotes(grid: dict, n_rows: int, n_cols: int) -> None:
    """把表格底部那几行「整段说明文字」标成脚注。

    判据（对着实测数据定的，三条必须同时成立）：
        1. 这一行没有任何数字；
        2. 非空格子 <= 2 个（脚注是一整段话，数据行是「标签 + 金额」）；
        3. 总字数 >= 60。
    只用「行首是 a) b)」会误伤 —— 实测「b) 7. 加工及分包 Subcontratos」这种
    数据行也是 b) 开头（b) 是旁边注释列的小字），会被整行吞掉。
    """
    import re

    for r in range(n_rows - 1, 0, -1):
        cells = sorted((c for (rr, _cc), c in grid.items()
                        if rr == r and c.text.strip()), key=lambda c: c.col)
        if not cells:
            continue
        if any(c.kind == "number" for c in cells):
            continue
        text = " ".join(c.text.strip() for c in cells).strip()
        if len(cells) > 2 or len(text) < 60:
            continue
        # 数据行一定带序号（1. / 2. / 10.）；实测「10. 銷貨成本 …」也会通过上面三条
        if re.match(r"^\s*\d+\s*[.、)]", text):
            continue
        keep = cells[0]
        for c in cells[1:]:
            grid.pop((c.row, c.col), None)
        keep.text = text
        keep.is_footnote = True
        keep.colspan = max(1, n_cols - keep.col)
        keep.align = "left"
        keep.bold = False
        keep.wrap = True
        keep.font_size = min(keep.font_size, 9.0)


def _build_block(rects, dpi: int, ocr_boxes) -> TableBlock | None:
    ocr_boxes = _attach_currency(list(ocr_boxes))
    xs = _cluster_edges([r[0] for r in rects] + [r[2] for r in rects])
    ys = _cluster_edges([r[1] for r in rects] + [r[3] for r in rects])
    if len(xs) < 3 or len(ys) < 3:
        return None

    widths = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    grid = _fill_grid(rects, xs, ys, ocr_boxes)

    # 符号列并回左邻列：删掉那条竖线后重新装一次格子
    drop = _symbolic_columns(grid, len(widths), widths)
    if drop and len(xs) - len(drop) >= 3:
        keep = {d + 1 for d in drop if d + 1 < len(xs)}
        xs = [x for i, x in enumerate(xs) if i not in keep]
        widths = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        grid = _fill_grid(rects, xs, ys, ocr_boxes)

    n_cols, n_rows = len(xs) - 1, len(ys) - 1
    _classify(grid, n_rows, dpi)
    # 先尝试表头跨列，再给放不下的格标 wrap（不再改字号）
    _widen_header_cells(grid, n_cols, dpi)
    for cell in grid.values():
        _mark_wrap_if_needed(cell, dpi)
    _mark_footnotes(grid, n_rows, n_cols)

    block = TableBlock(
        x1=min(r[0] for r in rects), y1=min(r[1] for r in rects),
        x2=max(r[2] for r in rects), y2=max(r[3] for r in rects),
        n_rows=n_rows, n_cols=n_cols,
        dpi=dpi,
        col_widths=widths,
        row_heights=[ys[i + 1] - ys[i] for i in range(n_rows)],
        cells=list(grid.values()),
    )
    _rows_needing_more_height(block)
    return block


def _tidy(raw: str) -> str:
    """收拾 OCR 拼出来的脏文本，只做形变、不改语义。

    实测：贴货币符号时可能贴两次（"$ $ 101,325,990"），
    或者符号被排到数字后面（"101,325,990 $"）—— 两种都解析不出来。
    """
    import re

    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    s = re.sub(r"^(\$\s*)+", "$ ", s)          # 多个前导 $ 合成一个
    s = re.sub(r"(\s*\$)+$", " $", s)          # 尾部的 $ 挪到语义上等价的位置
    if s.endswith(" $") and not s.startswith("$"):
        s = "$ " + s[:-2].strip()
    return s.strip()


def _classify(grid: dict[tuple[int, int], CellBox], n_rows: int, dpi: int) -> None:
    """定 kind / 对齐 / 字号 / 粗细。"""
    from .numbers import normalize

    for cell in grid.values():
        raw = _tidy(cell.text)
        cell.text = raw
        if not raw:
            cell.kind = "empty"
            continue
        parsed = normalize(raw)
        if parsed.value is not None:
            cell.kind = "number"
            cell.value = parsed.value
            cell.align = "right"
        else:
            cell.kind = "text"
            # 跨 3 列以上且不长的，基本是表名/分段标题 —— 居中加粗
            if cell.colspan >= 3 and len(raw) <= 60:
                cell.align = "center"
                cell.bold = True
            elif cell.row == 0:
                cell.align = "center"
                cell.bold = True
            else:
                cell.align = "left"

        # 字号：文字框高度 ≈ 0.9em，pt = px * 72/dpi / 0.9 ≈ px * 80/dpi
        if cell.text_h > 0:
            cell.font_size = max(7.0, min(18.0, cell.text_h * 80.0 / max(1, dpi)))
