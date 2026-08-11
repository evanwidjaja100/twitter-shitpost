"""X Shitpost Bot — orchestrator.

Commands:
  python main.py login        one-time manual login
  python main.py sources      scrape all sources once, print summary
  python main.py once         pick best item and post it now
  python main.py daemon       run the scheduler loop (posts 3-6/day)
  python main.py --selftest   environment checks (exit 0/1)
  python main.py --dry-run --seed-demo   end-to-end selection test, no posting
"""

import argparse
import logging
import logging.handlers
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

import retention

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _is_stdout_handler(h) -> bool:
    """Whether `h` is a real console handler writing to sys.stdout.

    RotatingFileHandler/FileHandler are subclasses of StreamHandler, so checking
    ``isinstance(h, logging.StreamHandler)`` alone would mistake the bot.log
    file handler for a console handler. A genuine console handler targets
    sys.stdout and is not a file handler.
    """
    return (
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stdout
    )


def load_config() -> dict:
    from config_validation import (
        ConfigurationError,
        config_warnings,
        configuration_error_lines,
        load_validated_config,
    )

    cfg_path = BASE / "config.json"
    try:
        cfg = load_validated_config(cfg_path)
    except ConfigurationError as exc:
        for line in configuration_error_lines(exc):
            print(line)
        sys.exit(1)

    for warning in config_warnings(cfg):
        print(f"WARNING: {warning}")
    return cfg


