@echo off
setlocal
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0windows\launcher.py"
if errorlevel 1 pause
