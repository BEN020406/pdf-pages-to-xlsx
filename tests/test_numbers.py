# -*- coding: utf-8 -*-
"""core.numbers 的回归测试。

样本全部来自实测 OCR 输出（扫描版损益表 + 模拟硬扫描件），
不是编的：_ocr_profit.json / _ocr_hard.json 里挑出的真实错误串。
"""
import pytest

from core.numbers import format_amount, is_numeric_like, normalize


class TestDigitSpaceRepair:
    """OCR 在数字内部插空格 —— 实测最主要的错误类型。"""

    @pytest.mark.parametrize("raw,expected", [
        ("2 7 5, 1 8 5. 3 1", 275185.31),
        ("2 7 4,68 5. 3 1", 274685.31),
        ("6 8,67 1. 3 3", 68671.33),
        ("2 06,013. 98", 206013.98),
        ("1 1,0 00. 00", 11000.0),
        ("1 2,345.67", 12345.67),
        ("1, 234, 567. 89", 1234567.89),
        ("1, 234, 567.89", 1234567.89),
        ("259, 000.00", 259000.0),
        ("800, 000. 00", 800000.0),
        ("80, 000. 00", 80000.0),
    ])
    def test_repair(self, raw, expected):
        r = normalize(raw)
        assert r.value == pytest.approx(expected), f"{raw!r} -> {r.value} / {r.rule}"
        assert "digit_space" in r.rule

    def test_paren_with_inner_spaces(self):
        r = normalize("(1 2, 345. 67)")
        assert r.value == pytest.approx(-12345.67)
        assert "paren_negative" in r.rule
        assert "digit_space" in r.rule


class TestAccountingFormats:
    def test_thousand_sep(self):
        assert normalize("1,234.56").value == pytest.approx(1234.56)

    def test_paren_negative(self):
        assert normalize("(1234.56)").value == pytest.approx(-1234.56)

    def test_fullwidth_paren_negative(self):
        assert normalize("（12,345.67）").value == pytest.approx(-12345.67)

    def test_minus_normalize(self):
        assert normalize("－1234.56").value == pytest.approx(-1234.56)
        assert normalize("−1234.56").value == pytest.approx(-1234.56)

    @pytest.mark.parametrize("dash", ["-", "—", "–", "－", "−", "／", "/"])
    def test_dash_zero(self, dash):
        r = normalize(dash)
        assert r.value == 0.0
        assert r.rule == "dash_zero"

    def test_fullwidth_digits(self):
        assert normalize("１２３４").value == pytest.approx(1234.0)

    def test_strip_unit(self):
        assert normalize("1234.56元").value == pytest.approx(1234.56)

    def test_wan_scale(self):
        assert normalize("1.2万").value == pytest.approx(12000.0)
        assert normalize("1.2万元").value == pytest.approx(12000.0)

    def test_percent(self):
        assert normalize("12.3%").value == pytest.approx(0.123)


class TestDotThousandRepair:
    """实测：OCR 把千分位逗号读成点，如 '1.234.567.89'。

    只在形状完全吻合时才还原（1~3 位 + 若干 3 位组 + 1~2 位小数），
    这个形状没有歧义，不算猜。
    """

    @pytest.mark.parametrize("raw,expected", [
        ("1.234.567.89", 1234567.89),
        ("800.000.00", 800000.0),
        ("1.100.000.00", 1100000.0),
        ("12.345.67", 12345.67),
        ("206.013.98", 206013.98),
        ("274.685.31", 274685.31),
    ])
    def test_dot_thousand(self, raw, expected):
        r = normalize(raw)
        assert r.value == pytest.approx(expected), f"{raw!r} -> {r.value}"
        assert "dot_thousand" in r.rule

    def test_plain_decimal_untouched(self):
        """'1.5' 是小数，不能被当成千分位。"""
        r = normalize("1.5")
        assert r.value == pytest.approx(1.5)
        assert "dot_thousand" not in r.rule

    def test_ambiguous_still_rejected(self):
        """'206.013.98.7' 多个小数点且形状不吻合 -> 必须拒绝。"""
        assert normalize("206.013.98.7").value is None


class TestDotRestore:
    """OCR 丢掉一个小数点，只留空格：'274.685 31' → 274685.31。

    实测场景：手写件里本地 OCR 读成 '274.685 31'（解析失败），
    大模型读成 '274,655.31'（错把 6 看成 5）。
    补上这条规则后本地能自己还原正确值，不必依赖模型。
    """

    @pytest.mark.parametrize("raw,expected", [
        ("274.685 31", 274685.31),
        ("800.000 00", 800000.0),
        ("12.345 67", 12345.67),
    ])
    def test_restore(self, raw, expected):
        r = normalize(raw)
        assert r.value == pytest.approx(expected), f"{raw!r} -> {r.value}"
        assert "dot_restore" in r.rule

    def test_shape_must_match(self):
        """形状不吻合就不动，避免猜。"""
        assert normalize("1.234 567.89").value is None
        assert normalize("274.685").value == pytest.approx(274.685)

    def test_plain_digit_space_still_works(self):
        """不能把普通插空格的数字误判成补点。"""
        r = normalize("1 234")
        assert r.value == pytest.approx(1234.0)
        assert "dot_restore" not in r.rule


class TestRefuseToGuess:
    """这些必须返回 None —— 猜错会得到差 100 倍的数，审计里是灾难。"""

    def test_bad_grouping_rejected(self):
        # 实测 OCR 把小数点认成逗号：1,100,000.00 -> 1,100,000,00
        r = normalize("1,100,000,00")
        assert r.value is None
        assert r.rule == "bad_grouping"

    def test_all_zero_noise_rejected(self):
        # 实测 OCR 输出 '000000'
        r = normalize("000000")
        assert r.value is None
        assert r.rule == "all_zero_noise"

    def test_missing_decimal_point_rejected(self):
        # 形状不吻合（小数点后 3 位 + 空格 + 3 位以上）必须拒绝
        r = normalize("1.234 567.89")
        assert r.value is None

    def test_text_rejected(self):
        assert normalize("营业收入").value is None
        assert normalize("合计").value is None

    def test_empty_rejected(self):
        assert normalize("").value is None
        assert normalize("   ").value is None
        assert normalize(None).value is None

    def test_too_many_decimals_rejected(self):
        assert normalize("1234.56789").value is None


class TestIsNumericLike:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234.56", True),
        ("(12,345.67)", True),
        ("2 7 5, 1 8 5. 3 1", True),
        ("1.2万", True),
        ("12.3%", True),
        ("—", False),
        ("营业收入", False),
        ("减：营业成本", False),
        ("", False),
    ])
    def test_classify(self, raw, expected):
        assert is_numeric_like(raw) is expected


class TestFormatAmount:
    def test_none_is_blank(self):
        assert format_amount(None) == ""

    def test_thousands(self):
        assert format_amount(1234567.891) == "1,234,567.89"

    def test_no_thousands(self):
        assert format_amount(1234.5, thousands=False) == "1234.50"