def setup_logging(cfg: dict):
    """Configure bounded log files (bot.log + alerts.log) and stdout.

    Size-bounded rotation keeps disk use bounded on a long-running daemon,
    independent of uptime. Idempotent: calling it more than once never adds
    duplicate handlers, so messages are never duplicated.
    """
    log_dir = Path(str(BASE / cfg["paths"]["logs_dir"]))
    log_dir.mkdir(parents=True, exist_ok=True)
    max_bytes, backups = retention.log_settings(cfg)
    fmt = logging.Formatter(LOG_FORMAT)

    root = logging.getLogger()
    if not any(
        isinstance(h, logging.FileHandler)
        and Path(getattr(h, "baseFilename", "")).name == "bot.log"
        for h in root.handlers
    ):
        fh = logging.handlers.RotatingFileHandler(
            str(log_dir / "bot.log"), maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(_is_stdout_handler(h) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    root.setLevel(logging.INFO)


def _alerts_handler(cfg: dict):
    log_dir = Path(str(BASE / cfg["paths"]["logs_dir"]))
    log_dir.mkdir(parents=True, exist_ok=True)
    max_bytes, backups = retention.log_settings(cfg)
    return logging.handlers.RotatingFileHandler(
        str(log_dir / "alerts.log"), maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )


def alert(cfg: dict, message: str):
    """Append a timestamped alert to the bounded alerts.log.

    Writes through the rotating "alerts" logger (same maxBytes/backupCount as
    bot.log) so alerts.log stays bounded; the message also propagates to the
    normal bot.log/stdout handlers like before.
    """
    logger = logging.getLogger("alerts")
    if not any(
        isinstance(h, logging.FileHandler)
        and Path(getattr(h, "baseFilename", "")).name == "alerts.log"
        for h in logger.handlers
    ):
        h = _alerts_handler(cfg)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    logger.error(message)


def _best_effort_alert(cfg: dict, message: str, log=None):
    """Send a diagnostic alert without letting alert failure break recovery.

    The daemon supervisor treats alerts as best-effort diagnostics: if writing
    the alert itself fails (disk full, unwritable logs dir, permission error,
    ...) the failure is logged and swallowed so it can never replace the
    primary daemon exception the supervisor is handling, and recovery/retry
    logic proceeds normally. Only ordinary ``Exception`` failures of ``alert``
    are contained — ``KeyboardInterrupt``/``SystemExit`` still propagate.
    """
    try:
        alert(cfg, message)
    except Exception:
        (log or logging.getLogger("daemon")).warning(
            "failed to send daemon alert: %s", message, exc_info=True
        )


# ------------------------------------------------------------- selection

def prepare_item(item: dict, cfg: dict, paths: dict, session=None):
    """Download + prepare media for an item. Returns local media path or None."""
    from pipeline import media as m
    from pipeline.filters import image_passes_dims

    ffmpeg = str(BASE / paths["ffmpeg"])
    ffprobe = str(BASE / paths["ffprobe"])
    assets = Path(str(BASE / paths["assets_dir"]))
    src_dir = assets / item["source"]
    src_dir.mkdir(parents=True, exist_ok=True)

    clip_max = cfg["youtube"]["clip_max_seconds"]
    max_image_bytes = cfg["posting"]["max_image_bytes"]
    max_video_bytes = cfg["posting"]["max_video_bytes"]

    try:
        if item["media_path"]:  # already downloaded (demo)
            src = item["media_path"]
        elif item["source"] == "x" and item["media_url"]:
            from scrapers.x_scraper import download_media

            max_bytes = max_video_bytes if item["kind"] == "video" else max_image_bytes
            src = download_media(session, item, str(src_dir), max_bytes=max_bytes)
            if not src:
                return None
        elif item["kind"] == "video":
            src = m.ytdl_download(
                item["source_url"],
                str(src_dir),
                max_video_bytes,
                ffmpeg_dir=str(BASE / paths["ffmpeg"]).rsplit("/", 1)[0],
            )
        elif item["kind"] == "image":
            src = m.download(
                item["media_url"],
                str(src_dir / f"{item['source_id']}_raw"),
                max_bytes=max_image_bytes,
            )
        else:
            return None
    except m.MediaError as e:
        logging.getLogger("select").warning("download failed: %s", e)
        return None

    try:
        if item["kind"] == "image":
            out = m.prepare_image(src, str(src_dir), max_image_bytes)
            if not image_passes_dims(out, 0, 0):
                return None
            return m.validate_final_media_size(out, "image", max_image_bytes, max_video_bytes)
        else:  # video
            if item["source"] == "x":
                try:
                    dur = m.video_duration(ffprobe, src)
                except m.MediaError:
                    return None
                if dur <= clip_max:
                    # Duration alone is NOT a byte-limit guarantee: the actual
                    # file size must already fit, else the source may not be
                    # returned untouched.
                    if Path(src).stat().st_size <= max_video_bytes:
                        return m.validate_final_media_size(src, "video", max_image_bytes, max_video_bytes)
            return m.validate_final_media_size(
                m.trim_video(
                    src, str(src_dir), ffmpeg, ffprobe, clip_max,
                    cfg["youtube"]["clip_min_seconds"],
                    max_bytes=max_video_bytes,
                ),
                "video", max_image_bytes, max_video_bytes,
            )
    except m.MediaError as e:
        logging.getLogger("select").warning("prepare failed: %s", e)
        return None


def _scrape_source(name: str, scrape):
    """Run one source without hiding unrecoverable browser-session failures."""
    try:
        return scrape()
    except Exception as exc:
        from publisher.x_publisher import BrowserSessionError, is_closed_context_error

        if isinstance(exc, BrowserSessionError) or is_closed_context_error(exc):
            raise
        logging.getLogger("select").warning(
            "%s source failed (%s); continuing with remaining sources: %s",
            name,
            type(exc).__name__,
            exc,
        )
        return []


def pick_item(cfg: dict, db, session=None) -> dict | None:
    """Scrape all sources, dedup against history, and return the single best
    postable item. Nothing is recorded here — dedup happens only after a
    publication is confirmed successful."""
    from pipeline.filters import title_contains_blocked_keywords

    log = logging.getLogger("select")
    secrets = cfg["secrets"]
    blocked = cfg["filters"]["blocked_keywords"]
    items: list[dict] = []

    tiktok = cfg.get("tiktok", {})
    if (tiktok.get("foryou") or tiktok.get("accounts")) and session is not None:
        from scrapers import tiktok_scraper

        items += _scrape_source(
            "TikTok",
            lambda: tiktok_scraper.scrape(
                session, tiktok, str(BASE / cfg["paths"]["assets_dir"])
            ),
        )

    if secrets.get("youtube_api_key"):
        from googleapiclient.discovery import build

        from scrapers import youtube_scraper

        items += _scrape_source(
            "YouTube API",
            lambda: youtube_scraper.scrape(
                build(
                    "youtube",
                    "v3",
                    developerKey=secrets["youtube_api_key"],
                    cache_discovery=False,
                ),
                cfg["youtube"],
            ),
        )

    if cfg.get("youtube", {}).get("shorts_feed") and session is not None:
        from scrapers import youtube_scraper

        items += _scrape_source(
            "YouTube Shorts",
            lambda: youtube_scraper.scrape_shorts(session, cfg["youtube"]),
        )

    if cfg["x_sources"].get("accounts") and session is not None:
        from scrapers import x_scraper

        items += _scrape_source(
            "X",
            lambda: x_scraper.scrape(
                session,
                cfg["x_sources"],
                str(BASE / cfg["paths"]["assets_dir"]),
            ),
        )

    log.info("scraped %d candidates", len(items))

    candidates = [
        it for it in items
        if not db.is_source_seen(it["source"], it["source_id"])
        and not title_contains_blocked_keywords(it.get("title", ""), blocked)
    ]
    candidates.sort(key=lambda it: it["score"], reverse=True)

    cooldown = cfg["filters"]["cooldown_days"]
    for item in candidates:
        media_path = prepare_item(item, cfg, cfg["paths"], session)
        if not media_path:
            continue
        from pipeline.media import hash_file

        h = hash_file(media_path)
        if db.is_hash_seen(h, cooldown):
            continue

        from pipeline import perceptual

        ffmpeg_exe = str(BASE / cfg["paths"]["ffmpeg"]) if cfg.get("paths", {}).get("ffmpeg") else None
        ffprobe_exe = str(BASE / cfg["paths"]["ffprobe"]) if cfg.get("paths", {}).get("ffprobe") else None
        media_kind = item.get("kind", "image")
        fps = perceptual.medium_fingerprints(
            media_path,
            media_kind,
            ffmpeg_exe,
            ffprobe_exe,
        )
        if fps:
            history = db.fingerprint_groups(media_kind, cooldown)
            if any(
                perceptual.is_near_duplicate(fps, group["fingerprints"])
                for group in history
            ):
                continue
        item["_fingerprints"] = fps

        from pipeline.filters import pick_caption

        caption = pick_caption(
            item.get("title", ""),
            cfg["posting"]["caption_style"],
            cfg["posting"]["caption_pool"],
            cfg["posting"]["random_caption_chance"],
            cfg["posting"]["max_caption_len"],
        )
        item["_media_path"] = media_path
        item["_caption"] = caption
        item["_hash"] = h
        return item
    return None


def mark_item_published(db, item) -> None:
    """Finalize a positively confirmed successful post in one transaction.

    Writes post history plus source/media-hash dedup together (the scheduler's
    per-window success count is derived from the posts table, so it is updated
    in the same atomic write). Shared by every publishing path — manual and
    daemon — so they cannot diverge.
    """
    finalize_args = dict(
        caption=item["_caption"],
        media_path=item["_media_path"],
        source=item["source"],
        source_id=item["source_id"],
        source_url=item["source_url"],
        content_hash=item.get("_hash"),
        fingerprints=item.get("_fingerprints"),
    )
    if item.get("_fingerprints") and item.get("kind") in ("image", "video"):
        finalize_args["media_kind"] = item["kind"]
    db.finalize_successful_post(**finalize_args)


def post_selected_item(session, item: dict, cfg: dict) -> dict:
    """Publish one selected item with its validated media readiness budget."""
    from config_validation import (
        publisher_post_click_timeout_seconds,
        publisher_ready_timeout_seconds,
    )

    media_kind = item.get("kind", "image")
    return session.post(
        item["_caption"],
        [item["_media_path"]],
        media_kind=media_kind,
        ready_timeout_s=publisher_ready_timeout_seconds(cfg, media_kind),
        post_click_timeout_s=publisher_post_click_timeout_seconds(cfg),
    )


# ------------------------------------------------------------- commands

# Last disk-maintenance timestamp (module-local: the daemon is single-process
# and single-threaded, so an in-memory state is sufficient; the throttle keeps
# cleanup to at most once per configured interval).
_RETENTION_STATE: dict = {}

def cmd_login():
    import login

    sys.exit(login.main())


def cmd_sources(cfg):
    from publisher.x_publisher import XSession
    from publishing_lock import browser_profile_lock

    # Browser-profile lock: sources opens the persistent Brave profile but
    # never publishes, so it needs the browser lock only (order: browser
    # lock -> browser startup).
    with browser_profile_lock(cfg, "sources"):
        db = _make_db(cfg)
        session = XSession(cfg["paths"])
        session.start()
        try:
            result = pick_item(cfg, db, session)
        finally:
            session.stop()
    if result is None:
        print("No postable item right now (no accounts configured, or everything already posted).")
        return
    print(f"Top pick: [{result['source']}] score={result['score']:.0f}")
    print(f"  title : {result.get('title', '')[:80]}")
    print(f"  url   : {result['source_url']}")
    print(f"  media : {result['_media_path']}")
    print(f"  caption: {result['_caption']}")


def cmd_once(cfg):
    from publisher.x_publisher import XSession
    from publishing_lock import browser_profile_lock, publishing_lock

    # Lock order is globally fixed: publishing -> browser profile -> browser.
    with publishing_lock(cfg, "once"):
        with browser_profile_lock(cfg, "once"):
            db = _make_db(cfg)
            session = XSession(cfg["paths"])
            session.start()
            try:
                item = pick_item(cfg, db, session)
                if item is None:
                    alert(cfg, "no item available to post")
                    return
                res = post_selected_item(session, item, cfg)
                if res["ok"]:
                    mark_item_published(db, item)
                    logging.getLogger("post").info("POSTED: %s | %s", item["source_url"], item["_caption"])
                else:
                    db.add_post(
                        item["_caption"], item["_media_path"], item["source"],
                        item["source_url"], item["_hash"], "failed", res["reason"],
                    )
                    alert(cfg, f"post failed: {res['reason']} | {item['source_url']}")
            finally:
                session.stop()


def cmd_stats(cfg, offline: bool):
    from tracker import get_follower_count, maybe_check_followers, write_csv

    db = _make_db(cfg)
    tracking = cfg.get("tracking", {})
    handle = tracking.get("own_handle", "average_pocka")
    session = None
    if not offline:
        from publisher.x_publisher import XSession
        from publishing_lock import browser_profile_lock

        # Online stats opens the persistent Brave profile for the follower
        # check: browser-profile lock only (order: browser lock -> browser).
        with browser_profile_lock(cfg, "stats"):
            session = XSession(cfg["paths"])
            session.start()
            try:
                maybe_check_followers(db, cfg, session)
            finally:
                session.stop()
    history = db.follower_history()
    csv_path = BASE / cfg["paths"]["logs_dir"] / "followers.csv"
    write_csv(str(csv_path), history)
    if not history:
        print("No follower data yet. First check happens automatically once the")
        print("daemon runs while logged in (or re-run `main.py stats` online).")
        return
    print(f"{'time (UTC)':<20} followers")
    for ts, count in history:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts)):<20} {count}")
    if len(history) >= 2:
        delta = history[-1][1] - history[0][1]
        days = max((history[-1][0] - history[0][0]) / 86400.0, 1 / 1440.0)
        print(f"\ngrowth: {delta:+d} followers over {days:.1f} days "
              f"({delta / days:+.1f}/day)")
    print(f"\nhistory CSV: {csv_path}")


