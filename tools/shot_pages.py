# -*- coding: utf-8 -*-
"""截取四个主界面，供说明书使用（offscreen，不弹真窗口）。

用法：python tools/shot_pages.py
输出：shot_import.png / shot_review.png / shot_export.png / shot_settings.png
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from gui.app import MainWindow, _setup_font  # noqa: E402


def pump(app, seconds: float) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)


def main() -> None:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    _setup_font(app)

    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    app.processEvents()

    # 导入页：放几个样例文件，让清单看起来是真实使用状态
    demo = ROOT / "demo_pdfs"
    files = [demo / f"demo0{i}.pdf" for i in (1, 2, 3)]
    files = [f for f in files if f.exists()]
    if files:
        w.import_page.add_paths(files)
        pump(app, 0.4)

    for name, page in (("import", w.import_page), ("review", w.review_page),
                       ("export", w.export_page), ("settings", w.settings_page)):
        w.switchTo(page)
        pump(app, 0.5)
        w.grab().save(str(ROOT / f"shot_{name}.png"))
        print(f"shot_{name}.png <- {page.objectName()}")

    w.close()


if __name__ == "__main__":
    main()
