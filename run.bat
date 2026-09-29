@echo off
REM ============================================================
REM  TrainTicket launcher
REM
REM  Usage: put the arguments right after it, e.g.
REM    run.bat -f ShanghaiHongQiao -t WuXi -d 2026-10-02 --time 06:00-12:00 -a
REM    (use the Chinese station names in a real console window)
REM
REM  Why this file exists: the dependencies live in .venv, so a bare
REM  "python main.py" may pick up the system Python and fail on requests.
REM  This always uses the venv interpreter.
REM
REM  NOTE: keep this file ASCII-only. cmd.exe reads .bat using the OEM
REM  codepage (GBK on Chinese Windows), so non-ASCII comments corrupt
REM  the batch parsing itself.
REM ============================================================
setlocal

REM Run from this script's own folder, so it works from anywhere
cd /d "%~dp0"

REM Switch the console to UTF-8, otherwise the Chinese table comes out garbled
chcp 65001 >nul 2>&1

set PY=.venv\Scripts\python.exe

if not exist "%PY%" (
    echo [ERROR] Cannot find %PY%
    echo         Run these first, inside the project folder:
    echo             python -m venv .venv
    echo             .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

"%PY%" main.py %*
exit /b %ERRORLEVEL%
