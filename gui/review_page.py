# -*- coding: utf-8 -*-
"""gui.review_page —— 校对：页面图（可框选）+ 可编辑表格（按状态着色）。"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QItemSelectionModel, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QSplitter,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import ComboBox, PushButton, TableWidget, TextEdit

from core.pipeline import PageResult
from core.reconcile import COLORS, Verdict
from gui.components import Card, Page, field_label, hint, title
from gui.theme import TABLE_QSS, T, mono_font, ui_font

LEGEND = [
    ("一致", COLORS[Verdict.AGREE]),
    ("采信本地", COLORS[Verdict.LOCAL]),
    ("采信模型", COLORS[Verdict.MODEL]),
    ("精度修正", COLORS[Verdict.PRECISION]),
    ("需人工", COLORS[Verdict.CONFLICT]),
    ("人工改", COLORS[Verdict.MANUAL]),
]


def _has_conflict(res: PageResult) -> bool:
    """这一页有没有红格（双方冲突，必须人工）。"""
    if res.report.cells:
        return any(cv.verdict is Verdict.CONFLICT for cv in res.report.cells)
    return any(c.verdict is Verdict.CONFLICT for row in res.cells for c in row)


class ImageView(QLabel):
    """页面图，支持橡皮筋框选，返回归一化坐标。"""

    box_selected = pyqtSignal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.setStyleSheet(
            f"background:{T.SURFACE_ALT};border:1px solid {T.BORDER};border-radius:{T.R_M}px")
        self.setMinimumWidth(300)
        self._pixmap: QPixmap | None = None
        self._origin = None
        self._cur = None
        self._rubber = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    def set_image(self, png: bytes, max_width: int):
        pm = QPixmap()
        if not pm.loadFromData(png):
            self.setText("（图片解码失败）")
            self._pixmap = None
            return
        self._pixmap = pm.scaledToWidth(max(300, max_width - 8), Qt.SmoothTransformation)
        self.setPixmap(self._pixmap)

    def _img_rect(self):
        if self._pixmap is None:
            return None
        w, h = self._pixmap.width(), self._pixmap.height()
        x = max(0, (self.width() - w) // 2)
        return x, 0, x + w, h

    def _to_norm(self, pos):
        r = self._img_rect()
        if r is None:
            return None
        x0, y0, x1, y1 = r
        nx = (pos.x() - x0) / max(1, x1 - x0)
        ny = (pos.y() - y0) / max(1, y1 - y0)
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._pixmap is not None:
            self._origin = self._cur = e.pos()
            self._rubber = True
            self.update()

    def mouseMoveEvent(self, e):
        if self._rubber:
            self._cur = e.pos()
            self.update()

    def mouseReleaseEvent(self, e):
        if not self._rubber:
            return
        self._rubber = False
        a, b = self._to_norm(self._origin), self._to_norm(e.pos())
        self._origin = self._cur = None
        self.update()
        if a and b:
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            if (x1 - x0) > 0.03 and (y1 - y0) > 0.03:
                self.box_selected.emit((x0, y0, x1, y1))

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._rubber and self._origin and self._cur:
            p = QPainter(self)
            p.setPen(QPen(QColor(T.PRIMARY), 2, Qt.DashLine))
            p.setBrush(QColor(26, 58, 92, 36))
            p.drawRect(self._origin.x(), self._origin.y(),
                       self._cur.x() - self._origin.x(),
                       self._cur.y() - self._origin.y())
            p.end()


class LegendBar(QWidget):
    """颜色图例：把状态含义直接摆在表格上方，不用记。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(T.SP_M)
        for text, color_hex in LEGEND:
            item = QWidget()
            ih = QHBoxLayout(item)
            ih.setContentsMargins(0, 0, 0, 0)
            ih.setSpacing(6)
            sw = QLabel()
            sw.setFixedSize(10, 10)
            sw.setStyleSheet(
                f"background:#{color_hex};border:1px solid {T.BORDER_STRONG};border-radius:2px")
            ih.addWidget(sw)
            lb = QLabel(text)
            lb.setFont(ui_font(T.FS_TINY))
            lb.setStyleSheet(f"color:{T.TEXT_SUB}")
            ih.addWidget(lb)
            h.addWidget(item)
        h.addStretch(1)


