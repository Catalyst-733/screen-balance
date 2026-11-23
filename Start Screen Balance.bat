@echo off
rem Starts Screen Balance in the background (no console window).
rem Runs from the Windows folder, not this one, so a stray "pythonw" file placed
rem next to this script can never be picked up instead of the real Python.
cd /d "%SystemRoot%"
start "" pythonw "%~dp0screen_balance.py"
