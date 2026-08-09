"""Scraper correctness regression tests (Issues 1A/1B/1C).

All scraper behaviour is exercised with deterministic fake page/locator DOM
trees — no live X, no live TikTok, no browser, no network.

Guarantees proven here:
  * X      — min_likes is an absolute engagement threshold that media can
             never bypass (Issue 1A), including missing/unparseable counts.
  * TikTok — feed metadata (likes/description) is scoped per video card and
             never crosses between videos (Issue 1B).
  * TikTok — foryou + accounts is additive, with per-id dedup (Issue 1C).
"""
from types import SimpleNamespace

import pytest

from scrapers import tiktok_scraper, x_scraper


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
    """A fake Playwright-style locator over a list of nodes.

    ``.first`` returns a locator narrowed to the first match so that callers
    can chain ``get_attribute``/``inner_text``/``count`` exactly like the real
    Playwright API.
    """

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def count(self):
        return len(self._nodes)

    def all(self):
        return list(self._nodes)

    def nth(self, i):
        return self._nodes[i]

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
        return self._nodes[0].locator(selector) if self._nodes else FakeLocator([])


class _StubMouse:
    def wheel(self, *a, **k):
        pass


class FakePage:
    def __init__(self, articles=None, cards=None):
        self._articles = articles or []
        self._cards = cards or []
        self.mouse = _StubMouse()
        self.visited = []

    def goto(self, url, **kwargs):
        self.visited.append(url)
        return None

    def wait_for_timeout(self, ms):
        return None

    def locator(self, selector):
        if selector == 'article[data-testid="tweet"]':
            return FakeLocator(self._articles)
        return FakeLocator(self._cards)

    def close(self):
        return None


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


def _session(page):
    return SimpleNamespace(new_page=lambda: page)


# ------------------------------------------------------------- Issue 1A: X

class XArticle:
    def __init__(self, like_text="0", has_media=False, has_link=True, media_url=None):
        self._like_text = like_text
        self._has_media = has_media
        self._media_url = media_url
        self._has_link = has_link

    def locator(self, selector):
        if selector == 'div[data-testid="tweetText"]':
            return FakeLocator([FakeNode(text="some tweet")])
        if selector == 'button[data-testid="like"] span[data-testid="app-text-transition-container"]':
            if self._like_text is None:
                return FakeLocator([])  # like button missing entirely
            return FakeLocator([FakeNode(text=self._like_text)])
        if selector == 'a[href*="/status/"]':
            if not self._has_link:
                return FakeLocator([])
            return FakeLocator([FakeNode(attrs={"href": "/handle/status/123"})])
        if selector == 'img[src*="pbs.twimg.com/media"]':
            if not self._has_media:
                return FakeLocator([])
            src = self._media_url or "https://pbs.twimg.com/media/photo.jpg"
            return FakeLocator([FakeNode(attrs={"src": src})])
        if selector == "video":
            return FakeLocator([])
        return FakeLocator([])


def _x_config(**overrides):
    cfg = {
        "accounts": ["@memelord"],
        "min_likes": 5000,
        "max_posts_per_account": 10,
        "scrolls": 0,
    }
    cfg.update(overrides)
    return cfg


def _x_items(page, config=None):
    return x_scraper.scrape(_session(page), config or _x_config(), "assets")


# X Test A: low likes + media must be rejected (media never bypasses).
def test_x_low_likes_with_media_rejected():
    page = FakePage(articles=[XArticle(like_text="12", has_media=True)])
    assert _x_items(page, _x_config()) == []


# X Test B: likes == min_likes is accepted (boundary equality).
def test_x_min_likes_equality_accepted():
    page = FakePage(articles=[XArticle(like_text="5000", has_media=True)])
    items = _x_items(page, _x_config())
    assert len(items) == 1
    assert items[0]["score"] == 5000.0


