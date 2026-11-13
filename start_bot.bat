@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title Binance Futures Bot
:loop
.venv\Scripts\python trade.py
echo Bot exited (code %errorlevel%). Restarting in 30 seconds... Close this window to stop.
timeout /t 30 /nobreak >nul
goto loop
