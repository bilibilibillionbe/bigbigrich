@echo off
cd /d "%~dp0"
echo [1/3] Creating Python virtual environment...
python -m venv .venv
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
echo [2/3] Installing packages...
.venv\Scripts\python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo [3/3] Preparing config.json...
if not exist config.json copy config.example.json config.json
echo.
echo Done. Edit config.json (API key), then run backtest.bat or start_bot.bat
pause
