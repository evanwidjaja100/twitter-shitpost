# Register the bot as a Windows scheduled task (runs at logon, no console window).
# Run once from an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File setup_task.ps1
$ErrorActionPreference = "Stop"
$base = "D:\Desktop\test\twitter shitpost"
$py = "$base\.venv\Scripts\pythonw.exe"
$script = "$base\main.py"
$logOut = "$base\logs\daemon_out.log"
$logErr = "$base\logs\daemon_err.log"

# cmd.exe wrapper preserves the Python daemon's exit code after redirecting
# output, so Task Scheduler only sees a "failed task" when the daemon really
# failed (non-zero), never for an intentional successful stop (exit 0).
$cmd = "cmd /c `"$py`" `"$script`" daemon >> `"$logOut`" 2>> `"$logErr`""

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $base
$trigger = New-ScheduledTaskTrigger -AtLogOn

# Second recovery layer (the Python-side supervisor is the first):
#  - RestartCount / RestartInterval: restart a task that ended in failure
#    (non-zero daemon exit) up to 3 times, waiting 5 minutes between attempts.
#  - MultipleInstances = IgnoreNew: never launch a second scheduled instance
#    while one is already running (defense in depth; the Python publishing and
#    browser-profile locks remain the authoritative ownership mechanism).
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "AveragePockaBot" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Run the X Shitpost Bot daemon; restart on genuine process failure." -Force

Write-Host "Task 'AveragePockaBot' registered (restart on failure: 3x every 5 min; overlapping instances ignored)."
Write-Host "Start manually with:"
Write-Host "  schtasks /Run /TN AveragePockaBot"
Write-Host "Remove with:"
Write-Host "  schtasks /Delete /TN AveragePockaBot /F"
