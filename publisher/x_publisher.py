"""Playwright publisher for x.com — composes, attaches media, posts, verifies.

Uses a persistent Brave profile (no API keys, $0 cost). All x.com selectors live
in this module so UI changes are fixed in one place.
"""

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
    "--start-maximized",
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
MEDIA_ERROR_PATTERNS = (
    ("upload_failed", re.compile(r"\b(?:media|photo|video|file) failed to upload\b", re.I)),
    ("upload_failed", re.compile(r"\bupload failed\b", re.I)),
    ("could_not_upload", re.compile(r"\b(?:media|photo|video file|video|file) could not be uploaded\b", re.I)),
    ("video_processing_failed", re.compile(r"\bvideo could not be processed\b", re.I)),
    ("media_processing_failed", re.compile(r"\bmedia processing failed\b", re.I)),
    ("unsupported_media", re.compile(r"\bunsupported (?:media|video|image|file)\b", re.I)),
    ("file_too_large", re.compile(r"\bfile (?:is )?too large\b", re.I)),
)

READINESS_POLL_MS = 250

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
POST_BUTTON_SELECTORS = (
    '[data-testid="tweetButton"]',
    '[data-testid="tweetButtonInline"]',
)
POST_BUTTON_QUERY = ", ".join(POST_BUTTON_SELECTORS)
DIALOG_ANCESTOR_SELECTOR = "xpath=ancestor::*[@role='dialog'][1]"
PRIMARY_COLUMN_ANCESTOR_SELECTOR = (
    "xpath=ancestor::*[@data-testid='primaryColumn'][1]"
)


@dataclass(frozen=True)
class ComposeSurface:
    """One active composer and the nearest surface that owns its controls."""

    root: object
    composer: object
    kind: str


class PublishError(Exception):
    """Raised when a required compose control cannot be located safely."""


class BrowserSessionError(RuntimeError):
    """The persistent browser session could not be made usable safely."""


