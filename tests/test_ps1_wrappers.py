"""Static/safe validation of the PowerShell deployment scripts (Issue 1 part 2).

* ``run_bot.ps1`` must propagate the Python daemon's real exit code so callers
  / Task Scheduler can distinguish an intentional successful stop (0) from an
  unrecovered crash (non-zero).
* ``setup_task.ps1`` must configure restart-on-failure (RestartCount /
  RestartInterval) and avoid overlapping scheduled instances
  (MultipleInstances = IgnoreNew) using the ScheduledTasks cmdlets.

Nothing here registers or modifies the real scheduled task, and nothing runs a
daemon. Only the propagation pattern is executed, against a harmless
``sys.exit`` stub, in a real PowerShell.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN_BOT = ROOT / "run_bot.ps1"
SETUP_TASK = ROOT / "setup_task.ps1"


def _powershell():
    for exe in ("powershell", "pwsh"):
        p = shutil.which(exe)
        if p:
            return p
    return None


def _run_ps(args, timeout=120):
    ps = _powershell()
    if ps is None:
        pytest.skip("no PowerShell available")
    return subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _parse_ok(path: Path) -> bool:
    script = str(path).replace("'", "''")
    code = (
        "$errs = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errs); "
        "if ($errs) { $errs | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    res = _run_ps(["-Command", code])
    return res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("script", [RUN_BOT, SETUP_TASK])
def test_powershell_scripts_parse(script):
    ok, detail = _parse_ok(script)
    assert ok, f"{script.name} failed to parse:\n{detail}"


def test_run_bot_propagates_python_exit_code():
    text = RUN_BOT.read_text(encoding="utf-8")
    assert "exit $LASTEXITCODE" in text or "exit $code" in text


def test_run_bot_propagation_pattern_works(tmp_path):
    """Execute the exact propagation pattern (external exe -> $LASTEXITCODE ->
    exit) against a stub that exits 42 and assert the PowerShell returns 42."""
    stub = tmp_path / "stub.py"
    stub.write_text("import sys\nsys.exit(42)\n", encoding="utf-8")
    script = tmp_path / "runlike.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"& '{sys.executable}' '{stub}'\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    res = _run_ps(["-File", str(script)])
    assert res.returncode == 42, res.stdout + res.stderr


def test_setup_task_has_restart_and_single_instance_settings():
    text = SETUP_TASK.read_text(encoding="utf-8")
    assert "-RestartCount" in text
    assert "-RestartInterval" in text
    assert "IgnoreNew" in text            # MultipleInstances = IgnoreNew
    assert "Register-ScheduledTask" in text
    assert "New-ScheduledTaskSettingsSet" in text


def test_setup_task_preserves_exit_code_path():
    """The task action must run the daemon through a wrapper that propagates
    the exit code (cmd /c returns the inner command's code)."""
    text = SETUP_TASK.read_text(encoding="utf-8")
    assert "cmd /c" in text
    assert "daemon" in text