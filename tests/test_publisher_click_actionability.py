"""Deterministic regressions for the enabled-but-click-times-out repair.

The live X failure had a correct, enabled Post button whose Playwright click
timed out after ~176s. This suite pins the four repairs:

1. fresh re-resolution of the active compose + Post button immediately before
   the click (a stale pre-readiness locator is never reused),
2. a dedicated short post-click timeout independent of media readiness,
3. preservation of the actual Playwright exception and pointer-hit
   diagnostics (elementFromPoint, descendants of the button are not blockers),
4. ambiguous click-timeout reconciliation: a positive "Your post was sent"
   signal wins; compose disappearance alone never counts as success; no
   automatic second click ever happens.

Fakes only — no real X account, browser, or network.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
HOME_URL = "https://x.com/home"

DEFAULT_RECT = {"x": 970, "y": 660, "width": 67, "height": 36}

CLICK_TIMEOUT_ERROR = (
    "Locator.click: Timeout 15000ms exceeded.\n"
    "Call log:\n"
    '  - waiting for locator("[data-testid=tweetButton]")\n'
    "  - <button ...> another element intercepts pointer events"
)


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
    rect: dict = field(default_factory=lambda: dict(DEFAULT_RECT))

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
        is_post_button = element.attrs.get("data-testid") in {
            "tweetButton",
            "tweetButtonInline",
        }
        if is_post_button:
            self.page.click_timeouts.append(timeout)
            if self.page.problem_on_click:
                self.page.problem_active = True
        if is_post_button and self.page.post_click_error is not None:
            if self.page.navigate_on_click_timeout:
                self.page.apply_click_timeout_navigation()
            raise self.page.post_click_error

    def bounding_box(self):
        return dict(self._one().rect)

    def evaluate(self, expression, arg=None):
        element = self._one()
        if "elementFromPoint" in str(expression):
            hit = self.page.element_from_point
            if hit is None:
                return {"inside_post_button": None, "top": None}
            inside = hit is element or any(d is hit for d in element.descendants())
            return {
                "inside_post_button": inside,
                "is_button_itself": hit is element,
                "top": {
                    "tag": hit.tag,
                    "role": hit.attrs.get("role"),
                    "testid": hit.attrs.get("data-testid"),
                    "aria_label": hit.attrs.get("aria-label"),
                    "title": hit.attrs.get("title"),
                    "class": hit.attrs.get("class"),
                    "text": " ".join((hit.text or "").split())[:80],
                },
            }
        return element.tag

    def press_sequentially(self, text, delay=None):
        self._one().typed.append(text)

    def input_value(self, timeout=None):
        return "".join(self._one().typed)

    def inner_text(self, timeout=None):
        element = self._one()
        if element.selector == BODY:
            return self.page.body_inner_text()
        return element.text or "".join(element.typed)

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
    def __init__(
        self,
        body,
        *,
        clock=None,
        ready_after_seconds=None,
        post_click_error=None,
        sent_toast=False,
        toast_after_seconds=None,
        navigate_on_click_timeout=False,
        element_from_point=None,
        rerender_fn=None,
        problem_text=None,
        problem_after_seconds=None,
        problem_on_click=False,
    ):
        self.body = body
        self.clock = clock or FakeClock()
        self.ready_after_seconds = ready_after_seconds
        self.post_click_error = post_click_error
        self.sent_toast = sent_toast
        self.toast_after_seconds = toast_after_seconds
        self.navigate_on_click_timeout = navigate_on_click_timeout
        self.element_from_point = element_from_point
        self.rerender_fn = rerender_fn
        self.problem_text = problem_text
        self.problem_after_seconds = problem_after_seconds
        self.problem_on_click = problem_on_click
        self.problem_active = False
        self.rerendered = False
        self.compose_finds = 0
        self.started_at = self.clock.time()
        self.url = "https://x.com/compose/post"
        self.closed = False
        self.waits = []
        self.click_timeouts = []
        self.screenshot_calls = []

    def all_elements(self):
        return [self.body, *self.body.descendants()]

    def locator(self, selector):
        if selector == COMPOSER:
            self.compose_finds += 1
            if self.compose_finds == 2 and self.rerender_fn is not None and not self.rerendered:
                self.rerendered = True
                self.rerender_fn()
        if selector == SENT:
            return Locator(self, [Element(SENT)] if self.sent_toast else [])
        if selector == LOGIN_LINK:
            return Locator(self, [])
        return Locator(self, [element for element in self.all_elements() if _matches(element, selector)])

    def goto(self, url, **_kwargs):
        self.url = url

    def title(self):
        return "Compose / X"

    def evaluate(self, *_args, **_kwargs):
        raise NotImplementedError("diagnostic JS is best-effort in this fake")

    def screenshot(self, path=None, **_kwargs):
        self.screenshot_calls.append(path)
        if path:
            Path(path).write_bytes(b"fake-screenshot")

    def apply_click_timeout_navigation(self):
        self.url = HOME_URL
        for element in self.all_elements():
            if element.attrs.get("data-testid") in {"tweetTextarea_0", "attachments"}:
                element.visible = False

    def body_inner_text(self):
        text = self.body.text or ""
        if self.problem_text and (not self.problem_on_click or self.problem_active):
            if self.problem_after_seconds is None or self.elapsed >= self.problem_after_seconds:
                text += "\n" + self.problem_text
        return text

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock.advance(milliseconds / 1000)
        if self.toast_after_seconds is not None and self.elapsed >= self.toast_after_seconds:
            self.sent_toast = True
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
    stale_dialog_visible=False,
    stale_button_enabled=True,
    ready_after_seconds=None,
    post_click_error=None,
    sent_toast=False,
    toast_after_seconds=None,
    navigate_on_click_timeout=False,
    element_from_point=None,
    rerender_fn=None,
    problem_text=None,
    problem_after_seconds=None,
    problem_on_click=False,
):
    body = node(BODY, text="compose page")
    stale_dialog = body.add(
        node('[role="dialog"]', attrs={"role": "dialog"}, visible=stale_dialog_visible)
    )
    stale_composer = stale_dialog.add(node(COMPOSER, visible=False))
    stale_post = stale_dialog.add(
        node(
            POST_INLINE,
            enabled=stale_button_enabled,
            tag="button",
            text="Post",
            attrs={"active-owner": "stale"},
        )
    )

    active_dialog = body.add(node('[role="dialog"]', attrs={"role": "dialog"}))
    active_composer = active_dialog.add(node(COMPOSER))
    active_input = active_dialog.add(node(FILE_INPUT, tag="input"))
    active_media = active_dialog.add(
        node(ATTACHMENT, text="Upload caption file (.srt) clip.mp4: Ready")
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

    page = Page(
        body,
        ready_after_seconds=ready_after_seconds,
        post_click_error=post_click_error,
        sent_toast=sent_toast,
        toast_after_seconds=toast_after_seconds,
        navigate_on_click_timeout=navigate_on_click_timeout,
        element_from_point=element_from_point,
        rerender_fn=rerender_fn,
        problem_text=problem_text,
        problem_after_seconds=problem_after_seconds,
        problem_on_click=problem_on_click,
    )
    return page, {
        "stale_dialog": stale_dialog,
        "stale_composer": stale_composer,
        "stale_post": stale_post,
        "active_dialog": active_dialog,
        "active_composer": active_composer,
        "active_input": active_input,
        "active_media": active_media,
        "active_post": active_post,
    }


def post(
    page,
    media,
    *,
    media_kind="video",
    ready_timeout_s=1,
    post_click_timeout_s=15,
    timeout_s=1,
):
    session = XSession({"browser_profile": "profile", "brave": "brave"})
    session.new_page = lambda: page
    with mock.patch("publisher.x_publisher.time.monotonic", page.clock.time):
        return session.post(
            "owned caption",
            [media],
            timeout_s=timeout_s,
            media_kind=media_kind,
            ready_timeout_s=ready_timeout_s,
            post_click_timeout_s=post_click_timeout_s,
        )


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fake-video")
    return str(path)


def _button_locator(page, element):
    return Locator(page, [element])


# ---------------------------------------------------------- fresh resolution


def test_a_fresh_reresolution_after_rerender_clicks_new_button_only(media):
    """X re-renders the composer at Ready: the pre-readiness button (A) becomes
    detached; only the freshly resolved button (B) may be clicked."""
    fresh_buttons = []
    state = {}

    def rerender():
        state["elements"]["active_post"].visible = False
        state["elements"]["active_post"].enabled = False
        fresh = node(
            POST_MODAL,
            enabled=True,
            tag="button",
            text="Post",
            attrs={"active-owner": "fresh"},
        )
        state["elements"]["active_dialog"].add(fresh)
        fresh_buttons.append(fresh)

    page, elements = build_page(
        active_button_selector=POST_INLINE,
        rerender_fn=lambda: rerender(),
    )
    state["elements"] = elements

    page.sent_toast = True
    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert len(fresh_buttons) == 1
    assert elements["active_post"].click_count == 0
    assert fresh_buttons[0].click_count == 1


def test_b_stale_hidden_dialog_with_enabled_button_never_clicked(media):
    """A hidden stale dialog holding an ENABLED Post must not supply the
    clicked button at any stage, including the pre-click fresh re-resolution."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        stale_dialog_visible=False,
        stale_button_enabled=True,
    )
    page.sent_toast = True

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert page.compose_finds == 2
    assert elements["stale_post"].click_count == 0
    assert elements["active_post"].click_count == 1


