# -*- coding: utf-8 -*-
"""gui.import_page —— 导入 PDF + 两种取页方式（按页码 / 按标题搜索）。"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFileDialog, QHBoxLayout, QHeaderView,
    QRadioButton, QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    ComboBox, InfoBar, InfoBarPosition, LineEdit, PrimaryPushButton, PushButton,
    SwitchButton, TableWidget,
)

import fitz

from gui.components import Card, Page, field_label, hint, section_title, title
from gui.theme import TABLE_QSS, T, ui_font
from gui.worker import JobItem

MODE_PAGE = "page"
MODE_TITLE = "title"

# 单项状态 → (中文, 颜色)
STATE_TEXT = {
    "pending": ("待处理", T.TEXT_MUTE),
    "running": ("处理中", T.INFO),
    "done": ("完成", T.OK),
    "review": ("需复核", T.WARN),
    "failed": ("失败", T.DANGER),
    "skipped": ("已跳过", T.NEUTRAL),
}


class ImportPage(Page):
    jobs_ready = pyqtSignal(list)
    search_requested = pyqtSignal(str, bool)
    retry_requested = pyqtSignal(list)      # list[JobItem]
    review_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    cancel_search_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("importPage", parent)
        self.files: list[Path] = []
        # 任务序号 → 清单行号（一个文件可能对应多页任务）
        self._job_rows: list[int] = []
        self._job_labels: list[str] = []
        self._job_state: dict[int, tuple[str, str]] = {}
        self._failed_jobs: list[JobItem] = []
        self._running = False
        self._searching = False
        self._build()
        self.setAcceptDrops(True)

    # ------------------------------------------------------------ UI
    def _build(self):
        lay = self.vlayout

        lay.addWidget(title(
            "导入 PDF",
            "先选文件，再决定取哪一页 —— 可以按页码，也可以输入表名让程序去找。"))

        lay.addWidget(self._build_file_card())
        lay.addWidget(self._build_mode_card())
        lay.addWidget(self._build_result_card())

        lay.addWidget(section_title("待处理清单"))
        self.table = TableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["文件名", "页数", "文本层", "取页", "工作表名", "状态"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setStyleSheet(TABLE_QSS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(T.SP_S)
        self.lbl_ready = hint("")
        row.addWidget(self.lbl_ready, 1)
        self.btn_run = PrimaryPushButton("开始处理")
        self.btn_run.setMinimumWidth(140)
        self.btn_run.setFixedHeight(36)
        row.addWidget(self.btn_run)
        lay.addLayout(row)

        self.btn_run.clicked.connect(self._on_run)

    def _build_file_card(self) -> Card:
        card = Card()
        card.v.addWidget(section_title("1 · 选择文件"))

        row = QHBoxLayout()
        row.setSpacing(T.SP_S)
        self.btn_add = PrimaryPushButton("添加文件")
        self.btn_add_dir = PushButton("添加文件夹")
        self.btn_remove = PushButton("移除选中")
        self.btn_clear = PushButton("清空")
        for b in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear):
            b.setFixedHeight(32)
            row.addWidget(b)
        row.addStretch(1)
        card.v.addLayout(row)

        card.v.addWidget(hint("也可以直接把 PDF 拖进窗口。"))
        self.btn_add.clicked.connect(self._on_add_files)
        self.btn_add_dir.clicked.connect(self._on_add_dir)
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_clear.clicked.connect(self._on_clear)
        return card

    def _build_mode_card(self) -> Card:
        card = Card()
        card.v.addWidget(section_title("2 · 怎么取页"))

        self.grp = QButtonGroup(self)
        r1 = QRadioButton("按页码")
        r2 = QRadioButton("按标题搜索")
        for r in (r1, r2):
            r.setFont(ui_font(T.FS_BODY))
        r1.setChecked(True)
        self.grp.addButton(r1, 0)
        self.grp.addButton(r2, 1)
        row = QHBoxLayout()
        row.setSpacing(T.SP_L)
        row.addWidget(r1)
        row.addWidget(r2)
        row.addStretch(1)
        card.v.addLayout(row)

        # 页码模式
        self.w_page = QWidget()
        vp = QHBoxLayout(self.w_page)
        vp.setContentsMargins(0, 0, 0, 0)
        vp.setSpacing(T.SP_S)
        vp.addWidget(field_label("取第"))
        self.ed_page = LineEdit()
        self.ed_page.setText("2")
        self.ed_page.setFixedWidth(64)
        vp.addWidget(self.ed_page)
        vp.addWidget(field_label("页"))
        vp.addSpacing(T.SP_L)
        self.cb_page_mode = ComboBox()
        self.cb_page_mode.addItem("所有文件用同一页", userData="same")
        self.cb_page_mode.addItem("在清单里逐个填", userData="each")
        self.cb_page_mode.addItem("每个文件的全部页", userData="all")
        vp.addWidget(self.cb_page_mode)
        vp.addStretch(1)
        card.v.addWidget(self.w_page)

        # 标题模式
        self.w_title = QWidget()
        vt = QVBoxLayout(self.w_title)
        vt.setContentsMargins(0, 0, 0, 0)
        vt.setSpacing(T.SP_S)

        r = QHBoxLayout()
        r.setSpacing(T.SP_S)
        r.addWidget(field_label("表名"))
        self.ed_title = LineEdit()
        self.ed_title.setPlaceholderText("例如：管理费用明细表")
        self.ed_title.setMinimumWidth(280)
        r.addWidget(self.ed_title, 1)
        self.btn_search = PushButton("搜索")
        self.btn_search.setFixedHeight(32)
        r.addWidget(self.btn_search)
        vt.addLayout(r)

        r2 = QHBoxLayout()
        r2.setSpacing(T.SP_S)
        r2.addWidget(field_label("扫描件也搜（会跑本地 OCR，较慢）"))
        self.sw_ocr = SwitchButton()
        r2.addWidget(self.sw_ocr)
        r2.addStretch(1)
        vt.addLayout(r2)
        vt.addWidget(hint("找到几页就提取几页；一个文件命中多页时各占一个工作表。"))
        card.v.addWidget(self.w_title)
        self.w_title.hide()

        self.grp.buttonClicked.connect(self._on_mode)
        self.btn_search.clicked.connect(self._emit_search)
        self.ed_title.returnPressed.connect(self._emit_search)
        return card

    def _build_result_card(self) -> Card:
        """处理结束后留在界面上的结果条 —— 失败项不会随 toast 一起消失。"""
        card = Card(warn=True)
        self.card_result = card

        head = QHBoxLayout()
        head.setSpacing(T.SP_S)
        self.lbl_result = field_label("")
        self.lbl_result.setFont(ui_font(T.FS_SECTION, bold=True))
        head.addWidget(self.lbl_result, 1)
        self.btn_goto_review = PushButton("去校对")
        self.btn_retry = PushButton("重试失败项")
        self.btn_dismiss = PushButton("关闭")
        for b in (self.btn_goto_review, self.btn_retry, self.btn_dismiss):
            b.setFixedHeight(30)
            head.addWidget(b)
        card.v.addLayout(head)

        self.lbl_failures = hint("")
        card.v.addWidget(self.lbl_failures)

        self.btn_goto_review.clicked.connect(self.review_requested.emit)
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_dismiss.clicked.connect(lambda: card.hide())
        card.hide()
        return card

    def _on_retry(self):
        if self._failed_jobs:
            self.retry_requested.emit(list(self._failed_jobs))

    def show_search_summary(self, headline: str, detail: str):
        """搜索没有命中时，把「搜了什么、扫了多少、为什么可能没中」留在界面上。"""
        self.card_result.show()
        self.card_result._fill = T.NEUTRAL_BG
        self.card_result._border = T.BORDER
        self.lbl_result.setText(headline)
        self.lbl_result.setStyleSheet(f"color:{T.TEXT}")
        self.lbl_failures.setText(detail)
        self.btn_goto_review.hide()
        self.btn_retry.hide()
        self.btn_dismiss.show()
        self.card_result.update()

    # ------------------------------------------------------------ 模式
    def _on_mode(self):
        by_title = self.grp.checkedId() == 1
        self.w_page.setVisible(not by_title)
        self.w_title.setVisible(by_title)
        self.table.setColumnHidden(3, by_title)
        self._update_ready()

    def current_mode(self) -> str:
        return MODE_TITLE if self.grp.checkedId() == 1 else MODE_PAGE

    def _emit_search(self):
        if self._searching:
            return
        t = self.ed_title.text().strip()
        if not t:
            self._warn("先填表名", "例如「管理费用明细表」。")
            return
        if not self.files:
            self._warn("还没有文件", "先添加 PDF 再搜索。")
            return
        self.search_requested.emit(t, self.sw_ocr.isChecked())

    # ------------------------------------------------------------ 文件
    def _probe(self, p: Path) -> tuple[int, str]:
        try:
            doc = fitz.open(str(p))
            n = doc.page_count
            has_text = any(doc[i].get_text("text").strip() for i in range(min(n, 3)))
            doc.close()
            return n, "有" if has_text else "无（扫描件）"
        except Exception as e:  # noqa: BLE001
            return 0, f"打不开：{type(e).__name__}"

    def add_paths(self, paths: list[Path]):
        existing = {f.resolve() for f in self.files}
        added = 0
        for p in paths:
            p = Path(p)
            if p.suffix.lower() != ".pdf" or p.resolve() in existing:
                continue
            self.files.append(p)
            existing.add(p.resolve())
            added += 1
        if added:
            self._refresh()
            self._ok("已添加", f"{added} 个 PDF")
        elif paths:
            self._warn("没有新增", "重复或不是 PDF。")

    def _on_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 PDF", "", "PDF (*.pdf)")
        if paths:
            self.add_paths([Path(p) for p in paths])

    def _on_add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.add_paths(sorted(Path(d).rglob("*.pdf")))

    def _on_remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.files):
                self.files.pop(r)
        self._refresh()

    def _on_clear(self):
        self.files.clear()
        self._refresh()

    def _refresh(self):
        self._job_rows = []
        self._job_state.clear()
        self._failed_jobs = []
        self.card_result.hide()
        self.table.setRowCount(len(self.files))
        default_page = self.ed_page.text().strip() or "2"
        for i, p in enumerate(self.files):
            n, text_state = self._probe(p)
            for col, val in enumerate([p.name, str(n), text_state, default_page,
                                       p.stem, "待处理"]):
                it = QTableWidgetItem(val)
                if col in (1, 3, 5):
                    it.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, col, it)
            self._paint_state(i, *STATE_TEXT["pending"])
        self._update_ready()

    def _update_ready(self):
        n = len(self.files)
        self.btn_run.setEnabled(n > 0 and not self._running)
        if not n:
            self.lbl_ready.setText("尚未选择文件")
        else:
            mode = "按标题搜索" if self.current_mode() == MODE_TITLE else "按页码"
            self.lbl_ready.setText(f"已选 {n} 个文件 ｜ 取页方式：{mode}")

    # ------------------------------------------------------------ 任务
    def build_jobs(self) -> list[JobItem] | None:
        if self.current_mode() == MODE_TITLE:
            return None
        mode = self.cb_page_mode.currentData()
        jobs: list[JobItem] = []
        try:
            same_page = int(self.ed_page.text().strip() or "2")
        except ValueError:
            same_page = 2

        for i, p in enumerate(self.files):
            sheet = self.table.item(i, 4).text().strip() if self.table.item(i, 4) else p.stem
            if mode == "same":
                jobs.append(JobItem(p, same_page, sheet))
            elif mode == "each":
                raw = self.table.item(i, 3).text().strip() if self.table.item(i, 3) else "1"
                try:
                    pg = int(raw)
                except ValueError:
                    self._warn("页码不合法", f"{p.name} 的页码是 {raw!r}")
                    return None
                jobs.append(JobItem(p, pg, sheet))
            else:
                n, _ = self._probe(p)
                for pg in range(1, max(1, n) + 1):
                    suffix = f"_p{pg}" if n > 1 else ""
                    jobs.append(JobItem(p, pg, f"{sheet}{suffix}"))
        return jobs

    def row_of(self, path: Path) -> int:
        """文件路径 → 清单行号；不在清单里返回 -1。"""
        try:
            return self.files.index(Path(path))
        except ValueError:
            return -1

    # ------------------------------------------------------------ 状态
    def set_job_rows(self, rows: list[int], labels: list[str] | None = None):
        """开始一批任务：绑定「任务序号 → 清单行」，并把状态清零。"""
        self._job_rows = rows
        self._job_labels = list(labels or [""] * len(rows))
        self._job_state.clear()
        self._failed_jobs = []
        self.card_result.hide()
        for r in sorted({r for r in rows if r >= 0}):
            self._paint_state(r, *STATE_TEXT["pending"])

    def _paint_state(self, row: int, text: str, tone: str, tip: str = ""):
        it = self.table.item(row, 5)
        if it is None:
            return
        it.setText(text)
        it.setForeground(QColor(tone))
        it.setFont(ui_font(T.FS_SMALL, bold=(tone == T.DANGER)))
        it.setToolTip(tip)
        if tone == T.DANGER:
            # 失败的行滚进可见区，否则清单一长就看不到是哪一页挂了
            self.table.scrollToItem(it)

    def update_job_state(self, index: int, state: str, detail: str = ""):
        """收一条任务状态，刷新它对应的清单行（可能几页共用一个文件行）。"""
        self._job_state[index] = (state, detail)
        if index < len(self._job_rows):
            self._render_row(self._job_rows[index])

    def _render_row(self, row: int):
        if row < 0 or row >= len(self.files):
            return
        idxs = [i for i, r in enumerate(self._job_rows) if r == row]
        states = [self._job_state.get(i) for i in idxs]
        live = [s for s in states if s is not None]
        total = len(idxs)
        n = len(self.files)

        if not live:
            self._paint_state(row, *STATE_TEXT["pending"])
            return

        kinds = [s[0] for s in live]
        finished = sum(1 for k in kinds if k in ("done", "review"))
        tips = []
        for i, s in zip(idxs, states):
            if s is None:
                continue
            name = self._job_labels[i] if i < len(self._job_labels) else ""
            tips.append(f"{name or Path(self.files[row]).name}：{STATE_TEXT[s[0]][0]}"
                        + (f" —— {s[1]}" if s[1] else ""))

        if "failed" in kinds:
            k = kinds.count("failed")
            text, tone = (f"失败 {k}" if total == 1 else f"失败 {k}/{total}", T.DANGER)
        elif "running" in kinds:
            text = "处理中" if total == 1 else f"处理中 {finished}/{total}"
            tone = T.INFO
        elif len(live) < total:
            text, tone = f"处理中 {len(live)}/{total}", T.INFO
        elif "skipped" in kinds:
            # 取消 / 预算中断：明确说清已完成多少，不能显示成「完成」
            text = ("已取消" if finished == 0 else f"已取消·完成 {finished}/{total}")
            tone = T.NEUTRAL
        elif "review" in kinds:
            text, tone = ("需复核" if total == 1 else f"需复核 {len(live)} 页"), T.WARN
        else:
            text, tone = ("完成" if total == 1 else f"完成 {total} 页"), T.OK

        self._paint_state(row, text, tone, "\n".join(tips))

    def show_result(self, summary, failures: list[tuple[object, str]], weak: int = 0):
        """批次结束后写结果条：成功几页、失败几页、失败原因逐条列出。"""
        self._failed_jobs = [job for job, _ in failures]
        self.card_result.show()
        self.btn_goto_review.show()
        self.btn_dismiss.show()
        if failures:
            self.card_result._fill = T.WARN_BG
            self.card_result._border = "#F0D9B5"
            self.lbl_result.setText(
                f"处理结束：成功 {summary.ok_count} 页，失败 {len(failures)} 页")
            self.lbl_result.setStyleSheet(f"color:{T.DANGER}")
            lines = [f"· {job.label}：{err}" for job, err in failures[:6]]
            if len(failures) > 6:
                lines.append(f"… 还有 {len(failures) - 6} 页，详见清单状态列")
            self.lbl_failures.setText("\n".join(lines))
            self.btn_retry.show()
        else:
            self.card_result._fill = T.OK_BG
            self.card_result._border = "#CBE7D7"
            self.lbl_result.setText(
                f"处理完成：{summary.ok_count} 页全部成功，用时 {summary.elapsed:.0f} 秒")
            self.lbl_result.setStyleSheet(f"color:{T.OK}")
            self.lbl_failures.setText(
                "红色标注的格子需要人工确认，可在「校对」页逐格核对。")
            self.btn_retry.hide()
        if summary.cancelled:
            self.lbl_result.setText("已取消：已完成的页保留，未开始的不再处理")
            self.lbl_result.setStyleSheet(f"color:{T.NEUTRAL}")
        if weak:
            # 「成功」但没抽出表，必须说清楚，否则用户以为导出的是表
            self.card_result._fill = T.WARN_BG
            self.card_result._border = "#F0D9B5"
            extra = (f"另有 {weak} 页只识别出整行文本、没有列结构，"
                     "导出后需要人工整理（建议开大模型重跑这几页）")
            if failures:
                self.lbl_failures.setText(self.lbl_failures.text() + "\n· " + extra)
            else:
                self.lbl_failures.setText(extra)
            self.card_result.update()
        self.card_result.update()
        if failures:
            # 结果条出现后表格会被压矮，等布局稳了再把失败行滚进视野
            row = self.row_of(failures[0][0].path)
            QTimer.singleShot(0, lambda: self._scroll_to(row))

    def _scroll_to(self, row: int):
        it = self.table.item(row, 5)
        if it is not None:
            self.table.scrollToItem(it)

    def set_running(self, running: bool):
        """处理中锁住清单编辑，避免边跑边改导致行号错位。

        主按钮原地变成「取消处理」：跑起来之后用户最可能的动作是反悔，
        而这个位置不会被右上角的提示条挡住。
        """
        self._running = running
        for b in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear,
                  self.btn_search):
            b.setEnabled(not running)
        self.btn_run.setEnabled(True if running else bool(self.files))
        self.btn_run.setText("取消处理" if running else "开始处理")
        self.btn_run.setToolTip(
            "停止派发后续页，已完成的保留" if running else "")
        self.table.setEditTriggers(
            QAbstractItemView.NoEditTriggers if running else QAbstractItemView.DoubleClicked)
        if not running:
            self._update_ready()

    def mark_cancelling(self):
        self.btn_run.setEnabled(False)
        self.btn_run.setText("停止中…")

    def set_searching(self, searching: bool):
        """搜索中把主按钮换成「取消搜索」——和取消处理同一个位置同一个道理。"""
        self._searching = searching
        for b in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear,
                  self.btn_search):
            b.setEnabled(not searching)
        self.btn_run.setEnabled(True if searching else bool(self.files))
        if searching:
            self.btn_run.setText("取消搜索")
            self.btn_run.setToolTip("停下搜索，已找到的页不会自动处理")
        else:
            self.btn_run.setText("开始处理")
            self.btn_run.setToolTip("")
            self._update_ready()

    def mark_search_stopping(self):
        self.btn_run.setEnabled(False)
        self.btn_run.setText("停止中…")

    def _on_run(self):
        if self._running:
            self.cancel_requested.emit()
            return
        if self._searching:
            self.cancel_search_requested.emit()
            return
        if self.current_mode() == MODE_TITLE:
            self._emit_search()
            return
        jobs = self.build_jobs()
        if jobs:
            self.jobs_ready.emit(jobs)

    # ------------------------------------------------------------ 提示
    def _ok(self, t: str, c: str):
        InfoBar.success(t, c, duration=2200,
                        position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def _warn(self, t: str, c: str):
        InfoBar.warning(t, c, duration=3500,
                        position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    # ------------------------------------------------------------ 拖拽
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        paths = [p for p in paths if p.suffix.lower() == ".pdf"]
        if paths:
            self.add_paths(paths)