def attempt_slot(cfg, db, session, now=None, rng=None) -> dict:
    """One scheduled-Slot posting attempt, gated by a fresh quota recheck.

    Called by the daemon immediately after `scheduler.sleep_until()` and
    BEFORE candidate selection / X posting. Slots computed earlier can go
    stale (another process posts, a manual post happens, a previous scheduled
    post succeeded, the day or window rolled over, the persisted target was
    reached...) so this function recomputes `check_posting_limits()` against
    the live database. If any cap is reached the attempt is vetoed: no item is
    picked, `session.post()` is never called and no success/dedup state is
    written.

    Returns a small outcome dict:
      {"outcome": "vetoed", "reason": "window_inactive"|"target_reached"|"daily_absolute_cap"}
      {"outcome": "no_item"}
      {"outcome": "posted", "source_url": ...}
      {"outcome": "failed", "reason": <publisher reason>}
    """
    import scheduler

    posting = cfg["posting"]
    safety = cfg["safety"]
    state = scheduler.check_posting_limits(
        db,
        posting["min_posts_per_day"],
        posting["max_posts_per_day"],
        posting["active_hours_start"],
        posting["active_hours_end"],
        max_absolute=safety["max_daily_posts_absolute"],
        now=now,
        rng=rng,
    )
    if not state["allowed"]:
        reason = state["reason"] or "limit_reached"
        logging.getLogger("daemon").info("skipping scheduled slot: %s", reason)
        return {"outcome": "vetoed", "reason": reason, "state": state}

    item = pick_item(cfg, db, session)
    if item is None:
        logging.getLogger("daemon").info("no item found; skipping slot")
        return {"outcome": "no_item", "state": state}

    res = post_selected_item(session, item, cfg)
    if res["ok"]:
        mark_item_published(db, item)
        logging.getLogger("daemon").info("POSTED: %s", item["source_url"])
        return {"outcome": "posted", "source_url": item["source_url"], "state": state}

    db.add_post(
        item["_caption"], item["_media_path"], item["source"],
        item["source_url"], item["_hash"], "failed", res["reason"],
    )
    alert(cfg, f"post failed: {res['reason']} | {item['source_url']}")
    return {"outcome": "failed", "reason": res["reason"], "state": state}


