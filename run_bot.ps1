# Run the bot daemon in a console window (venv on drive D).
$ErrorActionPreference = "Stop"
$base = "D:\Desktop\test\twitter shitpost"
& "$base\.venv\Scripts\python.exe" "$base\main.py" daemon
# Propagate the daemon's real exit code so callers (and Task Scheduler, when
# this script is used as the task action) can tell an intentional successful
# stop (exit 0: e.g. login/captcha shutdown) from an unrecovered crash
# (non-zero). Without this the script always "succeeds" and failures would be
# invisible to any outer restart layer.
exit $LASTEXITCODE
