@echo off
title DEADLINE Search Platform
color 0A

echo ============================================================
echo   DEADLINE Search Platform - Launcher
echo ============================================================

:start_server
echo [*] Starting DEADLINE server...
cd /d "%~dp0"
python server.py
pause
