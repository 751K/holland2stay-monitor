@echo off
REM Build Holland2Stay Monitor .exe for Windows
REM Requires: Python 3.11+, pip, pyinstaller
setlocal

set "PROJECT_DIR=%~dp0"
set "BUILD_DIR=%PROJECT_DIR%build"
set "DIST_DIR=%PROJECT_DIR%dist"

echo === Step 1: Install PyInstaller ===
pip install pyinstaller -q

echo === Step 2: Build binary with PyInstaller ===
python -m PyInstaller --clean --distpath "%DIST_DIR%" --workpath "%BUILD_DIR%" "%PROJECT_DIR%h2s_monitor.spec"

echo === Step 3: Create ZIP archive ===
if exist "%DIST_DIR%\Holland2Stay Monitor.zip" del "%DIST_DIR%\Holland2Stay Monitor.zip"
powershell -command "Compress-Archive -Path '%DIST_DIR%\h2s-monitor.exe' -DestinationPath '%DIST_DIR%\Holland2Stay Monitor.zip'"

echo.
echo === Done ===
echo EXE: %DIST_DIR%\h2s-monitor.exe
echo ZIP: %DIST_DIR%\Holland2Stay Monitor.zip

endlocal
