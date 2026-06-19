# Registers the daily OCS Power BI pull as a Windows scheduled task.
# Runs when you're logged on. Set for 7 AM, but "StartWhenAvailable" means if the
# laptop was off / you were logged out at 7 AM, it runs automatically as soon as
# you next log in. No password needed (avoids the Microsoft-account auth issue).
# The rolling-window + replace design means a missed day just catches up to the
# latest data on the next run.
#
# Run once, in an ADMIN PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\terroir-ops\register_powerbi_task.ps1
$ErrorActionPreference = "Stop"

$action = New-ScheduledTaskAction -Execute "C:\terroir-ops\run_powerbi_pull.bat" `
    -WorkingDirectory "C:\terroir-ops"

$trigger = New-ScheduledTaskTrigger -Daily -At 7:00am

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

# Interactive = run only when logged on; no stored password required.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "OCS PowerBI Pull" `
    -Description "Daily OCS Power BI market-intelligence pull (all stores x 14/30/90/180-day windows, headless)." `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

Write-Host ""
Write-Host "Registered 'OCS PowerBI Pull' - 7:00 AM daily, or right after you next log in if missed."
Write-Host 'Test it now with:  schtasks /Run /TN "OCS PowerBI Pull"'