# ------------------------------------------------------- dedicated click budget


def test_c_click_has_short_dedicated_timeout_not_readiness_budget(media):
    """Button becomes enabled after 4s, click never actionable: the click gets
    the 15s post-click budget, never the remaining readiness deadline."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        active_button_enabled=False,
        ready_after_seconds=4,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
    )

    result = post(page, media, ready_timeout_s=180)

    assert result == {"ok": False, "reason": "post_button_click_timeout"}
    assert elements["active_post"].click_count == 1
    assert page.click_timeouts == [15000]
    # 4s readiness + 5s reconciliation — a far cry from the 180s readiness
    # deadline that the click used to inherit.
    assert page.elapsed == pytest.approx(9.0)
    assert page.elapsed < 30


def test_d_click_succeeds_immediately_without_extra_delay(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        active_button_enabled=False,
        ready_after_seconds=4,
        sent_toast=True,
    )

    result = post(page, media, ready_timeout_s=180)

    assert result == {"ok": True, "reason": "posted"}
    assert page.elapsed == pytest.approx(4.0)
    assert page.click_timeouts == [15000]
    assert elements["active_post"].click_count == 1


# ----------------------------------------------------- pointer-hit diagnostics


def test_e_pointer_interception_reports_overlay_outside_button():
    page, elements = build_page(active_button_selector=POST_MODAL)
    overlay = node(
        "div.overlay",
        tag="div",
        text="overlay",
        attrs={
            "role": "dialog",
            "data-testid": "layover",
            "aria-label": "Overlay",
            "class": "layover",
        },
    )
    page.element_from_point = overlay

    hit = XSession._pointer_hit_diagnostics(
        page, _button_locator(page, elements["active_post"])
    )

    assert hit["inside_post_button"] is False
    assert hit["is_button_itself"] is False
    assert hit["top"]["testid"] == "layover"
    assert hit["top"]["role"] == "dialog"
    assert hit["top"]["tag"] == "div"
    assert hit["top"]["aria_label"] == "Overlay"


def test_f_element_from_point_descendant_of_button_is_not_a_blocker():
    page, elements = build_page(active_button_selector=POST_MODAL)
    label = elements["active_post"].add(
        node("span.label", tag="span", text="Post", attrs={"aria-label": "Post"})
    )
    page.element_from_point = label

    hit = XSession._pointer_hit_diagnostics(
        page, _button_locator(page, elements["active_post"])
    )

    assert hit["inside_post_button"] is True
    assert hit["is_button_itself"] is False
    assert hit["top"]["tag"] == "span"


# ----------------------------------------------------- playwright error kept


def test_g_playwright_exception_text_is_preserved_in_diagnostics(media, caplog):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "post_button_click_timeout"}
    assert "another element intercepts pointer events" in caplog.text
    assert "Timeout 15000ms exceeded" in caplog.text
    assert "post_click_timeout_seconds" in caplog.text


# ------------------------------------------------- ambiguous timeout outcomes


def test_h_click_timeout_without_success_stays_timeout_failure(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "post_button_click_timeout"}
    assert elements["active_post"].click_count == 1


def test_i_click_timeout_but_success_toast_already_visible_is_posted(media, caplog):
    caplog.set_level(logging.INFO, logger="publisher")
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
        sent_toast=True,
    )

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["active_post"].click_count == 1
    assert "positive success confirmation was observed" in caplog.text


def test_j_success_toast_two_seconds_after_click_timeout_is_posted(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
        toast_after_seconds=2,
    )

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["active_post"].click_count == 1
    assert page.elapsed == pytest.approx(2.0)


def test_k_reconciliation_is_bounded_without_success(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "post_button_click_timeout"}
    assert elements["active_post"].click_count == 1
    assert page.elapsed == pytest.approx(5.0)
    assert page.elapsed < 10  # never 60/180 seconds


def test_l_compose_disappearance_without_success_is_unverified_not_posted(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
        navigate_on_click_timeout=True,
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "unverified"}
    assert page.url == HOME_URL
    assert elements["active_post"].click_count == 1
    assert elements["active_post"].click_count == 1  # never a second click


# ----------------------------------------------------------- normal click flow


def test_m_normal_click_with_positive_success_is_posted(media):
    page, elements = build_page(
        active_button_selector=POST_MODAL, sent_toast=True
    )

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["active_post"].click_count == 1


def test_n_normal_click_without_confirmation_is_not_success(media):
    page, elements = build_page(active_button_selector=POST_MODAL)

    result = post(page, media)

    assert result == {"ok": False, "reason": "timeout"}
    assert elements["active_post"].click_count == 1


def test_q_no_second_click_across_all_timeout_paths(media):
    """Instrumented click counts: exactly one click per post() attempt."""
    cases = [
        dict(post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR)),
        dict(
            post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
            sent_toast=True,
        ),
        dict(
            post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
            navigate_on_click_timeout=True,
        ),
    ]
    for kwargs in cases:
        page, elements = build_page(active_button_selector=POST_MODAL, **kwargs)
        post(page, media)
        assert elements["active_post"].click_count == 1, kwargs


def test_caption_lost_during_rerender_fails_before_click(media):
    """Fresh pre-click revalidation: a reset composer with a requested caption
    must not be posted silently."""
    state = {}

    def rerender():
        state["elements"]["active_composer"].typed.clear()

    page, elements = build_page(
        active_button_selector=POST_MODAL,
        rerender_fn=lambda: rerender(),
    )
    state["elements"] = elements

    result = post(page, media)

    assert result == {"ok": False, "reason": "caption_lost_before_click"}
    assert elements["active_post"].click_count == 0


def test_click_timeout_captures_best_effort_screenshot(tmp_path, media, caplog):
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
    )
    logs = tmp_path / "logs"
    session = XSession(
        {
            "browser_profile": "profile",
            "brave": "brave",
            "logs_dir": str(logs),
        }
    )
    session.new_page = lambda: page
    with mock.patch("publisher.x_publisher.time.monotonic", page.clock.time):
        result = session.post(
            "owned caption",
            [media],
            timeout_s=1,
            media_kind="video",
            ready_timeout_s=1,
            post_click_timeout_s=15,
        )

    assert result == {"ok": False, "reason": "post_button_click_timeout"}
    assert len(page.screenshot_calls) == 1
    screenshot_path = page.screenshot_calls[0]
    assert screenshot_path.startswith(str(logs))
    assert Path(screenshot_path).exists()
    assert repr(screenshot_path) in caplog.text


# ------------------------------------------- post-click success over error UI


def test_a_success_and_error_coexist_after_normal_click_is_posted(media, caplog):
    """'Your post was sent' AND 'Something went wrong' visible after a normal
    click: the explicit positive confirmation wins."""
    caplog.set_level(logging.INFO, logger="publisher")
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        sent_toast=True,
        problem_text="Something went wrong",
        problem_on_click=True,
    )

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["active_post"].click_count == 1
    assert "takes precedence" in caplog.text


def test_b_click_timeout_success_and_error_coexist_is_posted(media):
    """The most important regression: click() timed out, but during
    reconciliation both the positive confirmation and a generic X problem are
    visible. Success wins and the click was attempted exactly once."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
        sent_toast=True,
        problem_text="Something went wrong",
        problem_on_click=True,
    )

    result = post(page, media)

    assert result == {"ok": True, "reason": "posted"}
    assert elements["active_post"].click_count == 1


