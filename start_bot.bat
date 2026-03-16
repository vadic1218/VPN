@echo off
cd /d "%~dp0"
title Telegram Pill Reminder Bot
python bot.py
echo.
echo Bot stopped. Press any key to close this window.
pause >nul
