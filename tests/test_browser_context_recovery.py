"""Deterministic coverage for persistent Playwright context recovery.

All browser objects are fakes. These tests never launch Brave, access a site,
or publish anything.
"""

import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from playwright._impl._errors import TargetClosedError
from playwright.sync_api import Error as PlaywrightError

import main
from publisher.x_publisher import BrowserSessionError, XSession
from storage.db import Database


ROOT = Path(__file__).resolve().parent.parent


def _closed_error(operation="BrowserContext.new_page"):
    return TargetClosedError(
        f"{operation}: Target page, context or browser has been closed"
    )


class FakeBrowser:
    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected


class FakePage:
    def __init__(self, close_error=None):
        self.close_error = close_error
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeContext:
    def __init__(self, outcomes=(), connected=True, close_error=None):
        self.browser = FakeBrowser(connected)
        self.pages = []
        self.outcomes = list(outcomes)
        self.close_error = close_error
        self.new_page_calls = 0
        self.close_calls = 0
        self.init_scripts = []

    def new_page(self):
        self.new_page_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        self.close_calls += 1
        self.browser.connected = False
        if self.close_error is not None:
            raise self.close_error

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def cookies(self, urls=None):
        return []


class FakePlaywright:
    def __init__(self, context=None):
        self.context = context
        self.launch_calls = 0
        self.launch_kwargs = []
        self.stop_calls = 0
        self.chromium = SimpleNamespace(
            launch_persistent_context=self.launch_persistent_context
        )

    def launch_persistent_context(self, **kwargs):
        self.launch_calls += 1
        self.launch_kwargs.append(kwargs)
        if isinstance(self.context, BaseException):
            raise self.context
        return self.context

    def stop(self):
        self.stop_calls += 1


def _sync_factory(*drivers):
    manager = mock.Mock()
    manager.start.side_effect = list(drivers)
    factory = mock.Mock(return_value=manager)
    return factory


def _session_with(context, driver=None):
    session = XSession({"browser_profile": "profile", "brave": "brave"})
    session._context = context
    session._playwright = driver or FakePlaywright()
    return session


def _pick_cfg():
    return {
        "tiktok": {
            "foryou": True,
            "accounts": [],
            "min_likes": 0,
            "max_posts_per_account": 1,
            "scrolls": 0,
        },
        "secrets": {"youtube_api_key": ""},
        "youtube": {
            "shorts_feed": True,
            "min_views": 0,
            "max_items_per_channel": 1,
            "scrolls": 0,
        },
        "x_sources": {"accounts": []},
        "paths": {"assets_dir": "assets", "ffmpeg": "", "ffprobe": ""},
        "filters": {"blocked_keywords": [], "cooldown_days": 30},
        "posting": {
            "caption_style": "title",
            "caption_pool": [],
            "random_caption_chance": 0.0,
            "max_caption_len": 200,
        },
    }


def test_stale_context_before_new_page_is_cleaned_and_replaced():
    old = FakeContext(
        [_closed_error()],
        connected=False,
        close_error=_closed_error("BrowserContext.close"),
    )
    old_driver = FakePlaywright()
    page = FakePage()
    fresh = FakeContext([page])
    fresh_driver = FakePlaywright(fresh)
    session = _session_with(old, old_driver)

    with mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)):
        assert session.new_page() is page

    assert old.new_page_calls == 0
    assert old.close_calls == 1
    assert old_driver.stop_calls == 1
    assert fresh_driver.launch_calls == 1
    assert session._context is fresh
    assert session._playwright is fresh_driver


def test_start_replaces_a_disconnected_context():
    old = FakeContext([], connected=False)
    old_driver = FakePlaywright()
    fresh = FakeContext([FakePage()])
    fresh_driver = FakePlaywright(fresh)
    session = _session_with(old, old_driver)

    with mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)):
        session.start()

    assert old.close_calls == old_driver.stop_calls == 1
    assert fresh_driver.launch_calls == 1
    assert session._context is fresh


def test_xsession_launch_uses_native_maximized_viewport():
    context = FakeContext([])
    driver = FakePlaywright(context)
    session = XSession({"browser_profile": "profile", "brave": "brave"})

    with mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(driver)):
        session.start()

    options = driver.launch_kwargs[0]
    assert options["headless"] is False
    assert options["no_viewport"] is True
    assert "viewport" not in options
    assert options["args"].count("--start-maximized") == 1


