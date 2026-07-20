@echo off
cd /d "C:\CLAUDE TRADING BOT"

REM --- Single-instance guard -------------------------------------------------
REM Only one watchdog needed. Fail-open: if the check can't run, start anyway
REM so the watchdog is never silently kept down.
set "RUNNING=0"
for /f %%c in ('wmic process where "name='python.exe'" get commandline 2^>nul ^| find /c /i "watchdog.py"') do set "RUNNING=%%c"
if not "%RUNNING%"=="0" (
    echo [%date% %time%] A watchdog instance is already running ^(count=%RUNNING%^). Not starting a duplicate.
    exit /b 0
)

:loop
echo [%date% %time%] Starting bot watchdog...
"C:\Users\kyawz\AppData\Local\Programs\Python\Python313\python.exe" -X utf8 "C:\CLAUDE TRADING BOT\watchdog.py"

echo [%date% %time%] Watchdog exited (code %errorlevel%). Restarting in 60 seconds...
timeout /t 60 /nobreak >nul
goto loop
