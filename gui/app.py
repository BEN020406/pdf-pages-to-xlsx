# -*- coding: utf-8 -*-
"""gui.app —— 主窗口。

视觉骨架：
    左侧 44px 图标导航（窄，不抢注意力）
    顶部状态条（处理进度 / 花费 / 需复核数）—— 常驻，这是唯一的成本来源
    中间内容区（卡片式页面）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon as FIF, FluentWindow, InfoBar, InfoBarPosition,
    NavigationItemPosition, ProgressBar, PushButton, setTheme, setThemeColor, Theme,
)

from core.secure_store import load_config
from gui.components import StatChip, divider
from gui.export_page import ExportPage
from gui.import_page import MODE_TITLE, ImportPage
from gui.review_page import ReviewPage
from gui.settings_page import SettingsPage
from gui.theme import BUTTON_QSS, PAGE_QSS, T, mono_font, ui_font
from gui.worker import JobItem, ProcessWorker, SearchWorker

APP_NAME = "PDF 取页 → Excel"
APP_VERSION = "0.3.0"


def _mmss(sec: float) -> str:
    """秒 → mm:ss。耗时用等宽字显示，不跳动。"""
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.resize(1200, 800)
        self.setMinimumSize(980, 640)
        self.cfg = load_config()
        self._worker: ProcessWorker | None = None
        self._search_worker: SearchWorker | None = None
        self._results = []

        # 运行期状态（进度反馈用）
        self._running = False
        self._run_t0 = 0.0
        self._run_total = 0
        self._done = 0
        self._cur_index = 0
        self._cur_stage = ""
        self._retry_keys: set[tuple[str, int]] = set()
        self._search_done = 0
        self._search_total = 1
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

        self.setStyleSheet(PAGE_QSS + BUTTON_QSS)
        # 主题色：影响所有 Fluent 自绘控件（主按钮、开关、进度条、选中态）
        setThemeColor(T.PRIMARY, save=False)

        self.import_page = ImportPage(self)
        self.review_page = ReviewPage(self)
        self.export_page = ExportPage(self)
        self.settings_page = SettingsPage(self)

        self.addSubInterface(self.import_page, FIF.DOCUMENT, "导入",
                             NavigationItemPosition.TOP)
        self.addSubInterface(self.review_page, FIF.VIEW, "校对",
                             NavigationItemPosition.TOP)
        self.addSubInterface(self.export_page, FIF.SAVE, "导出",
                             NavigationItemPosition.TOP)
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置",
                             NavigationItemPosition.BOTTOM)
        self._tune_navigation()

        self._build_status_bar()

        self.import_page.jobs_ready.connect(self._on_jobs)
        self.import_page.search_requested.connect(self._on_search)
        self.import_page.retry_requested.connect(self._on_retry)
        self.import_page.cancel_requested.connect(self._on_cancel)
        self.import_page.cancel_search_requested.connect(self._on_cancel_search)
        self.import_page.review_requested.connect(
            lambda: self.switchTo(self.review_page))
        self.settings_page.config_changed.connect(self._on_cfg_changed)
        self.review_page.reprocess_requested.connect(self._on_reprocess)
        self.switchTo(self.import_page)
        self._refresh_stats()

    def _tune_navigation(self):
        """把导航图标间距拉开。

        Fluent 默认图标 40px 高且紧挨，四个图标挤成一坨。
        这里统一 40px 项高、44px 宽。注意 NavigationToolButton
        没有 setIconSize（只有 setBaseSize），要分开处理。
        """
        from PyQt5.QtCore import QSize

        nav = self.navigationInterface
        nav.setExpandWidth(200)
        for btn in nav.findChildren(QWidget):
            name = type(btn).__name__
            if name == "NavigationPushButton":
                btn.setFixedHeight(40)
                btn.setMinimumWidth(44)
                if hasattr(btn, "setIconSize"):
                    btn.setIconSize(QSize(18, 18))
            elif name == "NavigationToolButton":
                btn.setFixedHeight(40)
                btn.setMinimumWidth(44)

    # ------------------------------------------------------------ 顶栏
    def _build_status_bar(self):
        """常驻状态条：进度 / 阶段 / 花费 / 需复核。

        进度反馈的三层，都在这一条里：
            1. 进度条 + 百分比 —— 已完成页数 / 总页数（真实的，不预支）
            2. 阶段文字 —— 当前页在做哪一步 + 已用时间 + 预计剩余

        取消按钮**不放这里**：Fluent 的 InfoBar 弹在右上角，正好压住这一带，
        刚点完「开始处理」想立刻反悔时按钮会被挡住。
        取消放在导入页「开始处理」的位置（跑起来就变成「取消处理」）。

        结构说明（实测）：FluentWindow 没有 `vBoxLayout`/`centralWidget`，
        只有 `hBoxLayout`，内容是
            [0] NavigationInterface
            [1] QHBoxLayout → [0] StackedWidget
        所以不能直接往那个 QHBoxLayout 里插状态条（会变成左右并排，
        把页面挤到右边）。正确做法是把 stackedWidget 取出，和状态条
        一起放进一个纵向容器，再把这个容器放回原处。
        """
        bar = QWidget()
        bar.setObjectName("pageBody")
        bar.setFixedHeight(46)
        h = QHBoxLayout(bar)
        h.setContentsMargins(T.SP_XL, T.SP_XS, T.SP_XL, T.SP_XS)
        h.setSpacing(T.SP_L)

        self.chip_files = StatChip("文件")
        self.chip_sheets = StatChip("工作表")
        self.chip_review = StatChip("需复核")
        self.chip_fail = StatChip("失败")
        self.chip_cost = StatChip("花费(元)")
        for c in (self.chip_files, self.chip_sheets, self.chip_review,
                  self.chip_fail, self.chip_cost):
            h.addWidget(c)
        h.addStretch(1)

        self.lbl_stage = QLabel("就绪")
        self.lbl_stage.setFont(ui_font(T.FS_SMALL))
        self.lbl_stage.setStyleSheet(f"color:{T.TEXT_SUB}")
        self.lbl_stage.setMinimumWidth(120)
        self.lbl_stage.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.lbl_stage)

        self.progress = ProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setFixedHeight(6)
        self.progress.setValue(0)
        self.progress.hide()
        h.addWidget(self.progress)

        self.lbl_pct = QLabel("")
        self.lbl_pct.setFont(mono_font(T.FS_SMALL))
        self.lbl_pct.setStyleSheet(f"color:{T.TEXT_SUB}")
        self.lbl_pct.setFixedWidth(38)
        self.lbl_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_pct.hide()
        h.addWidget(self.lbl_pct)

        right = self.hBoxLayout.itemAt(1).layout()
        if right is None:
            return
        # 把 stackedWidget 从横向容器里摘出来
        right.removeWidget(self.stackedWidget)

        holder = QWidget()
        holder.setObjectName("pageBody")
        v = QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(bar, 0)
        v.addWidget(self.stackedWidget, 1)

        right.addWidget(holder)

    # ------------------------------------------------------------ 进度
    def _tick(self):
        """每秒刷一次阶段文字：已用 / 预计剩余。让用户看得到「还在动」。"""
        if not self._running:
            return
        elapsed = time.monotonic() - self._run_t0
        done = self._done
        total = self._run_total
        parts = [f"第 {min(self._cur_index + 1, total)}/{total} 页"]
        if self._cur_stage:
            parts.append(self._cur_stage)
        parts.append(f"已用 {_mmss(elapsed)}")
        if done >= 1 and total > done:
            eta = elapsed / done * (total - done)
            parts.append(f"预计还需 {_mmss(eta)}")
        self.lbl_stage.setText("·".join(parts))

    def _on_progress(self, done: int, total: int):
        self._done = done
        self._run_total = max(1, total)
        pct = int(done * 100 / self._run_total)
        self.progress.setValue(pct)
        self.lbl_pct.setText(f"{pct}%")
        self._tick()

    def _on_item_state(self, index: int, state: str, detail: str):
        self.import_page.update_job_state(index, state, detail)
        if state == "running":
            self._cur_index = index
            self._cur_stage = "准备中"
            self._tick()

    def _on_stage(self, index: int, text: str):
        self._cur_index = index
        self._cur_stage = text
        self._tick()

    def _on_cancel(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.import_page.mark_cancelling()
            self.lbl_stage.setText("正在取消，等当前页处理完就停…")

    def _dismiss_toasts(self):
        """开始新批次前先收掉旧提示，别让它压住进度条。"""
        for w in self.findChildren(InfoBar):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    def _reset_bar(self):
        """批次结束三秒后把进度条收回，只留结论文字。"""
        if self._running:
            return
        self.progress.setValue(0)
        self.progress.hide()
        self.lbl_pct.hide()

    def _refresh_stats(self):
        """顶栏只统计「真正拿到表」的页 —— 失败页不能算工作表。"""
        ok = [r for r in self._results if not r.error]
        failed = [r for r in self._results if r.error]
        self.chip_files.set_value(str(len({r.path for r in ok})))
        self.chip_sheets.set_value(str(len(ok)))
        review = sum(r.review_count for r in ok)
        self.chip_review.set_value(str(review), T.DANGER if review else T.OK)
        self.chip_fail.set_value(str(len(failed)),
                                 T.DANGER if failed else T.TEXT_MUTE)
        cost = sum(r.cost_cny for r in self._results)
        self.chip_cost.set_value(f"{cost:.4f}")

    # ------------------------------------------------------------ 搜索
    def _on_search(self, title_text: str, use_ocr: bool):
        if self._search_worker is not None and self._search_worker.isRunning():
            InfoBar.warning("正在搜索", "请等当前搜索完成。", duration=2500,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        if self._worker is not None and self._worker.isRunning():
            InfoBar.warning("任务正在运行", "等这批处理完再搜。", duration=2500,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        self._dismiss_toasts()
        self._search_total = 1
        self._search_done = 0
        self.import_page.set_searching(True)
        self.progress.show()
        self.lbl_pct.show()
        self.progress.setValue(0)
        self.lbl_pct.setText("0%")
        self.lbl_stage.setText("准备搜索（先数一遍页数）…")
        self._search_worker = SearchWorker(
            list(self.import_page.files), title_text, use_ocr=use_ocr)
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.done.connect(self._on_search_done)
        self._search_worker.start()

    def _on_search_progress(self, done: int, total: int, name: str):
        self._search_done = done
        self._search_total = max(1, total)
        pct = int(done * 100 / self._search_total)
        self.progress.setValue(pct)
        self.lbl_pct.setText(f"{pct}%")
        self.lbl_stage.setText(f"搜索 {done}/{total} 页·{name}")

    def _on_cancel_search(self):
        if self._search_worker is not None and self._search_worker.isRunning():
            self._search_worker.cancel()
            self.import_page.mark_search_stopping()
            self.lbl_stage.setText("正在取消搜索，等当前页扫完就停…")

    def _on_search_done(self, results, cancelled: bool = False):
        self.import_page.set_searching(False)
        self._reset_bar()
        # 结论都写在导入页（结果条 / 状态列），切回去才看得见
        self.switchTo(self.import_page)
        if cancelled:
            self.lbl_stage.setText(f"搜索已取消·扫完 {self._search_done} 页")
            InfoBar.info(
                "已取消搜索",
                f"扫完 {self._search_done} 页，找到的页没有加入清单；"
                "需要的话重新点「开始处理」。",
                duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        self.lbl_stage.setText("就绪")

        hits = [r for r in results if r.matches]
        total_pages = sum(len(r.matches) for r in hits)
        if not hits:
            self._explain_no_hits(results)
            return

        # 把命中页转成任务
        jobs: list[JobItem] = []
        for r in hits:
            for m in r.matches:
                stem = Path(r.path).stem
                suffix = f"_p{m.page_no}"
                jobs.append(JobItem(r.path, m.page_no, f"{stem}{suffix}"))
        InfoBar.success(
            "找到目标页",
            f"{len(hits)} 个文件共 {total_pages} 页，已加入清单。",
            duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        self._start_jobs(jobs)

    def _explain_no_hits(self, results):
        """0 命中的解释必须留在界面上：说清搜了什么、扫了多少、有几种原因。

        之前只弹一条 6 秒 toast，用户看到的就是「搜完什么都没有」。
        """
        n_files = len(results)
        n_pages = sum(r.pages_scanned for r in results)
        chars = sum(r.text_chars for r in results)
        ocr_used = any(r.used_ocr for r in results)
        title = self.import_page.ed_title.text().strip()

        self.import_page.show_search_summary(
            f"搜索「{title}」：扫了 {n_files} 个文件 {n_pages} 页，0 命中",
            "\n".join([
                f"这 {n_pages} 页共识别到 {chars:,} 个字符"
                + ("（走了本地 OCR）" if ocr_used else "（来自文本层）"),
                "· 标题可能不是这几个字 —— 只填关键字再搜，比如「损益」而不是「损益表」",
                "· 繁体/简体已自动兼容，字形差异不是原因；扫描件把标题拆行或认错字才会对不上",
                "· 页面是纯图片/手写时 OCR 认不出来 —— 这种情况直接切回「按页码」取页更省事",
            ]))
        self.lbl_stage.setText(f"搜索「{title}」·0 命中·扫了 {n_pages} 页")

        if chars == 0:
            InfoBar.warning(
                "这些页没读出文字",
                f"{n_pages} 页一个字都没识别出来，多半是纯图片或加密页面；"
                "这种情形搜索用不上，直接按页码取页。",
                duration=8000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    # ------------------------------------------------------------ 处理
    def _on_jobs(self, jobs: list[JobItem]):
        self._start_jobs(jobs)

    def _start_jobs(self, jobs: list[JobItem], *, retry: bool = False):
        if self._worker is not None and self._worker.isRunning():
            InfoBar.warning("任务正在运行", "请等当前任务完成。", duration=3000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        if not jobs:
            return

        llm_cfg = self.settings_page.to_llm_config()
        self._dismiss_toasts()
        if llm_cfg is None:
            # 不用 toast：跑起来之后所有提示都留在界面里，别再弹窗压住进度条
            reason = self.settings_page.llm_block_reason()
            self.import_page.lbl_ready.setText(
                f"大模型这次用不上（{reason}）—— 只走文本层 + 本地 OCR；"
                "扫描件建议先到「设置」把 Key 和开关弄好")

        if retry:
            # 重试只替换这几页的结果，已成功的页保留
            self._retry_keys = {(str(j.path), j.page_no) for j in jobs}
            self._results = [r for r in self._results
                             if (str(r.path), r.page_no) not in self._retry_keys]
        else:
            self._retry_keys = set()
            self._results = []
            self.review_page.set_results([])
            self.export_page.set_results([])
            self._refresh_stats()

        # 任务序号 → 清单行号：一个文件可能出多页，靠路径找回行
        self.import_page.set_job_rows(
            [self.import_page.row_of(j.path) for j in jobs],
            [j.label for j in jobs])
        self.import_page.set_running(True)

        self._running = True
        self._done = 0
        self._run_total = len(jobs)
        self._cur_index = 0
        self._cur_stage = "准备中"
        self._run_t0 = time.monotonic()
        self.progress.show()
        self.lbl_pct.show()
        self.progress.setValue(0)
        self.lbl_pct.setText("0%")
        self.lbl_stage.setText(f"第 1/{len(jobs)} 页·准备中·已用 00:00")
        self._timer.start()

        self._worker = ProcessWorker(
            jobs, llm_cfg,
            force_ocr=self.import_page.sw_ocr.isChecked(),
            dpi=150,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.item_state.connect(self._on_item_state)
        self._worker.stage.connect(self._on_stage)
        self._worker.item_done.connect(self._on_item_done)
        self._worker.budget_exceeded.connect(self._on_budget)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _on_item_done(self, res):
        key = (str(res.path), res.page_no)
        if all((str(r.path), r.page_no) != key for r in self._results):
            self._results.append(res)
        else:
            self._results = [res if (str(r.path), r.page_no) == key else r
                             for r in self._results]
        self.review_page.set_results(self._results)
        self.export_page.set_results(self._results)
        self._refresh_stats()

    def _on_budget(self, spent: float):
        InfoBar.warning(
            "已达预算上限",
            f"本次已花费 {spent:.4f} 元，后续页面已停止调用大模型。",
            duration=8000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _on_finished(self, summary):
        self._timer.stop()
        self._running = False
        self.import_page.set_running(False)

        self.progress.setValue(100)
        self.lbl_pct.setText("100%")
        QTimer.singleShot(3000, self._reset_bar)
        elapsed = _mmss(summary.elapsed)
        if summary.cancelled:
            self.lbl_stage.setText(
                f"已取消·完成 {summary.ok_count} 页·用时 {elapsed}")
        else:
            self.lbl_stage.setText(
                f"完成 {summary.ok_count}/{summary.total} 页"
                f"·失败 {len(summary.failed)}·用时 {elapsed}")

        if self._retry_keys:
            # 重试批次只覆盖这几页，其余页（含之前成功的）原样保留
            kept = [r for r in self._results
                    if (str(r.path), r.page_no) not in self._retry_keys]
            self._results = kept + list(summary.results)
        else:
            self._results = summary.results
        self.review_page.set_results(self._results)
        self.export_page.set_results(self._results)
        self._refresh_stats()

        # 结果条：失败页留在界面上，不随 toast 消失
        weak = [r for r in summary.results if r.weak_structure]
        self.import_page.show_result(summary, summary.failed, weak=len(weak))

        review = sum(r.review_count for r in summary.results)
        cost = sum(r.cost_cny for r in summary.results)
        base = (f"{summary.ok_count} 个工作表 ｜ 需复核 {review} 格 ｜ "
                f"花费 {cost:.4f} 元 ｜ 用时 {elapsed}")
        if weak:
            InfoBar.warning(
                "有页面没抽出表格",
                f"{len(weak)} 页只有整行文本、没有列结构，导出后需要人工整理；"
                "建议到「设置」开启大模型重跑这几页。",
                duration=10000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        if summary.failed:
            InfoBar.warning(
                "处理完成，但有失败",
                f"{base}；失败 {len(summary.failed)} 页，见清单「状态」列",
                duration=9000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        elif summary.cancelled:
            InfoBar.info("已取消", f"已完成部分保留：{base}", duration=6000,
                         position=InfoBarPosition.TOP_RIGHT, parent=self)
        else:
            InfoBar.success("处理完成", base, duration=8000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
            # 全部成功才自动跳去校对；有失败先让用户看清失败项
            if summary.results:
                self.switchTo(self.review_page)

    def _on_retry(self, jobs: list[JobItem]):
        if not jobs:
            return
        InfoBar.info("重试失败项", f"重新处理 {len(jobs)} 页。", duration=2500,
                     position=InfoBarPosition.TOP_RIGHT, parent=self)
        self._start_jobs(jobs, retry=True)

    def _on_cfg_changed(self, cfg):
        self.cfg = cfg

    def _on_reprocess(self, old_res, clip: tuple):
        if self._worker is not None and self._worker.isRunning():
            InfoBar.warning("任务运行中", "等批处理完成后再框选重跑。",
                            duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        llm_cfg = self.settings_page.to_llm_config()
        try:
            from core.llm import LLMClient
            from core.pipeline import process_page
            llm = LLMClient(llm_cfg) if llm_cfg else None
            new_res = process_page(
                old_res.path, old_res.page_no, llm=llm, dpi=150,
                use_llm=llm is not None, clip=clip,
            )
        except Exception as e:  # noqa: BLE001
            InfoBar.error("重跑失败", f"{type(e).__name__}: {e}", duration=6000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        self.review_page.replace_result(old_res, new_res)
        self._results = [new_res if r is old_res else r for r in self._results]
        self.export_page.set_results(self._results)
        self._refresh_stats()
        InfoBar.success(
            "已按框选区域重新识别",
            f"{new_res.n_rows} 行 × {new_res.n_cols} 列 ｜ 需复核 {new_res.review_count}",
            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def closeEvent(self, e):
        for w in (self._worker, self._search_worker):
            if w is not None and w.isRunning():
                if hasattr(w, "cancel"):
                    w.cancel()
                w.wait(3000)
        super().closeEvent(e)


def _setup_font(app: QApplication) -> str:
    """显式指定中文字体。

    PyQt5 的 Qt5 wheel 已不再自带字体（控制台会刷
    "Qt no longer ships fonts ... Cannot find font directory"），
    不指定的话在部分环境下会整屏无字。
    """
    from PyQt5.QtGui import QFont, QFontDatabase

    available = set(QFontDatabase().families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                 "Noto Sans SC", "Source Han Sans SC", "SimHei", "SimSun"):
        if name in available:
            f = QFont(name, 9)
            f.setStyleStrategy(QFont.PreferAntialias)
            app.setFont(f)
            return name
    return ""


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    _setup_font(app)
    setTheme(Theme.LIGHT)
    # 主题色必须用 Fluent 自己的 API：PrimaryPushButton 是自绘控件，
    # QSS 覆盖不了它的背景色（实测：写了 PrimaryPushButton{background:...}
    # 依然显示默认青色）。
    setThemeColor(T.PRIMARY, save=False)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
