# -*- coding: utf-8 -*-
"""core.tables 的测试。"""
from pathlib import Path

import fitz
import pytest

from core.tables import (
    TableLayout,
    extract_layouts,
    extract_table,
    pick_main_table,
)


@pytest.fixture(scope="module")
def bordered_pdf(tmp_path_factory):
    """造一张带框线的 4 行 3 列表。"""
    d = tmp_path_factory.mktemp("tables")
    p = d / "bordered.pdf"
    doc = fitz.open()
    page = doc.new_page()
    rows = [["项目", "本期金额", "上期金额"],
            ["营业收入", "1,234.56", "1,100.00"],
            ["营业成本", "800.00", "700.00"],
            ["净利润", "206.01", "195.00"]]
    x0, y0, cw, rh = 72, 120, 110, 26
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            rect = fitz.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
            page.draw_rect(rect, color=(0, 0, 0), width=1.2)
            page.insert_text((rect.x0 + 4, rect.y1 - 8), val, fontsize=9, fontname="china-s")
    doc.save(p)
    doc.close()
    return p


@pytest.fixture(scope="module")
def borderless_pdf(tmp_path_factory):
    """造一张无框线、靠空白对齐的表。"""
    d = tmp_path_factory.mktemp("tables2")
    p = d / "borderless.pdf"
    doc = fitz.open()
    page = doc.new_page()
    rows = [["项目", "本期金额", "上期金额"],
            ["营业收入", "1,234.56", "1,100.00"],
            ["营业成本", "800.00", "700.00"],
            ["净利润", "206.01", "195.00"]]
    y = 120
    for row in rows:
        for x, val in zip((80, 260, 400), row):
            page.insert_text((x, y), val, fontsize=10, fontname="china-s")
        y += 24
    doc.save(p)
    doc.close()
    return p


class TestBordered:
    def test_extracts_structure(self, bordered_pdf):
        t = extract_table(bordered_pdf, 1)
        assert t is not None
        assert t.n_rows == 4
        assert t.n_cols == 3
        assert t.cells[0][0] == "项目"

    def test_has_borders_flag(self, bordered_pdf):
        t = extract_table(bordered_pdf, 1)
        assert t.has_borders is True
        assert t.line_width > 0

    def test_row_and_col_edges(self, bordered_pdf):
        t = extract_table(bordered_pdf, 1)
        assert len(t.row_edges) == 5          # 4 行 -> 5 条边界
        assert len(t.col_edges) == 4          # 3 列 -> 4 条边界
        assert all(b > a for a, b in zip(t.row_edges, t.row_edges[1:]))

    def test_row_heights_sum_to_bbox(self, bordered_pdf):
        t = extract_table(bordered_pdf, 1)
        total = sum(t.row_heights)
        assert total == pytest.approx(t.bbox[3] - t.bbox[1], abs=1.0)

    def test_col_widths_sum_to_bbox(self, bordered_pdf):
        t = extract_table(bordered_pdf, 1)
        total = sum(t.col_widths)
        assert total == pytest.approx(t.bbox[2] - t.bbox[0], abs=1.0)

    def test_cell_text_preserved(self, bordered_pdf):
        t = extract_table(bordered_pdf, 1)
        flat = [c for row in t.cells for c in row]
        assert "营业收入" in flat
        assert "1,234.56" in flat


class TestBorderless:
    def test_may_or_may_not_detect(self, borderless_pdf):
        """无框线时 pdfplumber 可能靠文字位置推断，可能失败 —— 都不算错。"""
        t = extract_table(borderless_pdf, 1)
        if t is not None:
            assert t.has_borders is False


class TestEdges:
    def test_page_out_of_range(self, bordered_pdf):
        assert extract_table(bordered_pdf, 99) is None

    def test_missing_file(self, tmp_path):
        assert extract_layouts(tmp_path / "nope.pdf") == []

    def test_pick_main_table_prefers_bigger(self):
        small = TableLayout(1, (0, 0, 100, 100), [0, 50, 100], [0, 50, 100],
                            [["a", "b"], ["c", "d"]])
        big = TableLayout(1, (0, 0, 400, 300), [0, 100, 200, 300],
                          [0, 100, 200, 300, 400],
                          [["a"] * 4, ["b"] * 4, ["c"] * 4])
        assert pick_main_table([small, big]) is big

    def test_empty_layout(self):
        t = TableLayout(1, (0, 0, 10, 10), [0, 10], [0, 10], [["", ""]])
        assert t.is_empty is True
