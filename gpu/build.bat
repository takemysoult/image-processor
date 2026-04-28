@echo off
REM =====================================================================
REM  Build ImageProcessor_GPU.exe (NVIDIA only)
REM
REM  Requires:
REM    - Python 3.10 / 3.11 / 3.12 / 3.13
REM    - At build time:  onnxruntime-gpu Python package (installed by pip)
REM    - At runtime:     NVIDIA driver and CUDA Toolkit on the target machine
REM =====================================================================

setlocal

echo.
echo === Python version that will be used ===
python --version
if errorlevel 1 (
    echo ERROR: 'python' not found on PATH.
    pause
    exit /b 1
)
where python

echo.
echo === Step 1/4: Creating virtual environment ===
if not exist venv (
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Could not create venv.
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat

echo.
echo === Step 2/4: Installing dependencies (this may take a few minutes) ===
echo NOTE: onnxruntime-gpu is ~400 MB. Be patient on first install.
python -m pip install --upgrade pip

REM Make sure CPU onnxruntime is NOT in this venv — it conflicts with -gpu.
pip uninstall -y onnxruntime >nul 2>&1

pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo.
echo === Step 3/4: Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo === Step 4/4: Building exe with PyInstaller ===
pyinstaller build.spec --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo =====================================================================
echo  DONE.
echo  Your app is in:  dist\ImageProcessor_GPU\
echo  Launch with:     dist\ImageProcessor_GPU\ImageProcessor_GPU.exe
echo.
echo  IMPORTANT: target machine still needs NVIDIA driver + CUDA Toolkit
echo  (11.8 or 12.x). The exe will fall back to CPU automatically if CUDA
echo  is not found, but you'll lose the speed advantage.
echo =====================================================================
echo.

pause
endlocal
