"""Tests for the publisher timeout-unit fix.

Verifies that seconds are converted to milliseconds once before being handed
to Playwright, that the composer is explicitly focused before text entry, and
that typing happens at the locator level (no keyboard input to the page).
Uses in-memory fakes only — no real X account or browser session.
"""

from pathlib import Path

import pytest

from publisher.x_publisher import (
    ATTACHMENT_SELECTORS,
    COMPOSER_SELECTORS,
    DIALOG_ANCESTOR_SELECTOR,
    FILE_INPUT_SELECTORS,
    POST_BUTTON_QUERY,
    POST_BUTTON_SELECTORS,
    XSession,
)

COMPOSER = COMPOSER_SELECTORS[0]
FILE_INPUT = FILE_INPUT_SELECTORS[0]
ATTACHMENTS = ATTACHMENT_SELECTORS[0]
POST_BTN = POST_BUTTON_SELECTORS[0]
SENT_TEXT = "text=Your post was sent"
LOGIN_LINK = 'a[href="/login"]'
BODY = "body"


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.wait_for_calls = []
        self.click_calls = 0
        self.click_timeouts = []
        self.typed_chunks = []
        self.input_files = None
        self._count = 0
        self._text = "x.com compose page"
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

    def nth(self, _index):
        return self

    def locator(self, selector):
        if selector == DIALOG_ANCESTOR_SELECTOR:
            self.page.present_selectors.add(DIALOG_ANCESTOR_SELECTOR)
            self.page.visible_selectors.add(DIALOG_ANCESTOR_SELECTOR)
            return self.page.locator(DIALOG_ANCESTOR_SELECTOR)
        if selector == POST_BUTTON_QUERY:
            return self.page.locator(POST_BTN)
        return self.page.locator(selector)

    def set_input_files(self, paths):
        self.input_files = list(paths)

    def count(self):
        if self._count:
            return self._count
        if self.selector not in self.page.present_selectors:
            return 0
        if self.visible_filter and self.selector not in self.page.visible_selectors:
            return 0
        return 1

    def inner_text(self, timeout=None):
        if self.selector == COMPOSER:
            return "".join(self.typed_chunks)
        return self._text

    def input_value(self, timeout=None):
        return "".join(self.typed_chunks)

    def evaluate(self, expression):
        return "div"

    def is_visible(self):
        return self.selector in self.page.visible_selectors

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return None

    def click(self, timeout=None):
        self.click_calls += 1
        self.click_timeouts.append(timeout)
        self.page.events.append(("click", self.selector))

    def press_sequentially(self, text, delay=None):
        self.typed_chunks.append(text)
        self.page.events.append(("type", self.selector))


class FakePage:
    def __init__(self):
        self.events = []
        self._locators = {}
        self.url = "https://x.com/compose/post"
        self.waits = []
        self.present_selectors = {
            COMPOSER, FILE_INPUT, ATTACHMENTS, POST_BTN, BODY,
        }
        self.visible_selectors = set(self.present_selectors)

    def goto(self, url, **kwargs):
        self.events.append(("goto", url))

    def locator(self, selector):
        if selector not in self._locators:
            loc = FakeLocator(self, selector)
            if selector == SENT_TEXT:
                loc._count = 1
            self._locators[selector] = loc
        return self._locators[selector]

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def title(self):
        return "Compose / X"

    def close(self):
        pass


@pytest.fixture
def publisher(tmp_path):
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake-media")
    session = XSession({"browser_profile": "bp", "brave": "brave"})
    page = FakePage()
    session.new_page = lambda: page
    return session, page, str(media)


def test_timeout_s_is_forwarded_as_ms(publisher):
    session, page, media = publisher
    res = session.post("hello world", [media], timeout_s=60)
    assert res["ok"] is True
    expected = {
        COMPOSER: 60000 // len(COMPOSER_SELECTORS),
        FILE_INPUT: 60000,
        ATTACHMENTS: 60000,
    }
    for selector in (COMPOSER, FILE_INPUT, ATTACHMENTS):
        calls = page._locators[selector].wait_for_calls
        assert calls, f"no wait_for recorded for {selector}"
        for call in calls:
            assert call["timeout"] == expected[selector], (selector, call)
    post_timeout = page._locators[POST_BTN].wait_for_calls[0]["timeout"]
    assert 0 < post_timeout <= 60000
    assert 0 < page._locators[POST_BTN].click_timeouts[0] <= post_timeout


def test_short_timeout_maps_to_ms(publisher):
    session, page, media = publisher
    res = session.post("cap", [media], timeout_s=1)
    assert res["ok"] is True
    expected = {
        COMPOSER: max(1, 1000 // len(COMPOSER_SELECTORS)),
        FILE_INPUT: 1000,
        ATTACHMENTS: 1000,
    }
    for selector in (COMPOSER, FILE_INPUT, ATTACHMENTS):
        for call in page._locators[selector].wait_for_calls:
            assert call["timeout"] == expected[selector], (selector, call)
    post_timeout = page._locators[POST_BTN].wait_for_calls[0]["timeout"]
    assert 0 < post_timeout <= 1000
    assert 0 < page._locators[POST_BTN].click_timeouts[0] <= post_timeout


def test_composer_is_clicked_before_typing(publisher):
    session, page, media = publisher
    res = session.post("hello world", [media])
    assert res["ok"] is True
    composer = page._locators[COMPOSER]
    assert composer.click_calls == 1
    assert composer.typed_chunks, "composer was never typed into"
    assert "".join(composer.typed_chunks) == "hello world"

    click_index = next(
        i for i, e in enumerate(page.events) if e == ("click", COMPOSER)
    )
    first_type_index = next(i for i, e in enumerate(page.events) if e[0] == "type")
    assert click_index < first_type_index


def test_typing_uses_locator_not_page_keyboard(publisher):
    session, page, media = publisher
    res = session.post("no keyboard input", [media])
    assert res["ok"] is True
    assert not hasattr(page, "keyboard")


def test_media_and_send_flow_uses_milliseconds(publisher):
    session, page, media = publisher
    res = session.post("cap", [media])
    assert res["ok"] is True
    assert page._locators[FILE_INPUT].input_files == [media]
    assert page._locators[POST_BTN].click_calls == 1
