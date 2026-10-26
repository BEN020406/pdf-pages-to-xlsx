# -*- coding: utf-8 -*-
"""core.pipeline 的编排测试 —— 用假 LLM，不花 token。"""
import json

import pytest

from core.llm import LLMConfig, LLMClient
from core.numbers import normalize
from core.pipeline import (
    CellOut,
    _align_rows,
    _norm_text,
    _rows_to_cells,
    estimate_cost,
    process_page,
)
from core.reconcile import Verdict
from tests.test_llm import FakeCompletions, make_resp


def fake_llm(rows, prompt=2200, completion=400):
    fake = FakeCompletions([make_resp(json.dumps({"rows": rows}), prompt, completion)])
    cfg = LLMConfig(api_key="sk-fake")
    return LLMClient(cfg, client=__import__("types").SimpleNamespace(
        chat=__import__("types").SimpleNamespace(completions=fake)))


# ---------------------------------------------------------------- 行对齐

class TestRowAlignment:
    def test_exact_match(self):
        model = [["项目", "金额"], ["营业收入", "100"]]
        local = [["项目", "金额"], ["营业收入", "100"]]
        assert _align_rows(model, local) == [0, 1]

    def test_prefix_match(self):
        """模型给 '减：营业成本'，本地给 '营业成本'，应视为同一行。"""
        model = [["减：营业成本", "800"]]
        local = [["营业成本", "800"]]
        assert _align_rows(model, local) == [0]

    def test_extra_header_row_shifts_correctly(self):
        """实测：模型多一行标题时，按索引比会全部错位。"""
        model = [["利润表", ""], ["项目", "金额"], ["营业收入", "100"]]
        local = [["项目", "金额"], ["营业收入", "100"]]
        mapping = _align_rows(model, local)
        assert mapping[0] == -1      # 标题在本地没有对应
        assert mapping[1] == 0
        assert mapping[2] == 1

    def test_no_local_rows(self):
        assert _align_rows([["a"]], []) == [-1]

    def test_norm_text_strips_punctuation(self):
        assert _norm_text("减：营业成本 (一)") == "减营业成本一"


# ---------------------------------------------------------------- 对账

class TestReconcileIntegration:
    def test_text_cells_not_flagged_conflict(self):
        """实测 bug：文本列被误判成 conflict，导致每行都标红。"""
        rows = [["项目", "本期金额"], ["营业收入", "1,234.56"]]
        local = [["项目", "本期金额"], ["营业收入", "1,234.56"]]
        cells, report = _rows_to_cells(rows, model_raw=local)
        assert cells[0][0].verdict is None       # 文本列不参与数值对账
        assert cells[0][1].verdict is None
        assert report.counts["conflict"] == 0

    def test_agree_when_same(self):
        rows = [["营业收入", "1,234.56"]]
        local = [["营业收入", "1,234.56"]]
        scores = [[1.0, 0.99]]
        cells, report = _rows_to_cells(rows, model_raw=local, local_scores=scores)
        assert cells[0][1].verdict is Verdict.AGREE
        assert cells[0][1].value == pytest.approx(1234.56)

    def test_low_ocr_score_lets_model_win(self):
        """实测 bug：本地 OCR 把 700,000.00 读成 720000（置信度 0.729），
        漏传 score 会导致采信错误值。"""
        rows = [["营业成本", "700,000.00"]]
        local = [["营业成本", "720000"]]
        scores = [[1.0, 0.729]]
        cells, report = _rows_to_cells(rows, model_raw=local, local_scores=scores)
        assert cells[0][1].verdict is Verdict.MODEL
        assert cells[0][1].value == pytest.approx(700000.0)

    def test_high_score_keeps_local(self):
        rows = [["营业成本", "800,000.00"]]
        local = [["营业成本", "800,000.00"]]
        scores = [[1.0, 0.99]]
        cells, report = _rows_to_cells(rows, model_raw=local, local_scores=scores)
        assert cells[0][1].verdict is Verdict.AGREE

    def test_row_misalignment_does_not_create_false_diff(self):
        """模型多一行标题时，不能把 A 行数字跟 B 行比。"""
        rows = [["利润表", ""], ["营业收入", "1,234.56"]]
        local = [["营业收入", "1,234.56"]]
        scores = [[0.99]]
        cells, report = _rows_to_cells(rows, model_raw=local, local_scores=scores)
        # 第二行的数字应与本地第一行对上
        assert cells[1][1].verdict is Verdict.AGREE

    def test_empty_cells_are_empty_kind(self):
        rows = [["营业收入", ""]]
        cells, _ = _rows_to_cells(rows)
        assert cells[0][1].kind == "empty"
        assert cells[0][1].value is None


