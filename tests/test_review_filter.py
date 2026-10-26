# -*- coding: utf-8 -*-
"""校对页的「只看有问题的」能力测试。

背景：10 页可以逐页翻，1 万页不可能。所以校对页要能：
    1. 只列出需要人看的页（有红/蓝格、或整页失败）；
    2. 进来就停在第一个有问题的格子上；
    3. 确认完自动跳下一处，而不是让用户自己找。
"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PyQt5")
pytest.importorskip("qfluentwidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from core.pipeline import CellOut, PageResult  # noqa: E402
from core.reconcile import Verdict  # noqa: E402


def make_page(i: int, verdicts: list[Verdict] | None = None,
              error: str = "") -> PageResult:
    verdicts = verdicts or []
    cells = [[CellOut(f"项目{j}", None, "text"),
              CellOut("1,000.00", 1000.0, "number",
                      verdict=verdicts[j] if j < len(verdicts) else Verdict.AGREE)]
             for j in range(3)]
    return PageResult(path=Path(f"f{i}.pdf"), page_no=i, source="ocr",
                      cells=cells, error=error)


@pytest.fixture
def review(app):
    from gui.review_page import ReviewPage
    page = ReviewPage()
    yield page
    page.deleteLater()


def test_all_pages_by_default(app, review):
    review.set_results([make_page(1), make_page(2), make_page(3)])
    assert review.cb_page.count() == 3
    assert "共 3 页" in review.lbl_summary.text()


def test_review_filter_shows_only_problem_pages(app, review):
    pages = [
        make_page(1),                                            # 干净
        make_page(2, [Verdict.CONFLICT]),                        # 有红格
        make_page(3, [Verdict.MODEL]),                           # 采信模型
        make_page(4, error="页码超出范围"),                       # 失败页
    ]
    review.set_results(pages)

    review.cb_filter.setCurrentIndex(1)          # 只看需复核
    assert review.cb_page.count() == 3, "干净页不该出现在列表里"

    review.cb_filter.setCurrentIndex(2)          # 只看冲突
    assert review.cb_page.count() == 1

    review.cb_filter.setCurrentIndex(3)          # 只看失败页
    assert review.cb_page.count() == 1

    review.cb_filter.setCurrentIndex(0)          # 回到全部
    assert review.cb_page.count() == 4


def test_filter_keeps_current_page_selected(app, review):
    """筛选切换时，如果当前页还在结果里，不该跳走。"""
    pages = [make_page(1), make_page(2, [Verdict.CONFLICT])]
    review.set_results(pages)
    review.cb_page.setCurrentIndex(1)            # 停在有红格那页
    assert review.current is pages[1]

    review.cb_filter.setCurrentIndex(1)          # 切到只看需复核
    assert review.current is pages[1], "当前页还在结果里却被切走了"


def test_enters_first_problem_cell(app, review):
    """进页就要停在第一个要人看的格子上。"""
    page = make_page(1, [Verdict.AGREE, Verdict.CONFLICT, Verdict.AGREE])
    review.set_results([page])
    item = review.table.currentItem()
    assert item is not None
    ri, ci = item.data(0x0100)                   # Qt.UserRole
    assert (ri, ci) == (1, 1), "没有停在红格上"


def test_step_moves_between_problem_pages(app, review):
    pages = [make_page(1), make_page(2, [Verdict.CONFLICT]),
             make_page(3), make_page(4, [Verdict.CONFLICT])]
    review.set_results(pages)
    review.cb_filter.setCurrentIndex(1)
    assert review.cb_page.count() == 2

    assert review.cb_page.currentIndex() == 0
    review._step(1)
    assert review.cb_page.currentIndex() == 1
    review._step(1)
    assert review.cb_page.currentIndex() == 1, "越界后不该跑出列表"
    review._step(-1)
    assert review.cb_page.currentIndex() == 0


def test_done_button_advances_to_next(app, review):
    pages = [make_page(1, [Verdict.CONFLICT]), make_page(2, [Verdict.CONFLICT])]
    review.set_results(pages)
    review.cb_filter.setCurrentIndex(1)
    review._on_done_page()
    assert review.cb_page.currentIndex() == 1


def test_done_button_disabled_when_page_needs_review(app, review):
    """本页还有红格时，「本页无问题」按钮应当不可点 —— 防止误跳过。"""
    review.set_results([make_page(1, [Verdict.CONFLICT])])
    assert review.btn_done.isEnabled() is False


def test_empty_filter_shows_message(app, review):
    review.set_results([make_page(1)])
    review.cb_filter.setCurrentIndex(2)          # 只看冲突，但没有
    assert review.cb_page.count() == 0
    assert "没有符合条件的页" in review.lbl_summary.text()
    assert "没有页" in review.lbl_img.text()


def test_editing_last_conflict_enables_skip(app, review):
    """改完最后一个红格，按钮要立刻变可点 —— 否则用户不知道可以往下走了。"""
    page = make_page(1, [Verdict.CONFLICT])     # 红格在 (0, 1)
    review.set_results([page])
    assert review.btn_done.isEnabled() is False
    assert page.review_count == 1

    item = review.table.item(0, 1)              # 就是那个红格
    assert item is not None
    item.setText("1,234.00")

    assert page.cells[0][1].verdict is Verdict.MANUAL
    assert page.review_count == 0, "改完红格，需复核数没有减少"
    assert review.btn_done.isEnabled() is True


def test_disabled_skip_button_explains_why(app, review):
    """灰着不解释等于坏了 —— tooltip 必须说明原因。"""
    review.set_results([make_page(1, [Verdict.CONFLICT])])
    assert "人工确认" in review.btn_done.toolTip()

    review.set_results([make_page(2, error="打不开")])
    assert "失败" in review.btn_done.toolTip()


def test_scales_to_thousand_pages(app, review):
    """几百页也要秒开 —— 列表只装筛出来的页。"""
    import time

    pages = [make_page(i) if i % 10 else make_page(i, [Verdict.CONFLICT])
             for i in range(1, 1001)]
    t0 = time.monotonic()
    review.set_results(pages)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"1000 页装载用了 {elapsed:.1f} 秒"

    review.cb_filter.setCurrentIndex(1)          # 只看需复核 → 100 页
    assert review.cb_page.count() == 100
