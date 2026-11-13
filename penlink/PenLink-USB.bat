@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -u usb_server.py %*
) else (
  py -u usb_server.py %*
)
pause
