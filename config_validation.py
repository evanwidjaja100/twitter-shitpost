"""Central, fail-fast loading and validation of the bot configuration.

Every safety-sensitive setting is checked here ONCE, at load time, before any
database is created, browser launched, network request made or post attempted.
A single ``validate_config`` call collects ALL problems so the operator sees an
actionable list in one pass instead of a confusing runtime crash.

Rules are derived from the actual ``config.example.json`` schema and from every
production call site (``main.py``, ``scheduler.py``, ``retention.py``,
``tracker.py``, the scrapers, ``publisher/x_publisher.py`` and
``publishing_lock.py``). Sections that production hard-indexes (``paths``,
``posting``, ``safety``, ``filters``, ``secrets``, ``x_sources``, ``youtube``)
are required; sections with defensive defaults (``tiktok``, ``tracking``,
``retention``) remain optional but must be objects when present.

Design choices:
* Overnight posting windows (start > end) are VALID by design.
* ``retention`` values are validated even when ``enabled == false`` because
  ``log_max_bytes``/``log_backup_count`` still bound log rotation, and flipping
  it on later must not suddenly crash cleanup with bad numbers.
* ``paths`` values are validated structurally (present, string) but file
  existence is deliberately NOT enforced here: availability is environment
  dependent and covered by ``main --selftest``. Enforcing it here would make
  every command - including offline ``--dry-run --seed-demo`` - fail when ffmpeg
  is missing even though no media work needs it.
* The optional ``publisher`` section has backward-compatible readiness
  defaults, so existing valid configs do not require migration.
* Unknown/obsolete keys are warned about (not errors) so old configs keep
  working. The obsolete ``safety.max_posts_per_attempt_cycle`` is the canonical
  example: it is ignored by every code path and only warned about here.
"""

import json
from pathlib import Path

_DEPRECATED_KEYS = {
    ("safety", "max_posts_per_attempt_cycle"):
        "no longer read: the daemon posts at most once per scheduled slot; "
        "remove this key (it is currently ignored)",
}

_REQUIRED_PATH_KEYS = (
    "ffmpeg",
    "ffprobe",
    "brave",
    "browser_profile",
    "assets_dir",
    "logs_dir",
    "db_file",
)

_POSTING_STYLES = ("title", "pool", "both")

DEFAULT_IMAGE_READY_TIMEOUT_SECONDS = 60
DEFAULT_VIDEO_READY_TIMEOUT_SECONDS = 180

# Maintained inventory of every nested value that production deliberately
# hard-indexes. Optional values are consumed with ``.get`` defaults instead.
# Tests remove every path in this tuple from the example configuration so a new
# hard access cannot silently drift away from validation again.
PRODUCTION_REQUIRED_PATHS = (
    *(f"paths.{key}" for key in _REQUIRED_PATH_KEYS),
    "posting.min_posts_per_day",
    "posting.max_posts_per_day",
    "posting.active_hours_start",
    "posting.active_hours_end",
    "posting.max_image_bytes",
    "posting.max_video_bytes",
    "posting.max_caption_len",
    "posting.caption_style",
    "posting.random_caption_chance",
    "posting.caption_pool",
    "safety.retry_backoff_minutes",
    "safety.stop_on_login_failure",
    "safety.max_daily_posts_absolute",
    "filters.cooldown_days",
    "filters.blocked_keywords",
    "youtube.clip_min_seconds",
    "youtube.clip_max_seconds",
)

_KNOWN_TOP_LEVEL = {
    "paths", "posting", "safety", "filters", "secrets",
    "x_sources", "tiktok", "youtube", "tracking", "retention", "publisher",
    "sources",
}

PRODUCTION_REQUIRED_SECTIONS = (
    "paths",
    "posting",
    "safety",
    "filters",
    "secrets",
    "youtube",
    "x_sources",
)


# ---------------------------------------------------------------- helpers


