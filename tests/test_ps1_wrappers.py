"""Safe, value-based validation of the PowerShell deployment scripts.

* ``run_bot.ps1`` must propagate the Python daemon's real exit code so callers /
  Task Scheduler can distinguish an intentional successful stop (0) from an
  unrecovered crash (non-zero).
* ``setup_task.ps1`` must build a *valid* Scheduled Task action:
    Execute   = cmd.exe
    Arguments = /c "<pythonw>" "<main.py>" daemon >> "<out>" 2>> "<err>"
  i.e. the arguments begin with cmd.exe's own ``/c`` switch, never with a
  redundant ``cmd /c`` (which would have Windows try to run
  ``cmd.exe cmd /c ...`` — cmd treats the second ``cmd`` as a program and the
  daemon never starts). Restart-on-failure and multiple-instance suppression
  must remain configured.

Nothing here registers or modifies the real scheduled task and nothing runs a
daemon. The construction functions are dot-sourced from ``setup_task.ps1`` with
``-SkipRegister`` and the real ``New-ScheduledTaskAction``/``New-
ScheduledTaskSettingsSet`` cmdlets are invoked to prove the produced values.
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


@staticmethod
def _action_snapshot():
    """Dot-source setup_task.ps1 (-SkipRegister) and sniff the real action and
    settings objects. Returns dict of field -> value (or list/ints)."""
    script = str(SETUP_TASK).replace("'", "''")
    base = str(ROOT).replace("'", "''")
    code = f"""
. '{script}' -SkipRegister
$a = New-BotScheduledTaskAction -BasePath '{base}'
$s = New-BotScheduledTaskSettings
"EXECUTE=" + $a.Execute
"ARGUMENTS=" + $a.Arguments
"RESTART_COUNT=" + $s.RestartCount
$minutes = 0
if ($s.RestartInterval -match 'PT(\\d+)M') {{ $minutes = [int]$Matches[1] }}
"RESTART_INTERVAL=" + $minutes
"MULTIPLE_INSTANCES=" + $s.MultipleInstances.ToString()
"""
    res = _run_ps(["-Command", code])
    if res.returncode != 0:
        return None, res.stdout + res.stderr
    snap = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            snap[k.strip()] = v.strip()
    return snap, ""


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


def test_setup_action_execute_is_cmd_and_args_start_with_slash_c():
    """B1/B2/B4: Execute == cmd.exe and Arguments begin with cmd.exe's own
    /c switch (never with a redundant 'cmd /c')."""
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    assert snap["EXECUTE"] == "cmd.exe"
    args = snap["ARGUMENTS"]
    assert args.startswith("/c"), f"'cmd /c' leaked into Arguments: {args!r}"
    assert not args.startswith("cmd"), args


def test_setup_action_contains_python_daemon_command():
    """B4/B5: the Arguments carry the Python daemon invocation."""
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    args = snap["ARGUMENTS"]
    assert "pythonw.exe" in args.lower() or "python.exe" in args.lower()
    assert "main.py" in args
    assert " daemon" in args


def test_setup_action_quotes_paths_with_spaces():
    """B6: any path containing spaces (the repo path does) is quoted."""
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    args = snap["ARGUMENTS"]
    assert "\\test\\twitter shitpost\\main.py" in args
    assert args.count('"') >= 4  # python + script + stdout + stderr paths quoted


def test_setup_action_keeps_output_redirection():
    """B7: stdout/stderr redirection to per-output logs survives."""
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    args = snap["ARGUMENTS"]
    assert ">>" in args and "2>>" in args
    assert "daemon_out.log" in args and "daemon_err.log" in args


def test_setup_settings_restart_and_single_instance():
    """B8/B9/Task 8: restart-on-failure + duplicate-suppression stay set."""
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    assert int(snap["RESTART_COUNT"]) >= 1
    assert float(snap["RESTART_INTERVAL"]) >= 5
    assert snap["MULTIPLE_INSTANCES"].lower() in ("ignorenew", "ignorenonew")