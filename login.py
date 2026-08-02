"""One-time manual login to x.com in the bot's isolated Brave profile.

Usage:  python login.py
After login succeeds, a marker file is written so the bot knows the session exists.
"""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from publisher.x_publisher import (
    BRAVE_WINDOWS_UA,
    BROWSER_EXTRA_ARGS,
    install_anti_detection,
    load_config_paths,
)


def write_marker(marker_path: str):
    Path(marker_path).parent.mkdir(parents=True, exist_ok=True)
    Path(marker_path).write_text(
        json.dumps({"logged_in": True, "at": time.time()}), encoding="utf-8"
    )


def main() -> int:
    paths = load_config_paths()
    profile_dir = str(Path(paths["browser_profile"]).resolve())
    brave = paths.get("brave")
    marker = str(Path(paths["logs_dir"]) / "logged_in.json")

    print("=" * 60)
    print("STEP 1: The bot's Brave window will open at x.com.")
    print("STEP 2: Log in MANUALLY with @average_pocka (email/password + 2FA).")
    print("STEP 3: If you see the Brave shield icon, click it and toggle")
    print("        'Shields' OFF for x.com (prevents login/upload issues).")
    print("        Also allow cookies for x.com if prompted.")
    print("STEP 4: Once you see your home timeline, come back here.")
    print("        (If X says 'browser is not safe', refresh the page once and")
    print("         log in again — automation detection is now hidden.)")
    print("The window will auto-close once login is detected (or after 6 min).")
    print("=" * 60)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            executable_path=brave,
            headless=False,
            viewport={"width": 1280, "height": 900},
            user_agent=BRAVE_WINDOWS_UA,
            locale="en-US",
            args=list(BROWSER_EXTRA_ARGS),
        )
        install_anti_detection(context)
        page = context.new_page()
        page.goto("https://x.com", wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + 6 * 60
        logged_in = False
        while time.time() < deadline:
            try:
                url = page.url
                compose_visible = page.locator(
                    'a[data-testid="SideNav_NewTweet_Button"]'
                ).count() > 0 or "home" in url
                if compose_visible or "x.com/home" in url:
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(5)

        if logged_in:
            write_marker(marker)
            print("[OK] Login detected. Session saved in the bot profile.")
            page.close()
            context.close()
            return 0
        else:
            print("[FAIL] Login not detected within 6 minutes. Run `python login.py` again.")
            page.close()
            context.close()
            return 1


if __name__ == "__main__":
    sys.exit(main())
