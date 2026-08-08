# Run the bot daemon in a console window.
# All paths derive from THIS script's own location, so the script works no
# matter which directory / drive you invoke it from (terminal, Task Scheduler,
# Explorer).
$ErrorActionPreference = "Stop"
$base = $PSScriptRoot
$python = Join-Path $base ".venv\Scripts\python.exe"
$main = Join-Path $base "main.py"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python executable not found at $python"
    exit 1
}
if (-not (Test-Path -LiteralPath $main)) {
    Write-Error "main.py not found at $main"
    exit 1
}
& $python $main daemon
# Propagate the daemon's real exit code so callers (and Task Scheduler, when
# this script is used as the task action) can tell an intentional successful
# stop (exit 0: e.g. login/captcha shutdown) from an unrecovered crash
# (non-zero). Without this the script always "succeeds" and failures would be
# invisible to any outer restart layer.
exit $LASTEXITCODE
