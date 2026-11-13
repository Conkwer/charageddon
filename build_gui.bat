@echo off
setlocal
cd /d "%~dp0"

if not exist "cai_export.py" (
    echo ERROR: cai_export.py must be in the same folder as cai_export_gui.py.
    pause
    exit /b 1
)

py -m pip install -r requirements.txt pyinstaller || goto :fail
py -m PyInstaller --noconfirm --onefile --console --name cai-backup-gui ^
    --hidden-import cai_export cai_export_gui.py || goto :fail

echo.
echo Built: dist\cai-backup-gui.exe
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