def cmd_daemon(cfg):
    from publishing_lock import browser_profile_lock, publishing_lock

    # Lock order is globally fixed: publishing -> browser profile -> browser.
    # Both ownership locks are acquired ONCE here and held for the whole
    # supervisor lifetime, so a competing `once`/`sources`/`stats`/`login`
    # cannot slip in while the daemon is between internal browser restarts.
    with publishing_lock(cfg, "daemon"):
        with browser_profile_lock(cfg, "daemon"):
            supervise_daemon(cfg)


class DaemonStop(Exception):
    """Intentional daemon stop requested by the publisher/configuration.

    Raised by the daemon loop when e.g. a login/captcha failure occurs with
    ``safety.stop_on_login_failure == true``. This is a deliberate shutdown,
    not a transient crash — the supervisor must NOT restart on it.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class NoSlotsNoticeState:
    """Ephemeral transition state for the daemon's normal no-slot notice."""

    def __init__(self):
        self.window_key = None
        self.no_slots = False

    def observe_no_slots(self, logger, window_key: str) -> None:
        first_for_state = not self.no_slots or self.window_key != window_key
        log_method = logger.info if first_for_state else logger.debug
        log_method("no remaining posting slots in the current window; waiting")
        self.window_key = window_key
        self.no_slots = True

    def observe_available_slots(self) -> None:
        self.window_key = None
        self.no_slots = False


