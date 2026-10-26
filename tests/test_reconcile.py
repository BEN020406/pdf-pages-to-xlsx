# -*- coding: utf-8 -*-
"""core.reconcile 的三方对账规则测试。"""
import pytest

from core.reconcile import COLORS, LOW_SCORE, ReconcileReport, Verdict, reconcile_cell


class TestAgree:
    def test_three_way_agree(self):
        c = reconcile_cell("1,234.56", model_raw="1234.56", ocr_score=0.99)
        assert c.verdict is Verdict.AGREE
        assert c.final_value == pytest.approx(1234.56)

    def test_no_model_high_score(self):
        c = reconcile_cell("1,234.56", ocr_score=0.99)
        assert c.verdict is Verdict.AGREE


class TestLocalWins:
    def test_model_differs_but_local_confident(self):
        # 本地高置信，模型给了不同的数 -> 默认采信本地
        c = reconcile_cell("1,234.56", model_raw="1234.65", ocr_score=0.98)
        assert c.verdict is Verdict.LOCAL
        assert c.final_value == pytest.approx(1234.56)

    def test_model_unparsable_falls_back_to_local(self):
        c = reconcile_cell("500.00", model_raw="看不清", ocr_score=0.95)
        assert c.verdict is Verdict.LOCAL
        assert c.final_value == pytest.approx(500.0)


class TestModelWins:
    def test_low_ocr_score_model_takes_over(self):
        c = reconcile_cell("1,100,000,00", model_raw="1,100,000.00", ocr_score=0.85)
        assert c.verdict is Verdict.MODEL
        assert c.final_value == pytest.approx(1100000.0)

    def test_local_unparsable_model_rescues(self):
        c = reconcile_cell("000000", model_raw="65,000.00", ocr_score=0.897)
        assert c.verdict is Verdict.MODEL
        assert c.final_value == pytest.approx(65000.0)

    def test_low_score_without_model_still_flags(self):
        # 本地解析不出来、又没有模型兜底 -> 必须进人工复核
        c = reconcile_cell("1.234 567.89", ocr_score=0.80)
        assert c.verdict is Verdict.CONFLICT
        assert c.needs_review is True

    def test_low_score_parsable_without_model(self):
        # 本地能解析但置信度低、又没调模型：仍标记为需复核，但值保留
        c = reconcile_cell("500.00", ocr_score=0.80)
        assert c.verdict is Verdict.MODEL
        assert c.final_value == pytest.approx(500.0)
        assert c.needs_review is True


class TestConflict:
    def test_both_unparsable(self):
        c = reconcile_cell("1.234 567.89", model_raw="也看不懂", ocr_score=0.95)
        assert c.verdict is Verdict.CONFLICT
        assert c.final_value is None
        assert c.needs_review is True

    def test_conflict_has_red_color(self):
        c = reconcile_cell("abc", model_raw="def")
        assert c.color == COLORS[Verdict.CONFLICT]


class TestManual:
    def test_manual_overrides_everything(self):
        c = reconcile_cell("1,234.56", model_raw="9999", ocr_score=0.5, manual_value=777.0)
        assert c.verdict is Verdict.MANUAL
        assert c.final_value == 777.0

    def test_manual_color(self):
        c = reconcile_cell("x", manual_value=1.0)
        assert c.color == COLORS[Verdict.MANUAL]


class TestPrecision:
    """实测高频失败：本地丢小数位但置信度仍高。

    本地把 275,185.31 读成 275,185.3（丢最后一位），
    把 274,685.31 读成 274,685（整段小数丢掉），置信度 0.97+。
    按普通规则会被误判为「采信本地」，得到少一位小数的错值。
    """

    def test_lost_last_digit(self):
        c = reconcile_cell("275,185.3", model_raw="275,185.31", ocr_score=0.970)
        assert c.verdict is Verdict.PRECISION
        assert c.final_value == pytest.approx(275185.31)

    def test_lost_all_decimals(self):
        c = reconcile_cell("274,685", model_raw="274,685.31", ocr_score=0.992)
        assert c.verdict is Verdict.PRECISION
        assert c.final_value == pytest.approx(274685.31)

    def test_real_difference_is_not_precision(self):
        c = reconcile_cell("1,234.56", model_raw="1,234.65", ocr_score=0.99)
        assert c.verdict is Verdict.LOCAL

    def test_integer_vs_integer_not_precision(self):
        c = reconcile_cell("700,000", model_raw="700,001", ocr_score=0.99)
        assert c.verdict is Verdict.LOCAL

    def test_precision_needs_review(self):
        c = reconcile_cell("275,185.3", model_raw="275,185.31", ocr_score=0.97)
        assert c.needs_review is True

    def test_precision_has_own_color(self):
        from core.reconcile import COLORS
        c = reconcile_cell("275,185.3", model_raw="275,185.31", ocr_score=0.97)
        assert c.color == COLORS[Verdict.PRECISION]
        assert COLORS[Verdict.PRECISION] != COLORS[Verdict.LOCAL]


class TestTolerance:
    def test_tiny_float_diff_is_agree(self):
        c = reconcile_cell("1234.56", model_raw="1234.560", ocr_score=0.99)
        assert c.verdict is Verdict.AGREE

    def test_real_diff_is_not_agree(self):
        c = reconcile_cell("1234.56", model_raw="1234.57", ocr_score=0.99)
        assert c.verdict is Verdict.LOCAL


class TestReport:
    def _make(self):
        rep = ReconcileReport()
        rep.add(reconcile_cell("1,234.56", model_raw="1234.56", ocr_score=0.99))
        rep.add(reconcile_cell("1,234.56", model_raw="1234.65", ocr_score=0.98))
        rep.add(reconcile_cell("abc", model_raw="def"))
        rep.add(reconcile_cell("x", manual_value=5.0))
        return rep

    def test_counts(self):
        counts = self._make().counts
        assert counts["agree"] == 1
        assert counts["local"] == 1
        assert counts["conflict"] == 1
        assert counts["manual"] == 1

    def test_review_count(self):
        # conflict + model 需要复核
        assert self._make().review_count == 1

    def test_sheet_rows_shape(self):
        rows = self._make().rows_for_sheet()
        assert rows[0][0] == "原文"
        assert len(rows) == 5
        assert all(len(r) == 8 for r in rows)

    def test_sheet_rows_keep_decimals(self):
        """实测 bug：用 :g 格式化会把 275185.31 显示成 275185，追溯时分不清。"""
        rep = ReconcileReport()
        rep.add(reconcile_cell("275,185.31", model_raw="275,185.31", ocr_score=0.99))
        rows = rep.rows_for_sheet()
        assert rows[1][4] == "275185.31"

    def test_sheet_rows_traceable(self):
        rows = self._make().rows_for_sheet()
        # 原文、规则、最终值、状态都要能追溯到
        assert rows[1][0] == "1,234.56"
        assert rows[1][5] == "agree"
        assert rows[3][5] == "conflict"


def test_low_score_threshold_is_sane():
    assert 0.8 <= LOW_SCORE <= 0.95
