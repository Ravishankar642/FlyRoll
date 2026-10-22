@echo off
rem Run FlyRoll from source (creates a local virtual environment on first start).
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv || goto :nopython
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt SimConnect
)
.venv\Scripts\python -m flyroll %*
goto :eof
:nopython
echo Python 3.10+ is required: https://www.python.org/downloads/ (tick "Add python.exe to PATH")
pause
