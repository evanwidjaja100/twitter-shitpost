"""Direct-login validation ordering and CWD-independent path regressions."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import login


BASE = Path(__file__).resolve().parent.parent


def _config():
    return json.loads((BASE / "config.example.json").read_text(encoding="utf-8"))


class _Page:
    url = "https://x.com/home"

    def goto(self, *args, **kwargs):
        return None

    def locator(self, *args, **kwargs):
        return SimpleNamespace(count=lambda: 1)

    def close(self):
        return None


class _Context:
    def __init__(self):
        self.page = _Page()

    def add_init_script(self, *args, **kwargs):
        return None

    def new_page(self):
        return self.page

    def close(self):
        return None


class _PlaywrightCM:
    def __init__(self, captured):
        context = _Context()

        def launch(**kwargs):
            captured.update(kwargs)
            return context

        self.playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=launch)
        )

    def __enter__(self):
        return self.playwright

    def __exit__(self, *args):
        return None


def test_direct_login_rejects_invalid_config_before_playwright(tmp_path, capsys):
    cfg = _config()
    cfg["posting"]["max_video_bytes"] = -1
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    sentinel = mock.MagicMock(side_effect=AssertionError("Playwright reached"))

    with mock.patch.object(login, "sync_playwright", sentinel), mock.patch(
        "publishing_lock.browser_profile_lock",
        side_effect=AssertionError("profile lock reached before validation"),
    ):
        assert login.main(config_path) == 1

    sentinel.assert_not_called()
    output = capsys.readouterr().out
    assert "Invalid configuration" in output
    assert "posting.max_video_bytes must be >= 1" in output


def test_login_paths_and_marker_are_independent_of_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "My Bot With Spaces"
    unrelated = tmp_path / "unrelated cwd"
    repo.mkdir()
    unrelated.mkdir()
    cfg = _config()
    cfg["paths"].update(
        {
            "browser_profile": "profile with spaces",
            "brave": "bin/Brave Browser.exe",
            "logs_dir": "logs with spaces",
        }
    )
    config_path = repo / "config.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    captured = {}
    monkeypatch.chdir(unrelated)

    with mock.patch.object(
        login, "sync_playwright", return_value=_PlaywrightCM(captured)
    ), mock.patch(
        "publishing_lock.browser_profile_lock", return_value=nullcontext()
    ):
        assert login.main(config_path) == 0

    assert Path(captured["user_data_dir"]) == (repo / "profile with spaces").resolve()
    assert Path(captured["executable_path"]) == (repo / "bin/Brave Browser.exe").resolve()
    assert captured["headless"] is False
    assert captured["no_viewport"] is True
    assert "viewport" not in captured
    assert captured["args"].count("--start-maximized") == 1
    assert (repo / "logs with spaces" / "logged_in.json").exists()
    assert not (unrelated / "logs with spaces" / "logged_in.json").exists()
