@echo off
rem Builds a standalone dist\FlyRoll\FlyRoll.exe (no Python needed on the target PC).
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :nopython
)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt SimConnect pyinstaller pytest || goto :fail
.venv\Scripts\python -m pytest tests -q || goto :fail
.venv\Scripts\pyinstaller --noconfirm FlyRoll.spec || goto :fail
echo.
echo Done:  dist\FlyRoll\FlyRoll.exe
echo Tip:   copy SimConnect.dll from "MSFS 2024 SDK\SimConnect SDK\lib" into dist\FlyRoll to use the 2024 client.
pause
goto :eof
:nopython
echo Python 3.10+ is required: https://www.python.org/downloads/
:fail
echo Build failed.
pause
exit /b 1
