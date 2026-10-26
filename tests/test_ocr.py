# -*- coding: utf-8 -*-
"""core.ocr 的表格重建测试 —— 用实测 OCR 坐标做 fixture。"""
import json
from pathlib import Path

import pytest

from core.ocr import (
    Box, assign_columns, cluster_rows, columns_from_numbers, find_columns,
    pick_columns, reconstruct,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_boxes(name: str) -> list[Box]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return [Box(d["x0"], d["x1"], d["y0"], d["y1"], d["t"], d["s"]) for d in data]


@pytest.fixture(scope="module")
def profit_boxes():
    return load_boxes("ocr_profit_scanned.json")


@pytest.fixture(scope="module")
def hard_boxes():
    return load_boxes("ocr_profit_hard_scan.json")


class TestRowClustering:
    def test_profit_rows_merge_correctly(self, profit_boxes):
        rows = cluster_rows(profit_boxes)
        # 实测该页 42 个框，标题/编制单位/表头 + 12 个数据行 = 15 行左右
        assert 13 <= len(rows) <= 16

    def test_rows_are_sorted_top_down(self, profit_boxes):
        rows = cluster_rows(profit_boxes)
        cys = [sum(b.cy for b in r) / len(r) for r in rows]
        assert cys == sorted(cys)

    def test_boxes_in_row_sorted_left_right(self, profit_boxes):
        for row in cluster_rows(profit_boxes):
            xs = [b.x0 for b in row]
            assert xs == sorted(xs)

    def test_empty_input(self):
        assert cluster_rows([]) == []


class TestColumnDetection:
    def test_profit_has_three_columns(self, profit_boxes):
        # 实测：项目 / 本期金额 / 上期金额
        bounds = find_columns(profit_boxes)
        assert 3 <= len(bounds) <= 5

    def test_bounds_do_not_overlap(self, profit_boxes):
        bounds = find_columns(profit_boxes)
        for (a0, a1), (b0, b1) in zip(bounds, bounds[1:]):
            assert a1 <= b0 or abs(a1 - b0) < 1

    def test_assign_puts_project_in_first_column(self, profit_boxes):
        bounds = find_columns(profit_boxes)
        project = [b for b in profit_boxes if "营业收入" in b.text][0]
        idx = assign_columns([project], bounds)
        assert idx[0] == 0

    def test_assign_separates_two_amount_columns(self, profit_boxes):
        bounds = find_columns(profit_boxes)
        # 同一行的两个金额必须落到不同列
        row = [b for b in profit_boxes if b.cy > 900 and b.cy < 1000]
        idx = assign_columns(row, bounds)
        assert len(set(idx)) >= 2


class TestReconstruct:
    def test_grid_shape(self, profit_boxes):
        grid = reconstruct(profit_boxes)
        assert grid.n_cols >= 3
        assert len(grid.rows) >= 13
        assert all(len(r) == grid.n_cols for r in grid.rows)

    def test_cell_text_preserved(self, profit_boxes):
        grid = reconstruct(profit_boxes)
        flat = [b.text for r in grid.rows for b in r if b is not None]
        assert any("营业收入" in t for t in flat)
        assert any("净利润" in t for t in flat)

    def test_indent_levels_differ(self, profit_boxes):
        """损益表靠缩进表达层级，顶格行和缩进行必须能区分。"""
        grid = reconstruct(profit_boxes)
        levels = {grid.indent_level(r[0]) for r in grid.rows if r[0] is not None}
        assert len(levels) >= 2, f"缩进层级没有区分出来: {levels}"

    def test_empty_input(self):
        grid = reconstruct([])
        assert grid.rows == []
        assert grid.n_cols == 0


def dense_form_boxes() -> list[Box]:
    """造一张「行号 + 长标签 + 两个金额列」的密集表单（几何仿真实表单，内容自造）。

    实测踩过的塌陷：这类表的左/右边缘会被长标签连成一大簇，
    bounds 互相覆盖，每行所有框都落到同一列，整页只剩 1 个非空格。
    """
    boxes: list[Box] = [
        Box(100, 420, 60, 80, "项目 Descricao", 0.95),
        Box(830, 960, 60, 80, "本期", 0.95),
        Box(1050, 1180, 60, 80, "前期", 0.95),
    ]
    for i in range(10):
        y = 100 + i * 26
        boxes.append(Box(70, 95, y, y + 20, str(i + 1), 0.98))
        boxes.append(Box(103, 422, y, y + 20, "营业成本 Custos", 0.95))
        boxes.append(Box(830, 960, y, y + 20, f"{1000000 + i * 777:,}.00", 0.96))
        boxes.append(Box(1050, 1180, y, y + 20, f"{2000000 + i * 333:,}.00", 0.96))
    return boxes


class TestDenseFormColumns:
    def test_finds_row_no_label_and_two_amounts(self):
        bounds = columns_from_numbers(dense_form_boxes())
        assert len(bounds) == 4, bounds
        # 必须是从左到右、互不覆盖
        for (a0, a1), (b0, b1) in zip(bounds, bounds[1:]):
            assert a1 <= b0 + 1, bounds

    def test_two_amounts_land_in_different_columns(self):
        boxes = dense_form_boxes()
        bounds = pick_columns(boxes, row_boxes=cluster_rows(boxes))
        row = [b for b in boxes if 100 <= b.cy <= 120]
        assert len(set(assign_columns(row, bounds))) >= 3

    def test_needs_enough_numbers(self):
        boxes = [Box(0, 10, 0, 10, "营业收入", 0.9),
                 Box(100, 160, 0, 10, "1,000.00", 0.9)]
        assert columns_from_numbers(boxes) == []

    def test_picker_never_loses_to_edge_clustering(self, profit_boxes, hard_boxes):
        """两套算法的选择器只能更好，不能更差。"""
        for boxes in (profit_boxes, hard_boxes):
            rows = cluster_rows(boxes)
            picked = pick_columns(boxes, row_boxes=rows)
            base = find_columns(boxes)

            def used(bounds):
                if not bounds:
                    return 0.0
                return sum(len(set(assign_columns(r, bounds))) for r in rows) / len(rows)

            assert used(picked) >= used(base) - 1e-9

    def test_reconstruct_uses_picked_columns(self):
        grid = reconstruct(dense_form_boxes())
        assert grid.n_cols >= 3
        # 多数行至少两个格有内容
        multi = sum(1 for r in grid.rows if sum(1 for b in r if b) >= 2)
        assert multi >= len(grid.rows) * 0.7


class TestHardScan:
    """模拟低质量扫描件（倾斜 + 模糊 + 噪点 + JPEG 压缩）。"""

    def test_still_reconstructs(self, hard_boxes):
        grid = reconstruct(hard_boxes)
        assert grid.n_cols >= 3
        assert len(grid.rows) >= 12

    def test_garbled_numbers_are_flagged_not_guessed(self, hard_boxes):
        """坏样本必须解析失败而不是猜一个数。"""
        from core.numbers import normalize

        texts = [b.text for b in hard_boxes]
        # 实测这条 OCR 出来是 '1,100,000,00'（小数点被认成逗号）
        bad = [t for t in texts if t == "1,100,000,00"]
        assert bad, "fixture 里应有这条实测坏样本"
        assert normalize(bad[0]).value is None
