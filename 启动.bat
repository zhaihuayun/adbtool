@echo off
cd /d "%~dp0"
set "PYW=%LocalAppData%\Python\bin\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0adb_gui.py"
    exit /b 0
)
where py >nul 2>&1
if %errorlevel%==0 (
    start "" py -3 "%~dp0adb_gui.py"
    exit /b 0
)
echo Python was not found.
pause
