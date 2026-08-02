# Run the bot daemon in a console window (venv on drive D).
$ErrorActionPreference = "Stop"
$base = "D:\Desktop\test\twitter shitpost"
& "$base\.venv\Scripts\python.exe" "$base\main.py" daemon
