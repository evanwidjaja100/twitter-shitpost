"""Playwright publisher for x.com — composes, attaches media, posts, verifies.

Uses a persistent Brave profile (no API keys, $0 cost). All x.com selectors live
in this module so UI changes are fixed in one place.
"""

import logging
import random
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

log = logging.getLogger("publisher")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Realistic Windows UA matching the installed Brave. X's bot detection sniffs the
# UA + navigator.webdriver; sending a clean UA is part of not being flagged.
BRAVE_WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Launch switches applied to the real Brave binary. The last one is the key
# anti-automation switch: it stops Chromium from exposing the "AutomationControlled"
# marker that Playwright otherwise leaves behind.
BROWSER_EXTRA_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-component-update",
    "--disable-blink-features=AutomationControlled",
]

# Injected on every page so X/Twitter (and other anti-bot pages) can't tell the
# browser is automated. Overrides the webdriver flag and restores the usual chrome
# objects that Playwright removes.
ANTI_DETECT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {}, loadTimes: function(){}, csi: function(){} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(parameters)
    );
}
"""


def install_anti_detection(context) -> None:
    """Hide automation fingerprints on a launched browser context.

    Shared by login.py and the publisher so the manual login and the daemon post
    through identical, non-flagging browser settings.
    """
    context.add_init_script(ANTI_DETECT_JS)

LOGGED_OUT_PATTERNS = [
    re.compile(r"log\s*in", re.I),
    re.compile(r"sign\s*in", re.I),
]
CAPTCHA_PATTERNS = [
    re.compile(r"not\s+a\s*bot", re.I),
    re.compile(r"verify\s+your\s+identity", re.I),
    re.compile(r"confirm\s+you", re.I),
]
ERROR_PATTERNS = [
    re.compile(r"something went wrong", re.I),
    re.compile(r"over the daily", re.I),
    re.compile(r"rate limit", re.I),
    re.compile(r"you are not permitted", re.I),
    re.compile(r"can.t (send|post|tweet)", re.I),
]

# Ordered, maintainable X compose selectors. The stable test ID is deliberately
# tag-agnostic: X currently renders a contenteditable ``div`` but has rendered
# a ``textarea`` in the past. Fallback textboxes are restricted to known compose
# surfaces so search, messages, and unrelated page editors cannot be selected.
COMPOSER_SELECTORS = (
    '[data-testid="tweetTextarea_0"]',
    '[role="dialog"] [role="textbox"][contenteditable="true"]',
    '[data-testid="primaryColumn"] [role="textbox"][contenteditable="true"]',
)
FILE_INPUT_SELECTORS = ('[data-testid="fileInput"]',)
ATTACHMENT_SELECTORS = ('[data-testid="attachments"]',)
POST_BUTTON_SELECTORS = ('[data-testid="tweetButtonInline"]',)


class PublishError(Exception):
    """Raised when a required compose control cannot be located safely."""


def load_config_paths(config_path=None) -> dict:
    """Load paths only after the complete config passes central validation."""
    from config_validation import load_validated_config

    default_base = Path(__file__).resolve().parent.parent
    cfg_path = (
        Path(config_path)
        if config_path is not None
        else default_base / "config.json"
    )
    cfg = load_validated_config(cfg_path)
    base = cfg_path.resolve().parent
    paths = dict(cfg.get("paths", {}))
    paths["_base"] = base
    return paths


def resolve_config_path(paths: dict, key: str) -> str:
    val = paths.get(key, "")
    if not val:
        return ""
    p = Path(val)
    if not p.is_absolute():
        base = paths.get("_base") or Path(__file__).resolve().parent.parent
        p = base / p
    return str(p.resolve())


# Backward-compatible private name used by older callers/tests.
_resolved = resolve_config_path


class XSession:
    def __init__(self, paths: dict):
        self.profile_dir = resolve_config_path(paths, "browser_profile")
        self.brave = resolve_config_path(paths, "brave")
        self._playwright = None
        self._context = None

    def start(self):
        if self._context is not None:
            return
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            executable_path=self.brave,
            headless=False,
            viewport={"width": 1280, "height": 900},
            user_agent=BRAVE_WINDOWS_UA,
            locale="en-US",
            args=list(BROWSER_EXTRA_ARGS),
        )
        install_anti_detection(self._context)

    def stop(self):
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._context = None
            self._playwright = None

    def new_page(self) -> Page:
        return self._context.new_page()

    # ------------------------------------------------------------- checks

    @staticmethod
    def detect_problem(page: Page, text: str | None = None) -> str | None:
        """Return a reason string if a blocking problem is visible, else None."""
        if text is None:
            try:
                text = page.locator("body").inner_text(timeout=3000)
            except Exception:
                return None
        text = text[:6000]
        if any(pat.search(text) for pat in LOGGED_OUT_PATTERNS) and page.locator(
            'a[href="/login"]'
        ).count():
            return "login"
        if any(pat.search(text) for pat in CAPTCHA_PATTERNS):
            return "captcha"
        if any(pat.search(text) for pat in ERROR_PATTERNS):
            return "error"
        return None

    @staticmethod
    def _type_humanized(composer, text: str):
        for chunk in [text[i : i + 3] for i in range(0, len(text), 3)]:
            composer.press_sequentially(chunk, delay=random.randint(30, 180))

    @classmethod
    def _log_dom_failure(cls, page: Page, stage: str, selectors) -> None:
        """Best-effort, non-sensitive diagnostics for X DOM drift."""
        try:
            url = page.url
        except Exception:
            url = "<unavailable>"
        try:
            title = page.title()
        except Exception:
            title = "<unavailable>"
        selector_counts = {}
        for selector in selectors:
            try:
                selector_counts[selector] = page.locator(selector).count()
            except Exception:
                selector_counts[selector] = "error"
        try:
            detected_problem = cls.detect_problem(page)
        except Exception:
            detected_problem = None
        log.warning(
            "%s selector failure: url=%r title=%r detected_problem=%r "
            "selector_counts=%r",
            stage,
            url,
            title,
            detected_problem,
            selector_counts,
        )

    @classmethod
    def _first_matching_locator(
        cls,
        page: Page,
        selectors,
        *,
        state: str,
        timeout_ms: int,
        failure_reason: str,
        stage: str,
    ):
        """Return the first ordered selector matching the requested state.

        The total caller timeout is divided across selectors, keeping fallback
        discovery bounded instead of multiplying the configured timeout by the
        number of fallbacks. Visible lookups filter hidden duplicate nodes
        before selecting ``first``.
        """
        selectors = tuple(selectors)
        per_selector_timeout = max(1, timeout_ms // max(1, len(selectors)))
        for selector in selectors:
            locator = page.locator(selector)
            if state == "visible":
                locator = locator.filter(visible=True)
            candidate = locator.first
            try:
                candidate.wait_for(
                    state=state,
                    timeout=per_selector_timeout,
                )
                return candidate
            except Exception:
                continue
        cls._log_dom_failure(page, stage, selectors)
        raise PublishError(failure_reason)

    # ------------------------------------------------------------- posting

    def post(self, caption: str, media_paths: list[str], timeout_s: int = 60) -> dict:
        """Post one tweet with attached media. Returns {"ok": bool, "reason": str}."""
        timeout_ms = max(1, int(timeout_s * 1000))
        for p in media_paths:
            if not Path(p).exists():
                return {"ok": False, "reason": f"missing media file {p}"}

        page = self.new_page()
        try:
            page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=45000)

            if problem := self.detect_problem(page):
                return {"ok": False, "reason": problem}

            composer = self._first_matching_locator(
                page,
                COMPOSER_SELECTORS,
                state="visible",
                timeout_ms=timeout_ms,
                failure_reason=(
                    "composer_not_found: known X composer selectors were not visible"
                ),
                stage="composer",
            )

            file_input = self._first_matching_locator(
                page,
                FILE_INPUT_SELECTORS,
                state="attached",
                timeout_ms=timeout_ms,
                failure_reason="file_input_not_found: X media input was not attached",
                stage="file_input",
            )
            file_input.set_input_files(media_paths)

            self._first_matching_locator(
                page,
                ATTACHMENT_SELECTORS,
                state="visible",
                timeout_ms=timeout_ms,
                failure_reason="attachments_not_found: X media attachment was not visible",
                stage="attachments",
            )

            if caption:
                composer.click()
                self._type_humanized(composer, caption)

            if problem := self.detect_problem(page):
                return {"ok": False, "reason": problem}

            post_btn = self._first_matching_locator(
                page,
                POST_BUTTON_SELECTORS,
                state="visible",
                timeout_ms=timeout_ms,
                failure_reason="post_button_not_found: X Post button was not visible",
                stage="post_button",
            )
            post_btn.click()

            # Success must be positively confirmed. Navigations, login/captcha
            # redirects or a changed URL are NEVER treated as proof of posting.
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                # Login/captcha/error state is checked before interpreting any
                # navigation so a redirect to those pages fails the post.
                if problem := self.detect_problem(page):
                    return {"ok": False, "reason": problem}
                if page.locator("text=Your post was sent").count() > 0:
                    return {"ok": True, "reason": "posted"}
                try:
                    if "compose/post" not in page.url:
                        # Navigated away without the success signal and without
                        # a detected problem: ambiguous, never claim success.
                        return {"ok": False, "reason": "unverified"}
                except Exception:
                    pass
                page.wait_for_timeout(1500)
            return {"ok": False, "reason": "timeout"}

        except PublishError as e:
            return {"ok": False, "reason": str(e)}
        except Exception as e:
            return {"ok": False, "reason": f"exception: {e}"}
        finally:
            try:
                page.close()
            except Exception:
                pass