def test_first_new_page_target_closed_restarts_once_then_succeeds():
    old = FakeContext([_closed_error()])
    old_driver = FakePlaywright()
    page = FakePage()
    fresh = FakeContext([page])
    fresh_driver = FakePlaywright(fresh)
    session = _session_with(old, old_driver)

    with mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)):
        assert session.new_page() is page

    assert old.new_page_calls == 1
    assert fresh.new_page_calls == 1
    assert fresh_driver.launch_calls == 1
    assert old.close_calls == old_driver.stop_calls == 1


def test_second_new_page_target_closed_fails_after_one_restart():
    old = FakeContext([_closed_error()])
    fresh = FakeContext([_closed_error()])
    fresh_driver = FakePlaywright(fresh)
    session = _session_with(old)

    with mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)):
        with pytest.raises(BrowserSessionError) as caught:
            session.new_page()

    assert isinstance(caught.value.__cause__, TargetClosedError)
    assert old.new_page_calls == fresh.new_page_calls == 1
    assert fresh_driver.launch_calls == 1
    assert fresh.close_calls == fresh_driver.stop_calls == 1
    assert session._context is session._playwright is None


def test_healthy_context_returns_page_without_restart():
    page = FakePage()
    context = FakeContext([page])
    session = _session_with(context)

    with mock.patch("publisher.x_publisher.sync_playwright") as sync:
        assert session.new_page() is page

    sync.assert_not_called()
    assert context.close_calls == 0


def test_non_closed_playwright_error_is_not_retried():
    context = FakeContext([PlaywrightError("invalid selector")])
    session = _session_with(context)

    with mock.patch("publisher.x_publisher.sync_playwright") as sync:
        with pytest.raises(PlaywrightError, match="invalid selector"):
            session.new_page()

    sync.assert_not_called()
    assert context.new_page_calls == 1


def test_intentional_stop_requires_explicit_start_before_relaunch():
    old = FakeContext([])
    old_driver = FakePlaywright()
    session = _session_with(old, old_driver)
    session.stop()

    with mock.patch("publisher.x_publisher.sync_playwright") as sync:
        with pytest.raises(BrowserSessionError, match="intentionally stopped"):
            session.new_page()
    sync.assert_not_called()

    fresh = FakeContext([FakePage()])
    fresh_driver = FakePlaywright(fresh)
    with mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)):
        session.start()
    assert session._context is fresh
    assert old.close_calls == old_driver.stop_calls == 1


def test_repeated_recoveries_do_not_accumulate_old_resources():
    first = FakeContext([_closed_error()])
    first_driver = FakePlaywright()
    page_b = FakePage()
    second = FakeContext([page_b])
    second_driver = FakePlaywright(second)
    page_c = FakePage()
    third = FakeContext([page_c])
    third_driver = FakePlaywright(third)
    session = _session_with(first, first_driver)

    with mock.patch(
        "publisher.x_publisher.sync_playwright",
        _sync_factory(second_driver, third_driver),
    ):
        assert session.new_page() is page_b
        second.browser.connected = False
        assert session.new_page() is page_c

    assert first.close_calls == second.close_calls == 1
    assert first_driver.stop_calls == second_driver.stop_calls == 1
    assert third_driver.stop_calls == 0
    assert session._context is third
    assert session._playwright is third_driver


def test_production_scrapers_never_access_private_session_context():
    scraper_dir = ROOT / "scrapers"
    violations = []
    for path in scraper_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_context":
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def test_tiktok_then_dead_context_then_youtube_recovers_in_pick_item(db):
    page_a = FakePage()
    context_a = FakeContext([page_a])
    old_driver = FakePlaywright()
    page_b = FakePage()
    context_b = FakeContext([page_b])
    fresh_driver = FakePlaywright(context_b)
    session = _session_with(context_a, old_driver)
    seen = []

    def tiktok_scrape(active_session, *_args):
        seen.append(("tiktok", active_session.new_page()))
        context_a.browser.connected = False
        return []

    def youtube_scrape(active_session, _config):
        seen.append(("youtube", active_session.new_page()))
        return []

    with mock.patch("scrapers.tiktok_scraper.scrape", side_effect=tiktok_scrape), \
            mock.patch("scrapers.youtube_scraper.scrape_shorts", side_effect=youtube_scrape), \
            mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)):
        assert main.pick_item(_pick_cfg(), db, session) is None

    assert seen == [("tiktok", page_a), ("youtube", page_b)]
    assert fresh_driver.launch_calls == 1
    assert old_driver.stop_calls == 1


