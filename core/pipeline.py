# -*- coding: utf-8 -*-
"""core.pipeline —— 单页处理编排：文本层 → 本地 OCR → 大模型 三级级联。

设计（依据实测）：

- 有文本层且抽表成功：**不调 API**，免费且精确；
- 有文本层但抽表失败，或纯扫描件：本地 RapidOCR 打底（实测中文几乎全对），
  再用大模型做结构标注 / 整表兜底；
- 数字一律走 numbers.normalize，解析不了就留空并标红，绝不猜。

一页的产物是 `PageResult`，包含 Grid（最终表）+ ReconcileReport（来源追溯）。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from .llm import LLMClient, LLMError
from .numbers import is_numeric_like, normalize
from .ocr import OcrGrid, is_weak_grid, reconstruct, run_ocr
from .reconcile import CellVerdict, ReconcileReport, Verdict, reconcile_cell

# 渲染 DPI：实测 150 已够 Qwen 24/24，200 更稳但 token 翻倍
DEFAULT_DPI = 150
# 单边上限，防超大页爆内存/超 token
MAX_SIDE_PX = 4000


@dataclass
class CellOut:
    """最终写进 Excel 的单元格。"""
    text: str
    value: float | None
    kind: str                 # "text" | "number" | "empty"
    verdict: Verdict | None = None
    note: str = ""
    indent: int = 0

    @property
    def color(self) -> str | None:
        from .reconcile import COLORS
        return COLORS.get(self.verdict) if self.verdict else None


@dataclass
class PageResult:
    path: Path
    page_no: int
    source: str                                  # "text" | "table" | "ocr" | "llm"
    cells: list[list[CellOut]] = field(default_factory=list)
    report: ReconcileReport = field(default_factory=ReconcileReport)
    png: bytes = b""
    layout: object | None = None                 # TableLayout，有则可用于还原版式
    notes: list[str] = field(default_factory=list)
    cost_cny: float = 0.0
    error: str = ""
    weak_structure: bool = False                 # 没抽出表，多数行只有整行文本
    blocks: list = field(default_factory=list)   # list[TableBlock]，按表格线还原的版式
    loose: list = field(default_factory=list)    # list[LooseLine]，表格外的页眉文字

    @property
    def n_rows(self) -> int:
        return len(self.cells)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.cells), default=0)

    @property
    def review_count(self) -> int:
        """需人工复核的单元格数。

        以对账报告为准（它才是权威记录）；没有报告时退回看单元格上的标记。
        早期版本只看 cells，导致导出汇总显示 0、但「OCR校验」页里却有红格。
        """
        if self.report.cells:
            return self.report.review_count
        return sum(1 for r in self.cells for c in r
                   if c.verdict in (Verdict.CONFLICT, Verdict.MODEL))


def render_page(
    doc: fitz.Document,
    page_index: int,
    dpi: int = DEFAULT_DPI,
    clip: tuple[float, float, float, float] | None = None,
) -> bytes:
    """渲染一页为 PNG。

    clip 是**归一化坐标** (x0, y0, x1, y1)，取值 0~1。
    用于只上传表格区域（裁掉页眉页脚里的公司名等），或用户手动画框重跑。
    """
    page = doc[page_index]
    rect = page.rect
    clip_rect = None
    if clip:
        x0, y0, x1, y1 = clip
        x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
        y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
        if (x1 - x0) > 0.02 and (y1 - y0) > 0.02:
            clip_rect = fitz.Rect(
                rect.x0 + (rect.x1 - rect.x0) * x0,
                rect.y0 + (rect.y1 - rect.y0) * y0,
                rect.x0 + (rect.x1 - rect.x0) * x1,
                rect.y0 + (rect.y1 - rect.y0) * y1,
            )

    pix = page.get_pixmap(dpi=dpi, clip=clip_rect)
    if max(pix.width, pix.height) > MAX_SIDE_PX:
        scale = MAX_SIDE_PX / max(pix.width, pix.height)
        pix = page.get_pixmap(dpi=int(dpi * scale), clip=clip_rect)
    return pix.tobytes("png")


def _grid_to_cells(grid: OcrGrid) -> list[list[CellOut]]:
    """OcrGrid → CellOut 二维表，数字走规范化，解析不了留空待人工。"""
    out: list[list[CellOut]] = []
    for row in grid.rows:
        line: list[CellOut] = []
        for i, box in enumerate(row):
            if box is None:
                line.append(CellOut("", None, "empty"))
                continue
            text = box.text.strip()
            if not text:
                line.append(CellOut("", None, "empty"))
                continue
            parsed = normalize(text)
            if parsed.value is not None:
                line.append(CellOut(text, parsed.value, "number", note=parsed.rule))
            elif is_numeric_like(text):
                # 看着像数字但解析不了 —— 标出来，别当文本糊过去
                line.append(CellOut(text, None, "number", note=f"无法解析（{parsed.rule}）"))
            else:
                line.append(CellOut(text, None, "text"))
            if i == 0:
                line[-1].indent = grid.indent_level(box)
        out.append(line)
    return out


def _norm_text(s: str) -> str:
    """行首文本归一化，用于行对齐。"""
    import re
    return re.sub(r"[\s:：、,，.。()（）—\-]", "", str(s or ""))


def _align_rows(model_rows: list[list], local_rows: list[list[str]]) -> list[int]:
    """把模型行对齐到本地 OCR 行，返回每个模型行对应的本地行下标（-1 表示没找到）。

    实测：两份结果的**行数常常不等**（模型会多出/漏掉表头），
    按索引直接比会把 A 行的数字跟 B 行比，产生假差异。
    所以按第 0 列文本相似度对齐。
    """
    if not local_rows:
        return [-1] * len(model_rows)
    local_keys = [_norm_text(r[0] if r else "") for r in local_rows]
    mapping: list[int] = []
    used: set[int] = set()
    for row in model_rows:
        key = _norm_text(row[0] if row else "")
        best, best_score = -1, 0
        for i, lk in enumerate(local_keys):
            if i in used or not lk or not key:
                continue
            # 互相包含即视为同一行（"减：营业成本" vs "营业成本"）
            if key == lk:
                score = 100
            elif key.endswith(lk) or lk.endswith(key):
                score = 80
            elif lk in key or key in lk:
                score = 60
            else:
                score = 0
            if score > best_score:
                best, best_score = i, score
        if best >= 0:
            used.add(best)
        mapping.append(best)
    return mapping


def _drop_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    """删掉整列为空的列。

    实测：本地 OCR 经常在「项目」和金额之间多出一个空列（4 列），
    而大模型返回 3 列。按位置逐格对账时会整体错位一格，
    导致第 3 列拿到第 2 列的本地值（导出结果里 1,100,000.00 的
    value 变成了 1,234,567.89）。
    """
    if not rows:
        return rows
    n_cols = max(len(r) for r in rows)
    keep = [i for i in range(n_cols)
            if any(len(r) > i and str(r[i] or "").strip() for r in rows)]
    if not keep:
        return rows
    return [[r[i] if i < len(r) else "" for i in keep] for r in rows]


def _rows_to_cells(
    rows: list[list],
    model_raw: list[list[str]] | None = None,
    local_scores: list[list[float | None]] | None = None,
) -> tuple[list[list[CellOut]], ReconcileReport]:
    """把 LLM 返回的 rows 转成 CellOut，并与本地 OCR 逐格对账。

    只对**数字单元格**做对账；文本单元格直接采信模型（它读中文比数字可靠）。
    """
    report = ReconcileReport()
    out: list[list[CellOut]] = []

    # 先去掉本地的全空列，否则与模型的列数不一致会整体错位。
    #
    # 关键细节（实测）：不能按「整列是否有内容」判断 —— 本地 OCR 常把
    # 「单位：元」这类页眉残留在第 1 列，导致该列只在标题行有值、
    # 数据行全空，整列判定为「非空」，与模型的 3 列错位一格。
    # 正确做法：只看**数据区**（跳过开头 1~2 行标题），数据区全空的列才删。
    # 同时 score 必须和 raw 用同一套 keep 索引裁剪。
    if model_raw:
        n_cols = max(len(r) for r in model_raw)
        skip = 2 if len(model_raw) > 3 else 0      # 跳过标题/编制单位行
        data_rows = model_raw[skip:] or model_raw
        keep = [i for i in range(n_cols)
                if any(len(r) > i and str(r[i] or "").strip() for r in data_rows)]
        if keep and len(keep) != n_cols:
            model_raw = [[r[i] if i < len(r) else "" for i in keep] for r in model_raw]
            if local_scores:
                local_scores = [[row[i] if i < len(row) else None for i in keep]
                                for row in local_scores]

    mapping = _align_rows(rows, model_raw or [])

    for ri, row in enumerate(rows):
        li = mapping[ri] if ri < len(mapping) else -1
        local_line = model_raw[li] if (model_raw and li >= 0 and li < len(model_raw)) else []
        score_line = local_scores[li] if (local_scores and li >= 0 and li < len(local_scores)) else []
        line: list[CellOut] = []
        for ci, raw in enumerate(row):
            text = "" if raw is None else str(raw).strip()
            if not text:
                line.append(CellOut("", None, "empty"))
                continue

            parsed = normalize(text)
            numeric = parsed.value is not None or is_numeric_like(text)

            if not numeric:
                # 文本单元格：不做数值对账，避免整列误报 conflict
                line.append(CellOut(text, None, "text"))
                continue

            local_raw = str(local_line[ci] or "") if ci < len(local_line) else ""
            # 把本地 OCR 的置信度传下去 —— 实测漏传这个参数会让低分错误
            # （手写件把 700,000.00 读成 720000，置信度 0.729）被当成可靠值采信。
            score = score_line[ci] if (score_line and ci < len(score_line)) else None
            cv = reconcile_cell(local_raw or text, model_raw=text, ocr_score=score)
            cv.row, cv.col = ri, ci          # 记位置，界面人工回填时能对上
            report.add(cv)
            line.append(CellOut(text, cv.final_value, "number",
                                verdict=cv.verdict, note=cv.note))
        out.append(line)
    return out, report


def _stage(on_stage, text: str) -> None:
    """报告当前阶段，供 UI 显示「这一页卡在哪一步」。

    UI 回调出错绝不能把处理带崩，所以吞掉异常。
    """
    if on_stage is None:
        return
    try:
        on_stage(text)
    except Exception:  # noqa: BLE001
        pass


def process_page(
    path: Path,
    page_no: int,
    *,
    llm: LLMClient | None = None,
    force_ocr: bool = False,
    dpi: int = DEFAULT_DPI,
    use_llm: bool = True,
    clip: tuple[float, float, float, float] | None = None,
    on_stage=None,
) -> PageResult:
    """处理一页。page_no 从 1 开始。

    clip: 归一化框选区域 (x0, y0, x1, y1)，只处理/上传该区域。
          有文本层时忽略 clip（整页文本层解析更完整）。
    on_stage: 可选回调，参数是一句当前阶段描述（中文）。
    """
    res = PageResult(path=path, page_no=page_no, source="")
    _stage(on_stage, "打开 PDF")
    try:
        doc = fitz.open(str(path))
    except Exception as e:  # noqa: BLE001
        res.error = f"无法打开 PDF：{e}"
        return res

    # 加密 PDF：fitz 打开不报错，但取页时会抛
    # `ValueError: document closed or encrypted`（实测）。
    # 必须在这里拦住，否则整个批处理中断。
    if getattr(doc, "needs_pass", False) or getattr(doc, "is_encrypted", False):
        doc.close()
        res.error = "PDF 已加密，需要密码；请先用其它工具解密后再处理"
        return res

    try:
        if page_no < 1 or page_no > doc.page_count:
            res.error = f"页码 {page_no} 超出范围（共 {doc.page_count} 页）"
            return res
        idx = page_no - 1
        try:
            page = doc[idx]
        except Exception as e:  # noqa: BLE001
            res.error = f"无法读取第 {page_no} 页：{type(e).__name__}: {e}"
            return res
        _stage(on_stage, "读取文本层")
        text_layer = page.get_text("text").strip()

        # ---- 1) 文本层：先抽结构化表格（保版式的关键）
        # 注意：用户框选了区域时必须走 OCR 路径 ——
        # 文本层抽表是整页的，会忽略 clip，导致「框选重跑」对
        # 有文本层的页面完全失效（实测 bug）。
        if text_layer and not force_ocr and clip is None:
            _stage(on_stage, "抽取表格结构")
            layout = _try_extract_table(res.path, page_no)
            if layout is not None:
                res.source = "table"
                res.layout = layout
                res.cells = _layout_to_cells(layout)
                res.notes.append(
                    f"文本层抽表：{layout.n_rows} 行 × {layout.n_cols} 列"
                    f"（{'有框线' if layout.has_borders else '无框线'}），未调用 API")
                res.png = render_page(doc, idx, dpi)
                return res

            # 抽不出结构才退回按行切文本
            cells = _cells_from_text_layer(page)
            if cells:
                res.source = "text"
                res.cells = cells
                res.notes.append(
                    f"文本层 {len(text_layer)} 字符，但没抽到表格结构，按行切分（{len(cells)} 行）")
                res.png = render_page(doc, idx, dpi)
                return res

        # ---- 2) 本地 OCR 打底（框选时一定走这里）
        _stage(on_stage, "渲染页面")
        png = render_page(doc, idx, dpi, clip)
        res.png = png
        if clip:
            res.notes.append(
                "只处理框选区域 "
                f"x={clip[0]:.2f}~{clip[2]:.2f} y={clip[1]:.2f}~{clip[3]:.2f}（已裁掉区域外内容）")
        _stage(on_stage, "本地 OCR")
        boxes = run_ocr(png)
        if not boxes:
            res.error = "OCR 没有识别出任何文字（页面可能为空或全是图形）"
            return res

        # ---- 2b) 有表格线就按线还原版式（合并/列宽/字号），优先于文本流
        _stage(on_stage, "按表格线还原")
        try:
            from .layout import analyze_page, blocks_to_cells

            layout = analyze_page(png, dpi, ocr_boxes=boxes)
            blocks = layout.blocks
        except Exception as e:  # noqa: BLE001
            blocks, layout = [], None
            res.notes.append(f"表格线还原不可用，退回文本流：{type(e).__name__}: {e}")
        if blocks:
            res.blocks = blocks
            res.loose = list(getattr(layout, "loose", []) or [])
            res.cells = blocks_to_cells(blocks)
            res.source = "layout"
            merged = sum(b.n_merged for b in blocks)
            res.notes.append(
                f"按表格线还原：{len(blocks)} 个子表，"
                f"{sum(b.n_rows for b in blocks)} 行，合并单元格 {merged} 处")
            if use_llm and llm is not None:
                res.notes.append("版式已由表格线确定，未调用大模型（省费用、也不外发图片）")
            return res

        grid = reconstruct(boxes)
        res.cells = _grid_to_cells(grid)
        res.source = "ocr"
        if is_weak_grid(grid):
            res.weak_structure = True
            res.notes.append(
                "本页没抽出表格结构：多数行只有一整段文本（"
                f"{grid.n_cols} 列）。建议开启大模型重跑，或在校对页框选表格区域再识别")

        # ---- 3) 大模型增强
        if use_llm and llm is not None:
            try:
                _stage(on_stage, "大模型识别（含对账）")
                before = llm.usage.cost_cny
                out = llm.read_table(png, rows_hint=max(20, grid.n_cols * 4))
                rows = out.get("rows") or []
                if rows:
                    local_rows = [[b.text if b else "" for b in r] for r in grid.rows]
                    local_scores = [[b.score if b else None for b in r] for r in grid.rows]
                    cells, report = _rows_to_cells(rows, model_raw=local_rows,
                                                   local_scores=local_scores)
                    res.cells = cells
                    res.report = report
                    res.source = "llm"
                    res.notes.append(f"大模型返回 {len(rows)} 行，与本地 OCR 逐格对账")
                    res.notes.extend(str(n) for n in (out.get("notes") or [])[:5])
                else:
                    res.notes.append("大模型未返回 rows，保留本地 OCR 结果")
                res.cost_cny = llm.usage.cost_cny - before
            except LLMError as e:
                res.notes.append(f"大模型调用失败，已回退本地 OCR：{e}")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"大模型异常，已回退本地 OCR：{type(e).__name__}: {e}")
        else:
            res.notes.append("未启用大模型")

        return res
    finally:
        doc.close()


def _try_extract_table(path: Path, page_no: int):
    """尝试抽结构化表格；失败返回 None。

    顺序：pdfplumber（能识别框线/空白）→ 自己的 word 坐标聚类。
    实测：无框线的损益表 pdfplumber 抽不出来，但 word 聚类能稳定得到 13×3。
    """
    try:
        from .tables import extract_table, extract_by_words
        t = extract_table(path, page_no)
        if t is not None and not t.is_empty:
            return t
        t = extract_by_words(path, page_no)
        if t is not None and not t.is_empty:
            return t
    except Exception:  # noqa: BLE001
        pass
    return None


def _layout_to_cells(layout) -> list[list[CellOut]]:
    """TableLayout → CellOut 二维表。

    数字走 normalize；解析不了但看着像数字的，标 note 提醒。
    缩进按第 0 列的 x 偏移量化，保留损益表的层级。
    """
    from .tables import TableLayout

    assert isinstance(layout, TableLayout)
    out: list[list[CellOut]] = []
    base_x = layout.col_edges[0] if layout.col_edges else 0.0
    col_w = layout.col_widths[0] if layout.col_widths else 40.0
    step = max(8.0, col_w / 6.0)

    for ri, row in enumerate(layout.cells):
        line: list[CellOut] = []
        for ci, text in enumerate(row):
            text = (text or "").strip()
            if not text:
                line.append(CellOut("", None, "empty"))
                continue
            parsed = normalize(text)
            if parsed.value is not None:
                line.append(CellOut(text, parsed.value, "number", note=parsed.rule))
            elif is_numeric_like(text):
                line.append(CellOut(text, None, "number",
                                    note=f"无法解析（{parsed.rule}）"))
            else:
                cell = CellOut(text, None, "text")
                if ci == 0:
                    # 用前导空格数近似缩进（原表靠左边界偏移表达层级）
                    indent = 0
                    stripped = text.lstrip()
                    pad = len(text) - len(stripped)
                    if pad:
                        indent = max(0, min(6, pad // 2))
                    cell.indent = indent
                line.append(cell)
        out.append(line)
    return out


def _cells_from_text_layer(page: fitz.Page) -> list[list[CellOut]]:
    """文本层按行切分。没有表格结构，只给粗粒度行文本。

    真正抽表交给 tables 模块；这里只是保证「有文本层就绝不调 API」。
    """
    lines = [ln.strip() for ln in page.get_text("text").splitlines() if ln.strip()]
    if not lines:
        return []
    out: list[list[CellOut]] = []
    for ln in lines:
        parsed = normalize(ln)
        if parsed.value is not None and is_numeric_like(ln):
            out.append([CellOut(ln, parsed.value, "number", note=parsed.rule)])
        else:
            out.append([CellOut(ln, None, "text")])
    return out


def estimate_cost(n_pages: int, model: str = "qwen3-vl-plus") -> float:
    """按实测 token 量估成本：约 2200 输入 + 400 输出 / 页。"""
    from .llm import PRICE
    p = PRICE.get(model, {"in": 1.0, "out": 10.0})
    return n_pages * (2200 / 1e6 * p["in"] + 400 / 1e6 * p["out"])
