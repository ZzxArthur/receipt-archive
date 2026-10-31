@echo off
setlocal
cd /d "%~dp0"
"%~dp0runtime\pythonw.exe" "%~dp0windows\launcher.py" --browser