def test_c_generic_error_only_after_normal_click_is_failure(media):
    """Generic X error with no positive confirmation after a normal click
    still fails; success precedence does not disable error detection."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        problem_text="Something went wrong",
        problem_on_click=True,
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "error"}
    assert elements["active_post"].click_count == 1


def test_d_generic_error_only_after_click_timeout_is_failure(media, caplog):
    """A click timeout with only a generic problem visible stays a failure;
    not every ambiguous timeout becomes success."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
        problem_text="Something went wrong",
        problem_on_click=True,
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "error"}
    assert elements["active_post"].click_count == 1
    assert "timed out and X reported a problem" in caplog.text


def test_g_pre_click_problem_still_blocks_posting(media):
    """Phase boundary: a problem visible BEFORE the click attempt stops the
    post with zero clicks, even when a success toast is already on screen."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        sent_toast=True,
        problem_text="Something went wrong",
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "error"}
    assert elements["active_post"].click_count == 0


def test_h_no_success_inference_before_click_attempt(media):
    """A success toast present before this session's Post action must never
    yield 'posted' without an actual click attempt."""
    page, elements = build_page(
        active_button_selector=POST_MODAL, sent_toast=True
    )
    elements["active_post"].visible = False

    result = post(page, media)

    assert result["ok"] is False
    assert result["reason"] != "posted"
    assert elements["active_post"].click_count == 0


def test_i_error_before_success_terminates_reconciliation(media):
    """Pinned policy: during reconciliation a generic X problem observed
    without the positive confirmation immediately fails the post. The success
    toast arriving later is never reached — no speculative grace window."""
    page, elements = build_page(
        active_button_selector=POST_MODAL,
        post_click_error=PlaywrightTimeoutError(CLICK_TIMEOUT_ERROR),
        problem_text="Something went wrong",
        problem_on_click=True,
        toast_after_seconds=2,
    )

    result = post(page, media)

    assert result == {"ok": False, "reason": "error"}
    assert elements["active_post"].click_count == 1
    assert page.elapsed < 1.0
