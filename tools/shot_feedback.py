# -*- coding: utf-8 -*-
"""截图脚本：处理中 / 处理完（含失败）两种状态。

用法：python tools/shot_feedback.py
输出：shot_running.png、shot_done.png
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
from gui.worker import JobItem  # noqa: E402


def pump(app, seconds):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    _setup_font(app)

    # 关掉大模型，截图不花钱；扫描件会走本地 OCR，每页要一两秒，方便抓「处理中」
    from gui import settings_page as m
    m.SettingsPage.to_llm_config = lambda self: None

    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    app.processEvents()

    demo = ROOT / "demo_pdfs"
    files = [demo / f"demo0{i}.pdf" for i in (1, 2, 3, 4)]
    files += [demo / "proft_demo_text.pdf"]
    files = [f for f in files if f.exists()]
    w.import_page.add_paths(files)
    w.import_page.sw_ocr.setChecked(True)      # 强制 OCR，一页一两秒，方便抓「处理中」
    app.processEvents()

    jobs = [JobItem(f, 1, f.stem) for f in files]
    jobs.append(JobItem(files[-1], 99, "越界页"))     # 制造一个必然失败
    w._start_jobs(jobs)

    # 跑到第 2 页时抓「处理中」
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        app.processEvents()
        if w._done >= 2:
            break
        time.sleep(0.02)
    pump(app, 0.15)
    w.grab().save(str(ROOT / "shot_running.png"))
    print("running:", w.progress.value(), "|", w.lbl_stage.text())
    # 等结束
    t0 = time.monotonic()
    while time.monotonic() - t0 < 180:
        app.processEvents()
        if not w._running:
            break
        time.sleep(0.02)
    pump(app, 1.0)
    w.grab().save(str(ROOT / "shot_done.png"))
    print("done:", w.lbl_stage.text())
    print("result:", w.import_page.lbl_result.text())
    print("row0:", w.import_page.table.item(0, 5).text())

    # 校对页：单独跑一张扫描件，保证截图里是有内容的表
    hard = demo / "profit_scan_hard.pdf"
    if hard.exists():
        w.import_page.add_paths([hard])
        w._start_jobs([JobItem(hard, 1, "profit_scan_hard")])
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            app.processEvents()
            if not w._running:
                break
            time.sleep(0.02)
        w.switchTo(w.review_page)
        pump(app, 1.0)
        w.grab().save(str(ROOT / "shot_review.png"))
        print("review:", w.review_page.lbl_stat.text())

    # 搜索中的「取消搜索」态：扫描件走 OCR，一页一两秒，来得及抓
    w.import_page.grp.button(1).setChecked(True)
    w.import_page._on_mode()
    w.import_page.ed_title.setText("管理费用明细表")
    w.import_page.sw_ocr.setChecked(True)
    scanned = [demo / "profit_scan_hard.pdf", demo / "profit_demo_scanned.pdf",
               demo / "profit_handwriting_hard.pdf"]
    w.import_page.add_paths([f for f in scanned if f.exists()])
    app.processEvents()
    w._on_search("管理费用明细表", True)
    pump(app, 0.8)
    w.grab().save(str(ROOT / "shot_search_cancel.png"))
    print("search:", w.lbl_stage.text(), "|", w.import_page.btn_run.text())
    w.import_page.btn_run.click()             # 收尾：取消掉，别等它扫完
    pump(app, 0.5)

    # 0 命中：解释必须留在界面上
    w.import_page.sw_ocr.setChecked(False)
    w.import_page.ed_title.setText("损益表")
    w._on_search("损益表", False)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        app.processEvents()
        if w._search_worker is not None and w._search_worker.isFinished():
            break
        time.sleep(0.02)
    pump(app, 0.8)
    w.grab().save(str(ROOT / "shot_search_nohit.png"))
    print("nohit:", w.import_page.lbl_result.text())
    w.close()


if __name__ == "__main__":
    main()
