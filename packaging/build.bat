@echo off
REM Build FlatRadar .exe for Windows
REM Requires: Python 3.11+, pip, pyinstaller
setlocal

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."
set "BUILD_DIR=%ROOT_DIR%\build"
set "DIST_DIR=%ROOT_DIR%\dist"

echo === Step 1: Install PyInstaller ===
pip install pyinstaller -q

echo === Step 2: Build binary with PyInstaller ===
python -m PyInstaller --clean --distpath "%DIST_DIR%" --workpath "%BUILD_DIR%" "%SCRIPT_DIR%flatradar.spec"

echo === Step 3: Create ZIP archive ===
if exist "%DIST_DIR%\FlatRadar.zip" del "%DIST_DIR%\FlatRadar.zip"
powershell -command "Compress-Archive -Path '%DIST_DIR%\flatradar.exe' -DestinationPath '%DIST_DIR%\FlatRadar.zip'"

echo.
echo === Done ===
echo EXE: %DIST_DIR%\flatradar.exe
echo ZIP: %DIST_DIR%\FlatRadar.zip

endlocal
