@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
.venv\Scripts\python backtest.py --start 2021-01-01 %*
pause
