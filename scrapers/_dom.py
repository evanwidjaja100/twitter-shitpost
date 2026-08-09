"""Shared, tiny DOM helper for the scrapers.

The X/TikTok scrapers execute untrusted third-party markup. When a site changes
its DOM, the goal is a *localized, safe* failure: the drift is found, the card
either still parses via a known fallback selector or is skipped conservatively,
and it never fabricates or mis-scopes metadata.

This module provides two primitives on top of the Playwright-style
``page/locator/card`` API (and the deterministic fakes used in tests):

* ``first_matching_locator(scope, selectors)`` — try an ordered list of
  selectors against one scope (a page OR a single card element) and return the
  first that has matches, else ``None``. Returning ``None`` (instead of a
  broad page-global match) is what keeps a missing sub-element safe.
* ``iter_matching_nodes(scope, selectors)`` — the elements of the first
  selector that matches (used for card containers).

Rules enforced by design:
* The scope for per-item metadata is ALWAYS the item's own container element;
  a selector that finds nothing yields an empty/"0"/None value — the code never
  falls back to another card's element or to page-global text.
* Ordered fallbacks only replace a *selector string*, never the scoping root,
  so a fallback cannot pull metadata across card boundaries.
* When no selector matches at all, the result is ``None``/empty so callers can
  distinguish "no qualifying candidates" from "the page structure disappeared".
"""


def first_matching_locator(scope, selectors):
    """Return ``scope.locator(sel).first`` for the first selector in
    ``selectors`` that matches at least one element, else ``None``.

    ``scope`` is a page or an element locator; it must answer
    ``.locator(selector)`` and the returned locator must answer ``.count()``.
    """
    for selector in selectors:
        try:
            loc = scope.locator(selector)
            if loc.count():
                return loc.first
        except Exception:
            continue
    return None


def iter_matching_nodes(scope, selectors):
    """Return the list of elements matched by the first matching selector, or
    ``[]`` when none match. Each element keeps the exact matching selector's
    scoping root, so per-item reads stay inside that element."""
    for selector in selectors:
        try:
            loc = scope.locator(selector)
            if loc.count():
                return loc.all()
        except Exception:
            continue
    return []