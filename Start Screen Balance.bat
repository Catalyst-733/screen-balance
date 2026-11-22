@echo off
rem Starts Screen Balance in the background (no console window).
cd /d "%~dp0"
start "" pythonw "%~dp0screen_balance.py"