# ---------------------------------------------------------------- 端到端

class TestProcessPage:
    def test_text_layer_never_calls_api(self):
        """有文本层就不该花钱。"""
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "proft_demo_text.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        llm = fake_llm([])
        res = process_page(pdf, 1, llm=llm)
        # 文本层能抽出结构时 source 是 "table"，抽不出才是 "text"
        assert res.source in ("table", "text")
        assert res.cost_cny == 0.0
        assert llm.usage.calls == 0
        assert "未调用 API" in " ".join(res.notes)

    def test_bordered_pdf_gets_real_structure(self):
        """实测 bug：有框线的表被降级成 1 列文本。

        demo01.pdf 有完整框线，pdfplumber 能抽 3×4，
        但旧实现只要有文本层就按行切，得到 13×1。
        """
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "demo01.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        res = process_page(pdf, 2, use_llm=False)
        assert res.source == "table"
        assert res.n_rows == 3
        assert res.n_cols == 4
        assert res.layout is not None
        assert res.layout.has_borders is True

    def test_borderless_pdf_still_gets_structure(self):
        """无框线的损益表也要抽出结构，不能退回 1 列。"""
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "proft_demo_text.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        res = process_page(pdf, 1, use_llm=False)
        assert res.source == "table"
        assert res.n_cols >= 3
        assert res.n_rows >= 10

    def test_page_out_of_range(self):
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "proft_demo_text.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        res = process_page(pdf, 99)
        assert "超出范围" in res.error

    def test_on_stage_reports_each_phase(self):
        """UI 靠 on_stage 显示「这一页卡在哪一步」，顺序不能乱。"""
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "profit_scan_hard.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        stages: list[str] = []
        res = process_page(pdf, 1, use_llm=False, on_stage=stages.append)
        assert not res.error, res.error
        assert stages[0] == "打开 PDF"
        assert "本地 OCR" in stages
        assert stages.index("渲染页面") < stages.index("本地 OCR")

    def test_on_stage_exception_is_swallowed(self):
        """UI 回调写错不能把整批处理带崩。"""
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "proft_demo_text.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")

        def boom(_):
            raise RuntimeError("UI 挂了")

        res = process_page(pdf, 1, use_llm=False, on_stage=boom)
        assert not res.error
        assert res.n_rows > 0

    def test_missing_file(self):
        from pathlib import Path
        res = process_page(Path("no_such_file.pdf"), 1)
        assert res.error

    def test_good_scan_is_not_flagged_weak(self):
        """正常扫描件抽出了列结构，不能误报「没抽出表格」。"""
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "profit_scan_hard.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        res = process_page(pdf, 1, use_llm=False)
        assert res.n_cols >= 3
        assert res.weak_structure is False

    def test_flat_text_page_is_flagged_weak(self, tmp_path):
        """整页只有整行文字的扫描件必须标出来，不能静默当成表导出。"""
        import fitz
        src = fitz.open()
        page = src.new_page(width=595, height=842)
        lines = [
            "Sociedade A Grupo A 申报书 说明事项第一条 本申报书依据第某号法律编制",
            "Direccao dos Servicos de Financas 财政局 纳税人编号及年度资料",
            "本期金额与前期金额之差额详见附注第二部分之说明内容",
            "以上数据来自纳税人自行填报之申报表并以澳门币作为计量单位",
            "补充说明本次申报所涵盖的期间与上一年度期间之对应关系",
            "纳税义务人签署栏位以及财政局受理人员填写之专用栏位",
        ]
        y = 80
        for ln in lines:
            page.insert_text((40, y), ln, fontsize=11, fontname="china-s")
            y += 40
        pix = page.get_pixmap(dpi=150)
        scanned = fitz.open()
        sp = scanned.new_page(width=595, height=842)
        sp.insert_image(sp.rect, stream=pix.tobytes("png"))
        out = tmp_path / "flat.pdf"
        scanned.save(str(out))
        scanned.close()
        src.close()

        res = process_page(out, 1, use_llm=False)
        assert not res.error
        assert res.weak_structure is True, "整行文本没被标出来，用户会以为导出的是表"
        assert any("没抽出表格结构" in n for n in res.notes)

    def test_encrypted_pdf_does_not_crash(self, tmp_path):
        """实测 bug：加密 PDF 让 fitz 在取页时抛
        `ValueError: document closed or encrypted`，整个批处理中断。"""
        import fitz
        p = tmp_path / "enc.pdf"
        d = fitz.open()
        pg = d.new_page()
        pg.insert_text((72, 72), "加密测试", fontsize=12, fontname="china-s")
        d.save(p, encryption=fitz.PDF_ENCRYPT_AES_256,
               owner_pw="owner", user_pw="user")
        d.close()

        res = process_page(p, 1)
        assert res.error, "加密 PDF 必须返回错误而不是抛异常"
        assert "加密" in res.error
        assert res.n_rows == 0

    def test_blank_page_handled(self, tmp_path):
        import fitz
        p = tmp_path / "blank.pdf"
        d = fitz.open()
        d.new_page()
        d.save(p)
        d.close()
        res = process_page(p, 1, use_llm=False)
        assert res.error or res.n_rows == 0


    def test_clip_bypasses_text_layer(self):
        """实测 bug：框选重跑对有文本层的页面失效。

        process_page 里「有文本层就抽结构化表」的分支没考虑 clip，
        直接返回整页结果，框选被完全忽略。
        修复：clip 不为 None 时必须走 OCR 路径。
        """
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "demo01.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        # 只框选表格区域（页面下半部分）
        res = process_page(pdf, 2, use_llm=False, clip=(0.05, 0.12, 0.95, 0.32))
        # 断言的是「没走文本层」，不是非得走老 OCR 路径：
        # 框出来的这块是有框线的表格，能按表格线还原（layout）比按文字框聚类更好。
        assert res.source in ("ocr", "layout"), f"应走 OCR/版式路径，实际 {res.source}"
        assert any("框选" in n for n in res.notes), f"notes: {res.notes}"

    def test_no_clip_uses_table(self):
        """不框选时仍然走结构化抽表（不能被上面的修复影响）。"""
        from pathlib import Path
        pdf = Path(__file__).parent.parent / "demo_pdfs" / "demo01.pdf"
        if not pdf.exists():
            pytest.skip("缺样例")
        res = process_page(pdf, 2, use_llm=False, clip=None)
        assert res.source == "table"
        assert res.n_cols == 4


