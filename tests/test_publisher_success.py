"""Regression tests for Issue 2: XSession.post() must only report success on
positive confirmation; navigations away from compose never count as success.

Fakes only — no browser, no network, no real clock waits.
"""

from unittest import mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from publisher.x_publisher import (
    ATTACHMENT_SELECTORS,
    COMPOSER_SELECTORS,
    FILE_INPUT_SELECTORS,
    POST_BUTTON_SELECTORS,
    XSession,
)

COMPOSER = COMPOSER_SELECTORS[0]
FILE_INPUT = FILE_INPUT_SELECTORS[0]
ATTACHMENTS = ATTACHMENT_SELECTORS[0]
POST_BTN = POST_BUTTON_SELECTORS[0]
LEGACY_TEXTAREA = 'textarea[data-testid="tweetTextarea_0"]'
DIV_COMPOSER = (
    'div[data-testid="tweetTextarea_0"]'
    '[role="textbox"][contenteditable="true"]'
)
UNRELATED_EDITOR = 'div[contenteditable="true"]'
SENT_TEXT = "text=Your post was sent"
LOGIN_LINK = 'a[href="/login"]'
BODY = "body"

COMPOSE_URL = "https://x.com/compose/post"
LOGIN_URL = "https://x.com/i/flow/login"
HOME_URL = "https://x.com/home"