def _logical_posting_window_key(posting: dict, now: datetime | None = None) -> str:
    """Reuse scheduler semantics to identify the logical window for logging."""
    import scheduler

    now = now or datetime.now()
    start = scheduler.window_start(
        posting["active_hours_start"],
        posting["active_hours_end"],
        now,
    )
    return scheduler.window_id(start)


def supervise_daemon(cfg):
    """Run the daemon, restarting it on unexpected recoverable exceptions.

    Only ordinary ``Exception`` subclass failures are retried. ``DaemonStop``
    (intentional login/captcha shutdown), ``KeyboardInterrupt`` and
    ``SystemExit`` end immediately and are never auto-restarted.

    Recovery model:
      - unexpected exception -> log stack trace -> best-effort alert -> the
        current browser session is stopped by ``_run_daemon``'s ``finally`` ->
        wait with bounded, growing backoff -> construct a fresh session -> run
        the loop again. Both global ownership locks (publishing + browser
        profile) stay held for the whole supervisor call (they are acquired in
        ``cmd_daemon``), so no other process can enter during recovery backoff.
      - a fully completed daemon loop iteration resets the consecutive-failure
        counter (successful daemon operation for a meaningful period).
      - after ``max_daemon_restarts`` consecutive failures the exception is
        re-raised so the process exits non-zero, letting Task Scheduler / a
        process supervisor act as the second recovery layer.
    """
    log = logging.getLogger("daemon")
    safety = cfg.get("safety", {})
    max_restarts = max(int(safety.get("max_daemon_restarts", 5)), 1)
    base_seconds = max(float(safety.get("retry_backoff_minutes", 30)) * 60.0, 5.0)
    cap_seconds = base_seconds * 10.0
    state = {"consecutive": 0}

    def clear_heartbeat():
        if state["consecutive"]:
            state["consecutive"] = 0

    while True:
        try:
            _run_daemon(cfg, on_success=clear_heartbeat)
            return  # reached an intentional stop -> clean, no restart
        except DaemonStop as stop:
            log.info("daemon stopping intentionally: %s", stop.reason)
            return
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except Exception as exc:
            state["consecutive"] += 1
            n = state["consecutive"]
            log.exception("daemon failed unexpectedly (consecutive failures: %d)", n)
            _best_effort_alert(cfg, f"daemon crashed unexpectedly (attempt {n}): {exc!r}")
            if n >= max_restarts:
                log.critical("daemon failed %d consecutive times; giving up", n)
                _best_effort_alert(cfg, "daemon gave up after repeated unexpected failures")
                raise  # non-zero process exit -> outer recovery layer
            delay = min(base_seconds * n, cap_seconds)
            log.warning("restarting daemon in %d seconds (failure %d)", delay, n)
            time.sleep(delay)


