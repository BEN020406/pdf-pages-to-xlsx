# -*- coding: utf-8 -*-
"""core.search 的测试 —— 用自造的多页 PDF。"""
from pathlib import Path

import fitz
import pytest

from core.search import (
    SearchResult,
    match_in_text,
    norm,
    search_many,
    search_pdf,
)


@pytest.fixture(scope="module")
def multi_page_pdf(tmp_path_factory):
    """造一个 5 页 PDF，第 2、4 页含「管理费用明细表」。"""
    d = tmp_path_factory.mktemp("search")
    p = d / "multi.pdf"
    doc = fitz.open()
    titles = [
        "资产负债表",
        "管理费用明细表",
        "利润表",
        "管理费用明细表（续）",
        "现金流量表",
    ]
    for t in titles:
        page = doc.new_page()
        page.insert_text((72, 80), t, fontsize=16, fontname="china-s")
        page.insert_text((72, 110), "编制单位：某公司", fontsize=9, fontname="china-s")
        page.insert_text((72, 130), "项目    本期金额", fontsize=9, fontname="china-s")
    doc.save(p)
    doc.close()
    return p


class TestNorm:
    def test_strip_punctuation_and_space(self):
        assert norm("管理费用 明细表：") == "管理费用明细表"

    def test_strip_brackets(self):
        assert norm("（一）管理费用") == "一管理费用"

    def test_none_safe(self):
        assert norm(None) == ""


class TestMatchInText:
    def test_exact(self):
        line, how, score = match_in_text("管理费用明细表", "管理费用明细表\n其他内容")
        assert how == "exact" and score == 1.0
        assert line == "管理费用明细表"

    def test_exact_with_spaces(self):
        line, how, _ = match_in_text("管理费用明细表", "管理费用 明细表")
        assert how == "exact"

    def test_contains(self):
        line, how, score = match_in_text("管理费用", "二、管理费用明细表")
        assert how == "contains"
        assert 0 < score < 1.0

    def test_shorter_line_scores_higher(self):
        _, _, s_short = match_in_text("管理费用", "管理费用明细表")
        _, _, s_long = match_in_text("管理费用", "管理费用明细表及附注说明补充材料")
        assert s_short > s_long

    def test_fuzzy_tolerates_extra_chars(self):
        # OCR 可能多插字
        _, how, _ = match_in_text("管理费用明细表", "管 理 费用 明细 表")
        assert how in ("exact", "fuzzy")

    def test_no_match(self):
        line, how, score = match_in_text("现金流量表", "管理费用明细表")
        assert how == "" and score == 0

    def test_empty_title(self):
        assert match_in_text("", "任意内容")[1] == ""

    def test_short_title_no_fuzzy(self):
        """太短的标题不做模糊匹配，否则误命中率爆炸。"""
        assert match_in_text("表", "管理费用明细表")[1] == ""

    def test_title_split_across_lines_still_matches(self):
        """实测：扫描件 OCR 会把「损益表」拆成三行，逐行比永远不命中。"""
        text = "损\n益\n表\n单位：元\n营业收入 100"
        line, how, score = match_in_text("损益表", text)
        assert how == "contains"
        assert score > 0

    def test_traditional_text_matches_simplified_query(self):
        """实测：澳门税表是繁体「損益表」，用户打简体「损益表」，不折叠就 0 命中。"""
        text = "損益表 DEMONSTRAÇÃO DE RESULTADOS\n營業收入"
        assert match_in_text("损益表", text)[1] != ""
        assert match_in_text("营业收入", text)[1] != ""

    def test_simplified_text_matches_traditional_query(self):
        text = "损益表 DEMONSTRAÇÃO DE RESULTADOS"
        assert match_in_text("損益表", text)[1] != ""

    def test_english_page_does_not_match_chinese_title(self):
        """英文税表里没有「损益表」三个字，就该老实返回未命中。"""
        en = "U.S. Corporation Income Tax Return\nForm 1120\nGross receipts"
        assert match_in_text("损益表", en)[1] == ""


