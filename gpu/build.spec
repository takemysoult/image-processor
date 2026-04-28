# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for ImageProcessor GPU build.
# Build with:    pyinstaller build.spec --noconfirm
#
# Notes:
# - GPU build pulls in onnxruntime-gpu, which adds ~400-700 MB of CUDA DLLs
#   (onnxruntime_providers_cuda.dll, etc.) to the bundle. Total ~800-1200 MB.
# - The bundle does NOT include the CUDA Toolkit itself — the user still
#   needs the NVIDIA driver and (for onnxruntime-gpu 1.17+) CUDA 11.8 or 12.x
#   installed on their machine.
# - rembg downloads its ONNX models on first use to %USERPROFILE%\.u2net\.

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

for pkg in [
    "rembg",
    "onnxruntime",      # this resolves to onnxruntime-gpu's installed package
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
        pass

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
    name="ImageProcessor_GPU",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX corrupts onnxruntime DLLs.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ImageProcessor_GPU",
)