def _run_daemon(cfg, on_success=None):
    """Run one daemon session: DB + browser + loop until stop or failure.

    The browser session is always stopped on the way out (success, intentional
    stop, or unexpected crash) via ``finally``; ``on_success`` fires after each
    fully completed loop iteration so a supervisor can reset its failure
    counter only after genuinely productive work.
    """
    from publisher.x_publisher import XSession

    db = _make_db(cfg)
    session = XSession(cfg["paths"])
    session._daemon_no_slots_notice = NoSlotsNoticeState()
    log = logging.getLogger("daemon")
    try:
        session.start()
        log.info("daemon session started")
        while True:
            _daemon_iteration(cfg, db, session)
            if on_success is not None:
                on_success()
    finally:
        try:
            session.stop()
        except Exception:
            log.warning("error stopping daemon session (best effort)", exc_info=True)


def _daemon_iteration(cfg, db, session, no_slots_notice=None):
    """One pass of the daemon main loop (followers + current posting window).

    Returns when the pass is complete without raising; raises
    :class:`DaemonStop` for an intentional configured shutdown (login/captcha
    with ``stop_on_login_failure``). Any other exception is an unexpected
    daemon failure for the supervisor to recover from.
    """
    from tracker import maybe_check_followers
    import scheduler

    log = logging.getLogger("daemon")
    posting = cfg["posting"]
    safety = cfg["safety"]
    if no_slots_notice is None:
        no_slots_notice = getattr(session, "_daemon_no_slots_notice", None)
        if not isinstance(no_slots_notice, NoSlotsNoticeState):
            no_slots_notice = NoSlotsNoticeState()
            session._daemon_no_slots_notice = no_slots_notice

    # Disk maintenance runs at a safe point (no media operation is active and
    # the publishing/browser locks are held) and is throttled to at most once
    # per configured interval. It is best-effort: failures are logged by
    # retention.maybe_run_retention and can never interrupt posting.
    retention.maybe_run_retention(BASE, cfg, _RETENTION_STATE)

    maybe_check_followers(db, cfg, session)
    times = scheduler.remaining_slots(
        db,
        posting["min_posts_per_day"],
        posting["max_posts_per_day"],
        posting["active_hours_start"],
        posting["active_hours_end"],
        max_absolute=safety["max_daily_posts_absolute"],
    )
    if not times:
        no_slots_notice.observe_no_slots(
            log, _logical_posting_window_key(posting)
        )
        time.sleep(60)
        return
    no_slots_notice.observe_available_slots()
    for t in times:
        scheduler.sleep_until(t)
        # Fresh quota re-check before any pick/post: precomputed slots are
        # only an estimate; the DB decides now.
        result = attempt_slot(cfg, db, session)
        if result["outcome"] == "failed":
            if result["reason"] in ("login", "captcha") and safety["stop_on_login_failure"]:
                _best_effort_alert(cfg, "stopping daemon due to login/captcha failure")
                raise DaemonStop(result["reason"])
            time.sleep(safety["retry_backoff_minutes"] * 60)
    time.sleep(60)


def _make_db(cfg):
    from storage.db import Database

    return Database(str(BASE / cfg["paths"]["db_file"]))


# ------------------------------------------------------------- selftest / dry-run