# X Test C: high likes with media accepted.
def test_x_high_likes_with_media_accepted():
    page = FakePage(articles=[XArticle(like_text="10000", has_media=True)])
    items = _x_items(page, _x_config())
    assert len(items) == 1
    assert items[0]["source"] == "x"
    assert items[0]["kind"] == "image"


# X Test D: unparseable like count never bypasses the threshold.
def test_x_unparseable_likes_rejected():
    page = FakePage(articles=[XArticle(like_text="abc", has_media=True)])
    assert _x_items(page, _x_config()) == []


# X Test D2: missing like-count element is treated as 0 (fail-safe).
def test_x_missing_like_count_rejected_conservatively():
    page = FakePage(articles=[XArticle(like_text=None, has_media=True)])
    assert _x_items(page, _x_config()) == []


def test_x_min_likes_zero_still_passes_unknown():
    page = FakePage(articles=[XArticle(like_text=None, has_media=True)])
    assert len(_x_items(page, _x_config(min_likes=0))) == 1  # 0 >= 0


# ------------------------------------------------------------------ Issue 1B TikTok

def _tt_card_node(like_text=None, caption="", has_desc=True):
    children = {'a[href*="/video/"]': [FakeNode(attrs={"href": "/video/child"})]}
    if like_text is not None:
        children['[data-e2e="like-count"]'] = [FakeNode(text=like_text)]
    if has_desc:
        children['[data-e2e="video-card-desc"], [data-e2e="video-desc"]'] = [
            FakeNode(text=caption)
        ]
    return FakeNode(children=children)


def _tt_node(video="111", like_count=None, has_like=True, desc_text="", has_desc=True):
    node = _tt_card_node(like_text=like_count, caption=desc_text, has_desc=has_desc)
    node.children['a[href*="/video/"]'][0].attrs = {"href": f"/video/{video}"}
    if not has_like:
        node.children.pop('[data-e2e="like-count"]', None)
    return node


def _feed_page(cards):
    return FakePage(cards=cards)


def _tt_config(**overrides):
    cfg = {
        "foryou": True,
        "accounts": [],
        "min_likes": 1000,
        "max_posts_per_account": 10,
        "scrolls": 0,
    }
    cfg.update(overrides)
    return cfg


def _tt_items(page, config=None):
    return tiktok_scraper.scrape(_session(page), config or _tt_config(), "assets")


# TikTok Test A: two videos keep separate metadata (mandatory).
def test_tiktok_a_two_videos_keep_separate_metadata():
    cards = tiktok_scraper._collect_cards(
        _feed_page([
            _tt_node(video="111", like_count="100K", desc_text="alpha"),
            _tt_node(video="222", like_count="1.2K", desc_text="beta"),
        ]),
        10,
    )
    by_id = {c["id"]: c for c in cards}

    assert by_id["111"]["likes"] == 100_000
    assert by_id["111"]["caption"] == "alpha"
    assert by_id["222"]["likes"] == 1_200
    assert by_id["222"]["caption"] == "beta"


# TikTok Test B: per-video engagement threshold (below min_likes is never an item).
def test_tiktok_b_likes_below_threshold_rejected():
    cards = tiktok_scraper._collect_cards(
        _feed_page([
            _tt_node(video="111", like_count="5", desc_text="meh"),
            _tt_node(video="222", like_count="1.2K", desc_text="top"),
        ]),
        10,
    )
    config = _tt_config()  # min_likes=1000
    items = _tt_items(_feed_page([_tt_node(video="111", like_count="5"),
                                  _tt_node(video="222", like_count="1.2K")]),
                      config)
    ids = [i["source_id"] for i in items]
    assert "111" not in ids
    assert "222" in ids
    assert [i["score"] for i in items] == [1200.0]


# TikTok Test C: captions stay glued to their own video URL end-to-end.
def test_tiktok_c_caption_binds_to_own_video_url():
    page = _feed_page([
        _tt_node(video="111", like_count="100K", desc_text="alpha"),
        _tt_node(video="222", like_count="1.2K", desc_text="beta"),
    ])
    items = _tt_items(page, _tt_config(min_likes=0))
    by_url = {i["source_url"]: i["title"] for i in items}
    assert by_url["https://www.tiktok.com/video/111"] == "alpha"
    assert by_url["https://www.tiktok.com/video/222"] == "beta"


