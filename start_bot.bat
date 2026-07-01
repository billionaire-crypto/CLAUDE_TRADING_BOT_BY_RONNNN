@echo off
cd /d "C:\CLAUDE TRADING BOT"

REM --- Single-instance guard -------------------------------------------------
REM Do not launch a second bot if one is already running (would duplicate
REM orders / fight over the same account). Fail-open: if the check can't run,
REM default to starting so the bot is never silently kept down.
set "RUNNING=0"
for /f %%c in ('wmic process where "name='python.exe'" get commandline 2^>nul ^| find /c /i "topstepx_runtime run-loop"') do set "RUNNING=%%c"
if not "%RUNNING%"=="0" (
    echo [%date% %time%] A bot instance is already running ^(count=%RUNNING%^). Not starting a duplicate.
    exit /b 0
)

:loop
echo [%date% %time%] Starting MNQ bot...
"C:\Users\kyawz\AppData\Local\Programs\Python\Python313\python.exe" -X utf8 -m src.topstepx_runtime run-loop --auto-submit

echo [%date% %time%] Bot exited (code %errorlevel%). Restarting in 60 seconds...
timeout /t 60 /nobreak >nul
goto loop
