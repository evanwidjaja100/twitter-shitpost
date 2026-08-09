"""Regression tests for Issue 2: XSession.post() must only report success on
positive confirmation; navigations away from compose never count as success.

Fakes only — no browser, no network, no real clock waits.
"""

import pytest

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
        self.typed_chunks = []
        self.input_files = None
        self.visible_filter = False

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
        return self.page.body_text

    def click(self):
        self.click_calls += 1
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
                 present_selectors=None, visible_selectors=None):
        self.url = url
        self.body_text = body_text
        self.login_link_count = 0
        self.sent_toast = sent_toast
        self.after_post_url = after_post_url
        self.after_post_body = after_post_body
        self.after_post_login_link = after_post_login_link
        self.clock = clock or _FakeClock()
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
    assert page.locator(DIV_COMPOSER) is composer


def test_legacy_textarea_with_same_testid_still_works(media):
    page = FakePage(
        sent_toast=True,
        present_selectors={
            COMPOSER, LEGACY_TEXTAREA, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY,
        },
    )
    res = _post(page, media)
    assert res == {"ok": True, "reason": "posted"}
    assert page.locator(LEGACY_TEXTAREA) is page.locator(COMPOSER)
    assert "".join(page.locator(LEGACY_TEXTAREA).typed_chunks) == "hello world"


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
