# -*- coding: utf-8 -*-
"""全局测试配置：整个会话只创建一次 QApplication。

坑（实测）：两个测试模块各写一个 module 级 `app` fixture 时，
先跑的模块拆掉 fixture 会把共享的 QApplication 释放掉，
后跑的模块再建窗口就报
`RuntimeError: wrapped C/C++ object of type QConfig has been deleted`
（qconfig 单例挂在 QApplication 上，随它一起销毁）。
所以这里放一个 session 级 fixture，生命周期覆盖整个会话。
"""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")


@pytest.fixture(scope="session")
def app():
    pytest.importorskip("PyQt5")
    pytest.importorskip("qfluentwidgets")

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    a = QApplication.instance() or QApplication(sys.argv)
    from gui.app import _setup_font
    _setup_font(a)
    yield a