# TikTok Test D: a card with missing like/desc never borrows the neighbour's.
def test_tiktok_d_missing_metadata_does_not_borrow():
    cards = tiktok_scraper._collect_cards(
        _feed_page([
            _tt_node(video="111", like_count=None, has_like=False, desc_text="", has_desc=False),
            _tt_node(video="222", like_count="1.2K", desc_text="borrowed-me"),
        ]),
        10,
    )
    by_id = {c["id"]: c for c in cards}
    assert by_id["111"]["likes"] == 0
    assert by_id["111"]["caption"] == ""
    assert by_id["111"]["caption"] != by_id["222"]["caption"]


def test_tiktok_d2_unparseable_likes_rejected_under_threshold():
    items = _tt_items(_feed_page([_tt_node(video="111", like_count="abc")]), _tt_config())
    assert items == []


def test_tiktok_d3_min_likes_equality_accepted():
    items = _tt_items(
        _feed_page([_tt_node(video="111", like_count="1.0K")]),
        _tt_config(min_likes=1000),
    )
    assert [i["score"] for i in items] == [1000.0]


# ----------------------------------------------------------- Issue 1C: modes

# TikTok Test E: foryou + accounts is additive (both sources are browsed).
def test_tiktok_e_foryou_and_accounts_are_additive():
    page = _feed_page([_tt_node(video="111", like_count="100K", desc_text="a")])
    items = _tt_items(page, _tt_config(accounts=["@handl"]))
    assert "https://www.tiktok.com/" in page.visited
    assert "https://www.tiktok.com/@handl" in page.visited
    assert len(items) == 1


# TikTok F: foryou=false still reads curated accounts.
def test_tiktok_f_accounts_alone_with_foryou_off():
    page = _feed_page([_tt_node(video="111", like_count="100K", desc_text="a")])
    items = _tt_items(page, _tt_config(foryou=False, accounts=["@handl"], min_likes=0))
    assert "https://www.tiktok.com/" not in page.visited
    assert "https://www.tiktok.com/@handl" in page.visited
    assert [i["source_id"] for i in items] == ["111"]


# TikTok G: foryou=false + no accounts falls back to the feed.
def test_tiktok_g_empty_accounts_falls_back_to_feed():
    page = _feed_page([_tt_node(video="111", like_count="100K", desc_text="a")])
    items = _tt_items(page, _tt_config(foryou=False, min_likes=0))
    assert "https://www.tiktok.com/" in page.visited
    assert items != []


# TikTok H: the same video in both feed and accounts is deduplicated.
def test_tiktok_h_same_video_deduplicated_across_modes():
    page = _feed_page([_tt_node(video="111", like_count="1000", desc_text="dup")])
    items = _tt_items(page, _tt_config(min_likes=0))
    ids = [i["source_id"] for i in items]
    assert ids.count("111") == 1


# ----------------------------------------------- Scrape-level X feed loop

def test_x_scrape_iterates_each_account():
    page = FakePage(articles=[XArticle(like_text="5000", has_media=True)])
    items = _x_items(
        page,
        _x_config(accounts=["@memelord", "@otherguy"], min_likes=0),
    )
    assert {"https://x.com/memelord", "https://x.com/otherguy"} <= set(page.visited)
    assert len(items) == 2  # no cross-account dedup in the scraper by design


def test_x_item_exposed_fields():
    page = FakePage(articles=[XArticle(like_text="3000", has_media=True)])
    items = _x_items(page, _x_config(min_likes=0))
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "image"
    assert it["source_id"] == "123"
    assert it["source_url"] == "https://x.com/handle/status/123"
    assert it["score"] == 3000.0


# ------------------------------------------------------ TikTok duplicate merge

