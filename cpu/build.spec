# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for ImageProcessor.
# Build with:    pyinstaller build.spec --noconfirm
#
# Notes:
# - We use --onedir (default) instead of --onefile because rembg + onnxruntime
#   produces a ~400 MB bundle that takes 10-30 seconds to unpack on every launch
#   in --onefile mode. --onedir starts in <1s.
# - rembg downloads its ONNX models on first use to %USERPROFILE%\.u2net\.
#   They are NOT bundled inside the exe. First run needs internet.

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# Packages that ship data files / native binaries that PyInstaller often misses.
for pkg in [
    "rembg",
    "onnxruntime",
    "tkinterdnd2",
    "pymatting",
    "pymatting_aot",
    "pooch",
    "numba",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        # Some packages (numba, pymatting_aot) are optional; ignore if absent.
        pass

# Pillow + Tk integration sometimes needs this hidden import explicitly.
hiddenimports += [
    "PIL._tkinter_finder",
]


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ImageProcessor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX can corrupt onnxruntime DLLs — keep it off.
    console=False,        # Hide the black console window (GUI app).
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',    # Uncomment if you drop an icon.ico next to this file.
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ImageProcessor",
)