class ReviewPage(Page):
    changed = pyqtSignal()
    reprocess_requested = pyqtSignal(object, tuple)

    def __init__(self, parent=None):
        super().__init__("reviewPage", parent)
        self.results: list[PageResult] = []
        self.current: PageResult | None = None
        self._editing = False
        self._build()

    # ------------------------------------------------------------ UI
    def _build(self):
        lay = self.vlayout
        lay.addWidget(title("校对", "核对识别结果；红色必须人工确认，双击任意单元格可改。"))

        head = QHBoxLayout()
        head.setSpacing(T.SP_M)
        head.addWidget(field_label("筛选"))
        self.cb_filter = ComboBox()
        self.cb_filter.addItem("全部页", userData="all")
        self.cb_filter.addItem("只看需复核", userData="review")
        self.cb_filter.addItem("只看冲突（红）", userData="conflict")
        self.cb_filter.addItem("只看失败页", userData="failed")
        self.cb_filter.setMinimumWidth(150)
        head.addWidget(self.cb_filter)

        head.addSpacing(T.SP_M)
        head.addWidget(field_label("切换页"))
        self.cb_page = ComboBox()
        self.cb_page.setMinimumWidth(320)
        head.addWidget(self.cb_page, 1)

        self.btn_prev = PushButton("上一处")
        self.btn_next = PushButton("下一处")
        for b in (self.btn_prev, self.btn_next):
            b.setFixedHeight(30)
            head.addWidget(b)

        lay.addLayout(head)

        row2 = QHBoxLayout()
        row2.setSpacing(T.SP_M)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setFont(ui_font(T.FS_SMALL))
        self.lbl_summary.setStyleSheet(f"color:{T.TEXT_SUB}")
        row2.addWidget(self.lbl_summary)
        row2.addSpacing(T.SP_L)
        self.lbl_stat = QLabel("")
        self.lbl_stat.setFont(mono_font(T.FS_SMALL))
        self.lbl_stat.setStyleSheet(f"color:{T.TEXT_SUB}")
        row2.addWidget(self.lbl_stat)
        row2.addStretch(1)
        self.btn_done = PushButton("本页无问题，下一处")
        self.btn_done.setFixedHeight(30)
        row2.addWidget(self.btn_done)
        lay.addLayout(row2)

        lay.addWidget(LegendBar())

        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(T.SP_XS)
        self.lbl_img = ImageView()
        lv.addWidget(self.lbl_img, 1)
        self.lbl_clip = hint("在图上拖框，可只识别框内区域")
        lv.addWidget(self.lbl_clip)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(T.SP_S)
        self.table = TableWidget()
        self.table.verticalHeader().hide()
        self.table.setStyleSheet(TABLE_QSS)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        rv.addWidget(self.table, 1)

        note_card = Card(muted=True)
        note_card.v.addWidget(field_label("本页说明"))
        self.txt_notes = TextEdit()
        self.txt_notes.setReadOnly(True)
        self.txt_notes.setFixedHeight(88)
        self.txt_notes.setStyleSheet(
            f"background:transparent;border:none;color:{T.TEXT_SUB};font-size:{T.FS_SMALL}px")
        note_card.v.addWidget(self.txt_notes)
        rv.addWidget(note_card)
        split.addWidget(right)

        split.setSizes([460, 640])
        lay.addWidget(split, 1)

        self.cb_page.currentIndexChanged.connect(self._on_switch)
        self.cb_filter.currentIndexChanged.connect(self._on_filter)
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))
        self.btn_done.clicked.connect(self._on_done_page)
        self.table.itemChanged.connect(self._on_cell_edited)
        self.lbl_img.box_selected.connect(self._on_box)

    # ------------------------------------------------------------ 数据
    def _visible_results(self) -> list[PageResult]:
        """按当前筛选口径返回要显示的页。

        1 万页时人不可能逐页翻，所以默认口径是「只看需复核」：
        只把有红/蓝格、或整页失败的页排进列表，其余页不出现在下拉框里。
        """
        mode = self.cb_filter.currentData() if hasattr(self, "cb_filter") else "all"
        if mode == "review":
            return [r for r in self.results if r.review_count or r.error]
        if mode == "conflict":
            return [r for r in self.results if _has_conflict(r)]
        if mode == "failed":
            return [r for r in self.results if r.error]
        return list(self.results)

    def _rebuild_list(self, keep_current: bool = False):
        cur = self.current if keep_current else None
        vis = self._visible_results()
        self._vis = vis

        self.cb_page.blockSignals(True)
        self.cb_page.clear()
        for r in vis:
            tag = r.source or "失败"
            mark = ""
            if r.error:
                mark = " ⚠"
            elif r.review_count:
                mark = f" · 需复核 {r.review_count}"
            self.cb_page.addItem(
                f"{Path(r.path).name} · 第 {r.page_no} 页 · {tag}{mark}",
                userData=id(r))
        self.cb_page.blockSignals(False)

        total = len(self.results)
        need = sum(1 for r in self.results if r.review_count or r.error)
        self.lbl_summary.setText(
            f"共 {total} 页　需复核 {need} 页　当前显示 {len(vis)} 页"
            + ("" if vis else "（没有符合条件的页）"))

        if not vis:
            self.current = None
            self.table.clear()
            self.table.setRowCount(0)
            self.lbl_img.clear()
            self.lbl_img.setText("这个筛选下没有页")
            self.txt_notes.setPlainText("")
            self.lbl_stat.setText("")
            self.btn_done.setEnabled(False)
            return

        idx = 0
        if cur is not None:
            for i, r in enumerate(vis):
                if r is cur:
                    idx = i
                    break
        self.cb_page.blockSignals(True)
        self.cb_page.setCurrentIndex(idx)
        self.cb_page.blockSignals(False)
        self._show(vis[idx])

    def set_results(self, results: list[PageResult]):
        self.results = results
        self._rebuild_list(keep_current=True)

    def _on_switch(self, idx: int):
        vis = getattr(self, "_vis", [])
        if 0 <= idx < len(vis):
            self._show(vis[idx])

    def _step(self, delta: int):
        """上一处 / 下一处：在筛选后的列表里移动。"""
        vis = getattr(self, "_vis", [])
        if len(vis) <= 1:
            return
        idx = self.cb_page.currentIndex() + delta
        idx = max(0, min(len(vis) - 1, idx))
        self.cb_page.setCurrentIndex(idx)

    def _on_filter(self):
        self._rebuild_list(keep_current=True)

    def _on_done_page(self):
        """本页确认无误 → 自动跳到下一处（看完最后一页给提示）。"""
        vis = getattr(self, "_vis", [])
        idx = self.cb_page.currentIndex()
        if idx + 1 < len(vis):
            self.cb_page.setCurrentIndex(idx + 1)
        else:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.success(
                "这一批已看完", f"共处理 {len(vis)} 页，可以到「导出」页生成 Excel。",
                duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def _show(self, res: PageResult):
        self.current = res
        self._render_image(res)
        self._render_table(res)
        self._render_notes(res)
        self._refresh_page_state()
        self._update_done_hint(res)

    def _update_done_hint(self, res: PageResult):
        """按钮为什么不能点，要说清楚 —— 灰着不解释等于坏了。"""
        if res.error:
            self.btn_done.setToolTip("本页处理失败，不能当作「无问题」跳过")
        elif res.review_count:
            self.btn_done.setToolTip(
                f"本页还有 {res.review_count} 格要人工确认（红/蓝），"
                "改完或确认完才会变成可点")
        else:
            self.btn_done.setToolTip("本页无需复核，点了直接跳到下一处")

    def _render_image(self, res: PageResult):
        if not res.png:
            self.lbl_img.setText("（无页面图）")
            return
        self.lbl_img.set_image(res.png, self.lbl_img.width())

    def _render_table(self, res: PageResult):
        self._editing = True
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setRowCount(len(res.cells))
        ncol = max(1, res.n_cols)
        self.table.setColumnCount(ncol)
        self.table.setHorizontalHeaderLabels([f"列{i + 1}" for i in range(ncol)])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setDefaultSectionSize(130)

        for ri, row in enumerate(res.cells):
            for ci, cell in enumerate(row):
                if cell.kind == "empty":
                    text = ""
                elif cell.value is None:
                    text = cell.text
                else:
                    text = f"{cell.value:,.2f}"
                it = QTableWidgetItem(text)
                it.setData(Qt.UserRole, (ri, ci))
                if cell.kind == "number":
                    it.setFont(mono_font(T.FS_BODY))
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    it.setFont(ui_font(T.FS_BODY))
                    it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                if cell.color:
                    it.setBackground(QColor("#" + cell.color))
                tip = cell.note or (cell.verdict.value if cell.verdict else "")
                if tip:
                    it.setToolTip(tip)
                self.table.setItem(ri, ci, it)

        self.table.blockSignals(False)
        self._editing = False
        self._focus_first_problem(res)

    def _focus_first_problem(self, res: PageResult):
        """自动把光标停在第一个「要人看」的格子上并滚进视野。

        1 万页时不可能从头翻，进来就得知道该看哪儿。
        优先级：红（冲突）> 蓝（采信模型）> 橙（精度修正）。
        """
        order = (Verdict.CONFLICT, Verdict.MODEL, Verdict.PRECISION)
        for want in order:
            for ri, row in enumerate(res.cells):
                for ci, cell in enumerate(row):
                    if cell.verdict is want:
                        item = self.table.item(ri, ci)
                        if item is not None:
                            self.table.scrollToItem(
                                item, QAbstractItemView.PositionAtCenter)
                            self.table.clearSelection()
                            self.table.setCurrentItem(
                                item, QItemSelectionModel.ClearAndSelect)
                            return

    def _render_notes(self, res: PageResult):
        lines = []
        if res.error:
            lines.append(f"错误：{res.error}")
        lines.extend(res.notes)
        if res.report.cells:
            c = res.report.counts
            lines.append("对账 " + "　".join(f"{k} {v}" for k, v in c.items() if v))
        self.txt_notes.setPlainText("\n".join(lines) or "（无）")

    # ------------------------------------------------------------ 编辑
    def _on_cell_edited(self, item: QTableWidgetItem):
        if self._editing or self.current is None:
            return
        pos = item.data(Qt.UserRole)
        if not pos:
            return
        ri, ci = pos
        try:
            cell = self.current.cells[ri][ci]
        except IndexError:
            return

        raw = item.text().strip()
        from core.numbers import normalize
        parsed = normalize(raw)

        if parsed.value is not None:
            cell.value = parsed.value
            cell.text = raw
            cell.kind = "number"
            item.setFont(mono_font(T.FS_BODY))
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        elif raw == "":
            cell.value = None
            cell.text = ""
            cell.kind = "empty"
        else:
            cell.text = raw
            cell.value = None
            cell.kind = "text"
            item.setFont(ui_font(T.FS_BODY))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        cell.verdict = Verdict.MANUAL
        cell.note = "人工修改"
        # 同步进对账报告：否则「需复核数」不会减少，「本页无问题」永远点不了
        self.current.report.mark_manual(ri, ci, cell.value, raw)
        item.setBackground(QColor("#" + COLORS[Verdict.MANUAL]))
        self.changed.emit()
        self._refresh_page_state()

    def _refresh_page_state(self):
        """改完格子后刷新本页统计和「下一处」按钮的可用状态。

        手工改过的格子会从「需复核」里剔除（verdict 变成 manual），
        所以改完最后一个红格，按钮应当立刻变成可点 —— 不能等切页才更新。
        """
        if self.current is None:
            return
        res = self.current
        vis = getattr(self, "_vis", [])
        pos = ""
        if vis:
            idx = self.cb_page.currentIndex()
            pos = f"　第 {idx + 1}/{len(vis)} 处"
        self.lbl_stat.setText(
            f"{res.n_rows} 行 × {res.n_cols} 列　需复核 {res.review_count}　"
            f"花费 {res.cost_cny:.4f} 元{pos}")
        self.btn_done.setEnabled(res.review_count == 0 and not res.error)

    def _on_box(self, clip: tuple):
        if self.current is None:
            return
        self.lbl_clip.setText(
            f"已框选 x={clip[0]:.2f}~{clip[2]:.2f} y={clip[1]:.2f}~{clip[3]:.2f}，重新识别中…")
        self.reprocess_requested.emit(self.current, clip)

    def replace_result(self, old: PageResult, new: PageResult):
        for i, r in enumerate(self.results):
            if r is old:
                self.results[i] = new
                break
        else:
            return
        self.lbl_clip.setText("在图上拖框，可只识别框内区域")
        # 重跑后这一页的可信度变了，列表要按当前筛选重排（可能因此从列表里消失）
        self.current = new
        self._rebuild_list(keep_current=True)
        self.changed.emit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.current is not None:
            self._render_image(self.current)
