# setup_scheduler.ps1
# Registers a Windows Task Scheduler job that starts the MNQ trading bot
# every weekday at 9:20 AM ET (= 8:20 AM CT).
#
# Does NOT require Administrator — uses schtasks.exe for the current user.
#
# Run from any normal PowerShell:
#   cd "C:\CLAUDE TRADING BOT"
#   .\setup_scheduler.ps1
#
# To remove the task later:
#   schtasks /delete /TN "MNQ Trading Bot" /F

$TaskName  = "MNQ Trading Bot"
$BatFile   = "C:\CLAUDE TRADING BOT\start_bot.bat"
$StartTime = "09:20"   # 9:20 AM ET — adjust if your PC clock is not ET

# Delete existing task if it exists (ignore error if not found)
schtasks /delete /TN $TaskName /F 2>$null

# Create new task — runs as current user, no admin needed
$result = schtasks /create `
    /TN  $TaskName `
    /TR  "`"$BatFile`"" `
    /SC  WEEKLY `
    /D   MON,TUE,WED,THU,FRI `
    /ST  $StartTime `
    /F

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "SUCCESS: Task '$TaskName' registered."
    Write-Host "Bot will start at $StartTime ET every Mon-Fri."
    Write-Host ""
    Write-Host "To verify : schtasks /query /TN `"$TaskName`" /FO LIST"
    Write-Host "To run now: schtasks /run /TN `"$TaskName`""
    Write-Host "To remove : schtasks /delete /TN `"$TaskName`" /F"
} else {
    Write-Host ""
    Write-Host "ERROR: Task registration failed (exit code $LASTEXITCODE)."
    Write-Host "Try running PowerShell as Administrator and re-run this script."
}
