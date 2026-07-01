@echo off
cd /d "C:\CLAUDE TRADING BOT"

:loop
echo [%date% %time%] Starting MNQ bot...
"C:\Users\kyawz\AppData\Local\Programs\Python\Python313\python.exe" -X utf8 -m src.topstepx_runtime run-loop --auto-submit

echo [%date% %time%] Bot exited (code %errorlevel%). Restarting in 60 seconds...
timeout /t 60 /nobreak >nul
goto loop
