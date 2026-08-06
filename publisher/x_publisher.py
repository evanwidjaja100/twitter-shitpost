"""Playwright publisher for x.com — composes, attaches media, posts, verifies.

Uses a persistent Brave profile (no API keys, $0 cost). All x.com selectors live
in this module so UI changes are fixed in one place.
"""

import json
import random
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

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


class PublishError(Exception):
    """Raised with a stable reason string (login|captcha|error|timeout)."""


def load_config_paths() -> dict:
    base = Path(__file__).resolve().parent.parent
    cfg_path = base / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"missing config.json (copy config.example.json)")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    paths = dict(cfg.get("paths", {}))
    paths["_base"] = base
    return paths


def _resolved(paths: dict, key: str) -> str:
    val = paths.get(key, "")
    if not val:
        return ""
    p = Path(val)
    if not p.is_absolute():
        base = paths.get("_base") or Path(__file__).resolve().parent.parent
        p = base / p
    return str(p.resolve())


class XSession:
    def __init__(self, paths: dict):
        self.profile_dir = _resolved(paths, "browser_profile")
        self.brave = _resolved(paths, "brave")
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
    def _type_humanized(page: Page, text: str):
        for chunk in [text[i : i + 3] for i in range(0, len(text), 3)]:
            page.keyboard.type(chunk)
            page.wait_for_timeout(random.randint(30, 180))

    # ------------------------------------------------------------- posting

    def post(self, caption: str, media_paths: list[str], timeout_s: int = 60) -> dict:
        """Post one tweet with attached media. Returns {"ok": bool, "reason": str}."""
        for p in media_paths:
            if not Path(p).exists():
                return {"ok": False, "reason": f"missing media file {p}"}

        page = self.new_page()
        try:
            page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=45000)

            if problem := self.detect_problem(page):
                return {"ok": False, "reason": problem}

            composer = page.locator('textarea[data-testid="tweetTextarea_0"]')
            composer.wait_for(state="visible", timeout=timeout_s)

            file_input = page.locator('input[data-testid="fileInput"]')
            file_input.wait_for(state="attached", timeout=timeout_s)
            file_input.set_input_files(media_paths)

            page.locator('div[data-testid="attachments"]').wait_for(
                state="visible", timeout=timeout_s
            )

            if caption:
                self._type_humanized(page, caption)

            if problem := self.detect_problem(page):
                return {"ok": False, "reason": problem}

            post_btn = page.locator('button[data-testid="tweetButtonInline"]')
            post_btn.wait_for(state="visible", timeout=timeout_s)
            post_btn.click()

            deadline = time.time() + timeout_s
            sent = False
            while time.time() < deadline:
                if page.locator("text=Your post was sent").count() > 0:
                    sent = True
                    break
                try:
                    if "compose/post" not in page.url:
                        sent = True
                        break
                except Exception:
                    pass
                if problem := self.detect_problem(page):
                    return {"ok": False, "reason": problem}
                page.wait_for_timeout(1500)

            if not sent:
                return {"ok": False, "reason": "timeout"}
            return {"ok": True, "reason": "posted"}

        except Exception as e:
            return {"ok": False, "reason": f"exception: {e}"}
        finally:
            try:
                page.close()
            except Exception:
                pass
