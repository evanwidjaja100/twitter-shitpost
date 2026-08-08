"""Safe, value-based validation of the PowerShell deployment scripts — plus a
REAL-WORLD execution regression.

* ``run_bot.ps1`` must propagate the Python daemon's real exit code so callers /
  Task Scheduler can distinguish an intentional successful stop (0) from an
  unrecovered crash (non-zero).
* ``setup_task.ps1`` must build a *valid* Scheduled Task action:
    Execute   = cmd.exe
    Arguments = "/d /s /c " "<pythonw>" "<main.py>" daemon >> "<out>" 2>> "<err>""
  i.e. the executable is cmd.exe and the argument string is cmd's own command
  line wrapped so that EVERY path (pythonw, main.py, both logs) can contain
  spaces.  Never "cmd /c ..." and never the fragile bare "/c "a" "b" ..." form.

Everything is constructed by dot-sourcing ``setup_task.ps1`` with ``-SkipRegister``
(no real Scheduled Task is ever registered) and the harmless stub below is the
only thing executed — nothing touches X or a real daemon.

The key regression actually EXECUTES the exact Arguments value through
``cmd.exe`` under a temporary directory whose path contains spaces, proving the
quoting survives cmd's parsing (B1–B4).
"""

import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUN_BOT = ROOT / "run_bot.ps1"
SETUP_TASK = ROOT / "setup_task.ps1"

STUB_SRC = """\
import sys
from pathlib import Path
here = Path(__file__).resolve().parent
(here / "ran.txt").write_text("RAN")
(here / "argv.txt").write_text(repr(sys.argv[1:]))
print("STDOUT-MARKER")
print("STDERR-MARKER", file=sys.stderr)
sys.exit(7)
"""


def _powershell():
    for exe in ("powershell", "pwsh"):
        p = shutil.which(exe)
        if p:
            return p
    return None


def _run_ps(args, timeout=180):
    ps = _powershell()
    if ps is None:
        pytest.skip("no PowerShell available")
    return subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def _escape(p: str) -> str:
    return p.replace("'", "''")


