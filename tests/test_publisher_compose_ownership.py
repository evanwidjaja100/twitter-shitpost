"""Compose ownership regressions using a deterministic DOM-shaped fake.

The fake models locator ancestry and descendant scoping. It therefore proves
selection by ownership; changing a global ``first`` to ``last`` cannot satisfy
the central wrong-button test.
"""

from dataclasses import dataclass, field
from unittest import mock

import pytest

from publisher.x_publisher import XSession


COMPOSER = '[data-testid="tweetTextarea_0"]'
FILE_INPUT = '[data-testid="fileInput"]'
ATTACHMENT = '[data-testid="attachments"]'
POST_INLINE = '[data-testid="tweetButtonInline"]'
POST_MODAL = '[data-testid="tweetButton"]'
BODY = "body"
SENT = "text=Your post was sent"
LOGIN_LINK = 'a[href="/login"]'
DIALOG_ANCESTOR = "xpath=ancestor::*[@role='dialog'][1]"
PRIMARY_ANCESTOR = "xpath=ancestor::*[@data-testid='primaryColumn'][1]"


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@dataclass
class Element:
    selector: str
    visible: bool = True
    enabled: bool = True
    text: str = ""
    tag: str = "div"
    attrs: dict = field(default_factory=dict)
    parent: "Element | None" = None
    children: list = field(default_factory=list)
    click_count: int = 0
    typed: list = field(default_factory=list)
    input_files: list | None = None

    def add(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()

    def nearest(self, *, role=None, testid=None):
        current = self.parent
        while current is not None:
            if role is not None and current.attrs.get("role") == role:
                return current
            if testid is not None and current.attrs.get("data-testid") == testid:
                return current
            current = current.parent
        return None


def _matches(element, selector):
    selectors = [part.strip() for part in selector.split(",")]
    for part in selectors:
        if part == BODY and element.selector == BODY:
            return True
        if part == SENT and element.selector == SENT:
            return True
        if part == LOGIN_LINK and element.selector == LOGIN_LINK:
            return True
        if part == COMPOSER and element.attrs.get("data-testid") == "tweetTextarea_0":
            return True
        if part == FILE_INPUT and element.attrs.get("data-testid") == "fileInput":
            return True
        if part == ATTACHMENT and element.attrs.get("data-testid") == "attachments":
            return True
        if part == POST_INLINE and element.attrs.get("data-testid") == "tweetButtonInline":
            return True
        if part == POST_MODAL and element.attrs.get("data-testid") == "tweetButton":
            return True
        if part == '[role="dialog"]' and element.attrs.get("role") == "dialog":
            return True
        if part == '[data-testid="primaryColumn"]' and element.attrs.get("data-testid") == "primaryColumn":
            return True
        if part == '[role="dialog"] [role="textbox"][contenteditable="true"]':
            return (
                element.attrs.get("role") == "textbox"
                and element.attrs.get("contenteditable") == "true"
                and element.nearest(role="dialog") is not None
            )
        if part == '[data-testid="primaryColumn"] [role="textbox"][contenteditable="true"]':
            return (
                element.attrs.get("role") == "textbox"
                and element.attrs.get("contenteditable") == "true"
                and element.nearest(testid="primaryColumn") is not None
            )
    return False


class Locator:
    def __init__(self, page, elements):
        self.page = page
        self.elements = list(elements)
        self.click_timeouts = []

    def filter(self, *, visible=None, **_kwargs):
        if visible:
            return Locator(self.page, [element for element in self.elements if element.visible])
        return Locator(self.page, self.elements)

    @property
    def first(self):
        return Locator(self.page, self.elements[:1])

    def nth(self, index):
        return Locator(self.page, self.elements[index:index + 1])

    def count(self):
        return len(self.elements)

    def wait_for(self, *, state, timeout=None):
        if state == "visible" and not any(element.visible for element in self.elements):
            raise TimeoutError(f"no visible element within {timeout}")
        if state == "attached" and not self.elements:
            raise TimeoutError(f"no attached element within {timeout}")

    def locator(self, selector):
        if selector == DIALOG_ANCESTOR:
            ancestors = [element.nearest(role="dialog") for element in self.elements]
            return Locator(self.page, [element for element in ancestors if element])
        if selector == PRIMARY_ANCESTOR:
            ancestors = [element.nearest(testid="primaryColumn") for element in self.elements]
            return Locator(self.page, [element for element in ancestors if element])
        matches = []
        for root in self.elements:
            matches.extend(
                element for element in root.descendants() if _matches(element, selector)
            )
        return Locator(self.page, matches)

    def _one(self):
        if len(self.elements) != 1:
            raise AssertionError(f"expected one fake element, got {len(self.elements)}")
        return self.elements[0]

    def set_input_files(self, paths, timeout=None):
        self._one().input_files = list(paths) if not isinstance(paths, str) else [paths]

    def click(self, timeout=None):
        element = self._one()
        element.click_count += 1
        self.click_timeouts.append(timeout)

    def press_sequentially(self, text, delay=None):
        self._one().typed.append(text)

    def evaluate(self, expression):
        return self._one().tag

    def input_value(self, timeout=None):
        return "".join(self._one().typed)

    def inner_text(self, timeout=None):
        return self._one().text or "".join(self._one().typed)

    def is_visible(self):
        return self._one().visible

    def is_enabled(self):
        return self._one().enabled

    def get_attribute(self, name):
        element = self._one()
        if name == "disabled":
            return "" if not element.enabled else None
        if name == "aria-disabled":
            return "false" if element.enabled else "true"
        return element.attrs.get(name)


class Page:
    def __init__(self, body, *, clock=None, ready_after_seconds=None):
        self.body = body
        self.clock = clock or FakeClock()
        self.ready_after_seconds = ready_after_seconds
        self.started_at = self.clock.time()
        self.url = "https://x.com/compose/post"
        self.closed = False
        self.waits = []

    def all_elements(self):
        return [self.body, *self.body.descendants()]

    def locator(self, selector):
        if selector == SENT:
            return Locator(self, [Element(SENT)])
        if selector == LOGIN_LINK:
            return Locator(self, [])
        return Locator(self, [element for element in self.all_elements() if _matches(element, selector)])

    def goto(self, url, **_kwargs):
        self.url = url

    def title(self):
        return "Compose / X"

    def evaluate(self, *_args, **_kwargs):
        raise NotImplementedError("diagnostic JS is best-effort in this fake")

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock.advance(milliseconds / 1000)
        if self.ready_after_seconds is not None and self.elapsed >= self.ready_after_seconds:
            for element in self.all_elements():
                if element.attrs.get("data-testid") in {"tweetButton", "tweetButtonInline"}:
                    if element.attrs.get("active-owner") == "true":
                        element.enabled = True
                if element.attrs.get("data-testid") == "attachments":
                    element.text = "Upload caption file (.srt) clip.mp4: Ready"

    @property
    def elapsed(self):
        return self.clock.time() - self.started_at

    def close(self):
        self.closed = True


def node(selector, **kwargs):
    attrs = dict(kwargs.pop("attrs", {}))
    if selector.startswith('[data-testid="'):
        attrs.setdefault("data-testid", selector.split('"')[1])
    if selector == COMPOSER:
        attrs.update({"role": "textbox", "contenteditable": "true"})
    return Element(selector, attrs=attrs, **kwargs)


def build_page(
    *,
    active_button_selector=POST_INLINE,
    active_button_enabled=True,
    stale_button=True,
    stale_attachment=False,
    active_attachment=True,
    duplicate_active_button=False,
    media_text="Upload caption file (.srt) clip.mp4: Ready",
    ready_after_seconds=None,
):
    body = node(BODY, text="compose page")
    stale_dialog = body.add(node('[role="dialog"]', attrs={"role": "dialog"}))
    stale_composer = stale_dialog.add(node(COMPOSER, visible=False))
    stale_input = stale_dialog.add(node(FILE_INPUT))
    stale_media = stale_dialog.add(node(ATTACHMENT, visible=False)) if stale_attachment else None
    stale_post = (
        stale_dialog.add(node(POST_INLINE, enabled=False, tag="button", text="Post"))
        if stale_button
        else None
    )

    active_dialog = body.add(node('[role="dialog"]', attrs={"role": "dialog"}))
    active_composer = active_dialog.add(node(COMPOSER))
    active_input = active_dialog.add(node(FILE_INPUT, tag="input"))
    active_media = (
        active_dialog.add(node(ATTACHMENT, text=media_text))
        if active_attachment
        else None
    )
    edit_media = active_dialog.add(
        node("button.edit-media", tag="button", text="Edit", attrs={"aria-label": "Edit media"})
    )
    remove_media = active_dialog.add(
        node("button.remove-media", tag="button", attrs={"aria-label": "Remove media"})
    )
    active_post = active_dialog.add(
        node(
            active_button_selector,
            enabled=active_button_enabled,
            tag="button",
            text="Post",
            attrs={"active-owner": "true"},
        )
    )
    second_active_post = None
    if duplicate_active_button:
        second_active_post = active_dialog.add(
            node(POST_MODAL, enabled=True, tag="button", text="Post")
        )
    page = Page(body, ready_after_seconds=ready_after_seconds)
    return page, {
        "stale_dialog": stale_dialog,
        "stale_composer": stale_composer,
        "stale_input": stale_input,
        "stale_media": stale_media,
        "stale_post": stale_post,
        "active_dialog": active_dialog,
        "active_composer": active_composer,
        "active_input": active_input,
        "active_media": active_media,
        "edit_media": edit_media,
        "remove_media": remove_media,
        "active_post": active_post,
        "second_active_post": second_active_post,
    }


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake-video")
    return str(path)


def post(page, media, *, ready_timeout_s=1):
    session = XSession({"browser_profile": "profile", "brave": "brave"})
    session.new_page = lambda: page
    with mock.patch("publisher.x_publisher.time.monotonic", page.clock.time):
        return session.post(
            "owned caption",
            [media],
            timeout_s=1,
            media_kind="video",
            ready_timeout_s=ready_timeout_s,
        )


def test_wrong_global_first_inline_button_is_skipped_by_compose_ownership(media):
    """The requested decisive regression: both buttons share one testid.

    Global #0 is visible/disabled in the wrong dialog; global #1 is the only
    candidate owned by the visible active composer. DOM order must be irrelevant.
    """
    page, elements = build_page(active_button_selector=POST_INLINE)

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert page.locator(POST_INLINE).count() == 2
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 1


def test_live_dom_shape_selects_modal_tweet_button_not_outside_inline(media):
    """Production capture: sidebar inline disabled; modal tweetButton enabled."""
    page, elements = build_page(active_button_selector=POST_MODAL)

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 1


def test_caption_file_ready_is_inline_attachment_state_not_completion_dialog(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        media_text="Edit Upload caption file (.srt) clip.mp4: Ready",
    )

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["active_media"].text.endswith("clip.mp4: Ready")
    assert XSession._video_attachment_state(Locator(page, [elements["active_media"]])) == (
        "attachment_inline_ready"
    )
    assert elements["edit_media"].click_count == 0
    assert elements["remove_media"].click_count == 0
    assert elements["active_post"].click_count == 1
    assert elements["stale_post"].click_count == 0


def test_video_processing_polls_then_uses_owned_button_immediately(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        active_button_enabled=False,
        media_text="Upload caption file (.srt) clip.mp4: Processing",
        ready_after_seconds=0.5,
    )

    result = post(page, media, ready_timeout_s=2)

    assert result == {"ok": True, "reason": "posted"}
    assert page.elapsed == pytest.approx(0.5)
    assert elements["active_media"].text.endswith("clip.mp4: Ready")
    assert elements["active_post"].click_count == 1


def test_secondary_editor_processing_blocks_post_until_dialog_is_gone(media):
    page, elements = build_page(active_button_selector=POST_MODAL)

    def editor_state(_page):
        state = "secondary_processing" if page.elapsed < 1 else "no_editor"
        return {"state": state, "dialog_count": int(state != "no_editor"), "action_candidates": []}

    with mock.patch.object(XSession, "_secondary_video_editor_state", side_effect=editor_state):
        result = post(page, media, ready_timeout_s=2)

    assert result == {"ok": True, "reason": "posted"}
    assert page.elapsed == pytest.approx(1.0)
    assert elements["active_post"].click_count == 1


def test_secondary_ready_editor_without_verified_action_fails_safely(media):
    page, elements = build_page(active_button_selector=POST_MODAL)
    observed = {
        "state": "secondary_ready",
        "dialog_count": 1,
        "action_candidates": [{"text": "Close", "aria_label": "Close"}],
    }

    with mock.patch.object(XSession, "_secondary_video_editor_state", return_value=observed):
        result = post(page, media, ready_timeout_s=2)

    assert result == {"ok": False, "reason": "media_editor_unresolved"}
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 0
    assert elements["edit_media"].click_count == 0
    assert elements["remove_media"].click_count == 0


def test_secondary_editor_error_fails_without_post_click(media):
    page, elements = build_page(active_button_selector=POST_MODAL)
    observed = {
        "state": "secondary_error",
        "dialog_count": 1,
        "action_candidates": [],
    }

    with mock.patch.object(XSession, "_secondary_video_editor_state", return_value=observed):
        result = post(page, media, ready_timeout_s=2)

    assert result == {
        "ok": False,
        "reason": "media_upload_error:media_processing_failed",
    }
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 0


def test_hidden_stale_attachment_cannot_satisfy_active_compose(media):
    page, elements = build_page(stale_attachment=True, active_attachment=False)

    result = post(page, media)

    assert result["ok"] is False
    assert result["reason"].startswith("attachments_not_found:")
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 0


def test_multiple_visible_owned_post_buttons_fail_ambiguously(media, caplog):
    page, elements = build_page(duplicate_active_button=True)

    result = post(page, media)

    assert result == {"ok": False, "reason": "ambiguous_post_button"}
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 0
    assert elements["second_active_post"].click_count == 0
    assert "ambiguous_post_button" in caplog.text


def test_readiness_timeout_logs_all_button_ownership_diagnostics(media, caplog):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        active_button_enabled=False,
    )
    diagnostic = {
        "post_button_candidates": [
            {
                "index": 0,
                "testid": "tweetButtonInline",
                "visible": True,
                "enabled": False,
                "inside_active_compose": False,
            },
            {
                "index": 1,
                "testid": "tweetButton",
                "visible": True,
                "enabled": False,
                "inside_active_compose": True,
            },
        ],
        "visible_dialogs": [
            {
                "index": 0,
                "contains_active_composer": True,
                "contains_attachment": True,
                "post_button_count": 1,
                "media_editor_markers": {"srt": True, "ready": True},
            }
        ],
        "secondary_media_editor": None,
    }

    with mock.patch.object(XSession, "_bounded_dom_diagnostics", return_value=diagnostic):
        result = post(page, media, ready_timeout_s=0.5)

    assert result == {"ok": False, "reason": "post_button_disabled_timeout"}
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 0
    assert "post_button_candidates" in caplog.text
    assert "inside_active_compose" in caplog.text
    assert "visible_dialogs" in caplog.text


def test_hidden_stale_composer_is_never_typed_or_used(media):
    page, elements = build_page()

    assert post(page, media) == {"ok": True, "reason": "posted"}

    assert elements["stale_composer"].typed == []
    assert "".join(elements["active_composer"].typed) == "owned caption"
    assert elements["stale_input"].input_files is None
    assert elements["active_input"].input_files == [media]
