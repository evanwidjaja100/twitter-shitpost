"""Regression tests for Issue 2: XSession.post() must only report success on
positive confirmation; navigations away from compose never count as success.

Fakes only — no browser, no network, no real clock waits.
"""

import pytest

from publisher.x_publisher import XSession

COMPOSER = 'textarea[data-testid="tweetTextarea_0"]'
FILE_INPUT = 'input[data-testid="fileInput"]'
ATTACHMENTS = 'div[data-testid="attachments"]'
POST_BTN = 'button[data-testid="tweetButtonInline"]'
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

    def wait_for(self, **kwargs):
        self.wait_for_calls.append(kwargs)

    def set_input_files(self, paths):
        self.input_files = list(paths)

    def count(self):
        if self.selector == SENT_TEXT:
            return 1 if self.page.sent_toast else 0
        if self.selector == LOGIN_LINK:
            return self.page.login_link_count
        return 0

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
                 after_post_body=None, after_post_login_link=None):
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

    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self, selector)
        return self._locators[selector]

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.clock.advance(ms / 1000.0)

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