# -*- coding: utf-8 -*-
"""gui.worker —— 后台任务线程。

所有 PDF / OCR / 网络调用都在这里跑，UI 线程只收信号。
取消用 threading.Event，模型调用之间检查，不会卡在某个请求上等不到响应。

信号设计（进度反馈的唯一真相来源）：
    progress(done, total)       已完成页数 / 总页数 —— 只驱动进度条
    item_state(index, state, detail)
                                state ∈ running/done/review/failed/skipped
                                detail 是这一页的结果摘要或失败原因
    stage(index, text)          当前页走到哪一步（读文本层 / 本地 OCR / 大模型…）
    finished_all(JobSummary)    批次结束（含失败、取消、耗时）
所有状态都按「每一项」发，界面才能把状态留在清单上，而不是靠会消失的 toast。
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal

from core.llm import LLMClient, LLMConfig, LLMError
from core.pipeline import PageResult, process_page
from core.writer import ExportOptions, ExportReport, write_workbook


@dataclass
class JobItem:
    """一个待处理项：一个 PDF 的一页。"""
    path: Path
    page_no: int
    sheet_name: str = ""

    @property
    def label(self) -> str:
        return f"{Path(self.path).name} · 第 {self.page_no} 页"


@dataclass
class JobSummary:
    """一批处理结束时的统计。失败页单独计数，不许混进「完成」。"""
    results: list[PageResult] = field(default_factory=list)
    total: int = 0
    failed: list[tuple[JobItem, str]] = field(default_factory=list)
    cancelled: bool = False
    budget_hit: bool = False
    error: str = ""          # 批次层面的致命错误（非单页失败）
    elapsed: float = 0.0

    @property
    def ok_count(self) -> int:
        return len(self.results) - len(self.failed)

    @property
    def skipped(self) -> int:
        return max(0, self.total - len(self.results))


class ProcessWorker(QThread):
    """批量处理 PDF 页面。"""

    progress = pyqtSignal(int, int)             # done, total
    item_state = pyqtSignal(int, str, str)      # index, state, detail
    stage = pyqtSignal(int, str)                # index, stage text
    item_done = pyqtSignal(object)              # PageResult
    item_failed = pyqtSignal(str, str)          # label, error
    finished_all = pyqtSignal(object)           # JobSummary
    log = pyqtSignal(str)
    budget_exceeded = pyqtSignal(float)         # spent

    def __init__(self, items: list[JobItem], llm_cfg: LLMConfig | None,
                 *, force_ocr: bool = False, dpi: int = 150, parent=None):
        super().__init__(parent)
        self.items = items
        self.llm_cfg = llm_cfg
        self.force_ocr = force_ocr
        self.dpi = dpi
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self):
        summary = JobSummary(total=len(self.items))
        total = len(self.items)
        t0 = time.monotonic()
        self.progress.emit(0, total)
        try:
            self._loop(summary, total)
        except Exception as e:  # noqa: BLE001
            # 连批次框架都炸了（比如 key 初始化失败）也必须收尾，
            # 否则界面永远停在「处理中」，用户只能强杀。
            self.log.emit(f"批次异常终止：{type(e).__name__}: {e}")
            self.log.emit(traceback.format_exc())
            summary.error = f"{type(e).__name__}: {e}"
        finally:
            summary.elapsed = time.monotonic() - t0
            self.finished_all.emit(summary)

    def _loop(self, summary: "JobSummary", total: int):
        llm = LLMClient(self.llm_cfg) if self.llm_cfg else None

        for i, item in enumerate(self.items):
            if self._stop.is_set():
                summary.cancelled = True
                self._skip_rest(i, total, "已取消")
                self.log.emit("已取消")
                break

            # 预算护栏：超了就停，已处理的页保留
            if llm is not None and self.llm_cfg is not None:
                if llm.usage.cost_cny >= self.llm_cfg.max_cost_cny:
                    summary.budget_hit = True
                    self.budget_exceeded.emit(llm.usage.cost_cny)
                    self._skip_rest(i, total, "预算已到上限，未处理")
                    self.log.emit(
                        f"已达预算上限 {self.llm_cfg.max_cost_cny} 元，停止后续处理")
                    break

            self.item_state.emit(i, "running", "")
            try:
                res = process_page(
                    item.path, item.page_no,
                    llm=llm, force_ocr=self.force_ocr, dpi=self.dpi,
                    use_llm=llm is not None,
                    on_stage=lambda s, idx=i: self.stage.emit(idx, s),
                )
                if item.sheet_name:
                    res.notes.append(f"工作表名：{item.sheet_name}")
                summary.results.append(res)
                self.item_done.emit(res)
                if res.error:
                    summary.failed.append((item, res.error))
                    self.item_state.emit(i, "failed", res.error)
                    self.item_failed.emit(item.label, res.error)
                else:
                    detail = (f"{res.n_rows} 行 × {res.n_cols} 列｜"
                              f"需复核 {res.review_count}｜{res.source}")
                    self.item_state.emit(
                        i, "review" if res.review_count else "done", detail)
                    self.log.emit(f"[{i + 1}/{total}] {item.label} → {res.n_rows} 行，"
                                  f"来源 {res.source}，需复核 {res.review_count}")
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                summary.failed.append((item, msg))
                self.log.emit(f"[{i + 1}/{total}] {item.label} 失败：{msg}")
                self.log.emit(traceback.format_exc())
                self.item_state.emit(i, "failed", msg)
                self.item_failed.emit(item.label, msg)

            self.progress.emit(i + 1, total)

    def _skip_rest(self, start: int, total: int, why: str):
        for j in range(start, total):
            self.item_state.emit(j, "skipped", why)


class ExportWorker(QThread):
    """单独导出，避免大工作簿阻塞 UI。"""

    done = pyqtSignal(object)       # ExportReport
    failed = pyqtSignal(str)

    def __init__(self, results: list[PageResult], out_path: Path,
                 opts: ExportOptions, parent=None):
        super().__init__(parent)
        self.results = results
        self.out_path = out_path
        self.opts = opts

    def run(self):
        try:
            rep = write_workbook(self.results, self.out_path, self.opts)
            self.done.emit(rep)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class SearchWorker(QThread):
    """按标题在各 PDF 里搜页。"""

    progress = pyqtSignal(int, int, str)     # done, total, name
    done = pyqtSignal(object, bool)          # list[SearchResult], cancelled

    def __init__(self, paths: list[Path], title: str, *, use_ocr: bool = False,
                 parent=None):
        super().__init__(parent)
        self.paths = paths
        self.title = title
        self.use_ocr = use_ocr
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    def run(self):
        from core.search import search_many

        results = search_many(
            self.paths, self.title, use_ocr=self.use_ocr,
            on_progress=lambda i, n, name: self.progress.emit(i, n, name),
            should_stop=self._stop.is_set,
        )
        self.done.emit(results, self.cancelled)


class TestConnectionWorker(QThread):
    """设置页的「测试连接」，不阻塞 UI。"""

    done = pyqtSignal(bool, str)

    def __init__(self, cfg: LLMConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):
        ok, msg = LLMClient(self.cfg).test_connection()
        self.done.emit(ok, msg)
