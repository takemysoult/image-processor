@echo off
REM =====================================================================
REM  Build ImageProcessor.exe
REM  Uses whatever 'python' is on PATH. Python 3.10 / 3.11 / 3.12 / 3.13
REM  all work — every dependency has wheels for them.
REM =====================================================================

setlocal

echo.
echo === Python version that will be used ===
python --version
if errorlevel 1 (
    echo ERROR: 'python' not found on PATH.
    echo Install Python from https://www.python.org/downloads/ and tick
    echo "Add Python to PATH" during install.
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
python -m pip install --upgrade pip
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
echo  Your app is in:  dist\ImageProcessor\
echo  Launch it with:  dist\ImageProcessor\ImageProcessor.exe
echo.
echo  IMPORTANT: ship the WHOLE dist\ImageProcessor folder, not just the
echo  .exe — it needs the DLLs and data files next to it.
echo =====================================================================
echo.

pause
endlocal
