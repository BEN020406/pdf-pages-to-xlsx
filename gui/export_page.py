# -*- coding: utf-8 -*-
"""gui.export_page —— 导出设置与执行。"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    InfoBar, InfoBarPosition, LineEdit, PrimaryPushButton, ProgressBar, PushButton,
    SwitchButton,
)

from core.pipeline import estimate_cost
from core.writer import ExportOptions
from gui.components import Card, Page, field_label, hint, section_title, title
from gui.theme import T
from gui.worker import ExportWorker


class ExportPage(Page):
    def __init__(self, parent=None):
        super().__init__("exportPage", parent, scroll=True)
        self.results = []
        self._worker: ExportWorker | None = None
        self._build()

    def _build(self):
        lay = self.vlayout
        lay.addWidget(title(
            "导出",
            "数字写成真数值，可直接求和；解析不出的单元格留空，不会写成 0。"))

        card = Card()
        card.v.addWidget(section_title("保存到"))
        row = QHBoxLayout()
        row.setSpacing(T.SP_S)
        self.ed_out = LineEdit()
        self.ed_out.setPlaceholderText("选择保存位置…")
        row.addWidget(self.ed_out, 1)
        self.btn_browse = PushButton("浏览…")
        self.btn_browse.setFixedHeight(32)
        row.addWidget(self.btn_browse)
        card.v.addLayout(row)
        lay.addWidget(card)

        card2 = Card()
        card2.v.addWidget(section_title("工作表内容"))
        self.sw_table = self._toggle(card2, "表格数据", True,
                                     "数字写成真数值，带状态着色")
        self.sw_image = self._toggle(card2, "原始页面截图", True,
                                     "放在表格下方，便于对照原件")
        self.sw_color = self._toggle(card2, "按状态着色", True,
                                     "绿=一致　黄=采信本地　蓝=采信模型　橙=精度修正　红=需人工")
        self.sw_audit = self._toggle(card2, "OCR 校验页", True,
                                     "逐格记录原文、规则、来源，可追溯")
        self.sw_summary = self._toggle(card2, "汇总页", True,
                                       "每页行数、需复核数、花费合计")
        lay.addWidget(card2)

        self.lbl_cost = field_label("")
        lay.addWidget(self.lbl_cost)

        self.progress = ProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        row2 = QHBoxLayout()
        row2.addStretch(1)
        self.btn_export = PrimaryPushButton("开始导出")
        self.btn_export.setFixedHeight(36)
        self.btn_export.setMinimumWidth(140)
        row2.addWidget(self.btn_export)
        lay.addLayout(row2)
        lay.addStretch(1)

        self.btn_browse.clicked.connect(self._on_browse)
        self.btn_export.clicked.connect(self._on_export)

    def _toggle(self, card: Card, label: str, default: bool, desc: str) -> SwitchButton:
        row = QHBoxLayout()
        row.setSpacing(T.SP_M)
        col = QVBoxLayout()
        col.setSpacing(1)
        lb = field_label(label)
        lb.setStyleSheet(f"color:{T.TEXT};font-size:{T.FS_BODY}px")
        col.addWidget(lb)
        col.addWidget(hint(desc))
        row.addLayout(col, 1)
        sw = SwitchButton()
        sw.setChecked(default)
        sw.setOnText("开")
        sw.setOffText("关")
        row.addWidget(sw, 0, Qt.AlignVCenter)
        card.v.addLayout(row)
        return sw

    # ------------------------------------------------------------ 数据
    def set_results(self, results):
        self.results = results
        n = len(results)
        est = estimate_cost(n)
        self.lbl_cost.setText(
            f"共 {n} 个工作表 ｜ 预计大模型花费约 {est:.4f} 元"
            f"（有文本层的页为 0，实际以调用记录为准）")
        if results and not self.ed_out.text().strip():
            first = Path(results[0].path)
            self.ed_out.setText(str(first.with_name(f"{first.stem}_导出.xlsx")))

    def _opts(self) -> ExportOptions:
        return ExportOptions(
            include_image=self.sw_image.isChecked(),
            include_table=self.sw_table.isChecked(),
            colorize=self.sw_color.isChecked(),
            include_audit_sheet=self.sw_audit.isChecked(),
            include_summary_sheet=self.sw_summary.isChecked(),
        )

    # ------------------------------------------------------------ 交互
    def _on_browse(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "保存为", self.ed_out.text() or "", "Excel (*.xlsx)")
        if p:
            self.ed_out.setText(p)

    def _on_export(self):
        if not self.results:
            InfoBar.warning("没有可导出的内容", "先完成「开始处理」。", duration=3000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())
            return
        out = self.ed_out.text().strip()
        if not out:
            InfoBar.warning("请先选择输出文件", "", duration=3000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())
            return
        self.btn_export.setEnabled(False)
        self.btn_export.setText("导出中…")
        self.progress.setValue(20)
        self._worker = ExportWorker(self.results, Path(out), self._opts())
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_done(self, rep):
        self.btn_export.setEnabled(True)
        self.btn_export.setText("开始导出")
        self.progress.setValue(100)
        InfoBar.success(
            "导出完成",
            f"{len(rep.sheets)} 个工作表 ｜ 需复核 {rep.total_review} ｜ "
            f"花费 {rep.total_cost:.4f} 元 → {rep.out_path}",
            duration=8000, position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def _on_failed(self, msg: str):
        self.btn_export.setEnabled(True)
        self.btn_export.setText("开始导出")
        self.progress.setValue(0)
        InfoBar.error("导出失败", msg, duration=8000,
                      position=InfoBarPosition.TOP_RIGHT, parent=self.window())
