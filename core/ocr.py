# -*- coding: utf-8 -*-
"""core.ocr —— RapidOCR 封装 + 文字框→表格重建。

关键设计（都有实测依据）：

1. **数字列按左/右边缘择优聚类**：财务报表数字可能左对齐也可能右对齐，
   两种都试，取「簇内方差更小」的那一种（实测左对齐表左边缘簇方差 ≈ 0）。
2. **缩进保留**：损益表靠缩进表达层级，丢了就看不懂。按左边缘量化成层级。
3. **不猜数字**：OCR 原文原样交给 numbers.normalize，解析不了就留空待人工。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# RapidOCR 单例（首次调用才加载模型，实测 init+run 约 0.9s）
_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


@dataclass
class Box:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str
    score: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def run_ocr(png_bytes: bytes) -> list[Box]:
    """对一张 PNG 跑 OCR，返回按 y 排序的文字框。"""
    engine = get_engine()
    res = engine(png_bytes)
    boxes: list[Box] = []
    if res.boxes is None:
        return boxes
    for b, t, s in zip(res.boxes, res.txts, res.scores):
        xs = [float(p[0]) for p in b]
        ys = [float(p[1]) for p in b]
        boxes.append(Box(min(xs), max(xs), min(ys), max(ys), str(t), float(s)))
    boxes.sort(key=lambda b: b.cy)
    return boxes


# ---------------------------------------------------------------- 行聚类

def cluster_rows(boxes: list[Box]) -> list[list[Box]]:
    """按 y 中心归并成行。阈值用行高中位数的 0.55 倍（实测有效）。"""
    if not boxes:
        return []
    heights = [b.h for b in boxes]
    med_h = statistics.median(heights) or 1.0

    rows: list[dict] = []
    for b in sorted(boxes, key=lambda x: x.cy):
        placed = False
        for r in rows:
            if abs(b.cy - r["cy"]) < max(med_h, r["h"]) * 0.55:
                r["items"].append(b)
                n = len(r["items"])
                r["cy"] = (r["cy"] * (n - 1) + b.cy) / n
                r["h"] = max(r["h"], b.h)
                placed = True
                break
        if not placed:
            rows.append({"cy": b.cy, "h": b.h, "items": [b]})
    rows.sort(key=lambda r: r["cy"])
    return [sorted(r["items"], key=lambda b: b.x0) for r in rows]


# ---------------------------------------------------------------- 列聚类

def _cluster_1d(values: list[float], tol: float) -> list[list[float]]:
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _cluster_quality(clusters: list[list[float]]) -> float:
    """簇内标准差之和，越小说明这一侧边缘越整齐。"""
    if not clusters:
        return float("inf")
    total = 0.0
    for c in clusters:
        total += statistics.pstdev(c) if len(c) > 1 else 0.0
    return total


def find_columns(boxes: list[Box], tol_ratio: float = 0.6) -> list[tuple[float, float]]:
    """找列边界，返回 [(left, right), ...]。

    左对齐和右对齐都试，取簇更整齐的一侧；边缘都按中位数取，减少离群值影响。
    """
    if not boxes:
        return []
    widths = [b.x1 - b.x0 for b in boxes if b.x1 > b.x0]
    med_w = statistics.median(widths) if widths else 40.0
    tol = max(12.0, med_w * tol_ratio)

    left_cl = _cluster_1d([b.x0 for b in boxes], tol)
    right_cl = _cluster_1d([b.x1 for b in boxes], tol)

    use_left = _cluster_quality(left_cl) <= _cluster_quality(right_cl)
    clusters = left_cl if use_left else right_cl

    # 用簇内所有框的完整范围作为列边界
    bounds: list[tuple[float, float]] = []
    for c in clusters:
        if use_left:
            members = [b for b in boxes if any(abs(b.x0 - v) <= tol for v in c)]
        else:
            members = [b for b in boxes if any(abs(b.x1 - v) <= tol for v in c)]
        bounds.append((min(b.x0 for b in members), max(b.x1 for b in members)))
    bounds.sort()
    return bounds


def assign_columns(boxes: list[Box], bounds: list[tuple[float, float]]) -> list[int]:
    """把每个框分到最近的列。用中心点判断，避免跨列误判。"""
    idx = []
    for b in boxes:
        best, best_d = 0, float("inf")
        for i, (lo, hi) in enumerate(bounds):
            d = 0.0 if lo <= b.cx <= hi else min(abs(b.cx - lo), abs(b.cx - hi))
            if d < best_d:
                best, best_d = i, d
        idx.append(best)
    return idx


# ---------------------------------------------------------------- 数字列法
#
# 为什么要有第二套：密集表单（双语、行号 + 长标签 + 两个金额列）里，
# 左/右边缘的聚类会被长标签撑开，几条 bounds 互相覆盖，
# 结果每行所有框都落到同一列 —— 实测一整页 52 行只剩 1 个非空格。
# 金额是右对齐的，用「能解析成数字的框的右边缘」聚类非常稳，
# 再在最左数字列右边插一个标签列，就能还原 行号｜项目｜本期｜前期。

def columns_from_numbers(boxes: list[Box], tol_ratio: float = 0.6) -> list[tuple[float, float]]:
    """用数字框的右边缘找列，成功返回 bounds，失败返回 []。"""
    from .numbers import normalize

    nums = []
    for b in boxes:
        if normalize(b.text).value is not None:
            nums.append(b)
    if len(nums) < 4:
        return []

    tol = max(12.0, statistics.median([b.h for b in boxes]) * tol_ratio)
    groups = [c for c in _cluster_1d([b.x1 for b in nums], tol) if len(c) >= 3]
    if not groups:
        return []

    bounds: list[tuple[float, float]] = []
    for g in groups:
        members = [b for b in nums if any(abs(b.x1 - v) <= tol for v in g)]
        bounds.append((min(b.x0 for b in members), max(b.x1 for b in members)))
    bounds.sort()
    merged = [bounds[0]]
    for lo, hi in bounds[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))

    # 最左数字列（通常是行号）右边、下一个数字列左边之间是标签列
    if len(merged) >= 2:
        label = [b for b in boxes if b.x0 > merged[0][1] and b.x1 < merged[1][0]]
        if len(label) >= 3:
            merged.insert(1, (min(b.x0 for b in label), max(b.x1 for b in label)))
    return merged


def _column_score(row_boxes: list[list[Box]], bounds: list[tuple[float, float]]) -> float:
    """这套列边界到底用上了几列：每行去重后的列数取平均。"""
    if not bounds or len(bounds) > 8 or not row_boxes:
        return 0.0
    used = [len(set(assign_columns(r, bounds))) for r in row_boxes]
    return statistics.mean(used)


def pick_columns(boxes: list[Box], tol_ratio: float = 0.6,
                 row_boxes: list[list[Box]] | None = None) -> list[tuple[float, float]]:
    """两套列检测都跑，取「实际用上的列数更多」的那套。

    边缘聚类在稀疏表格上更准，数字列法在密集表单上更准，没有哪套永远赢，
    所以按测出来的效果选，而不是写死。
    """
    if not boxes:
        return []
    rows = row_boxes if row_boxes is not None else cluster_rows(boxes)
    base = find_columns(boxes, tol_ratio)
    alt = columns_from_numbers(boxes, tol_ratio)
    if alt and _column_score(rows, alt) > _column_score(rows, base) + 0.05:
        return alt
    return base


# ---------------------------------------------------------------- 成表

@dataclass
class OcrGrid:
    rows: list[list[Box | None]]
    n_cols: int
    bounds: list[tuple[float, float]]
    median_height: float
    base_x: float
    char_width: float
    boxes: list[Box] = field(default_factory=list)

    def indent_level(self, col0_box: Box | None) -> int:
        """第 0 列的缩进层级（0 = 顶格）。"""
        if col0_box is None:
            return 0
        step = max(self.char_width * 2, 8.0)
        return max(0, int(round((col0_box.x0 - self.base_x) / step)))


def is_weak_grid(grid: OcrGrid) -> bool:
    """这一页到底有没有抽出「表」？

    判据：七成以上的非空行只有一格有内容 —— 那就不是表，是整行文本。
    这种结果导出后用户没法用，必须在界面上说清，不能静默当成成功。
    """
    rows = [r for r in grid.rows if any(r)]
    if not rows:
        return True
    if grid.n_cols <= 1:
        return True
    multi = sum(1 for r in rows if sum(1 for b in r if b) >= 2)
    return multi / len(rows) < 0.3


def reconstruct(boxes: list[Box], *, tol_ratio: float = 0.6) -> OcrGrid:
    """把散落的文字框还原成二维表。"""
    if not boxes:
        return OcrGrid([], 0, [], 1.0, 0.0, 1.0)

    row_boxes = cluster_rows(boxes)
    bounds = pick_columns(boxes, tol_ratio, row_boxes=row_boxes)
    n_cols = max(1, len(bounds))

    grid: list[list[Box | None]] = []
    for rb in row_boxes:
        row: list[Box | None] = [None] * n_cols
        for b, ci in zip(rb, assign_columns(rb, bounds)):
            if row[ci] is None:
                row[ci] = b
            else:
                # 同格多框：拼成一段文本（保留第一个框的坐标做缩进判断）
                prev = row[ci]
                assert prev is not None
                row[ci] = Box(prev.x0, max(prev.x1, b.x1), prev.y0, prev.y1,
                              f"{prev.text} {b.text}", min(prev.score, b.score))
        grid.append(row)

    # 缩进基准：第 0 列所有框的最小左边缘
    col0 = [r[0] for r in grid if r and r[0] is not None]
    base_x = min((b.x0 for b in col0), default=0.0)
    widths = [b.x1 - b.x0 for b in boxes if b.x1 > b.x0]
    # 用「每个字符宽度」的粗估：宽度中位数 / 文本长度中位数
    char_widths = [(b.x1 - b.x0) / max(1, len(b.text)) for b in boxes if b.text]
    char_width = statistics.median(char_widths) if char_widths else 8.0

    return OcrGrid(
        rows=grid, n_cols=n_cols, bounds=bounds,
        median_height=statistics.median([b.h for b in boxes]) if boxes else 1.0,
        base_x=base_x, char_width=char_width, boxes=boxes,
    )