def test_ordinary_source_failure_logs_and_continues_to_youtube(db, caplog):
    with mock.patch(
        "scrapers.tiktok_scraper.scrape", side_effect=RuntimeError("selector drift")
    ), mock.patch("scrapers.youtube_scraper.scrape_shorts", return_value=[]) as youtube:
        assert main.pick_item(_pick_cfg(), db, mock.Mock()) is None

    youtube.assert_called_once()
    assert "TikTok source failed (RuntimeError); continuing" in caplog.text


def test_global_browser_failure_is_not_swallowed_by_source_isolation(db):
    failure = BrowserSessionError("browser context recovery failed after one retry")
    with mock.patch("scrapers.tiktok_scraper.scrape", side_effect=failure), \
            mock.patch("scrapers.youtube_scraper.scrape_shorts") as youtube:
        with pytest.raises(BrowserSessionError) as caught:
            main.pick_item(_pick_cfg(), db, mock.Mock())

    assert caught.value is failure
    youtube.assert_not_called()


def test_internal_recovery_does_not_reenter_browser_profile_lock(tmp_path):
    context_a = FakeContext([_closed_error()])
    context_b = FakeContext([FakePage()])
    fresh_driver = FakePlaywright(context_b)
    session = _session_with(context_a)
    browser_lock = mock.Mock(side_effect=lambda *_args: nullcontext())
    publish_lock = mock.Mock(side_effect=lambda *_args: nullcontext())
    cfg = {"paths": {"db_file": str(tmp_path / "bot.db")}}

    def pick(_cfg, _db, active_session):
        active_session.new_page()
        return None

    with mock.patch("publisher.x_publisher.XSession", return_value=session), \
            mock.patch("publishing_lock.browser_profile_lock", browser_lock), \
            mock.patch("publishing_lock.publishing_lock", publish_lock), \
            mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)), \
            mock.patch("main.pick_item", side_effect=pick), \
            mock.patch("main.alert"):
        main.cmd_once(cfg)

    assert browser_lock.call_count == 1
    assert publish_lock.call_count == 1
    assert fresh_driver.launch_calls == 1


def test_unrecoverable_recovery_never_posts_or_finalizes(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    context_a = FakeContext([_closed_error()])
    context_b = FakeContext([_closed_error()])
    fresh_driver = FakePlaywright(context_b)
    session = _session_with(context_a)
    session.post = mock.Mock()
    cfg = {"paths": {"db_file": str(tmp_path / "bot.db")}}

    def pick(_cfg, _db, active_session):
        active_session.new_page()
        return None

    with mock.patch("main._make_db", return_value=db), \
            mock.patch("publisher.x_publisher.XSession", return_value=session), \
            mock.patch("publishing_lock.browser_profile_lock", side_effect=lambda *_args: nullcontext()), \
            mock.patch("publishing_lock.publishing_lock", side_effect=lambda *_args: nullcontext()), \
            mock.patch("publisher.x_publisher.sync_playwright", _sync_factory(fresh_driver)), \
            mock.patch("main.pick_item", side_effect=pick), \
            mock.patch("main.mark_item_published") as finalize:
        with pytest.raises(BrowserSessionError):
            main.cmd_once(cfg)

    session.post.assert_not_called()
    finalize.assert_not_called()
    assert not db.is_source_seen("youtube", "never-posted")
    assert not db.is_hash_seen("never-posted-hash", 30)


def test_page_cleanup_error_does_not_replace_primary_scrape_error():
    primary = RuntimeError("navigation failed")

    class FailingPage(FakePage):
        def goto(self, *_args, **_kwargs):
            raise primary

    page = FailingPage(close_error=_closed_error("Page.close"))
    session = SimpleNamespace(new_page=lambda: page)
    from scrapers import youtube_scraper

    with pytest.raises(RuntimeError) as caught:
        youtube_scraper.scrape_shorts(session, _pick_cfg()["youtube"])

    assert caught.value is primary
    assert page.close_calls == 1
