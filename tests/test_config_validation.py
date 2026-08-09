"""Issue 1: deterministic tests for the centralized configuration validator.

Requirements proven here:
  * ``config.example.json`` itself is valid.
  * invalid numeric limits fail with actionable messages (negative/zero/type).
  * impossible quota combinations (min > max) fail clearly.
  * valid overnight posting hours remain accepted; invalid times fail.
  * retention values are validated (even with ``enabled=false``).
  * wrong boolean / account / list types are actionable, not silent.
  * multiple errors are reported together in one pass.
  * validation runs BEFORE browser/session startup (``main`` never reaches
    ``XSession`` after a validation failure).
  * obsolete/unknown keys only warn, so old configs keep working.
"""

import ast
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import config_validation
import main

BASE = Path(__file__).resolve().parent.parent


def _valid() -> dict:
    """A fully valid configuration (mirrors config.example.json)."""
    return {
        "paths": {
            "ffmpeg": "tools/ffmpeg/ffmpeg.exe",
            "ffprobe": "tools/ffmpeg/ffprobe.exe",
            "brave": "C:/Program Files/BraveSoftware/brave.exe",
            "browser_profile": "browser_profile",
            "assets_dir": "assets",
            "logs_dir": "logs",
            "db_file": "data/bot.db",
        },
        "posting": {
            "min_posts_per_day": 3,
            "max_posts_per_day": 6,
            "active_hours_start": 16,
            "active_hours_end": 1,
            "max_image_bytes": 20_000_000,
            "max_video_bytes": 450_000_000,
            "max_caption_len": 270,
            "caption_style": "title",
            "random_caption_chance": 0.15,
            "caption_pool": ["gg ez", "rate the build 1-10"],
        },
        "publisher": {
            "image_ready_timeout_seconds": 60,
            "video_ready_timeout_seconds": 180,
        },
        "safety": {
            "retry_backoff_minutes": 30,
            "stop_on_login_failure": True,
            "max_daily_posts_absolute": 10,
            "max_daemon_restarts": 5,
        },
        "filters": {"cooldown_days": 30, "blocked_keywords": ["trump"]},
        "secrets": {"youtube_api_key": ""},
        "youtube": {
            "shorts_feed": True,
            "channels": [],
            "max_items_per_channel": 10,
            "min_views": 10000,
            "max_age_days": 21,
            "clip_max_seconds": 60,
            "clip_min_seconds": 8,
            "max_source_video_minutes": 8,
        },
        "x_sources": {
            "accounts": ["@memelord"],
            "max_posts_per_account": 10,
            "min_likes": 5000,
            "scrolls": 3,
        },
        "tiktok": {
            "foryou": True,
            "accounts": [],
            "max_posts_per_account": 10,
            "min_likes": 50000,
            "scrolls": 3,
        },
        "tracking": {"follow_check_hours": 168, "own_handle": "average_pocka"},
        "retention": {
            "enabled": True,
            "media_days": 7,
            "temp_hours": 24,
            "log_max_bytes": 5242880,
            "log_backup_count": 5,
            "interval_hours": 24,
        },
    }


def _mutate(base: dict, path_str: str, value):
    """Set a nested key like ``posting.max_image_bytes``."""
    parts = path_str.split(".")
    holder = base
    for p in parts[:-1]:
        holder = holder[p]
    holder[parts[-1]] = value
    return base


def _errors(cfg) -> list:
    return config_validation.validate_config(cfg)