def is_closed_context_error(exc: Exception) -> bool:
    """Narrowly classify Playwright browser/context closure failures.

    Playwright does not publicly export ``TargetClosedError`` in the installed
    API, but it is a subclass of the public :class:`PlaywrightError`. Use the
    concrete class name plus Playwright's stable closed-target messages without
    importing undocumented implementation modules.
    """
    if not isinstance(exc, PlaywrightError):
        return False
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "target page, context or browser has been closed",
            "browsercontext has been closed",
            "browser has been closed",
        )
    )


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
        self._intentionally_stopped = False

    def start(self):
        """Explicitly ensure a healthy persistent context exists."""
        self._intentionally_stopped = False
        self._ensure_healthy_context()

    def _context_is_healthy(self) -> bool:
        """Check context health using public Playwright API surfaces."""
        context = self._context
        if context is None:
            return False
        try:
            browser = context.browser
            if browser is not None and not browser.is_connected():
                return False
            # Accessing public context state also rejects some already-closed
            # implementations even when a browser handle is unavailable.
            context.pages
            return True
        except Exception as exc:
            if is_closed_context_error(exc):
                return False
            raise

    def _cleanup_session_state(self) -> None:
        """Clear and best-effort close one exact stale context/driver pair."""
        context, playwright = self._context, self._playwright
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                if is_closed_context_error(exc):
                    log.debug("stale browser context was already closed")
                else:
                    log.warning("error closing stale browser context", exc_info=True)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                log.warning("error stopping stale Playwright handle", exc_info=True)

    def _launch_context(self) -> None:
        """Launch one persistent context, cleaning partial state on failure."""
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.profile_dir,
                executable_path=self.brave,
                headless=False,
                no_viewport=True,
                user_agent=BRAVE_WINDOWS_UA,
                locale="en-US",
                args=list(BROWSER_EXTRA_ARGS),
            )
            install_anti_detection(self._context)
        except Exception:
            self._cleanup_session_state()
            raise

    def _ensure_healthy_context(self) -> bool:
        """Ensure usability; return True only when stale state was replaced."""
        if self._intentionally_stopped:
            raise BrowserSessionError(
                "browser session was intentionally stopped; call start() explicitly"
            )
        if self._context_is_healthy():
            return False
        had_stale_state = self._context is not None or self._playwright is not None
        if had_stale_state:
            log.warning("stale browser context detected; rebuilding persistent session")
            self._cleanup_session_state()
        self._launch_context()
        return had_stale_state

    def stop(self):
        self._intentionally_stopped = True
        self._cleanup_session_state()

    def new_page(self) -> Page:
        """Create a page, rebuilding a context at most once if it died."""
        already_recovered = self._ensure_healthy_context()
        try:
            return self._context.new_page()
        except Exception as first_error:
            if not is_closed_context_error(first_error):
                raise
            if already_recovered:
                log.error("browser context recovery failed after one retry")
                self._cleanup_session_state()
                raise BrowserSessionError(
                    "browser context recovery failed after one retry"
                ) from first_error
            log.warning(
                "browser context closed unexpectedly; restarting persistent "
                "session (1/1)"
            )
            self._cleanup_session_state()
            try:
                self._launch_context()
                return self._context.new_page()
            except Exception as recovery_error:
                log.error("browser context recovery failed after one retry")
                if is_closed_context_error(recovery_error):
                    self._cleanup_session_state()
                raise BrowserSessionError(
                    "browser context recovery failed after one retry"
                ) from recovery_error

    def cookies(self, urls=None) -> list[dict]:
        """Return context cookies without exposing the owned context object."""
        self._ensure_healthy_context()
        if urls is None:
            return self._context.cookies()
        return self._context.cookies(urls)

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

    @staticmethod
    def _composer_has_text(composer) -> bool | None:
        """Conservatively verify that a requested caption produced text.

        ``False`` is returned only after a reliable, tag-appropriate read says
        the composer is empty. Unsupported/changed DOM behavior returns
        ``None`` so diagnostics improve without inventing a false failure.
        """
        try:
            tag_name = str(
                composer.evaluate("element => element.tagName.toLowerCase()")
            ).lower()
            if tag_name in ("input", "textarea"):
                value = composer.input_value(timeout=1000)
            else:
                value = composer.inner_text(timeout=1000)
            return bool((value or "").strip())
        except Exception as exc:
            if is_closed_context_error(exc):
                raise
            log.warning("could not verify composer text after typing", exc_info=True)
            return None

    @staticmethod
    def _media_error(text: str, ignored_text: str = "") -> str | None:
        """Return a stable media failure code for known visible X messages."""
        normalized = " ".join((text or "").split()).casefold()
        ignored = " ".join((ignored_text or "").split()).casefold()
        if ignored:
            normalized = normalized.replace(ignored, "")
        for code, pattern in MEDIA_ERROR_PATTERNS:
            if pattern.search(normalized):
                return code
        return None

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        return max(0, int((deadline - time.monotonic()) * 1000))

    @staticmethod
    def _surface_for_composer(composer) -> ComposeSurface | None:
        """Derive the nearest visible owner for one visible composer node."""
        for kind, ancestor_selector in (
            ("dialog", DIALOG_ANCESTOR_SELECTOR),
            ("primaryColumn", PRIMARY_COLUMN_ANCESTOR_SELECTOR),
        ):
            root = composer.locator(ancestor_selector)
            if root.count() == 1 and root.is_visible():
                return ComposeSurface(root=root, composer=composer, kind=kind)
        return None

    @classmethod
    def _find_active_compose_surface(
        cls, page: Page, *, timeout_ms: int
    ) -> ComposeSurface:
        """Find one composer, preferring an active modal over inline compose.

        X can render the Home inline composer behind ``/compose/post``. A
        visible composer inside its nearest visible dialog therefore owns the
        operation; DOM order is never used to choose between the modal and the
        underlying timeline composer.
        """
        selectors = tuple(COMPOSER_SELECTORS)
        per_selector_timeout = max(1, timeout_ms // max(1, len(selectors)))
        rootless_composer_seen = False
        for selector in selectors:
            visible = page.locator(selector).filter(visible=True)
            try:
                # This waits only for existence. Selection below enumerates and
                # classifies every visible candidate by ownership.
                visible.first.wait_for(
                    state="visible", timeout=per_selector_timeout
                )
            except Exception as exc:
                if is_closed_context_error(exc):
                    raise
                continue

            modal_surfaces = []
            inline_surfaces = []
            try:
                count = visible.count()
                for index in range(count):
                    composer = visible.nth(index)
                    surface = cls._surface_for_composer(composer)
                    if surface is None:
                        rootless_composer_seen = True
                    elif surface.kind == "dialog":
                        modal_surfaces.append(surface)
                    else:
                        inline_surfaces.append(surface)
            except Exception as exc:
                if is_closed_context_error(exc):
                    raise
                continue

            preferred = modal_surfaces or inline_surfaces
            if len(preferred) == 1:
                return preferred[0]
            if len(preferred) > 1:
                diagnostics = cls._bounded_dom_diagnostics(page)
                log.warning(
                    "ambiguous active X composers: selector=%r kind=%r count=%d "
                    "dom=%r",
                    selector,
                    preferred[0].kind,
                    len(preferred),
                    diagnostics,
                )
                raise PublishError("ambiguous_composer")

        cls._log_dom_failure(page, "composer", selectors)
        if rootless_composer_seen:
            raise PublishError(
                "compose_root_not_found: visible composer had no owned dialog "
                "or primaryColumn"
            )
        raise PublishError(
            "composer_not_found: known X composer selectors were not visible"
        )

    @staticmethod
    def _video_attachment_state(attachment) -> str:
        """Classify X's inline caption-file/video-status attachment text.

        Live DOM evidence shows ``Upload caption file (.srt)`` and
        ``<filename>: Ready`` inside ``[data-testid=attachments]``. It is
        informational attachment state, not an editor completion dialog.
        """
        try:
            text = " ".join((attachment.inner_text(timeout=1000) or "").split())
        except Exception as exc:
            if is_closed_context_error(exc):
                raise
            return "unavailable"
        if "upload caption file (.srt)" not in text.casefold():
            return "not_detected"
        if re.search(r"\b(?:failed|error|could not be processed)\b", text, re.I):
            return "attachment_inline_error"
        if re.search(r"\bready\b", text, re.I):
            return "attachment_inline_ready"
        if re.search(r"\b(?:processing|uploading)\b", text, re.I):
            return "attachment_inline_processing"
        return "attachment_inline_present"

    @staticmethod
    def _secondary_video_editor_state(page) -> dict:
        """Detect a distinct visible media dialog without guessing an action."""
        script = r"""
() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const composers = [...document.querySelectorAll('[data-testid="tweetTextarea_0"]')]
    .filter(visible);
  const modalComposers = composers.filter((el) => {
    const dialog = el.closest('[role="dialog"]');
    return dialog && visible(dialog);
  });
  const composer = modalComposers.length === 1 ? modalComposers[0] :
    (composers.length === 1 ? composers[0] : null);
  if (!composer) return {state: "unavailable", dialog_count: 0, action_candidates: []};
  const dialogs = [...document.querySelectorAll('[role="dialog"]')]
    .filter(visible)
    .filter((dialog) => !dialog.contains(composer))
    .filter((dialog) => /Upload caption file \(\.srt\)/i.test(dialog.innerText || ""));
  if (!dialogs.length) return {state: "no_editor", dialog_count: 0, action_candidates: []};
  const text = dialogs.map((dialog) => dialog.innerText || "").join(" ");
  let state = "secondary_present";
  if (/\b(?:failed|error|could not be processed)\b/i.test(text)) state = "secondary_error";
  else if (/\bready\b/i.test(text)) state = "secondary_ready";
  else if (/\b(?:processing|uploading)\b/i.test(text)) state = "secondary_processing";
  const actions = dialogs.flatMap((dialog) => [...dialog.querySelectorAll('button')])
    .filter(visible)
    .slice(0, 12)
    .map((button) => ({
      testid: button.getAttribute('data-testid'),
      aria_label: button.getAttribute('aria-label'),
      title: button.getAttribute('title'),
      text: (button.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80),
    }));
  return {state, dialog_count: dialogs.length, action_candidates: actions};
}
"""
        try:
            value = page.evaluate(script)
            if isinstance(value, dict):
                return value
        except Exception as exc:
            if is_closed_context_error(exc):
                raise
        return {"state": "unavailable", "dialog_count": 0, "action_candidates": []}

    @staticmethod
    def _bounded_dom_diagnostics(page) -> dict:
        """Return bounded ownership diagnostics without page HTML or caption text."""
        script = r"""
() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const composers = [...document.querySelectorAll('[data-testid="tweetTextarea_0"]')]
    .filter(visible);
  const modalComposers = composers.filter((el) => {
    const dialog = el.closest('[role="dialog"]');
    return dialog && visible(dialog);
  });
  const composer = modalComposers.length === 1 ? modalComposers[0] :
    (composers.length === 1 ? composers[0] : null);
  const dialogRoot = composer && composer.closest('[role="dialog"]');
  const primaryRoot = composer && composer.closest('[data-testid="primaryColumn"]');
  const root = dialogRoot || primaryRoot || null;
  const safeText = (el) => {
    let text = (el && el.innerText || "").replace(/\s+/g, " ").trim();
    const caption = (composer && composer.innerText || "").replace(/\s+/g, " ").trim();
    if (caption) text = text.replace(caption, "<composer-text>");
    return text.slice(0, 260);
  };
  const buttons = [...document.querySelectorAll(
    '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
  )].slice(0, 12).map((button, index) => {
    const rect = button.getBoundingClientRect();
    const dialog = button.closest('[role="dialog"]');
    return {
      index,
      testid: button.getAttribute('data-testid'),
      visible: visible(button),
      enabled: !button.disabled && button.getAttribute('aria-disabled') !== 'true',
      aria_disabled: button.getAttribute('aria-disabled'),
      disabled_attribute: button.hasAttribute('disabled'),
      name: (button.getAttribute('aria-label') || button.innerText || "").trim().slice(0, 80),
      inside_active_compose: Boolean(root && root.contains(button)),
      dialog_text: safeText(dialog),
      bounding_box: rect.width && rect.height ? {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height),
      } : null,
    };
  });
  const dialogs = [...document.querySelectorAll('[role="dialog"]')]
    .slice(0, 12).map((dialog, index) => ({
      index,
      visible: visible(dialog),
      testid: dialog.getAttribute('data-testid'),
      contains_active_composer: Boolean(composer && dialog.contains(composer)),
      contains_attachment: Boolean(dialog.querySelector('[data-testid="attachments"]')),
      post_button_count: dialog.querySelectorAll(
        '[data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
      ).length,
      media_editor_markers: {
        srt: /Upload caption file \(\.srt\)/i.test(dialog.innerText || ""),
        ready: /\bReady\b/i.test(dialog.innerText || ""),
      },
      text: safeText(dialog),
    }));
  const secondary = dialogs.filter((dialog) =>
    dialog.visible && !dialog.contains_active_composer && dialog.media_editor_markers.srt
  );
  return {
    active_compose_root: dialogRoot ? "dialog" : primaryRoot ? "primaryColumn" : null,
    post_button_candidates: buttons,
    visible_dialogs: dialogs,
    secondary_media_editor: secondary.length ? secondary : null,
  };
}
"""
        try:
            value = page.evaluate(script)
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            if is_closed_context_error(exc):
                raise
            return {}

    @classmethod
    def _readiness_diagnostics(
        cls,
        page,
        post_btn,
        attachment,
        *,
        composer_non_empty,
        media_kind,
        configured_timeout_seconds,
        readiness_started_at,
        compose_surface=None,
        detected_problem=None,
        media_error=None,
    ) -> dict:
        """Collect bounded, non-sensitive compose readiness state."""
        diagnostics = {
            "button_visible": None,
            "button_enabled": None,
            "aria_disabled": None,
            "disabled_attribute": None,
            "attachment_count": None,
            "composer_non_empty": composer_non_empty,
            "media_kind": media_kind,
            "configured_ready_timeout_seconds": configured_timeout_seconds,
            "elapsed_seconds": round(
                max(0.0, time.monotonic() - readiness_started_at), 1
            ),
            "url": "<unavailable>",
            "detected_problem": detected_problem,
            "media_error": media_error,
            "compose_root_kind": (
                compose_surface.kind if compose_surface is not None else None
            ),
            "video_media_state": cls._video_attachment_state(attachment),
        }
        checks = (
            ("button_visible", lambda: post_btn.is_visible()),
            ("button_enabled", lambda: post_btn.is_enabled()),
            ("aria_disabled", lambda: post_btn.get_attribute("aria-disabled")),
            ("disabled_attribute", lambda: post_btn.get_attribute("disabled")),
            ("attachment_count", lambda: attachment.count()),
            ("url", lambda: page.url),
        )
        for key, check in checks:
            try:
                diagnostics[key] = check()
            except Exception:
                diagnostics[key] = "error"
        diagnostics.update(cls._bounded_dom_diagnostics(page))
        return diagnostics

    @classmethod
    def _wait_until_post_ready(
        cls,
        page,
        post_btn,
        attachment,
        deadline: float,
        *,
        composer_non_empty,
        caption_text="",
        media_kind="image",
        configured_timeout_seconds=60,
        readiness_started_at=None,
        compose_surface=None,
    ) -> dict:
        """Wait until the visible Post button is enabled within one deadline."""
        if readiness_started_at is None:
            readiness_started_at = time.monotonic()
        last_state = {"visible": False, "enabled": False}
        announced_video_wait = False
        announced_inline_ready = False
        editor_state = {
            "state": "no_editor",
            "dialog_count": 0,
            "action_candidates": [],
        }
        editor_blocks_post = False
        last_editor_check_at = None
        while True:
            remaining_ms = cls._remaining_ms(deadline)
            body_text = ""
            try:
                body_text = page.locator("body").inner_text(
                    timeout=max(1, min(1000, remaining_ms or 1))
                )
            except Exception as exc:
                if is_closed_context_error(exc):
                    raise
                pass

            if problem := cls.detect_problem(page, body_text):
                return {"ready": False, "reason": problem, "remaining_ms": remaining_ms}
            if media_error := cls._media_error(body_text, caption_text):
                diagnostics = cls._readiness_diagnostics(
                    page,
                    post_btn,
                    attachment,
                    composer_non_empty=composer_non_empty,
                    media_kind=media_kind,
                    configured_timeout_seconds=configured_timeout_seconds,
                    readiness_started_at=readiness_started_at,
                    compose_surface=compose_surface,
                    media_error=media_error,
                )
                log.warning("X media readiness failure: %r", diagnostics)
                return {
                    "ready": False,
                    "reason": f"media_upload_error:{media_error}",
                    "remaining_ms": remaining_ms,
                }

            try:
                attachment_count = attachment.count()
            except Exception as exc:
                if is_closed_context_error(exc):
                    raise
                attachment_count = None
            if attachment_count == 0:
                diagnostics = cls._readiness_diagnostics(
                    page,
                    post_btn,
                    attachment,
                    composer_non_empty=composer_non_empty,
                    media_kind=media_kind,
                    configured_timeout_seconds=configured_timeout_seconds,
                    readiness_started_at=readiness_started_at,
                    compose_surface=compose_surface,
                )
                log.warning("X attachment disappeared during readiness: %r", diagnostics)
                return {
                    "ready": False,
                    "reason": "attachment_missing_during_readiness",
                    "remaining_ms": remaining_ms,
                }

            video_media_state = cls._video_attachment_state(attachment)
            if video_media_state == "attachment_inline_error":
                return {
                    "ready": False,
                    "reason": "media_upload_error:media_processing_failed",
                    "remaining_ms": remaining_ms,
                }
            if (
                media_kind == "video"
                and video_media_state == "attachment_inline_ready"
                and not announced_inline_ready
            ):
                log.info(
                    "X video attachment reports Ready inside the active compose; "
                    "no editor completion action is required by this DOM state"
                )
                announced_inline_ready = True

            now = time.monotonic()
            if media_kind == "video" and (
                last_editor_check_at is None or now - last_editor_check_at >= 1.0
            ):
                editor_state = cls._secondary_video_editor_state(page)
                last_editor_check_at = now
                editor_blocks_post = editor_state.get("state") in {
                    "secondary_present",
                    "secondary_processing",
                }
                if editor_state.get("state") == "secondary_error":
                    log.warning("X secondary media editor error: %r", editor_state)
                    return {
                        "ready": False,
                        "reason": "media_upload_error:media_processing_failed",
                        "remaining_ms": remaining_ms,
                    }
                if editor_state.get("state") == "secondary_ready":
                    # The live X DOM captured for this repair has no secondary
                    # editor and no completion control. If a future DOM does,
                    # fail instead of guessing Done/Save/Close semantics.
                    log.warning(
                        "X secondary media editor is ready but has no verified "
                        "completion contract: %r",
                        editor_state,
                    )
                    return {
                        "ready": False,
                        "reason": "media_editor_unresolved",
                        "remaining_ms": remaining_ms,
                    }

            try:
                last_state["visible"] = bool(post_btn.is_visible())
                last_state["enabled"] = bool(post_btn.is_enabled())
            except Exception as exc:
                if is_closed_context_error(exc):
                    raise
                last_state = {"visible": False, "enabled": False}

            remaining_ms = cls._remaining_ms(deadline)
            if (
                last_state["visible"]
                and last_state["enabled"]
                and not editor_blocks_post
                and remaining_ms > 0
            ):
                if announced_video_wait:
                    elapsed = max(0.0, time.monotonic() - readiness_started_at)
                    log.info("X video Post button enabled after %.1fs", elapsed)
                return {"ready": True, "reason": None, "remaining_ms": remaining_ms}
            if media_kind == "video" and not announced_video_wait:
                log.info(
                    "X video attached; waiting up to %.0fs for Post button readiness",
                    configured_timeout_seconds,
                )
                announced_video_wait = True
            if remaining_ms <= 0:
                diagnostics = cls._readiness_diagnostics(
                    page,
                    post_btn,
                    attachment,
                    composer_non_empty=composer_non_empty,
                    media_kind=media_kind,
                    configured_timeout_seconds=configured_timeout_seconds,
                    readiness_started_at=readiness_started_at,
                    compose_surface=compose_surface,
                )
                diagnostics["secondary_media_editor_state"] = editor_state
                log.warning("X Post button readiness timed out: %r", diagnostics)
                return {
                    "ready": False,
                    "reason": "post_button_disabled_timeout",
                    "remaining_ms": 0,
                }
            page.wait_for_timeout(min(READINESS_POLL_MS, remaining_ms))

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
        dom_diagnostics = cls._bounded_dom_diagnostics(page)
        log.warning(
            "%s selector failure: url=%r title=%r detected_problem=%r "
            "selector_counts=%r dom=%r",
            stage,
            url,
            title,
            detected_problem,
            selector_counts,
            dom_diagnostics,
        )

    @classmethod
    def _unique_matching_locator(
        cls,
        page: Page,
        scope,
        selectors,
        *,
        state: str,
        timeout_ms: int,
        failure_reason: str,
        ambiguity_reason: str,
        stage: str,
    ):
        """Return one control within an already established ownership scope.

        The total caller timeout is divided across selectors, keeping fallback
        discovery bounded. A matching selector must resolve to exactly one
        control; ambiguity fails instead of picking a DOM position.
        """
        selectors = tuple(selectors)
        per_selector_timeout = max(1, timeout_ms // max(1, len(selectors)))
        for selector in selectors:
            locator = scope.locator(selector)
            if state == "visible":
                locator = locator.filter(visible=True)
            try:
                # ``first`` is used only to wait for any match. The returned
                # control is selected below only after an exact count check.
                locator.first.wait_for(
                    state=state,
                    timeout=per_selector_timeout,
                )
                count = locator.count()
            except Exception as exc:
                if is_closed_context_error(exc):
                    raise
                continue
            if count == 1:
                return locator.nth(0)
            if count > 1:
                diagnostics = cls._bounded_dom_diagnostics(page)
                log.warning(
                    "%s ambiguity: reason=%s selector=%r owned_count=%d dom=%r",
                    stage,
                    ambiguity_reason,
                    selector,
                    count,
                    diagnostics,
                )
                raise PublishError(ambiguity_reason)
        cls._log_dom_failure(page, stage, selectors)
        raise PublishError(failure_reason)

    @classmethod
    def _find_owned_post_button(
        cls,
        page: Page,
        surface: ComposeSurface,
        *,
        timeout_ms: int,
    ):
        """Return the sole visible Post candidate owned by ``surface``."""
        candidates = surface.root.locator(POST_BUTTON_QUERY).filter(visible=True)
        try:
            candidates.first.wait_for(state="visible", timeout=timeout_ms)
            count = candidates.count()
        except Exception as exc:
            if is_closed_context_error(exc):
                raise
            cls._log_dom_failure(page, "post_button", POST_BUTTON_SELECTORS)
            raise PublishError(
                "post_button_not_found: active X compose owned no visible Post button"
            ) from None
        if count != 1:
            diagnostics = cls._bounded_dom_diagnostics(page)
            log.warning(
                "post_button ambiguity: reason=ambiguous_post_button "
                "owned_visible_count=%d dom=%r",
                count,
                diagnostics,
            )
            raise PublishError("ambiguous_post_button")
        return candidates.nth(0)

    # ------------------------------------------------------------- posting

    def post(
        self,
        caption: str,
        media_paths: list[str],
        timeout_s: int = 60,
        *,
        media_kind: str = "image",
        ready_timeout_s: int | float | None = None,
    ) -> dict:
        """Post one tweet with attached media.

        ``media_kind`` describes the attached set; callers must use ``video``
        when any attachment is a video. ``ready_timeout_s`` applies only to
        Post-button readiness/actionability, not general DOM operations or the
        separate positive-confirmation phase.
        """
        timeout_ms = max(1, int(timeout_s * 1000))
        if ready_timeout_s is None:
            # Backward-compatible direct-call behavior. Production orchestration
            # always supplies the centrally validated media-specific value.
            ready_timeout_s = timeout_s
        ready_timeout_ms = max(1, int(ready_timeout_s * 1000))
        for p in media_paths:
            if not Path(p).exists():
                return {"ok": False, "reason": f"missing media file {p}"}

        page = self.new_page()
        try:
            page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=45000)

            if problem := self.detect_problem(page):
                return {"ok": False, "reason": problem}

            compose = self._find_active_compose_surface(
                page, timeout_ms=timeout_ms
            )
            composer = compose.composer

            file_input = self._unique_matching_locator(
                page,
                compose.root,
                FILE_INPUT_SELECTORS,
                state="attached",
                timeout_ms=timeout_ms,
                failure_reason="file_input_not_found: X media input was not attached",
                ambiguity_reason="ambiguous_file_input",
                stage="file_input",
            )
            file_input.set_input_files(media_paths)

            attachment = self._unique_matching_locator(
                page,
                compose.root,
                ATTACHMENT_SELECTORS,
                state="visible",
                timeout_ms=timeout_ms,
                failure_reason="attachments_not_found: X media attachment was not visible",
                ambiguity_reason="ambiguous_attachments",
                stage="attachments",
            )

            if caption:
                composer.click()
                self._type_humanized(composer, caption)
                composer_non_empty = self._composer_has_text(composer)
                if composer_non_empty is False:
                    return {"ok": False, "reason": "caption_not_entered"}
            else:
                composer_non_empty = None

            if problem := self.detect_problem(page):
                return {"ok": False, "reason": problem}

            post_btn = self._find_owned_post_button(
                page,
                compose,
                timeout_ms=timeout_ms,
            )
            readiness_started_at = time.monotonic()
            readiness_deadline = (
                readiness_started_at + (ready_timeout_ms / 1000.0)
            )
            readiness = self._wait_until_post_ready(
                page,
                post_btn,
                attachment,
                readiness_deadline,
                composer_non_empty=composer_non_empty,
                caption_text=caption,
                media_kind=media_kind,
                configured_timeout_seconds=ready_timeout_ms / 1000.0,
                readiness_started_at=readiness_started_at,
                compose_surface=compose,
            )
            if not readiness["ready"]:
                return {"ok": False, "reason": readiness["reason"]}
            click_timeout_ms = self._remaining_ms(readiness_deadline)
            if click_timeout_ms <= 0:
                return {"ok": False, "reason": "post_button_disabled_timeout"}
            try:
                post_btn.click(timeout=click_timeout_ms)
            except PlaywrightTimeoutError:
                diagnostics = self._readiness_diagnostics(
                    page,
                    post_btn,
                    attachment,
                    composer_non_empty=composer_non_empty,
                    media_kind=media_kind,
                    configured_timeout_seconds=ready_timeout_ms / 1000.0,
                    readiness_started_at=readiness_started_at,
                    compose_surface=compose,
                )
                log.warning("X Post button click timed out: %r", diagnostics)
                return {"ok": False, "reason": "post_button_click_timeout"}

            # Success must be positively confirmed. Navigations, login/captcha
            # redirects or a changed URL are NEVER treated as proof of posting.
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while time.monotonic() < deadline:
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
