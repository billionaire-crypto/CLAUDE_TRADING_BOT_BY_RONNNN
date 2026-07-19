@echo off
cd /d "C:\CLAUDE TRADING BOT"

REM --- Single-instance guard -------------------------------------------------
set "RUNNING=0"
for /f %%c in ('wmic process where "name='python.exe'" get commandline 2^>nul ^| find /c /i "shadow_engine.py --daemon"') do set "RUNNING=%%c"
if not "%RUNNING%"=="0" (
    echo [%date% %time%] Shadow Engine already running ^(count=%RUNNING%^). Not starting a duplicate.
    exit /b 0
)

:loop
echo [%date% %time%] Starting Shadow Engine daemon...
"C:\Users\kyawz\AppData\Local\Programs\Python\Python313\python.exe" -X utf8 "C:\CLAUDE TRADING BOT\shadow\shadow_engine.py" --daemon

echo [%date% %time%] Shadow Engine exited (code %errorlevel%). Restarting in 120 seconds...
timeout /t 120 /nobreak >nul
goto loop
