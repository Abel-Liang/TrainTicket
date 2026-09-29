@echo off
REM ============================================================
REM  TrainTicket GUI launcher
REM
REM  Opens the Tkinter window. No arguments needed.
REM
REM  NOTE: keep this file ASCII-only. cmd.exe reads .bat using the
REM  OEM codepage (GBK on Chinese Windows), so non-ASCII comments
REM  corrupt the batch parsing itself.
REM ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1

set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
    echo [ERROR] Cannot find %PY%
    echo         Run these first, inside the project folder:
    echo             python -m venv .venv
    echo             .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

REM pythonw.exe runs without a console window. Fall back to python.exe if absent.
set PYW=.venv\Scripts\pythonw.exe
if exist "%PYW%" (
    start "" "%PYW%" ui.py
) else (
    "%PY%" ui.py
)
exit /b %ERRORLEVEL%