def _as_int(value, default=None):
    """Integral int/float; `bool` is a Python int subclass and is refused."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_string_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _add(errors, section, field, message):
    errors.append(f"{section}.{field} {message}")


def _require_dict(cfg, key, errors):
    """Return the section dict if usable, else record errors and return None."""
    value = cfg.get(key)
    if value is None:
        _add(errors, key, "(section)", "is required")
        return None
    if isinstance(value, dict):
        return value
    _add(errors, key, "(section)", f"must be an object, got {type(value).__name__}")
    return None


def _pos_int(errors, section, field, section_dict, minimum=1):
    if field not in section_dict:
        return None
    value = section_dict.get(field)
    if value is None:
        return None
    v = _as_int(value, None)
    if v is None:
        _add(errors, section, field, "must be an integer")
        return None
    if v < minimum:
        _add(errors, section, field, f"must be >= {minimum}")
        return None
    return v


def _number(errors, section, field, section_dict, minimum=0):
    if field not in section_dict:
        return None
    value = section_dict.get(field)
    if value is None:
        return None
    if not _is_number(value):
        _add(errors, section, field, "must be a number")
        return None
    if value < minimum:
        _add(errors, section, field, f"must be >= {minimum}")
        return None
    return value


def _bool_field(errors, section, field, section_dict):
    value = section_dict.get(field)
    if value is not None and not isinstance(value, bool):
        _add(errors, section, field,
             f"must be a boolean true/false, got {type(value).__name__}")
        return None
    return value


def _account_list(errors, section, field, value):
    if value is None:
        return
    if not isinstance(value, list):
        _add(errors, section, field, "must be a list of strings")
        return
    for i, entry in enumerate(value):
        if not isinstance(entry, str):
            _add(errors, section, field, f"item {i} must be a string")


def _scraper_common(errors, name, section):
    """Shared scalar checks for the tiktok / x_sources scraper sections."""
    _pos_int(errors, name, "max_posts_per_account", section, minimum=1)
    _pos_int(errors, name, "scrolls", section, minimum=0)
    _pos_int(errors, name, "min_likes", section, minimum=0)
    _account_list(errors, name, "accounts", section.get("accounts"))


def publisher_ready_timeout_seconds(cfg: dict, media_kind: str) -> int:
    """Return the validated media-specific X readiness maximum.

    The section and either field may be absent in an older config. Invalid
    explicit values are rejected by :func:`validate_config`; this helper still
    falls back defensively when called with an unvalidated mapping.
    """
    field = (
        "video_ready_timeout_seconds"
        if media_kind == "video"
        else "image_ready_timeout_seconds"
    )
    default = (
        DEFAULT_VIDEO_READY_TIMEOUT_SECONDS
        if media_kind == "video"
        else DEFAULT_IMAGE_READY_TIMEOUT_SECONDS
    )
    section = cfg.get("publisher", {})
    if not isinstance(section, dict):
        return default
    return _as_int(section.get(field), default)


def _require_production_fields(cfg, errors):
    """Reject a missing/null hard-indexed leaf with one actionable error."""
    for path in PRODUCTION_REQUIRED_PATHS:
        section, field = path.split(".", 1)
        holder = cfg.get(section)
        if isinstance(holder, dict) and (
            field not in holder or holder[field] is None
        ):
            _add(errors, section, field, "is required")


# --------------------------------------------------------------- validator


def validate_config(cfg: dict) -> list[str]:
    """Return a list of human-readable problems (empty => configuration valid)."""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return [f"configuration root must be an object, got {type(cfg).__name__}"]

    _require_production_fields(cfg, errors)

    # ------------------------------------------------------------- paths
    paths = _require_dict(cfg, "paths", errors)
    if paths is not None:
        for key in _REQUIRED_PATH_KEYS:
            value = paths.get(key)
            if value is None:
                continue
            elif not isinstance(value, str) or not value.strip():
                _add(errors, "paths", key, "must be a non-empty string path")

    # ------------------------------------------------------------- posting
    posting = _require_dict(cfg, "posting", errors)
    if posting is not None:
        min_p = _pos_int(errors, "posting", "min_posts_per_day", posting, minimum=0)
        max_p = _pos_int(errors, "posting", "max_posts_per_day", posting, minimum=1)
        if min_p is not None and max_p is not None and min_p > max_p:
            _add(errors, "posting", "min_posts_per_day",
                 "must be <= posting.max_posts_per_day")

        for field in ("active_hours_start", "active_hours_end"):
            if posting.get(field) is None:
                continue
            h = _as_int(posting.get(field), None)
            if h is None:
                _add(errors, "posting", field, "must be an integer hour 0-23")
            elif not 0 <= h <= 23:
                _add(errors, "posting", field, "must be in the range 0-23")
        # Overnight windows (start > end) are intentional and never rejected.

        for field in ("max_image_bytes", "max_video_bytes"):
            _pos_int(errors, "posting", field, posting, minimum=1)

        cap = _as_int(posting.get("max_caption_len"), None)
        if posting.get("max_caption_len") is not None and (cap is None or cap < 1):
            _add(errors, "posting", "max_caption_len", "must be a positive integer")

        style = posting.get("caption_style")
        if style is not None and not (
            isinstance(style, str) and style in _POSTING_STYLES
        ):
            _add(errors, "posting", "caption_style", f"must be one of {_POSTING_STYLES}")
        chance = posting.get("random_caption_chance")
        if chance is not None and (not _is_number(chance) or not 0 <= chance <= 1):
            _add(errors, "posting", "random_caption_chance",
                 "must be a number between 0 and 1")
        pool = posting.get("caption_pool")
        if pool is not None and not _is_string_list(pool):
            _add(errors, "posting", "caption_pool", "must be a list of strings")

    # ----------------------------------------------------------- publisher
    publisher = cfg.get("publisher")
    if publisher is not None:
        if not isinstance(publisher, dict):
            _add(errors, "publisher", "(section)",
                 f"must be an object, got {type(publisher).__name__}")
        else:
            for field in (
                "image_ready_timeout_seconds",
                "video_ready_timeout_seconds",
            ):
                if field not in publisher:
                    continue
                value = publisher[field]
                parsed = _as_int(value, None)
                if parsed is None:
                    _add(errors, "publisher", field, "must be an integer")
                elif parsed < 1:
                    _add(errors, "publisher", field, "must be >= 1")

    # ------------------------------------------------------------- safety
    safety = _require_dict(cfg, "safety", errors)
    if safety is not None:
        _pos_int(errors, "safety", "max_daily_posts_absolute", safety, minimum=0)
        _number(errors, "safety", "retry_backoff_minutes", safety, minimum=0)
        _pos_int(errors, "safety", "max_daemon_restarts", safety, minimum=1)
        _bool_field(errors, "safety", "stop_on_login_failure", safety)

    # ------------------------------------------------------------- filters
    filters = _require_dict(cfg, "filters", errors)
    if filters is not None:
        _number(errors, "filters", "cooldown_days", filters, 0)
        blocked = filters.get("blocked_keywords")
        if blocked is not None and not _is_string_list(blocked):
            _add(errors, "filters", "blocked_keywords", "must be a list of strings")

    # ------------------------------------------------------------- secrets
    secrets = _require_dict(cfg, "secrets", errors)
    if secrets is not None:
        for key, value in secrets.items():
            if not isinstance(value, str):
                _add(errors, "secrets", key,
                     f"must be a string, got {type(value).__name__}")

    # ------------------------------------------------------------- youtube
    yt = _require_dict(cfg, "youtube", errors)
    if yt is not None:
        _bool_field(errors, "youtube", "shorts_feed", yt)
        _pos_int(errors, "youtube", "max_items_per_channel", yt, minimum=1)
        _pos_int(errors, "youtube", "min_views", yt, minimum=0)
        _number(errors, "youtube", "max_age_days", yt, 0)
        clip_min = _number(errors, "youtube", "clip_min_seconds", yt, 0)
        clip_max = _number(errors, "youtube", "clip_max_seconds", yt, 1)
        if clip_min is not None and clip_max is not None and clip_min > clip_max:
            _add(errors, "youtube", "clip_min_seconds",
                 "must be <= youtube.clip_max_seconds")
        _number(errors, "youtube", "max_source_video_minutes", yt, 1)
        channels = yt.get("channels")
        if channels is not None:
            if not isinstance(channels, list):
                _add(errors, "youtube", "channels", "must be a list")
            else:
                for i, ch in enumerate(channels):
                    if not isinstance(ch, dict):
                        _add(errors, "youtube", f"channels[{i}]",
                             f"must be an object, got {type(ch).__name__}")
                        continue
                    if ch.get("name") is not None and not isinstance(ch["name"], str):
                        _add(errors, "youtube", f"channels[{i}].name", "must be a string")
                    if ch.get("handle") is not None and not isinstance(ch["handle"], str):
                        _add(errors, "youtube", f"channels[{i}].handle", "must be a string")
                    if ch.get("playlist_id") is not None and not isinstance(
                        ch["playlist_id"], str
                    ):
                        _add(errors, "youtube", f"channels[{i}].playlist_id", "must be a string")

    # ------------------------------------------------------------- x_sources
    xs = _require_dict(cfg, "x_sources", errors)
    if xs is not None:
        _scraper_common(errors, "x_sources", xs)

    # --------------------------------------------------------- tiktok (optional)
    tt = cfg.get("tiktok")
    if tt is not None:
        if not isinstance(tt, dict):
            _add(errors, "tiktok", "(section)",
                 f"must be an object, got {type(tt).__name__}")
        else:
            _scraper_common(errors, "tiktok", tt)
            _bool_field(errors, "tiktok", "foryou", tt)

    # ------------------------------------------------------- tracking (optional)
    tracking = cfg.get("tracking")
    if tracking is not None:
        if not isinstance(tracking, dict):
            _add(errors, "tracking", "(section)",
                 f"must be an object, got {type(tracking).__name__}")
        else:
            _number(errors, "tracking", "follow_check_hours", tracking, 0)
            handle = tracking.get("own_handle")
            if handle is not None and not isinstance(handle, str):
                _add(errors, "tracking", "own_handle",
                     f"must be a string, got {type(handle).__name__}")

    # ---------------------------------------------------- retention (optional)
    retention = cfg.get("retention")
    if retention is not None:
        if not isinstance(retention, dict):
            _add(errors, "retention", "(section)",
                 f"must be an object, got {type(retention).__name__}")
        else:
            _bool_field(errors, "retention", "enabled", retention)
            _number(errors, "retention", "media_days", retention, minimum=1)
            _number(errors, "retention", "temp_hours", retention, minimum=1)
            _number(errors, "retention", "log_max_bytes", retention, minimum=1)
            _pos_int(errors, "retention", "log_backup_count", retention, minimum=1)
            _number(errors, "retention", "interval_hours", retention, minimum=1)

    return errors


class ConfigurationError(Exception):
    """A config file could not be read, parsed, or validated."""

    def __init__(self, kind: str, problems: list[str]):
        self.kind = kind
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def load_validated_config(config_path) -> dict:
    """Read JSON and validate it through the one authoritative config path."""
    path = Path(config_path)
    if not path.exists():
        raise ConfigurationError(
            "missing", [f"{path.name} not found — copy config.example.json to config.json"]
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "json", [f"{path.name} is not valid JSON: {exc}"]
        ) from exc
    problems = validate_config(cfg)
    if problems:
        raise ConfigurationError("validation", problems)
    return cfg


def configuration_error_lines(exc: ConfigurationError) -> list[str]:
    """Stable operator-facing formatting shared by both entrypoints."""
    if exc.kind == "validation":
        return [
            "Invalid configuration: (fix config.json and retry)",
            *(f"  - {problem}" for problem in exc.problems),
        ]
    return [f"ERROR: {problem}" for problem in exc.problems]


def config_warnings(cfg) -> list:
    """Non-fatal diagnostics (deprecated/obsolete keys). Never block startup,
    preserving backward compatibility for old configs."""
    warnings: list[str] = []
    for (section, key), message in _DEPRECATED_KEYS.items():
        holder = cfg.get(section)
        if isinstance(holder, dict) and key in holder:
            warnings.append(f"{section}.{key} is deprecated: {message}")
    unknown_top = set(cfg) - _KNOWN_TOP_LEVEL
    if unknown_top:
        warnings.append(
            "unknown top-level config keys ignored: " + ", ".join(sorted(unknown_top))
        )
    return warnings