class TestExampleConfig:
    def test_config_example_file_is_valid(self):
        cfg = json.loads((BASE / "config.example.json").read_text(encoding="utf-8"))
        assert config_validation.validate_config(cfg) == []

    def test_valid_template_passes(self):
        assert config_validation.validate_config(_valid()) == []

    def test_overnight_window_accepted(self):
        cfg = _valid()
        cfg["posting"]["active_hours_start"] = 16
        cfg["posting"]["active_hours_end"] = 1
        assert config_validation.validate_config(cfg) == []

    def test_missing_retention_still_valid(self):
        cfg = _valid()
        cfg.pop("retention")
        assert config_validation.validate_config(cfg) == []

    def test_no_accounts_is_valid(self):
        cfg = _valid()
        cfg["tiktok"]["accounts"] = []
        cfg["x_sources"]["accounts"] = []
        assert config_validation.validate_config(cfg) == []

    @pytest.mark.parametrize("section", config_validation.PRODUCTION_REQUIRED_SECTIONS)
    def test_every_required_section_is_rejected_when_missing(self, section):
        cfg = _valid()
        cfg.pop(section)
        assert any(
            section in error and "required" in error
            for error in config_validation.validate_config(cfg)
        )

    @pytest.mark.parametrize("path", config_validation.PRODUCTION_REQUIRED_PATHS)
    def test_every_maintained_required_leaf_is_rejected_when_missing(self, path):
        cfg = _valid()
        section, field = path.split(".", 1)
        cfg[section].pop(field)
        errors = config_validation.validate_config(cfg)
        assert any(path in error and "required" in error for error in errors)

    def test_tracking_is_optional_by_contract(self):
        cfg = _valid()
        cfg.pop("tracking")
        assert config_validation.validate_config(cfg) == []

    def test_optional_tracking_fields_use_defaults(self):
        cfg = _valid()
        cfg["tracking"] = {}
        assert config_validation.validate_config(cfg) == []

    def test_all_production_hard_leaf_accesses_are_in_required_schema(self):
        """Static guard against adding cfg[section][field] without validation."""
        roots = {
            "cfg": None,
            "paths": "paths",
            "posting": "posting",
            "safety": "safety",
            "filters": "filters",
            "tracking": "tracking",
        }
        found = set()
        for path in BASE.rglob("*.py"):
            if any(
                part in {"tests", ".venv", "browser_profile", "__pycache__"}
                for part in path.parts
            ) or path.name == "config_validation.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                keys = []
                current = node
                while isinstance(current, ast.Subscript):
                    key = current.slice
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        keys = []
                        break
                    keys.append(key.value)
                    current = current.value
                if not keys or not isinstance(current, ast.Name) or current.id not in roots:
                    continue
                keys.reverse()
                prefix = roots[current.id]
                if current.id == "cfg" and len(keys) >= 2:
                    found.add(".".join(keys))
                elif prefix and not keys[0].startswith("_"):
                    found.add(f"{prefix}." + ".".join(keys))

        assert found <= set(config_validation.PRODUCTION_REQUIRED_PATHS)


class TestNumericLimits:
    def test_negative_media_byte_limit_rejected(self):
        cfg = _mutate(_valid(), "posting.max_video_bytes", -1)
        errs = config_validation.validate_config(cfg)
        assert any("posting.max_video_bytes" in e and ">= 1" in e for e in errs)

    def test_zero_required_byte_limit_rejected(self):
        cfg = _mutate(_valid(), "posting.max_image_bytes", 0)
        errs = config_validation.validate_config(cfg)
        assert any("posting.max_image_bytes" in e for e in errs)

    def test_float_media_limit_rejected(self):
        cfg = _mutate(_valid(), "posting.max_video_bytes", 123.5)
        errs = config_validation.validate_config(cfg)
        assert any("posting.max_video_bytes" in e and "integer" in e for e in errs)

    def test_non_numeric_limit_rejected(self):
        cfg = _mutate(_valid(), "posting.max_image_bytes", "lots")
        errs = config_validation.validate_config(cfg)
        assert any("posting.max_image_bytes" in e for e in errs)

    def test_bool_never_stands_in_for_count(self):
        cfg = _mutate(_valid(), "safety.max_daemon_restarts", True)
        errs = config_validation.validate_config(cfg)
        assert any("max_daemon_restarts" in e and "integer" in e for e in errs)

    def test_negative_retention_interval_rejected(self):
        cfg = _valid()
        cfg["retention"]["media_days"] = -7
        cfg["retention"]["temp_hours"] = -1
        errs = config_validation.validate_config(cfg)
        assert any("media_days" in e for e in errs)
        assert any("temp_hours" in e for e in errs)

    def test_retention_still_validated_when_disabled(self):
        cfg = _valid()
        cfg["retention"]["enabled"] = False
        cfg["retention"]["log_max_bytes"] = 0
        cfg["retention"]["log_backup_count"] = 0
        errs = config_validation.validate_config(cfg)
        assert any("log_max_bytes" in e for e in errs)
        assert any("log_backup_count" in e for e in errs)

    def test_negative_retry_delay_rejected(self):
        cfg = _mutate(_valid(), "safety.retry_backoff_minutes", -5)
        errs = config_validation.validate_config(cfg)
        assert any("retry_backoff_minutes" in e for e in errs)

    def test_negative_absolute_quota_rejected(self):
        cfg = _mutate(_valid(), "safety.max_daily_posts_absolute", -3)
        errs = config_validation.validate_config(cfg)
        assert any("max_daily_posts_absolute" in e for e in errs)

    def test_zero_daemon_restarts_rejected(self):
        cfg = _mutate(_valid(), "safety.max_daemon_restarts", 0)
        errs = config_validation.validate_config(cfg)
        assert any("max_daemon_restarts" in e for e in errs)


