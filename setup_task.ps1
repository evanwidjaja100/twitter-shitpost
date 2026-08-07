# Register the bot as a Windows scheduled task (runs at logon, no console window).
# Run once from an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File setup_task.ps1

param([switch]$SkipRegister)

$ErrorActionPreference = "Stop"
$base = "D:\Desktop\test\twitter shitpost"

# cmd.exe wrapper preserves the Python daemon's exit code after redirecting
# output, so Task Scheduler only sees a "failed task" when the daemon really
# failed (non-zero), never for an intentional successful stop (exit 0).
#
# Execution model:
#   Execute   = cmd.exe
#   Arguments = /c "<pythonw>" "<main.py>" daemon >> "<out>" 2>> "<err>"
# i.e. the executable is cmd.exe and the argument string is the full
# cmd.exe command line starting with its /c flag.  Never "cmd /c ...", which
# would make Windows try to run "cmd.exe cmd /c ..." (cmd would treat the
# second "cmd" as a program name and fail to start the daemon).
function New-BotScheduledTaskAction {
    param(
        [string]$BasePath = $base,
        [string]$PythonRel = ".venv\Scripts\pythonw.exe",
        [string]$ScriptRel = "main.py"
    )
    $absBase = (Resolve-Path -LiteralPath $BasePath -ErrorAction Stop).Path
    $py = Join-Path $absBase $PythonRel
    $script = Join-Path $absBase $ScriptRel
    $logOut = Join-Path $absBase "logs\daemon_out.log"
    $logErr = Join-Path $absBase "logs\daemon_err.log"
    $argLine = "/c `"$py`" `"$script`" daemon >> `"$logOut`" 2>> `"$logErr`""
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument $argLine -WorkingDirectory $absBase
}

# Second recovery layer (the Python-side supervisor is the first):
#  - RestartCount / RestartInterval: restart a task that ended in failure
#    (non-zero daemon exit) up to 3 times, waiting 5 minutes between attempts.
#  - MultipleInstances = IgnoreNew: never launch a second scheduled instance
#    while one is already running (defense in depth; the Python publishing and
#    browser-profile locks remain the authoritative ownership mechanism).
function New-BotScheduledTaskSettings {
    New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
}

if ($SkipRegister) {
    return
}

$action = New-BotScheduledTaskAction
$settings = New-BotScheduledTaskSettings
$trigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask -TaskName "AveragePockaBot" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Run the X Shitpost Bot daemon; restart on genuine process failure." -Force

Write-Host "Task 'AveragePockaBot' registered (restart on failure: 3x every 5 min; overlapping instances ignored)."
Write-Host "Start manually with:"
Write-Host "  schtasks /Run /TN AveragePockaBot"
Write-Host "Remove with:"
Write-Host "  schtasks /Delete /TN AveragePockaBot /F"