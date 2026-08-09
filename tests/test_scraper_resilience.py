"""Scraper DOM-resilience tests (Issue 3).

Deterministic fake DOM trees only — no live X, no live TikTok, no browser,
no network. Proves the resilience contract:

* Ordered selector fallbacks work at the *selector-string* level while the
  scoping root stays the item's own container (metadata never crosses cards).
* A container page that matches nothing yields a diagnostics warning and ZERO
  fabricated items (correct failure beats wrong data).
* A malformed card is isolated — it may produce an empty/None value but never
  stops the other cards or borrows their metadata.
* TikTok duplicate observations merge deterministically regardless of order.
"""
from types import SimpleNamespace

import pytest

from scrapers import tiktok_scraper, x_scraper
from scrapers._dom import first_matching_locator, iter_matching_nodes

# Real selector strings, read from the modules so the tests can never drift.
X_ARTICLE = x_scraper.X_SELECTORS["article"]
X_LIKE = x_scraper.X_SELECTORS["like_count"][0]
X_LINK = x_scraper.X_SELECTORS["status_link"][0]
X_IMG = x_scraper.X_SELECTORS["media_img"][0]
X_TEXT = x_scraper.X_SELECTORS["tweet_text"][0]

TT_CARD = tiktok_scraper.TIKTOK_SELECTORS["card"]
TT_LINK = tiktok_scraper.TIKTOK_SELECTORS["video_link"][0]
TT_LIKE = tiktok_scraper.TIKTOK_SELECTORS["like_count"][0]
TT_DESC = tiktok_scraper.TIKTOK_SELECTORS["description"][0]

IMG_SRC = "https://pbs.twimg.com/media/abc.jpg"

# ---------------------------------------------------------------- generic fakes

class FakeNode:
    """One fake DOM node: attributes, text, and a selector->children map."""

    def __init__(self, attrs=None, text=None, children=None):
        self.attrs = attrs or {}
        self.text = text
        self.children = children or {}

    def get_attribute(self, name):
        return self.attrs.get(name)

    def count(self):
        return 1

    def locator(self, selector):
        return FakeLocator(list(self.children.get(selector, [])))

    def all(self):
        return FakeLocator([self])


class FakeLocator:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def count(self):
        return len(self._nodes)

    def all(self):
        return list(self._nodes)

    @property
    def first(self):
        if not self._nodes:
            return FakeLocator([])
        return FakeLocator([self._nodes[0]])

    def get_attribute(self, name):
        return self._nodes[0].get_attribute(name) if self._nodes else None

    def inner_text(self, timeout=None):
        return self._nodes[0].text or "" if self._nodes else ""

    def locator(self, selector):
        if not self._nodes:
            return FakeLocator([])
        return self._nodes[0].locator(selector)


class _StubMouse:
    def wheel(self, *a, **k):
        pass


class FakePage:
    """A page whose ``selector -> nodes`` is supplied by the test."""

    def __init__(self, match):
        self._match = match
        self.mouse = _StubMouse()
        self.visited = []

    def locator(self, selector):
        return FakeLocator(list(self._match.get(selector, [])))

    def goto(self, url, **kwargs):
        self.visited.append(url)
        return None

    def wait_for_timeout(self, ms):
        return None

    def close(self):
        return None


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


def _session(page):
    return SimpleNamespace(new_page=lambda: page)


def _x_tweet_card(status_id, likes, has_img=True, has_link=True):
    children = {X_TEXT: [FakeNode(text="some tweet")]}
    if has_link:
        children[X_LINK] = [FakeNode(attrs={"href": f"/handle/status/{status_id}"})]
    children[X_LIKE] = [FakeNode(text=likes)]
    if has_img:
        children[X_IMG] = [FakeNode(attrs={"src": IMG_SRC})]
    return [FakeNode(children=children)]


def _tt_card(video_id, likes, caption=""):
    children = {TT_LINK: [FakeNode(attrs={"href": f"/@u/video/{video_id}"})]}
    children[TT_LIKE] = [FakeNode(text=likes)]
    if caption:
        children[TT_DESC] = [FakeNode(text=caption)]
    return [FakeNode(children=children)]


# ---------------------------------------------------------------- _dom helpers

def test_first_matching_locator_tries_ordered_selectors():
    scope = FakeNode(children={
        "missing": [],
        "present": [FakeNode(text="found")],
        "other": [FakeNode(text="ignored")],
    })
    loc = first_matching_locator(scope, ["missing", "present", "other"])
    assert loc is not None
    assert loc.inner_text() == "found"


def test_first_matching_locator_none_when_nothing_matches():
    scope = FakeNode(children={"a": [], "b": []})
    assert first_matching_locator(scope, ["a", "b"]) is None


def test_iter_matching_nodes_empty_when_nothing_matches():
    scope = FakeNode(children={"a": [], "b": []})
    assert iter_matching_nodes(scope, ["a", "b"]) == []


def test_iter_matching_nodes_returns_first_matching_selector_nodes():
    scope = FakeNode(children={"u": [FakeNode(text="1"), FakeNode(text="2")]})
    nodes = iter_matching_nodes(scope, ["u", "v"])
    assert [n.text for n in nodes] == ["1", "2"]


# ------------------------------------------------------------------ X scraper

def test_x_primary_selector_used_when_present():
    page = FakePage({X_ARTICLE[0]: _x_tweet_card(11, "5K")})
    items, stats = x_scraper._scrape_site(_session(page), {"accounts": ["@a"]}, "assets")
    assert stats["containers"] == 1
    assert len(items) == 1
    assert items[0]["source"] == "x"
    assert items[0]["source_id"] == "11"
    assert items[0]["kind"] == "image"


