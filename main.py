# -*- coding: utf-8 -*-
"""启动入口。

直接双击 run_gui.cmd，或在项目根目录执行：
    python main.py
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fix_console_encoding():
    """让中文提示在 GBK 控制台里也能正确显示。

    批处理文件里不能写 chcp 65001 —— cmd 会在执行到它之前就按 GBK
    解析整个文件，中文被截断后会被当成命令执行（实测报
    「'把上面的错误信息发给开发者' 不是内部或外部命令」）。
    所以编码切换放在 Python 里做。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and getattr(stream, "encoding", "").lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _check_deps() -> str:
    """返回缺失依赖的可读提示；齐全返回空串。"""
    missing = []
    for mod, pkg in (
        ("PyQt5", "PyQt5"),
        ("qfluentwidgets", "PyQt-Fluent-Widgets"),
        ("qframelesswindow", "PyQt5-Frameless-Window"),
        ("fitz", "PyMuPDF"),
        ("openpyxl", "openpyxl"),
        ("rapidocr", "rapidocr"),
        ("openai", "openai"),
    ):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(pkg)
    if missing:
        return ("缺少依赖：" + "、".join(missing)
                + "\n\n请先安装：\n  python -m pip install "
                + " ".join(missing))
    return ""


def main() -> int:
    _fix_console_encoding()

    err = _check_deps()
    if err:
        print(err)
        return 1

    try:
        from gui.app import main as gui_main
        gui_main()
        return 0
    except Exception:  # noqa: BLE001
        print("启动失败，完整堆栈：\n")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
