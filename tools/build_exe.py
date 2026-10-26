# -*- coding: utf-8 -*-
"""打包成可分发的 Windows 程序（PyInstaller，目录模式）。

用法：python tools/build_exe.py
产物：dist/PDF取页转Excel/  →  压缩成 dist/PDF取页转Excel.zip

为什么用目录模式（onedir）而不是单文件（onefile）：
    实测单文件模式在本机报
        [PYI-xxxxx:ERROR] Could not create temporary directory!
    启动即失败 —— 单文件要先把自己解压到临时目录，这一步在本机环境不可用。
    目录模式实测可正常运行（窗口标题、GUI、OCR 全部正常），所以改用目录模式，
    分发时压成 zip。

为什么体积能压到 ~200MB：
    PyInstaller 会把「被 import 到但用不上」的大包一起收进去，实测收了
    torch(293MB) / llvmlite(101MB) / pyarrow(72MB) / scipy(19MB)，
    这些都是 pandas、img2table 的可选依赖牵连进来的，本项目不用，显式排除。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "PDF取页转Excel"

# 这些包本工具不用，但会被可选依赖牵连进来（体积很大）
EXCLUDE = [
    "torch", "torchvision", "torchaudio",
    "llvmlite", "numba",
    "pyarrow",
    "scipy", "sklearn", "matplotlib",
    "pandas",           # img2table 只用它的 DataFrame，我们不用；保留会拖 pyarrow
    "paddle", "paddleocr", "paddlex",
    "tensorboard", "tensorflow",
    "tkinter",
    "IPython", "jupyter", "notebook",
    "pytest", "sphinx",
]


def build() -> None:
    icon = ROOT / "assets" / "app.ico"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir",
        "--windowed",
        "--name", APP_NAME,
        "--collect-data", "rapidocr",          # 3 个 ONNX 模型必须内嵌
        "--collect-submodules", "img2table",
        "--collect-submodules", "qfluentwidgets",
        "--hidden-import", "zhconv",
        "--hidden-import", "core",
        "--hidden-import", "gui",
    ]
    for mod in EXCLUDE:
        cmd += ["--exclude-module", mod]
    if icon.exists():
        cmd[-1:-1] = ["--icon", str(icon)]
    cmd.append(str(ROOT / "main.py"))

    print("开始打包（约 3-6 分钟）…\n")
    subprocess.run(cmd, cwd=str(ROOT), check=True)

    out_dir = ROOT / "dist" / APP_NAME
    exe = out_dir / f"{APP_NAME}.exe"
    if not exe.exists():
        print("打包结束但没找到 exe，检查 PyInstaller 输出。")
        return

    total = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
    print(f"\n目录：{out_dir}  ({total / 1048576:.0f} MB)")

    zip_base = ROOT / "dist" / APP_NAME
    print("压缩中…")
    shutil.make_archive(str(zip_base), "zip", root_dir=str(out_dir.parent),
                        base_dir=APP_NAME)
    z = zip_base.with_suffix(".zip")
    print(f"完成：{z}  ({z.stat().st_size / 1048576:.0f} MB)")
    print("解压后双击 exe 即可运行，不需要装 Python。")


if __name__ == "__main__":
    build()