class TestColumnAlignment:
    """实测 bug：本地 OCR 多一个空列，与模型的列数不一致，逐格对账整体错位。

    现象：`1,100,000.00` 的 value 变成了 `1,234,567.89`（第 2 列的值）。
    原因：本地 OCR 在第 1 列残留「单位：元」，数据行全空但整列判定为「非空」。
    """

    def test_extra_empty_local_column_dropped(self):
        from core.pipeline import _rows_to_cells
        # 模型 3 列；本地 4 列，第 1 列只有标题行有值
        rows = [["项目", "本期金额", "上期金额"],
                ["营业收入", "1,234.56", "1,100.00"]]
        local = [["利润表", "", "", ""],
                 ["编制单位", "单位：元", "", ""],
                 ["项 目", "", "本期金额", "上期金额"],
                 ["营业收入", "", "1,234.56", "1,100.00"]]
        scores = [[1.0, 1.0, 1.0, 1.0]] * 4

        cells, report = _rows_to_cells(rows, model_raw=local, local_scores=scores)
        assert cells[1][1].value == pytest.approx(1234.56)
        assert cells[1][2].value == pytest.approx(1100.00), \
            f"第 3 列错位：{cells[1][2].value}"

    def test_no_extra_column_unchanged(self):
        from core.pipeline import _rows_to_cells
        rows = [["项目", "本期金额"], ["营业收入", "1,234.56"]]
        local = [["项目", "本期金额"], ["营业收入", "1,234.56"]]
        cells, _ = _rows_to_cells(rows, model_raw=local,
                                  local_scores=[[1.0, 0.99]] * 2)
        assert cells[1][1].value == pytest.approx(1234.56)


class TestCostEstimate:
    def test_ten_pages_under_one_yuan(self):
        assert estimate_cost(10) < 1.0

    def test_scales_linearly(self):
        assert estimate_cost(100) == pytest.approx(estimate_cost(10) * 10)
