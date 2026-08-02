# Register the bot as a Windows scheduled task (runs at logon, no console window).
# Run once from an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File setup_task.ps1
$ErrorActionPreference = "Stop"
$base = "D:\Desktop\test\twitter shitpost"
$py = "$base\.venv\Scripts\pythonw.exe"
$script = "$base\main.py"
$logOut = "$base\logs\daemon_out.log"
$logErr = "$base\logs\daemon_err.log"

$cmd = "cmd /c `"$py`" `"$script`" daemon >> `"$logOut`" 2>> `"$logErr`""

schtasks /Create /F /TN "AveragePockaBot" /TR $cmd /SC ONLOGON /RL LIMITED

Write-Host "Task 'AveragePockaBot' registered. Start manually with:"
Write-Host "  schtasks /Run /TN AveragePockaBot"
Write-Host "Remove with:"
Write-Host "  schtasks /Delete /TN AveragePockaBot /F"
