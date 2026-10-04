# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all

# Resolve Windows dependencies from Python, package hooks and the OS itself.
# Unrelated programs on PATH (for example Poppler) may ship a different
# icuuc.dll with versioned exports, which is incompatible with Qt's system ICU.
if sys.platform == "win32":
    windows_root = Path(os.environ["SystemRoot"])
    os.environ["PATH"] = os.pathsep.join(
        str(path)
        for path in (
            Path(sys.executable).parent,
            Path(sys.base_prefix),
            Path(sys.base_prefix) / "DLLs",
            windows_root / "System32",
            windows_root,
        )
    )

rapid_datas, rapid_binaries, rapid_hiddenimports = collect_all("rapidocr")
onnx_datas, onnx_binaries, onnx_hiddenimports = collect_all("onnxruntime")
pdfium_datas, pdfium_binaries, pdfium_hiddenimports = collect_all("pypdfium2")

a = Analysis(
    ["run_app.py"],
    pathex=["src"],
    binaries=rapid_binaries + onnx_binaries + pdfium_binaries,
    datas=rapid_datas + onnx_datas + pdfium_datas,
    hiddenimports=rapid_hiddenimports + onnx_hiddenimports + pdfium_hiddenimports,
    excludes=["tkinter", "matplotlib"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="本地发票识别工具",
    console=False,
    upx=False,
    exclude_binaries=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="本地发票识别工具",
)
