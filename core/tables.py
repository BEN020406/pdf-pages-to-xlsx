# -*- coding: utf-8 -*-
"""core.tables —— 从 PDF 文本层抽取「带几何信息」的表格。

为什么要这个模块：
    旧实现只要页面有文本层，就按行切文本（`_cells_from_text_layer`），
    结果是 13 行 × 1 列 —— 明明 pdfplumber 能从同一页抽出完美的 4×3 表格。
    这是结构信息的白白丢失，不是识别能力不够。

本模块保留：
    - 单元格文本
    - 行边界 y / 列边界 x（PDF pt 坐标）
    - 原表框线宽度（用于导出时还原粗细）
    - 是否真的画了框线（决定导出要不要画黑框）
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pdfplumber


@dataclass
class TableLayout:
    """一张表的结构化描述，坐标单位是 PDF pt（1pt = 1/72 inch）。"""

    page_no: int
    bbox: tuple[float, float, float, float]
    row_edges: list[float]           # 长度 = 行数 + 1，从上到下
    col_edges: list[float]           # 长度 = 列数 + 1，从左到右
    cells: list[list[str]]
    has_borders: bool = False
    line_width: float = 0.8
    page_width: float = 595.0
    page_height: float = 842.0
    notes: list[str] = field(default_factory=list)

    # ---------------------------------------------------------- 尺寸
    @property
    def n_rows(self) -> int:
        return len(self.cells)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.cells), default=0)

    @property
    def row_heights(self) -> list[float]:
        """每行高度（pt）。"""
        if len(self.row_edges) >= 2:
            return [self.row_edges[i + 1] - self.row_edges[i]
                    for i in range(len(self.row_edges) - 1)]
        h = self.bbox[3] - self.bbox[1]
        return [h / max(1, self.n_rows)] * max(1, self.n_rows)

    @property
    def col_widths(self) -> list[float]:
        """每列宽度（pt）。"""
        if len(self.col_edges) >= 2:
            return [self.col_edges[i + 1] - self.col_edges[i]
                    for i in range(len(self.col_edges) - 1)]
        w = self.bbox[2] - self.bbox[0]
        return [w / max(1, self.n_cols)] * max(1, self.n_cols)

    @property
    def is_empty(self) -> bool:
        return self.n_rows == 0 or not any(
            any((c or "").strip() for c in row) for row in self.cells)


def _line_width_of(page) -> float:
    """取页面上表格线的主宽度。没有线返回 0。

    注意：不能只看 `page.lines` / `page.rects` —— 实测 `page.draw_rect()`
    画出来的框被 pdfplumber 归为 `curves`，那样会误判成「无框线」。
    `page.edges` 合并了 rect_edges + curve_edges + lines，覆盖全部情况。
    """
    widths = []
    try:
        for e in page.edges:
            w = e.get("linewidth") or 0
            if w > 0:
                widths.append(w)
    except Exception:  # noqa: BLE001
        pass
    if not widths:
        for coll in (page.lines, page.rects, page.curves):
            for o in coll:
                w = o.get("linewidth") or 0
                if w > 0:
                    widths.append(w)
    if not widths:
        return 0.0
    widths.sort()
    return float(widths[len(widths) // 2])


def _edges_from_table(t) -> tuple[list[float], list[float]]:
    """从 pdfplumber Table 提取行列边界。"""
    ys: set[float] = set()
    xs: set[float] = set()
    for cell in t.cells:
        if not cell:
            continue
        x0, y0, x1, y1 = cell
        ys.update((round(y0, 2), round(y1, 2)))
        xs.update((round(x0, 2), round(x1, 2)))
    return sorted(ys), sorted(xs)


def extract_layouts(pdf_path, *, pages: list[int] | None = None,
                    min_rows: int = 2, min_cols: int = 2) -> list[TableLayout]:
    """抽出所有「像表格」的结构。

    pages 为 1 基页码列表；None 表示全部页。
    只有行列数都达到 min_rows/min_cols 才收，避免把单行标题当成表。
    """
    out: list[TableLayout] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            targets = pages or range(1, len(pdf.pages) + 1)
            for pno in targets:
                if pno < 1 or pno > len(pdf.pages):
                    continue
                page = pdf.pages[pno - 1]
                lw = _line_width_of(page)
                try:
                    tables = page.find_tables()
                except Exception:  # noqa: BLE001
                    continue
                for t in tables:
                    try:
                        data = t.extract()
                    except Exception:  # noqa: BLE001
                        continue
                    if not data:
                        continue
                    rows = [[(c or "").strip() for c in row] for row in data]
                    rows = [r for r in rows if any(r)]
                    if len(rows) < min_rows:
                        continue
                    ncols = max(len(r) for r in rows)
                    if ncols < min_cols:
                        continue
                    ys, xs = _edges_from_table(t)
                    if len(ys) < 2 or len(xs) < 2:
                        ys = [t.bbox[1], t.bbox[3]]
                        xs = [t.bbox[0], t.bbox[2]]
                    layout = TableLayout(
                        page_no=pno,
                        bbox=tuple(round(v, 2) for v in t.bbox),  # type: ignore[arg-type]
                        row_edges=ys,
                        col_edges=xs,
                        cells=rows,
                        has_borders=lw > 0,
                        line_width=lw,
                        page_width=float(page.width),
                        page_height=float(page.height),
                    )
                    out.append(layout)
    except Exception:  # noqa: BLE001
        return out
    return out


def pick_main_table(layouts: list[TableLayout]) -> TableLayout | None:
    """一页可能抽出多张表，取面积最大、行数最多的那张。"""
    if not layouts:
        return None

    def score(t: TableLayout) -> tuple:
        w = t.bbox[2] - t.bbox[0]
        h = t.bbox[3] - t.bbox[1]
        return (t.n_rows * t.n_cols, w * h)

    return max(layouts, key=score)


def extract_table(pdf_path, page_no: int) -> TableLayout | None:
    """抽一页的主表。"""
    return pick_main_table(extract_layouts(pdf_path, pages=[page_no]))


# ---------------------------------------------------------------- 无框线兜底

def _cluster(values: list[float], tol: float) -> list[list[float]]:
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def extract_by_words(pdf_path, page_no: int, *, tol_ratio: float = 0.8) -> TableLayout | None:
    """无框线表格的兜底：用 fitz 的 word 级坐标自己聚类成行列。

    为什么不用 pdfplumber 的 text 策略：
        它靠空白推断，实测对「项目 | 本期金额 | 上期金额」这种
        中文左对齐 + 数字右对齐混排的表容易把列并掉。
        word 级坐标 + 双向聚类更可控。

    数字列按**右边缘**聚类（财务数字右对齐），文本列按**左边缘**聚类。
    """
    import statistics

    import fitz

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return None

    try:
        if page_no < 1 or page_no > doc.page_count:
            return None
        page = doc[page_no - 1]
        words = page.get_text("words")   # (x0, y0, x1, y1, text, block, line, wordno)
        if not words:
            return None

        # ---- 先聚成行（按 y 中心）
        heights = [w[3] - w[1] for w in words]
        med_h = statistics.median(heights) if heights else 10.0
        rows_raw: list[dict] = []
        for w in sorted(words, key=lambda x: (x[1] + x[3]) / 2):
            cy = (w[1] + w[3]) / 2
            for r in rows_raw:
                if abs(cy - r["cy"]) < med_h * 0.6:
                    r["items"].append(w)
                    n = len(r["items"])
                    r["cy"] = (r["cy"] * (n - 1) + cy) / n
                    break
            else:
                rows_raw.append({"cy": cy, "items": [w]})
        rows_raw.sort(key=lambda r: r["cy"])

        # ---- 判断哪些 token 是数字
        def is_num(t: str) -> bool:
            import re
            s = re.sub(r"[\s,，.]", "", t)
            return bool(s) and all(ch in "0123456789()（）-—–−/" for ch in s)

        # ---- 列边界
        # 思路：找「列之间的空白间隙」，而不是找列起点。
        # 实测教训：
        #   1) 分别按数字右边缘/文本左边缘聚类，会把 x=240~547 全并成一列；
        #   2) 用列起点合并，表头「项 目」被拆成两个词，多出一个假列。
        # 做法：把所有词的 x 区间投影到 x 轴，找宽度超过阈值的空白段，
        #      每个空白段的中点就是列分隔线。
        char_w = statistics.median(
            [(w[2] - w[0]) / max(1, len(w[4])) for w in words if w[4]]
        ) if words else 6.0

        spans = sorted((w[0], w[2]) for w in words)
        # 合并重叠区间，得到「有文字的横向区域」
        occupied: list[list[float]] = []
        for lo, hi in spans:
            if occupied and lo <= occupied[-1][1] + 1.0:
                occupied[-1][1] = max(occupied[-1][1], hi)
            else:
                occupied.append([lo, hi])

        # 收集所有间隙，用「相对阈值」判断哪些是真正的列分隔
        gaps = [b[0] - a[1] for a, b in zip(occupied, occupied[1:])]
        if not gaps:
            return None
        max_gap = max(gaps)
        # 实测：固定阈值 22.5 会把 22pt 的真间隙漏掉（少切一列）。
        # 改为「不小于最大间隙的 45%，且至少 1.2 个字宽」。
        gap_threshold = max(char_w * 1.2, max_gap * 0.45)

        cuts: list[float] = []
        for a, b in zip(occupied, occupied[1:]):
            if b[0] - a[1] >= gap_threshold:
                cuts.append((a[1] + b[0]) / 2)

        if not cuts:
            return None

        # 列区间 = 相邻切点之间
        left = min(w[0] for w in words)
        right = max(w[2] for w in words)
        edges = [left] + cuts + [right]
        bounds = list(zip(edges[:-1], edges[1:]))
        if len(bounds) < 2:
            return None

        # ---- 落格：按词框中心分配到所在列
        cells: list[list[str]] = []
        for r in rows_raw:
            line = [""] * len(bounds)
            for w in sorted(r["items"], key=lambda x: x[0]):
                cx = (w[0] + w[2]) / 2
                best, best_d = 0, float("inf")
                for i, (lo, hi) in enumerate(bounds):
                    d = 0.0 if lo <= cx <= hi else min(abs(cx - lo), abs(cx - hi))
                    if d < best_d:
                        best, best_d = i, d
                line[best] = (line[best] + " " + w[4]).strip() if line[best] else w[4]
            cells.append(line)

        cells = [r for r in cells if any(r)]
        if len(cells) < 2:
            return None

        # 删掉整列为空的列（实测：中文标题和数字之间会多出一列空隙）
        keep = [i for i in range(len(bounds))
                if any(len(r) > i and r[i] for r in cells)]
        if len(keep) < 2:
            return None
        if len(keep) != len(bounds):
            cells = [[r[i] if i < len(r) else "" for i in keep] for r in cells]
            bounds = [bounds[i] for i in keep]

        # 裁掉表格上方的标题/编制单位行：找到「像表头」的那一行作为起点。
        # 判据（实测）：表头行会跨越到最右侧的金额列；而标题行、编制单位行
        # 通常只占前两列。用「最右非空列索引 >= 列数-2」判断。
        n_cols_now = len(bounds)

        def spans_wide(row: list[str]) -> bool:
            idx = [i for i, c in enumerate(row) if c]
            return bool(idx) and max(idx) >= n_cols_now - 2

        start = 0
        for i in range(len(cells) - 1):
            if spans_wide(cells[i]) and spans_wide(cells[i + 1]):
                start = i
                break
        if start:
            cells = cells[start:]

        # 再次删掉整列为空的列（裁掉表头后可能出现）
        keep = [i for i in range(len(bounds))
                if any(len(r) > i and r[i] for r in cells)]
        if len(keep) < 2:
            return None
        if len(keep) != len(bounds):
            cells = [[r[i] if i < len(r) else "" for i in keep] for r in cells]
            bounds = [bounds[i] for i in keep]

        ys: list[float] = []
        for r in rows_raw:
            ys.extend([min(w[1] for w in r["items"]), max(w[3] for w in r["items"])])
        ys = sorted(set(round(v, 2) for v in ys))

        return TableLayout(
            page_no=page_no,
            bbox=(min(w[0] for w in words), min(w[1] for w in words),
                  max(w[2] for w in words), max(w[3] for w in words)),
            row_edges=ys,
            col_edges=[b[0] for b in bounds] + [bounds[-1][1]],
            cells=cells,
            has_borders=False,
            line_width=0.0,
            page_width=float(page.rect.width),
            page_height=float(page.rect.height),
            notes=["无框线，按文字坐标聚类"],
        )
    except Exception:  # noqa: BLE001
        return None
    finally:
        doc.close()
