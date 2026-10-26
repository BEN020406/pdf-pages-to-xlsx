# -*- coding: utf-8 -*-
"""gui.components —— 复用的界面积木。

刻意做薄：只是把「卡片 + 标题 + 说明 + 内容」的组合固化，
保证每个页面视觉节奏一致，不在各页面各写一套间距。
"""
from __future__ import annotations

from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from gui.theme import T, color, mono_font, ui_font


class Page(QWidget):
    """页面容器：统一背景与内边距。

    scroll=True 时内容放进 SmoothScrollArea，长页面不会被窗口底部截断。

    ⚠️ 不要重写 `layout()` 来返回内部布局 —— 实测那会让 Qt 认为页面
    本身没有布局，页面尺寸塌成 100×30，滚动区也失去作用
    （设置页底部按钮被截断）。用 `vlayout` 属性暴露内部布局。
    """

    def __init__(self, object_name: str, parent=None, *, scroll: bool = False):
        super().__init__(parent)
        self.setObjectName(object_name)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.body = QWidget(self)
        self.body.setObjectName("pageBody")
        self.vlayout = QVBoxLayout(self.body)
        self.vlayout.setContentsMargins(T.SP_XXL, T.SP_XL, T.SP_XXL, T.SP_XL)
        self.vlayout.setSpacing(T.SP_L)

        if scroll:
            from qfluentwidgets import SmoothScrollArea
            sa = SmoothScrollArea(self)
            sa.setWidgetResizable(True)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sa.setStyleSheet("QScrollArea{background:transparent;border:none}")
            # widgetResizable=True 会把内容拉伸到视口高度，内容超出时
            # 溢出部分被裁掉、滚动条不出现（实测「保存」按钮被切掉）。
            # 解法：把 body 的最小高度锁到它的 sizeHint 高度，超出即出现滚动条。
            self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            sa.setWidget(self.body)
            outer.addWidget(sa)
            self.scroll_area = sa
            self.body.installEventFilter(self)
            QTimer.singleShot(0, self._sync_body_height)
        else:
            outer.addWidget(self.body)
            self.scroll_area = None

    def _sync_body_height(self):
        """把 body 最小高度设为内容需要的高度，让滚动区真正滚动。"""
        if self.scroll_area is None:
            return
        h = self.vlayout.sizeHint().height()
        if h > 0:
            self.body.setMinimumHeight(h)

    def eventFilter(self, obj, ev):
        if obj is self.body and ev.type() == ev.LayoutRequest:
            self._sync_body_height()
        return super().eventFilter(obj, ev)


def title(text: str, lead: str = "") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(T.SP_XS)
    lb = QLabel(text)
    lb.setObjectName("pageTitle")
    lb.setFont(ui_font(T.FS_TITLE, bold=True))
    v.addWidget(lb)
    if lead:
        lp = QLabel(lead)
        lp.setObjectName("pageLead")
        lp.setFont(ui_font(T.FS_BODY))
        lp.setWordWrap(True)
        v.addWidget(lp)
    return w


def section_title(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("sectionTitle")
    lb.setFont(ui_font(T.FS_SECTION, bold=True))
    return lb


def field_label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("fieldLabel")
    lb.setFont(ui_font(T.FS_SMALL))
    return lb


def hint(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("hint")
    lb.setFont(ui_font(T.FS_TINY))
    lb.setWordWrap(True)
    return lb


class Card(QWidget):
    """白底圆角卡片，自带内边距与纵向布局。

    背景用 paintEvent 直接绘制，不依赖 QSS ——
    实测：FluentWindow 内部主题会覆盖 `QWidget#card { background-color }`，
    palette 始终是 #efefef，卡片显示为灰。直接画最可靠。
    """

    def __init__(self, parent=None, *, muted: bool = False, warn: bool = False):
        super().__init__(parent)
        self._fill = T.WARN_BG if warn else (T.SURFACE_ALT if muted else T.SURFACE)
        self._border = "#F0D9B5" if warn else T.BORDER
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(T.SP_L, T.SP_M, T.SP_L, T.SP_M)
        self.v.setSpacing(T.SP_S)

    def paintEvent(self, e):
        from PyQt5.QtGui import QPainter, QPainterPath, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        r = self.rect().adjusted(0, 0, -1, -1)
        path.addRoundedRect(float(r.x()), float(r.y()),
                            float(r.width()), float(r.height()),
                            T.R_M, T.R_M)
        p.fillPath(path, color(self._fill))
        p.setPen(QPen(color(self._border), 1))
        p.drawPath(path)
        p.end()


def divider() -> QFrame:
    f = QFrame()
    f.setObjectName("divider")
    f.setFrameShape(QFrame.HLine)
    f.setFixedHeight(1)
    f.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return f


class StatChip(QWidget):
    """顶栏状态块：一个数字 + 一行说明。"""

    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(T.SP_M, T.SP_XS, T.SP_M, T.SP_XS)
        h.setSpacing(T.SP_S)

        self.lbl = QLabel(label)
        self.lbl.setObjectName("hint")
        self.lbl.setFont(ui_font(T.FS_TINY))
        h.addWidget(self.lbl)

        self.val = QLabel(value)
        self.val.setFont(mono_font(T.FS_BODY, bold=True))
        self.val.setStyleSheet(f"color:{T.TEXT}")
        h.addWidget(self.val)

    def set_value(self, text: str, tone: str = T.TEXT):
        self.val.setText(text)
        self.val.setStyleSheet(f"color:{tone}")