def _merge_dict(video_id, likes, caption):
    return {
        "id": video_id,
        "href": f"https://www.tiktok.com/video/{video_id}",
        "likes": likes,
        "caption": caption,
    }


def _merge(existing, incoming):
    return tiktok_scraper._merge_tiktok_candidate(existing, incoming)


# Merge Test A — higher likes win regardless of discovery order.
def test_merge_a_higher_likes_preserved_order_independent():
    low = _merge_dict("111", 1000, "feed caption")
    high = _merge_dict("111", 9000, "account caption")
    assert _merge(low, high)["likes"] == 9000
    assert _merge(high, low)["likes"] == 9000


# Merge Test A (scrape level) — same-id duplicates in one feed merge to the best.
def test_merge_a_scrape_same_video_duplicates_keep_highest_likes():
    page = _feed_page([
        _tt_node(video="111", like_count="1000", desc_text="first"),
        _tt_node(video="111", like_count="9000", desc_text="second"),
        _tt_node(video="222", like_count="1.2K", desc_text="other"),
    ])
    items = _tt_items(page, _tt_config(min_likes=0))
    by_id = {i["source_id"]: i for i in items}
    assert by_id["111"]["score"] == 9000.0
    assert len(items) == 2


# Merge Test B — non-empty caption beats empty, both discovery orders.
def test_merge_b_caption_nonempty_beats_empty():
    empty = _merge_dict("111", 1, "")
    full = _merge_dict("111", 1, "actual caption")
    assert _merge(empty, full)["caption"] == "actual caption"
    assert _merge(full, empty)["caption"] == "actual caption"


# Merge Test C — missing/unknown likes (0) never overwrite a valid value.
def test_merge_c_missing_likes_do_not_overwrite():
    valid = _merge_dict("111", 5000, "top")
    missing = _merge_dict("111", 0, "top")
    assert _merge(valid, missing)["likes"] == 5000
    assert _merge(missing, valid)["likes"] == 5000


def test_merge_c_scrape_unknown_likes_never_replaces_valid():
    page = _feed_page([
        _tt_node(video="111", like_count="5000", desc_text="top"),
        _tt_node(video="111", like_count="abc", desc_text="dup"),
    ])
    items = _tt_items(page, _tt_config(min_likes=0))
    by_id = {i["source_id"]: i for i in items}
    assert by_id["111"]["score"] == 5000.0


# Merge Test D — feed + account exposing the same video yield exactly one candidate.
def test_merge_d_duplicate_emitted_once():
    page = _feed_page([_tt_node(video="111", like_count="1000", desc_text="dup")])
    items = _tt_items(page, _tt_config(foryou=True, accounts=["@handl"], min_likes=0))
    ids = [i["source_id"] for i in items]
    assert ids.count("111") == 1
    assert len(items) == 1


# Merge Test E — distinct videos must not be merged.
def test_merge_e_distinct_videos_remain_distinct():
    page = _feed_page([
        _tt_node(video="111", like_count="100K", desc_text="a"),
        _tt_node(video="222", like_count="1.2K", desc_text="b"),
    ])
    items = _tt_items(page, _tt_config(foryou=True, accounts=["@handl"], min_likes=0))
    assert {i["source_id"] for i in items} == {"111", "222"}


# Merge Test F — no metadata crossover when a duplicate observation is present.
def test_merge_f_no_metadata_crossover():
    page = _feed_page([
        _tt_node(video="111", like_count="1000", desc_text="alpha"),
        _tt_node(video="111", like_count="9000", desc_text="alpha2"),
        _tt_node(video="222", like_count="1.2K", desc_text="beta"),
    ])
    items = _tt_items(page, _tt_config(min_likes=0))
    by_id = {i["source_id"]: i for i in items}
    assert by_id["111"]["score"] == 9000.0
    assert by_id["111"]["title"] == "alpha2"          # video A's own caption only
    assert by_id["222"]["score"] == 1200.0
    assert by_id["222"]["title"] == "beta"            # video B's own caption only
