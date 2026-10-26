# -*- coding: utf-8 -*-
"""进度 / 失败反馈回归测试。

钉住四条设计约束（都是实测发现的问题）：
    1. 进度条必须真实前进：旧代码在每页开始前发 i-1，最后一页跑完永远到不了 100%
    2. 失败必须留在清单上：旧代码只发一条 6 秒 toast，消失后无从查证
    3. 失败页不能算进「完成」：旧代码用 len(results) 当工作表数，失败页也算
    4. 取消 / 预算中断后，没跑的页要显示「已跳过」，不能让用户以为都跑过了
"""
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PyQt5")
pytest.importorskip("qfluentwidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

DEMO = Path(__file__).resolve().parent.parent / "demo_pdfs"


@pytest.fixture
def window(app, monkeypatch):
    """不带大模型的主窗口，避免测试真的打 API。"""
    from gui import settings_page as m
    monkeypatch.setattr(m.SettingsPage, "to_llm_config", lambda self: None)
    monkeypatch.setattr(m.SettingsPage, "_confirm_privacy", lambda self: False)
    from gui.app import MainWindow
    w = MainWindow()
    w.resize(1180, 780)
    w.show()          # 不 show 的话子控件 isVisible() 全是 False
    app.processEvents()
    yield w
    w.close()


def _pump(app, seconds: float, until=None):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else False


def _wait_idle(app, w, timeout: float = 120.0):
    ok = _pump(app, timeout, until=lambda: w._worker is not None
               and w._worker.isFinished() and not w._running)
    assert ok, "批次没有在超时内结束"


def test_progress_reaches_100_and_states_persist(app, window):
    from gui.worker import JobItem

    pdf = DEMO / "proft_demo_text.pdf"
    assert pdf.exists()

    # 第 1 页正常，第 99 页必然失败 —— 一页成功一页失败，最贴近真实场景
    jobs = [JobItem(pdf, 1, "利润表"), JobItem(pdf, 99, "越界页")]
    window.import_page.add_paths([pdf])
    window._start_jobs(jobs)

    assert window.progress.isVisible() is not None
    # 取消就长在「开始处理」的位置上，不会被右上角提示条挡住
    assert window.import_page.btn_run.text() == "取消处理"
    assert window.import_page.btn_run.isEnabled()
    assert window.import_page.btn_add.isEnabled() is False, "处理中不许再改清单"

    _wait_idle(app, window)

    # 1) 进度条真实到 100
    assert window.progress.value() == 100
    assert window.lbl_pct.text() == "100%"
    # 2) 状态留在清单上，不靠 toast（同一文件两页 → 聚合显示「失败 1/2」）
    state = window.import_page.table.item(0, 5).text()
    assert state == "失败 1/2", state
    assert "页码 99 超出范围" in window.import_page.table.item(0, 5).toolTip()
    assert "失败 1" in window.lbl_stage.text()
    # 3) 失败不能混进成功
    assert window.import_page.card_result.isVisible()
    assert "成功 1 页，失败 1 页" in window.import_page.lbl_result.text()
    # 4) 失败项可重试
    assert len(window.import_page._failed_jobs) == 1
    assert window.import_page.btn_retry.isVisible()
    # 5) 结果里只保留成功页的表格（失败页 n_rows=0）
    assert len(window._results) == 2
    assert sum(1 for r in window._results if not r.error) == 1


def test_cancel_marks_remaining_skipped(app, window):
    from gui.worker import JobItem

    pdf = DEMO / "demo01.pdf"
    if not pdf.exists():
        pytest.skip("缺 demo01.pdf")
    jobs = [JobItem(pdf, 1, f"s{i}") for i in range(3)]
    window.import_page.add_paths([pdf])
    window._start_jobs(jobs)
    window.import_page.btn_run.click()       # 立即反悔：点「取消处理」
    assert window.import_page.btn_run.text() == "停止中…"
    _wait_idle(app, window)
    assert window._timer.isActive() is False
    assert window.import_page.btn_run.text() == "开始处理"
    assert "取消" in window.lbl_stage.text() or "已取消" in window.import_page.lbl_result.text()
    # 取消的页必须写明，不能显示成「完成」
    state = window.import_page.table.item(0, 5).text()
    assert "取消" in state, state


def test_retry_keeps_finished_pages(app, window):
    """重试失败项时不能把已成功的页冲掉 —— 旧代码每次都 self._results = []。"""
    from gui.worker import JobItem

    pdf = DEMO / "demo01.pdf"
    if not pdf.exists():
        pytest.skip("缺 demo01.pdf")
    window.import_page.add_paths([pdf])

    window._start_jobs([JobItem(pdf, 1, "s1"), JobItem(pdf, 99, "bad")])
    _wait_idle(app, window)
    assert len(window._results) == 2
    ok_before = [r for r in window._results if not r.error]
    assert len(ok_before) == 1

    window.import_page._failed_jobs = [JobItem(pdf, 99, "bad")]
    window._on_retry(list(window.import_page._failed_jobs))
    _wait_idle(app, window)

    # 成功的那页还在，失败的页只留一份
    ok_after = [r for r in window._results if not r.error]
    assert len(ok_after) == 1
    assert ok_after[0].page_no == 1
    assert len(window._results) == 2


def test_search_cancel_restores_ui(app, window, monkeypatch):
    """搜索也能取消：主按钮变「取消搜索」，取消后界面恢复且不偷偷开工。"""
    import time as _t

    from core.search import SearchResult
    from PyQt5.QtWidgets import QApplication

    def slow_many(paths, title, *, use_ocr=False, on_progress=None, should_stop=None):
        out = []
        for i, p in enumerate(paths, 1):
            if should_stop and should_stop():
                return out
            if on_progress:
                on_progress(i, len(paths), str(p))
            _t.sleep(0.3)
            out.append(SearchResult(path=p, title=title))
        return out

    monkeypatch.setattr("core.search.search_many", slow_many)

    pdf = DEMO / "demo01.pdf"
    if not pdf.exists():
        pytest.skip("缺 demo01.pdf")
    window.import_page.add_paths([pdf, pdf.with_name("demo02.pdf")])

    window._on_search("管理费用明细表", False)
    assert window.import_page.btn_run.text() == "取消搜索"
    app.processEvents()

    window.import_page.btn_run.click()          # 点「取消搜索」
    assert window.import_page.btn_run.text() == "停止中…"
    assert "取消搜索" in window.lbl_stage.text()

    ok = _pump(app, 20, until=lambda: window._search_worker.isFinished())
    assert ok
    for _ in range(20):
        app.processEvents()
        _t.sleep(0.02)

    assert window.import_page.btn_run.text() == "开始处理"
    assert window.import_page.btn_add.isEnabled()
    assert window.import_page.btn_search.isEnabled()
    assert window.progress.isVisible() is False
    assert window._results == [], "取消后不能自动开始处理"
    assert window._worker is None


def test_search_without_hits_explains_itself(app, window):
    """0 命中必须留在界面上说清原因 —— 之前只弹 6 秒 toast，用户只看到「什么都没」。"""
    import time as _t

    pdf = DEMO / "demo01.pdf"
    if not pdf.exists():
        pytest.skip("缺 demo01.pdf")
    window.import_page.add_paths([pdf])

    window._on_search("根本不存在的表名", False)
    ok = _pump(app, 30, until=lambda: window._search_worker is not None
               and window._search_worker.isFinished())
    assert ok
    for _ in range(20):
        app.processEvents()
        _t.sleep(0.02)

    head = window.import_page.lbl_result.text()
    assert "0 命中" in head, head
    detail = window.import_page.lbl_failures.text()
    assert "个字符" in detail
    assert "关键字" in detail
    assert window.import_page.card_result.isVisible()
    assert window.import_page.btn_retry.isVisible() is False, "搜索没命中不该显示重试处理"
    assert window._results == [], "0 命中不能自动开始处理"
    assert "0 命中" in window.lbl_stage.text()


def test_row_state_aggregates_multi_page(app, window):
    """一个文件多页：清单行要显示「完成 2 页」这类聚合，而不是只反映最后一页。"""
    pdf = DEMO / "demo01.pdf"
    if not pdf.exists():
        pytest.skip("缺 demo01.pdf")
    window.import_page.add_paths([pdf])
    window.import_page.set_job_rows([0, 0])
    window.import_page.update_job_state(0, "running", "")
    assert window.import_page.table.item(0, 5).text().startswith("处理中")
    window.import_page.update_job_state(0, "done", "10 行")
    window.import_page.update_job_state(1, "failed", "页码超出范围")
    assert window.import_page.table.item(0, 5).text() == "失败 1/2"
    tip = window.import_page.table.item(0, 5).toolTip()
    assert "页码超出范围" in tip, tip