class TestQuotaRelationships:
    def test_min_above_max_rejected(self):
        cfg = _valid()
        cfg["posting"]["min_posts_per_day"] = 6
        cfg["posting"]["max_posts_per_day"] = 3
        errs = config_validation.validate_config(cfg)
        assert any(
            "min_posts_per_day" in e and "<= posting.max_posts_per_day" in e
            for e in errs
        )

    def test_absolute_cap_stricter_than_window_still_valid(self):
        cfg = _valid()
        cfg["safety"]["max_daily_posts_absolute"] = 2  # below min 3: allowed
        assert config_validation.validate_config(cfg) == []


class TestHours:
    def test_invalid_hour_rejected(self):
        for bad in (24, -1, 99, 16.5, "16"):
            cfg = _mutate(_valid(), "posting.active_hours_end", bad)
            errs = config_validation.validate_config(cfg)
            assert any("active_hours_end" in e for e in errs), f"hour {bad}"

    def test_boundary_hours_accepted(self):
        for start, end in ((0, 23), (23, 0), (16, 1), (0, 0)):
            cfg = _valid()
            cfg["posting"]["active_hours_start"] = start
            cfg["posting"]["active_hours_end"] = end
            assert config_validation.validate_config(cfg) == [], f"{start}->{end}"


class TestTypes:
    def test_wrong_boolean_type_rejected(self):
        cfg = _mutate(_valid(), "safety.stop_on_login_failure", "false")
        errs = config_validation.validate_config(cfg)
        assert any("stop_on_login_failure" in e for e in errs)

    def test_wrong_foryou_type_rejected(self):
        cfg = _mutate(_valid(), "tiktok.foryou", "true")
        errs = config_validation.validate_config(cfg)
        assert any("tiktok.foryou" in e for e in errs)

    def test_bad_account_type_rejected(self):
        cfg = _valid()
        cfg["x_sources"]["accounts"] = ["@ok", 123]
        errs = config_validation.validate_config(cfg)
        assert any("accounts" in e and "item 1" in e for e in errs)

    def test_accounts_wrong_shape_rejected(self):
        cfg = _valid()
        cfg["tiktok"]["accounts"] = "memelord"
        errs = config_validation.validate_config(cfg)
        assert any("tiktok.accounts" in e for e in errs)

    def test_secrets_non_string_rejected(self):
        cfg = _valid()
        cfg["secrets"]["youtube_api_key"] = 12345
        errs = config_validation.validate_config(cfg)
        assert any("secrets.youtube_api_key" in e for e in errs)

    def test_invalid_caption_style_rejected(self):
        cfg = _mutate(_valid(), "posting.caption_style", "legacy")
        errs = config_validation.validate_config(cfg)
        assert any("caption_style" in e for e in errs)

    def test_caption_chance_out_of_range_rejected(self):
        cfg = _mutate(_valid(), "posting.random_caption_chance", 7)
        errs = config_validation.validate_config(cfg)
        assert any("random_caption_chance" in e for e in errs)

    @pytest.mark.parametrize(
        ("path", "bad"),
        (
            ("posting.caption_pool", "hello"),
            ("posting.random_caption_chance", "0.5"),
            ("filters.blocked_keywords", "spam"),
        ),
    )
    def test_required_caption_and_filter_wrong_types_are_actionable(self, path, bad):
        cfg = _mutate(_valid(), path, bad)
        assert any(path in error for error in config_validation.validate_config(cfg))

    def test_paths_missing(self):
        cfg = _valid()
        cfg["paths"].pop("db_file")
        errs = config_validation.validate_config(cfg)
        assert any("paths.db_file" in e for e in errs)

    def test_non_dict_section_rejected(self):
        cfg = _valid()
        cfg["posting"] = "nope"
        errs = config_validation.validate_config(cfg)
        assert any("posting" in e and "object" in e for e in errs)


class TestMultipleErrors:
    def test_errors_collected_in_one_pass(self):
        cfg = _valid()
        cfg["posting"]["min_posts_per_day"] = 6
        cfg["posting"]["max_posts_per_day"] = 3
        cfg["posting"]["max_video_bytes"] = -1
        cfg["safety"]["stop_on_login_failure"] = "true"
        cfg["retention"]["media_days"] = -2
        errs = config_validation.validate_config(cfg)
        assert len(errs) >= 4
        joined = "\n".join(errs)
        assert "min_posts_per_day" in joined
        assert "max_video_bytes" in joined
        assert "stop_on_login_failure" in joined
        assert "media_days" in joined