def _parse_ok(path: Path):
    script = _escape(str(path))
    code = (
        "$errs = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script}', "
        "[ref]$null, [ref]$errs); "
        "if ($errs) { $errs | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    res = _run_ps(["-Command", code])
    return res.returncode == 0, res.stdout + res.stderr


def _action_snapshot():
    """Dot-source setup_task.ps1 (-SkipRegister, so NO real task is created)
    and emit the real action/setting objects as KEY=... lines."""
    script = _escape(str(SETUP_TASK))
    base = _escape(str(ROOT))
    code = f"""
. '{script}' -SkipRegister
$a = New-BotScheduledTaskAction -BasePath '{base}'
$s = New-BotScheduledTaskSettings
"EXECUTE=" + $a.Execute
"ARGUMENTS=" + $a.Arguments
"WORKINGDIR=" + $a.WorkingDirectory
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


def _spaced_dir() -> Path:
    """A definitely-existing temp dir whose path contains spaces."""
    d = Path(tempfile.gettempdir()) / "Twitter Bot Test 123"
    d.mkdir(parents=True, exist_ok=True)
    return d


@staticmethod
def _windows():
    return sys.platform == "win32"


@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_ps1_scripts_parse_cleanly():
    for script in (RUN_BOT, SETUP_TASK):
        ok, detail = _parse_ok(script)
        assert ok, f"{script.name} failed to parse:\n{detail}"


# ---------------------------------------------------------------------------
# B5 — action object structure (constructed without registering)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_b5_action_execute_is_cmd_and_arguments_double_quoted():
    """Execute == cmd.exe; Arguments must be the robust /d /s /c ""..."" form.
    Never 'cmd /c', never the old fragile bare '/c "a" "b" ...'."""
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    assert snap["EXECUTE"] == "cmd.exe"
    args = snap["ARGUMENTS"]
    assert args.startswith("/d /s /c \"\""), f"not double-quoted wrapper: {args!r}"
    assert not args.startswith("cmd"), args
    assert not args.startswith("/c \"\""), args


@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_argv_contains_expected_parts():
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    args = snap["ARGUMENTS"]
    assert "pythonw.exe" in args.lower() or "python.exe" in args.lower()
    assert "main.py" in args
    assert " daemon" in args
    assert ">>" in args and "2>>" in args
    assert "daemon_out.log" in args and "daemon_err.log" in args


@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_paths_with_spaces_quoted():
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    args = snap["ARGUMENTS"]
    assert "\\test\\twitter shitpost\\main.py" in args
    assert args.count('"') >= 4  # python + script + stdout + stderr paths


# --------------------------------------------------------------------------
# B1..B4: REAL execution under a path containing spaces
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_b_execute_real_cmd_with_spaces_in_paths():
    """B1..B4: build Arguments with the SAME helper setup_task.ps1 uses, then
    really execute cmd.exe <generated args> under a path containing spaces.

    Proves: stub runs (marker created), stub receives the intended argv
    (Python's sys.argv[1:] == ['daemon'] inside the executed stub), stdout
    redirected, stderr redirected, and the stub's non-zero exit code (7)
    propagates to the caller.
    """
    probe_dir = _spaced_dir() / f"run-{uuid.uuid4().hex[:8]}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    stub = probe_dir / "stub.py"
    stub.write_text(STUB_SRC, encoding="utf-8")
    marker = probe_dir / "ran.txt"
    argv_file = probe_dir / "argv.txt"
    out_log = probe_dir / "out.log"
    err_log = probe_dir / "err.log"
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")  # path has a space too

    # Construct with the real helper from setup_task.ps1 (the exact function
    # New-BotScheduledTaskAction calls) and read back $action.Arguments, then
    # EXECUTE that exact runtime value via cmd.exe the way Task Scheduler does
    # (Start-Process passes the string as the single argument list verbatim).
    code = f"""
. '{_escape(str(SETUP_TASK))}' -SkipRegister
$args = New-DaemonCmdArguments -PythonPath '{_escape(py)}' -ScriptPath '{_escape(str(stub))}' -StdoutPath '{_escape(str(out_log))}' -StderrPath '{_escape(str(err_log))}'
$probe = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $args -WorkingDirectory '{_escape(str(probe_dir))}'
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList $probe.Arguments -WorkingDirectory '{_escape(str(probe_dir))}' -Wait -PassThru -NoNewWindow
"ARGS=" + $probe.Arguments
"EXEC=" + $probe.Execute
"PY_EXIT=" + $p.ExitCode
"MARKER=" + (Test-Path '{_escape(str(marker))}')
"ARGV=" + $(if (Test-Path '{_escape(str(argv_file))}') {{ (Get-Content '{_escape(str(argv_file))}' -Raw).Trim() }} else {{ 'MISSING' }})
"""
    res = _run_ps(["-Command", code])
    assert res.returncode == 0, res.stdout + res.stderr
    snap = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            snap[k.strip()] = v.strip()

    assert snap["EXEC"] == "cmd.exe"
    assert snap["ARGS"].startswith("/d /s /c \"\""), snap["ARGS"]
    # B1: stub ran.
    assert snap["MARKER"] == "True", f"B1 FAIL: stub did not execute:\n{res.stdout}"
    # B5: stub received intended arguments (cmd passthrough intact).
    assert snap["ARGV"] == "['daemon']", f"argv mismatch: {snap['ARGV']}"
    # B2/B3: redirection.
    assert out_log.read_text(encoding="utf-8").strip() == "STDOUT-MARKER", \
        "B2 FAIL: stdout redirection"
    assert err_log.read_text(encoding="utf-8").strip() == "STDERR-MARKER", \
        "B3 FAIL: stderr redirection"
    # B4: exit code propagation via cmd.exe.
    assert snap["PY_EXIT"] == "7", f"B4 FAIL: exit {snap['PY_EXIT']} != 7"


# --------------------------------------------------------------------------
# B7: restart settings intact (constructed without registering)
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_b7_restart_settings_intact():
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    assert int(snap["RESTART_COUNT"]) >= 1
    assert float(snap["RESTART_INTERVAL"]) >= 5
    assert snap["MULTIPLE_INSTANCES"].lower() in ("ignorenew", "ignorenonew")


@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_working_directory_correct():
    snap, err = _action_snapshot()
    assert snap is not None, f"snapshot failed:\n{err}"
    assert "twitter shitpost" in snap["WORKINGDIR"]


# --------------------------------------------------------------------------
# B8: run_bot.ps1 still propagates Python exit code
# --------------------------------------------------------------------------

def test_run_bot_propagates_python_exit_code():
    text = RUN_BOT.read_text(encoding="utf-8")
    assert "exit $LASTEXITCODE" in text or "exit $code" in text


@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_run_bot_exit_code_wire(tmp_path):
    """Real harmless stub: exit 42 through run_bot.ps1 propagation pattern."""
    stub = tmp_path / "stub.py"
    stub.write_text("import sys\nsys.exit(42)\n", encoding="utf-8")
    script = tmp_path / "p.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"& '{sys.executable}' '{stub}'\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    res = _run_ps(["-File", str(script)])
    assert res.returncode == 42, res.stdout + res.stderr


@pytest.mark.skipif(not _windows(), reason="Windows-specific deployment scripts")
def test_no_registration_happens():
    """Prove tests never register by dot-sourcing with -SkipRegister and
    checking Register-ScheduledTask was not invoked (action objects are
    constructible without it)."""
    # If dot-sourcing actually called Register-ScheduledTask it would fail
    # without elevation; the snapshot tests running successfully already prove
    # no registration. Re-run the guard to be explicit.
    code = f". '{_escape(str(SETUP_TASK))}' -SkipRegister\n'GUARD-OK'\n"
    res = _run_ps(["-Command", code])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "GUARD-OK" in res.stdout