def _demo_image() -> str:
    assets = BASE / "assets" / "demo"
    assets.mkdir(parents=True, exist_ok=True)
    out = assets / "demo1.jpg"
    if not out.exists():
        img = Image.new("RGB", (900, 700), (24, 24, 40))
        d = ImageDraw.Draw(img)
        d.text((350, 330), "demo shitpost", fill=(255, 255, 255))
        img.save(out, "JPEG", quality=90)
    return str(out)


def cmd_selftest(cfg) -> int:
    checks = []
    ok = True

    def check(name, passed, detail=""):
        nonlocal ok
        checks.append((name, passed, detail))
        if not passed:
            ok = False

    check("python>=3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    for mod in ("playwright", "googleapiclient", "yt_dlp", "PIL", "requests"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as e:
            check(f"import {mod}", False, str(e))

    check("config.json", cfg is not None)
    ffmpeg = BASE / cfg["paths"]["ffmpeg"]
    ffprobe = BASE / cfg["paths"]["ffprobe"]
    check("ffmpeg exists", ffmpeg.exists(), str(ffmpeg))
    check("ffprobe exists", ffprobe.exists(), str(ffprobe))
    brave = Path(str(BASE / cfg["paths"]["brave"]))
    check("brave exists", brave.exists(), str(brave))
    try:
        db = _make_db(cfg)
        check("database init", True, str(db.stats()))
    except Exception as e:
        check("database init", False, str(e))
    log_dir = BASE / cfg["paths"]["logs_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
    check("logs dir writable", log_dir.is_dir())

    missing = [k for k, v in cfg["secrets"].items() if not v]
    check("secrets (warn only)", True, f"missing (optional for now): {', '.join(missing) or 'none'}")

    for name, passed, detail in checks:
        print(f"[{'OK' if passed else 'FAIL'}] {name} {('— ' + detail) if detail else ''}")
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def cmd_dry_run(cfg, seed_demo: bool) -> int:
    """Full selection pipeline without posting."""
    db = _make_db(cfg)
    if seed_demo:
        demo = {
            "source": "demo",
            "source_id": "demo-1",
            "source_url": "https://example.invalid/demo",
            "title": "demo shitpost test item",
            "media_url": None,
            "media_path": _demo_image(),
            "score": 99999.0,
            "created_utc": time.time(),
            "nsfw": False,
            "kind": "image",
        }
        item = prepare_item(demo, cfg, cfg["paths"])
        if not item:
            print("DRY-RUN FAILED: demo item could not be prepared")
            return 1
        from pipeline.media import hash_file
        from pipeline.filters import pick_caption

        h = hash_file(item)
        caption = pick_caption(
            demo["title"], cfg["posting"]["caption_style"], cfg["posting"]["caption_pool"],
            cfg["posting"]["random_caption_chance"], cfg["posting"]["max_caption_len"],
        )
        print(f"[OK] demo media prepared: {item}")
        print(f"[OK] caption: {caption!r}")
        print(f"[OK] content hash: {h}")
        print("DRY-RUN PASSED")
        return 0

    print("Scraping real sources (requires credentials)...")
    item = pick_item(cfg, db)
    if item is None:
        print("No postable item found (credentials missing or queue empty).")
        print("DRY-RUN PASSED (selection pipeline ran cleanly)")
        return 0
    print(f"[OK] would post: {item['source']} score={item['score']:.0f}")
    print(f"     caption: {item['_caption']!r}")
    print(f"     media:   {item['_media_path']}")
    print("DRY-RUN PASSED")
    return 0


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="X Shitpost Bot")
    parser.add_argument("command", nargs="?", default=None,
                        help="login | sources | once | daemon | stats")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--offline", action="store_true",
                        help="stats: read stored data without opening a browser")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg)

    if args.selftest:
        sys.exit(cmd_selftest(cfg))
    if args.dry_run:
        sys.exit(cmd_dry_run(cfg, args.seed_demo))

    if args.command == "login":
        cmd_login()
    elif args.command == "sources":
        cmd_sources(cfg)
    elif args.command == "once":
        cmd_once(cfg)
    elif args.command == "daemon":
        cmd_daemon(cfg)
    elif args.command == "stats":
        cmd_stats(cfg, args.offline)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