class TestBackwardCompatibility:
    def test_missing_publisher_section_uses_safe_defaults(self):
        cfg = _valid()
        cfg.pop("publisher")
        assert config_validation.validate_config(cfg) == []
        assert config_validation.publisher_ready_timeout_seconds(cfg, "image") == 60
        assert config_validation.publisher_ready_timeout_seconds(cfg, "video") == 180

    def test_missing_individual_timeout_uses_its_default(self):
        cfg = _valid()
        cfg["publisher"].pop("video_ready_timeout_seconds")
        assert config_validation.validate_config(cfg) == []
        assert config_validation.publisher_ready_timeout_seconds(cfg, "video") == 180

    def test_custom_media_readiness_timeouts_are_selected(self):
        cfg = _valid()
        cfg["publisher"] = {
            "image_ready_timeout_seconds": 15,
            "video_ready_timeout_seconds": 240,
        }
        assert config_validation.validate_config(cfg) == []
        assert config_validation.publisher_ready_timeout_seconds(cfg, "image") == 15
        assert config_validation.publisher_ready_timeout_seconds(cfg, "video") == 240

    @pytest.mark.parametrize(
        ("field", "bad", "message"),
        (
            ("video_ready_timeout_seconds", 0, ">= 1"),
            ("video_ready_timeout_seconds", -10, ">= 1"),
            ("video_ready_timeout_seconds", "180", "integer"),
            ("image_ready_timeout_seconds", None, "integer"),
        ),
    )
    def test_invalid_media_readiness_timeout_is_rejected(self, field, bad, message):
        cfg = _valid()
        cfg["publisher"][field] = bad
        errors = config_validation.validate_config(cfg)
        assert any(
            f"publisher.{field}" in error and message in error
            for error in errors
        )

    def test_publisher_section_must_be_an_object(self):
        cfg = _valid()
        cfg["publisher"] = "slow"
        assert any(
            "publisher.(section)" in error and "object" in error
            for error in config_validation.validate_config(cfg)
        )

    def test_obsolete_attempt_cycle_only_warns(self):
        cfg = _valid()
        cfg["safety"] = dict(cfg["safety"])
        cfg["safety"]["max_posts_per_attempt_cycle"] = 1
        assert config_validation.validate_config(cfg) == []
        warnings = config_validation.config_warnings(cfg)
        assert any("max_posts_per_attempt_cycle" in w for w in warnings)
        assert any("deprecated" in w for w in warnings)

    def test_unknown_top_level_key_warns_only(self):
        cfg = _valid()
        cfg["mystery_section"] = True
        assert config_validation.validate_config(cfg) == []
        assert any("mystery_section" in w for w in config_validation.config_warnings(cfg))


class TestFailsBeforeBrowserStartup:
    def _invalid_config(self):
        cfg = _valid()
        cfg["posting"]["min_posts_per_day"] = 6
        cfg["posting"]["max_posts_per_day"] = 3
        cfg["posting"]["max_video_bytes"] = -1
        return json.dumps(cfg).encode("utf-8")

    def test_invalid_config_exits_before_command_dispatch(self, tmp_path, monkeypatch, capsys):
        """The authoritative acceptance test for Issue 1 #10:
        an invalid config must never reach browser/session/publishing startup."""
        (tmp_path / "config.json").write_bytes(self._invalid_config())
        monkeypatch.setattr(main, "BASE", tmp_path)

        boom = mock.MagicMock(side_effect=AssertionError("XSession must NOT start"))
        with mock.patch("publisher.x_publisher.XSession", new=boom), \
                mock.patch(
                    "main.setup_logging",
                    side_effect=AssertionError("logging must not start"),
                ), \
                mock.patch("sys.argv", ["main.py", "once"]):
            with pytest.raises(SystemExit) as exc:
                main.main()

        assert exc.value.code == 1
        boom.assert_not_called()
        out = capsys.readouterr().out
        assert "Invalid configuration" in out
        assert "posting.min_posts_per_day must be <= posting.max_posts_per_day" in out
        assert "posting.max_video_bytes must be >= 1" in out

    def test_main_load_config_rejects_bad_json(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(main, "BASE", tmp_path)
        with pytest.raises(SystemExit) as exc:
            main.load_config()
        assert exc.value.code == 1
        assert "not valid JSON" in capsys.readouterr().out


def test_stats_offline_accepts_absent_tracking(tmp_path, monkeypatch, capsys):
    """The optional tracking section must not reappear as a stats KeyError."""
    cfg = _valid()
    cfg.pop("tracking")
    cfg["paths"]["logs_dir"] = str(tmp_path / "logs")
    fake_db = mock.MagicMock()
    fake_db.follower_history.return_value = []
    monkeypatch.setattr(main, "_make_db", lambda _cfg: fake_db)
    with mock.patch("tracker.write_csv"):
        main.cmd_stats(cfg, offline=True)
    assert "No follower data" in capsys.readouterr().out