class TestSearchPdf:
    def test_finds_both_pages(self, multi_page_pdf):
        r = search_pdf(multi_page_pdf, "管理费用明细表")
        assert r.page_numbers == [2, 4]
        assert r.pages_scanned == 5
        assert not r.error

    def test_match_how_recorded(self, multi_page_pdf):
        r = search_pdf(multi_page_pdf, "管理费用明细表")
        assert all(m.how in ("exact", "contains") for m in r.matches)
        assert r.matches[0].matched_text

    def test_no_match_returns_empty(self, multi_page_pdf):
        r = search_pdf(multi_page_pdf, "不存在的表")
        assert r.matches == []
        assert not r.error

    def test_single_page_title(self, multi_page_pdf):
        r = search_pdf(multi_page_pdf, "现金流量表")
        assert r.page_numbers == [5]

    def test_missing_file(self, tmp_path):
        r = search_pdf(tmp_path / "nope.pdf", "x")
        assert r.error

    def test_max_pages_limits_scan(self, multi_page_pdf):
        r = search_pdf(multi_page_pdf, "管理费用明细表", max_pages=2)
        assert r.pages_scanned == 2
        assert r.page_numbers == [2]


class TestSearchMany:
    def test_multiple_files(self, multi_page_pdf, tmp_path):
        import shutil
        p2 = tmp_path / "copy.pdf"
        shutil.copy(multi_page_pdf, p2)
        results = search_many([multi_page_pdf, p2], "管理费用明细表")
        assert len(results) == 2
        assert all(r.page_numbers == [2, 4] for r in results)

    def test_text_chars_counted(self, multi_page_pdf):
        """字符数用来区分「没这个词」和「根本没识别出字」，必须累计。"""
        results = search_many([multi_page_pdf], "不存在的标题")
        assert results[0].text_chars > 0

    def test_progress_called(self, multi_page_pdf):
        """进度按「页」报：5 页的 PDF 要能看到 0→5 的推进，不是一上来就 1/1。"""
        calls = []
        search_many([multi_page_pdf], "x",
                    on_progress=lambda i, n, name: calls.append((i, n)))
        assert calls, "没有报进度"
        assert all(n == 5 for _, n in calls), calls
        assert calls[0][0] == 0
        assert calls[-1][0] == 5
        assert [i for i, _ in calls] == sorted(i for i, _ in calls), "进度必须单调"

    def test_progress_names_the_page(self, multi_page_pdf):
        """扫描件单文件多页也要走完全程 —— 旧实现按文件报，单文件永远 100%。"""
        names = []
        search_many([multi_page_pdf], "x",
                    on_progress=lambda i, n, name: names.append(name))
        assert any("第 3/5 页" in p for p in names), names


class TestCancel:
    """取消搜索：扫描件一页要一两秒，必须能按文件、按页中断。"""

    def test_stop_before_first_file(self, multi_page_pdf):
        results = search_many([multi_page_pdf], "管理费用明细表",
                              should_stop=lambda: True)
        assert results == []

    def test_stop_between_files(self, monkeypatch):
        """文件之间也要能停：第一个扫完、第二个还没开始时中断。"""
        from pathlib import Path

        from core import search as s

        seen: list[str] = []

        def fake(path, title, **kw):
            seen.append(Path(path).name)
            return s.SearchResult(path=Path(path), title=title)

        monkeypatch.setattr(s, "search_pdf", fake)
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 1

        out = s.search_many([Path("a.pdf"), Path("b.pdf")], "x", should_stop=stop)
        assert seen == ["a.pdf"]
        assert len(out) == 1

    def test_stop_mid_pdf_returns_partial(self, multi_page_pdf):
        """按页中断：扫到一半停下，已扫到的命中要保留并标记 cancelled。"""
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3          # 前 3 页放行

        r = search_pdf(multi_page_pdf, "管理费用明细表", should_stop=stop)
        assert r.cancelled is True
        assert r.page_numbers == [2]       # 第 4 页的命中没扫到


class TestScannedPdf:
    def test_scanned_needs_ocr_flag(self, tmp_path):
        """无文本层的 PDF，不开 OCR 就搜不到。"""
        src = fitz.open()
        pg = src.new_page()
        pg.insert_text((72, 80), "管理费用明细表", fontsize=16, fontname="china-s")
        pix = pg.get_pixmap(dpi=150)
        scanned = fitz.open()
        sp = scanned.new_page(width=pg.rect.width, height=pg.rect.height)
        sp.insert_image(sp.rect, stream=pix.tobytes("png"))
        out = tmp_path / "scan.pdf"
        scanned.save(out)
        scanned.close()
        src.close()

        assert search_pdf(out, "管理费用明细表").matches == []
        r = search_pdf(out, "管理费用明细表", use_ocr=True)
        assert r.page_numbers == [1]
        assert r.used_ocr is True
