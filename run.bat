@echo off
cd /d "%~dp0"
echo Starting Drop server...
".venv\Scripts\python.exe" drop.py
pause