def test_x_fallback_selector_used_when_primary_dom_drifted():
    page = FakePage({X_ARTICLE[1]: _x_tweet_card(2, "5K")})
    items, stats = x_scraper._scrape_site(_session(page), {"accounts": ["@x"]}, "assets")
    assert stats["containers"] == 1
    assert len(items) == 1
    assert items[0]["source_id"] == "2"


def test_x_no_containers_yields_zero_candidates_and_failure_flag():
    page = FakePage(match={})
    items, stats = x_scraper._scrape_site(_session(page), {"accounts": ["@x"]}, "assets")
    assert items == []
    assert stats["containers"] == 0
    assert stats["selector_failures"] == 1


@pytest.mark.parametrize("likes,min_likes,has_img,expected", [
    ("5K", 5000, True, 1),
    ("0", 5000, True, 0),     # like 0 under threshold -> skipped
    ("5K", 5000, False, 0),   # no media -> not a candidate (matches production)
    ("", 0, True, 1),         # no like element + min 0 -> still parsed if media
])
def test_x_missing_or_low_likes_is_conservative(likes, min_likes, has_img, expected):
    children = {
        X_TEXT: [FakeNode(text="t")],
        X_LINK: [FakeNode(attrs={"href": "/u/status/42"})],
    }
    if likes:
        children[X_LIKE] = [FakeNode(text=likes)]
    if has_img:
        children[X_IMG] = [FakeNode(attrs={"src": IMG_SRC})]
    page = FakePage({X_ARTICLE[0]: [FakeNode(children=children)]})
    items, _ = x_scraper._scrape_site(
        _session(page), {"accounts": ["@x"], "min_likes": min_likes}, "assets"
    )
    assert len(items) == expected


def test_x_malformed_card_isolated_from_healthy_card():
    # Card with no status link but a huge like count must be skipped as
    # incomplete and must NOT borrow/merge metadata with the healthy card.
    bad = FakeNode(children={X_LIKE: [FakeNode(text="9M")]})
    page = FakePage({X_ARTICLE[0]: [bad] + _x_tweet_card(1, "5K")})
    items, stats = x_scraper._scrape_site(_session(page), {"accounts": ["@x"], "min_likes": 0}, "assets")
    assert len(items) == 1
    assert items[0]["source_id"] == "1"
    assert stats["incomplete"] == 1


def test_x_text_only_card_is_not_a_candidate_no_crash():
    page = FakePage({X_ARTICLE[0]: _x_tweet_card(7, "100", has_img=False)})
    items, stats = x_scraper._scrape_site(_session(page), {"accounts": ["@x"], "min_likes": 0}, "assets")
    assert items == []
    assert stats["parsed"] == 1  # parsed but deliberately not a candidate


# -------------------------------------------------------------- TikTok scraper

def test_tiktok_scopes_metadata_to_each_card():
    page = FakePage({TT_CARD[0]: _tt_card(101, "1.2K", "first") + _tt_card(102, "45", "second")})
    cards = tiktok_scraper._collect_cards(page, 10)
    by_id = {c["id"]: c for c in cards}
    assert by_id["101"]["likes"] == 1200
    assert by_id["101"]["caption"] == "first"
    assert by_id["102"]["likes"] == 45
    assert by_id["102"]["caption"] == "second"


def test_tiktok_merge_is_order_independent():
    low = tiktok_scraper._card("/@u/video/5", 100, "cap")
    high = tiktok_scraper._card("/@u/video/5", 10000, "")
    assert tiktok_scraper._merge_tiktok_candidate(None, low) == low
    merged = tiktok_scraper._merge_tiktok_candidate(low, high)
    assert merged["likes"] == 10000
    assert merged["caption"] == "cap"  # non-empty caption wins
    rev = tiktok_scraper._merge_tiktok_candidate(high, low)
    assert rev == merged  # order independent


def test_tiktok_better_caption_nondeterministic_tie_keeps_first():
    assert tiktok_scraper._better_caption("", "x") == "x"
    assert tiktok_scraper._better_caption("x", "") == "x"
    assert tiktok_scraper._better_caption("short", "longer") == "longer"
    assert tiktok_scraper._better_caption("ab", "xy") == "ab"  # equal length


def test_tiktok_unparseable_likes_treated_as_zero():
    card = FakeNode(children={
        TT_LINK: [FakeNode(attrs={"href": "/@u/video/3"})],
        TT_LIKE: [FakeNode(text="garbage")],
    })
    page = FakePage({TT_CARD[0]: [card]})
    cards = tiktok_scraper._collect_cards(page, 10)
    assert cards[0]["likes"] == 0


def test_tiktok_card_without_video_link_skipped():
    card = FakeNode(children={TT_LIKE: [FakeNode(text="9M")]})
    page = FakePage({TT_CARD[0]: [card]})
    cards = tiktok_scraper._collect_cards(page, 10)
    assert cards == []


def test_tiktok_fallback_card_selector_used_when_feed_item_drifted():
    page = FakePage({TT_CARD[1]: _tt_card_elsewhere()})
    cards = tiktok_scraper._collect_cards(page, 10)
    assert len(cards) == 1
    assert cards[0]["id"] == "99"


def _tt_card_elsewhere():
    return [FakeNode(children={
        TT_LINK: [FakeNode(attrs={"href": "/@u/video/99"})],
        TT_LIKE: [FakeNode(text="5")],
    })]


def test_tiktok_zero_cards_returns_empty():
    page = FakePage(match={})
    cards = tiktok_scraper._collect_cards(page, 10)
    assert cards == []