class _FakeClock:
    def __init__(self, start=1_000_000.0):
        self._now = start

    def time(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.wait_for_calls = []
        self.click_calls = 0
        self.click_timeouts = []
        self.typed_chunks = []
        self.input_files = None
        self.visible_filter = False
        self.evaluate_calls = 0
        self.input_value_calls = 0
        self.inner_text_calls = 0

    def wait_for(self, **kwargs):
        self.wait_for_calls.append(kwargs)
        state = kwargs.get("state")
        if state == "attached" and self.selector not in self.page.present_selectors:
            raise TimeoutError(f"{self.selector} not attached")
        if state == "visible" and self.selector not in self.page.visible_selectors:
            raise TimeoutError(f"{self.selector} not visible")

    def filter(self, *, visible=None, **kwargs):
        self.visible_filter = bool(visible)
        return self

    @property
    def first(self):
        return self

    def set_input_files(self, paths):
        self.input_files = list(paths)

    def count(self):
        if self.selector == SENT_TEXT:
            return 1 if self.page.sent_toast else 0
        if self.selector == LOGIN_LINK:
            return self.page.login_link_count
        if self.selector not in self.page.present_selectors:
            return 0
        if self.visible_filter and self.selector not in self.page.visible_selectors:
            return 0
        return 1

    def inner_text(self, timeout=None):
        if self.selector in COMPOSER_SELECTORS:
            self.inner_text_calls += 1
            return "" if self.page.force_empty_composer else "".join(self.typed_chunks)
        return self.page.body_text

    def input_value(self, timeout=None):
        self.input_value_calls += 1
        return "" if self.page.force_empty_composer else "".join(self.typed_chunks)

    def evaluate(self, expression):
        self.evaluate_calls += 1
        if self.selector in COMPOSER_SELECTORS:
            return self.page.composer_tag
        return "div"

    def is_visible(self):
        return self.selector in self.page.visible_selectors

    def is_enabled(self):
        if self.selector != POST_BTN:
            return True
        self.page.post_readiness_polls += 1
        enabled_after = self.page.post_enabled_after_polls
        enabled = (
            enabled_after is not None
            and self.page.post_readiness_polls >= enabled_after
        )
        self.page.last_post_enabled = enabled
        return enabled

    def get_attribute(self, name):
        if self.selector == POST_BTN and name in ("aria-disabled", "disabled"):
            if name == "aria-disabled":
                return "false" if self.page.last_post_enabled else "true"
            return None if self.page.last_post_enabled else ""
        return None

    def click(self, timeout=None):
        self.click_calls += 1
        self.click_timeouts.append(timeout)
        if self.selector == POST_BTN and self.page.post_click_error is not None:
            raise self.page.post_click_error
        if self.selector == POST_BTN:
            if self.page.after_post_url is not None:
                self.page.url = self.page.after_post_url
            if self.page.after_post_body is not None:
                self.page.body_text = self.page.after_post_body
            if self.page.after_post_login_link is not None:
                self.page.login_link_count = self.page.after_post_login_link

    def press_sequentially(self, text, delay=None):
        self.typed_chunks.append(text)


class FakePage:
    def __init__(self, url=COMPOSE_URL, body_text="compose page",
                 sent_toast=False, clock=None, after_post_url=None,
                 after_post_body=None, after_post_login_link=None,
                 present_selectors=None, visible_selectors=None,
                 post_enabled_after_polls=1, composer_tag="div",
                 media_error_after_waits=None, force_empty_composer=False,
                 post_click_error=None):
        self.url = url
        self.body_text = body_text
        self.login_link_count = 0
        self.sent_toast = sent_toast
        self.after_post_url = after_post_url
        self.after_post_body = after_post_body
        self.after_post_login_link = after_post_login_link
        self.clock = clock or _FakeClock()
        self.post_enabled_after_polls = post_enabled_after_polls
        self.post_readiness_polls = 0
        self.last_post_enabled = post_enabled_after_polls == 0
        self.composer_tag = composer_tag
        self.media_error_after_waits = media_error_after_waits
        self.force_empty_composer = force_empty_composer
        self.post_click_error = post_click_error
        self.waits = []
        self._locators = {}
        defaults = {COMPOSER, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY}
        self.present_selectors = set(
            defaults if present_selectors is None else present_selectors
        )
        self.visible_selectors = set(
            self.present_selectors if visible_selectors is None else visible_selectors
        )

    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        # Tag-qualified and tag-agnostic selectors resolve to the same fake DOM
        # node when both selectors describe the exposed composer element.
        key = selector
        if (
            selector in {DIV_COMPOSER, LEGACY_TEXTAREA}
            and selector in self.present_selectors
            and COMPOSER in self.present_selectors
        ):
            key = COMPOSER
        if key not in self._locators:
            self._locators[key] = FakeLocator(self, key)
        return self._locators[key]

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.clock.advance(ms / 1000.0)
        if (
            self.media_error_after_waits is not None
            and len(self.waits) >= self.media_error_after_waits
        ):
            self.body_text = "Video could not be processed"

    def title(self):
        return "Compose / X"

    def close(self):
        pass


@pytest.fixture
def media(tmp_path):
    p = tmp_path / "media.mp4"
    p.write_bytes(b"fake-media")
    return str(p)


def _post(page, media_path, timeout_s=60):
    session = XSession({"browser_profile": "bp", "brave": "brave"})
    session.new_page = lambda: page
    with mock.patch("publisher.x_publisher.time.monotonic", page.clock.time):
        return session.post("hello world", [media_path], timeout_s=timeout_s)


def test_confirmed_success_returns_ok(media):
    page = FakePage(sent_toast=True)
    res = _post(page, media)
    assert res == {"ok": True, "reason": "posted"}
    assert page.locator(COMPOSER).click_calls == 1


def test_non_textarea_composer_is_found_and_typed(media):
    page = FakePage(
        sent_toast=True,
        present_selectors={
            COMPOSER, DIV_COMPOSER, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY,
        },
    )
    assert LEGACY_TEXTAREA not in page.present_selectors
    res = _post(page, media)
    assert res == {"ok": True, "reason": "posted"}
    composer = page.locator(COMPOSER)
    assert composer.click_calls == 1
    assert "".join(composer.typed_chunks) == "hello world"
    assert composer.evaluate_calls == 1
    assert composer.inner_text_calls == 1
    assert page.locator(DIV_COMPOSER) is composer


def test_legacy_textarea_with_same_testid_still_works(media):
    page = FakePage(
        sent_toast=True,
        composer_tag="textarea",
        present_selectors={
            COMPOSER, LEGACY_TEXTAREA, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY,
        },
    )
    res = _post(page, media)
    assert res == {"ok": True, "reason": "posted"}
    assert page.locator(LEGACY_TEXTAREA) is page.locator(COMPOSER)
    assert "".join(page.locator(LEGACY_TEXTAREA).typed_chunks) == "hello world"
    assert page.locator(LEGACY_TEXTAREA).input_value_calls == 1


def test_primary_missing_uses_scoped_dialog_fallback(media):
    fallback = COMPOSER_SELECTORS[1]
    page = FakePage(
        sent_toast=True,
        present_selectors={fallback, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY},
    )
    res = _post(page, media)
    assert res == {"ok": True, "reason": "posted"}
    assert page.locator(COMPOSER).click_calls == 0
    assert "".join(page.locator(fallback).typed_chunks) == "hello world"


def test_hidden_primary_is_skipped_for_visible_scoped_fallback(media):
    fallback = COMPOSER_SELECTORS[1]
    present = {COMPOSER, fallback, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY}
    page = FakePage(
        sent_toast=True,
        present_selectors=present,
        visible_selectors=present - {COMPOSER},
    )
    res = _post(page, media)
    assert res == {"ok": True, "reason": "posted"}
    assert page.locator(COMPOSER).typed_chunks == []
    assert "".join(page.locator(fallback).typed_chunks) == "hello world"


def test_unrelated_contenteditable_is_never_chosen(media):
    page = FakePage(
        present_selectors={UNRELATED_EDITOR, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY},
    )
    res = _post(page, media, timeout_s=1)
    assert res == {
        "ok": False,
        "reason": "composer_not_found: known X composer selectors were not visible",
    }
    assert page.locator(UNRELATED_EDITOR).typed_chunks == []
    assert page.locator(POST_BTN).click_calls == 0


def test_no_composer_has_stable_reason_and_diagnostics(media, caplog):
    page = FakePage(
        present_selectors={FILE_INPUT, ATTACHMENTS, POST_BTN, BODY},
    )
    res = _post(page, media, timeout_s=1)
    assert res["ok"] is False
    assert res["reason"].startswith("composer_not_found:")
    assert page.locator(POST_BTN).click_calls == 0
    assert "composer selector failure" in caplog.text
    assert COMPOSE_URL in caplog.text


def test_stable_control_testids_do_not_require_html_tag_names():
    assert COMPOSER == '[data-testid="tweetTextarea_0"]'
    assert FILE_INPUT == '[data-testid="fileInput"]'
    assert ATTACHMENTS == '[data-testid="attachments"]'
    assert POST_BTN == '[data-testid="tweetButtonInline"]'


def test_login_redirect_returns_failure(media):
    page = FakePage(
        body_text="Log in to continue",
        after_post_url=LOGIN_URL,
        after_post_body="Log in to continue",
        after_post_login_link=1,
    )
    res = _post(page, media)
    assert res["ok"] is False
    assert res["reason"] == "login"


def test_captcha_redirect_returns_failure(media):
    page = FakePage(
        after_post_url=LOGIN_URL,
        after_post_body="Verify your identity — you are not a bot",
    )
    res = _post(page, media)
    assert res["ok"] is False
    assert res["reason"] == "captcha"


def test_error_page_returns_failure(media):
    page = FakePage(
        after_post_url=HOME_URL,
        after_post_body="Something went wrong",
    )
    res = _post(page, media)
    assert res["ok"] is False
    assert res["reason"] == "error"


def test_arbitrary_navigation_without_confirmation_is_failure(media):
    page = FakePage(after_post_url=HOME_URL)  # no toast, no problem text
    res = _post(page, media)
    assert res["ok"] is False
    assert res["reason"] == "unverified"


def test_no_positive_confirmation_times_out(media):
    clock = _FakeClock()
    page = FakePage(clock=clock)  # stays on compose, no toast, no problem
    res = _post(page, media, timeout_s=1)
    assert res["ok"] is False
    assert res["reason"] == "timeout"
    assert page.locator(POST_BTN).click_calls == 1
    assert clock.time() >= 1_000_000.0 + 1.0  # deadline was enforced


def test_problem_checked_before_url_interpreting(media):
    """Login state + navigation: must be login failure, never success."""
    page = FakePage(
        body_text="Sign in to continue",
        after_post_url=LOGIN_URL,
        after_post_body="Sign in to continue",
        after_post_login_link=1,
        sent_toast=True,
    )
    res = _post(page, media)
    assert res["ok"] is False
    assert res["reason"] == "login"


def test_visible_disabled_button_waits_until_enabled_then_posts(media):
    page = FakePage(sent_toast=True, post_enabled_after_polls=3)

    res = _post(page, media, timeout_s=1)

    button = page.locator(POST_BTN)
    assert res == {"ok": True, "reason": "posted"}
    assert page.post_readiness_polls == 3
    assert button.click_calls == 1
    assert 0 < button.click_timeouts[0] < 1000


def test_visible_button_that_remains_disabled_fails_without_click(media, caplog):
    page = FakePage(post_enabled_after_polls=None)

    res = _post(page, media, timeout_s=1)

    assert res == {"ok": False, "reason": "post_button_disabled_timeout"}
    assert page.locator(POST_BTN).click_calls == 0
    assert page.clock.time() == pytest.approx(1_000_001.0)
    assert "button_visible" in caplog.text
    assert "button_enabled" in caplog.text
    assert "attachment_count" in caplog.text
    assert "composer_non_empty" in caplog.text
    assert COMPOSE_URL in caplog.text


def test_small_configured_readiness_timeout_is_the_actual_deadline(media):
    page = FakePage(post_enabled_after_polls=None)

    res = _post(page, media, timeout_s=0.4)

    assert res["reason"] == "post_button_disabled_timeout"
    assert page.locator(POST_BTN).click_calls == 0
    assert page.clock.time() == pytest.approx(1_000_000.4)


def test_media_processing_error_ends_readiness_early(media):
    page = FakePage(
        post_enabled_after_polls=None,
        media_error_after_waits=1,
    )

    res = _post(page, media, timeout_s=10)

    assert res == {
        "ok": False,
        "reason": "media_upload_error:video_processing_failed",
    }
    assert page.locator(POST_BTN).click_calls == 0
    assert page.clock.time() < 1_000_001.0


def test_immediately_ready_image_clicks_without_readiness_sleep(tmp_path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fake-image")
    page = FakePage(sent_toast=True, post_enabled_after_polls=1)

    res = _post(page, str(image), timeout_s=1)

    assert res == {"ok": True, "reason": "posted"}
    assert page.waits == []
    assert page.locator(POST_BTN).click_calls == 1


def test_requested_caption_that_remains_empty_fails_before_readiness(media):
    page = FakePage(sent_toast=True, force_empty_composer=True)

    res = _post(page, media, timeout_s=1)

    assert res == {"ok": False, "reason": "caption_not_entered"}
    assert page.locator(POST_BTN).click_calls == 0


def test_caption_text_cannot_impersonate_a_media_error(media):
    page = FakePage(
        body_text="compose page VIDEO COULD NOT BE PROCESSED",
        sent_toast=True,
    )
    session = XSession({"browser_profile": "bp", "brave": "brave"})
    session.new_page = lambda: page

    with mock.patch("publisher.x_publisher.time.monotonic", page.clock.time):
        res = session.post(
            "video could not be processed",
            [media],
            timeout_s=1,
        )

    assert res == {"ok": True, "reason": "posted"}


def test_enabled_button_click_timeout_has_stable_failure(media):
    page = FakePage(
        post_click_error=PlaywrightTimeoutError(
            "Locator.click: element became disabled"
        )
    )

    res = _post(page, media, timeout_s=1)

    assert res == {"ok": False, "reason": "post_button_click_timeout"}
    button = page.locator(POST_BTN)
    assert button.click_calls == 1
    assert 0 < button.click_timeouts[0] <= 1000
