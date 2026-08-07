"""X Shitpost Bot — orchestrator.

Commands:
  python main.py login        one-time manual login
  python main.py sources      scrape all sources once, print summary
  python main.py once         pick best item and post it now
  python main.py daemon       run the scheduler loop (posts 3-6/day)
  python main.py --selftest   environment checks (exit 0/1)
  python main.py --dry-run --seed-demo   end-to-end selection test, no posting
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def load_config() -> dict:
    cfg_path = BASE / "config.json"
    if not cfg_path.exists():
        print("ERROR: config.json not found — copy config.example.json to config.json")
        sys.exit(1)
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def setup_logging(cfg: dict):
    log_dir = Path(str(BASE / cfg["paths"]["logs_dir"]))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def alert(cfg: dict, message: str):
    log_dir = Path(str(BASE / cfg["paths"]["logs_dir"]))
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "alerts.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    logging.getLogger("alert").error(message)


# ------------------------------------------------------------- selection

def prepare_item(item: dict, cfg: dict, paths: dict, session=None):
    """Download + prepare media for an item. Returns local media path or None."""
    from pipeline import media as m
    from pipeline.filters import image_passes_dims

    ffmpeg = str(BASE / paths["ffmpeg"])
    ffprobe = str(BASE / paths["ffprobe"])
    assets = Path(str(BASE / paths["assets_dir"]))
    src_dir = assets / item["source"]
    src_dir.mkdir(parents=True, exist_ok=True)

    clip_max = cfg["youtube"]["clip_max_seconds"]

    try:
        if item["media_path"]:  # already downloaded (demo)
            src = item["media_path"]
        elif item["source"] == "x" and item["media_url"]:
            from scrapers.x_scraper import download_media

            src = download_media(session, item, str(src_dir))
            if not src:
                return None
        elif item["kind"] == "video":
            src = m.ytdl_download(
                item["source_url"],
                str(src_dir),
                cfg["posting"]["max_video_bytes"],
                ffmpeg_dir=str(BASE / paths["ffmpeg"]).rsplit("/", 1)[0],
            )
        elif item["kind"] == "image":
            src = m.download(item["media_url"], str(src_dir / f"{item['source_id']}_raw"))
        else:
            return None
    except m.MediaError as e:
        logging.getLogger("select").warning("download failed: %s", e)
        return None

    try:
        if item["kind"] == "image":
            out = m.prepare_image(src, str(src_dir), cfg["posting"]["max_image_bytes"])
            if not image_passes_dims(out, 0, 0):
                return None
            return out
        else:  # video
            if item["source"] == "x":
                try:
                    dur = m.video_duration(ffprobe, src)
                except m.MediaError:
                    return None
                if dur <= clip_max:
                    return src
            return m.trim_video(
                src, str(src_dir), ffmpeg, ffprobe, clip_max,
                cfg["youtube"]["clip_min_seconds"],
            )
    except m.MediaError as e:
        logging.getLogger("select").warning("prepare failed: %s", e)
        return None


def pick_item(cfg: dict, db, session=None) -> dict | None:
    """Scrape all sources, dedup against history, and return the single best
    postable item. Nothing is recorded here — dedup happens only after a
    publication is confirmed successful."""
    from pipeline.filters import title_contains_blocked_keywords

    log = logging.getLogger("select")
    secrets = cfg["secrets"]
    blocked = cfg["filters"]["blocked_keywords"]
    items: list[dict] = []

    tiktok = cfg.get("tiktok", {})
    if (tiktok.get("foryou") or tiktok.get("accounts")) and session is not None:
        from scrapers import tiktok_scraper

        items += tiktok_scraper.scrape(
            session, tiktok, str(BASE / cfg["paths"]["assets_dir"])
        )

    if secrets.get("youtube_api_key"):
        from googleapiclient.discovery import build

        yt = build("youtube", "v3", developerKey=secrets["youtube_api_key"], cache_discovery=False)
        from scrapers import youtube_scraper

        items += youtube_scraper.scrape(yt, cfg["youtube"])

    if cfg.get("youtube", {}).get("shorts_feed") and session is not None:
        from scrapers import youtube_scraper

        items += youtube_scraper.scrape_shorts(session, cfg["youtube"])

    if cfg["x_sources"].get("accounts") and session is not None:
        from scrapers import x_scraper

        items += x_scraper.scrape(session, cfg["x_sources"], str(BASE / cfg["paths"]["assets_dir"]))

    log.info("scraped %d candidates", len(items))

    candidates = [
        it for it in items
        if not db.is_source_seen(it["source"], it["source_id"])
        and not title_contains_blocked_keywords(it.get("title", ""), blocked)
    ]
    candidates.sort(key=lambda it: it["score"], reverse=True)

    cooldown = cfg["filters"]["cooldown_days"]
    for item in candidates:
        media_path = prepare_item(item, cfg, cfg["paths"], session)
        if not media_path:
            continue
        from pipeline.media import hash_file

        h = hash_file(media_path)
        if db.is_hash_seen(h, cooldown):
            continue

        from pipeline.filters import pick_caption

        caption = pick_caption(
            item.get("title", ""),
            cfg["posting"]["caption_style"],
            cfg["posting"]["caption_pool"],
            cfg["posting"]["random_caption_chance"],
            cfg["posting"]["max_caption_len"],
        )
        item["_media_path"] = media_path
        item["_caption"] = caption
        item["_hash"] = h
        return item
    return None


def mark_item_published(db, item) -> None:
    """Record dedup state only after a post has been confirmed successful.

    Done in one database transaction so source + media hash stay consistent.
    Shared by every publishing path (manual and daemon) so they cannot diverge.
    """
    db.record_successful_item(
        source=item["source"],
        source_id=item["source_id"],
        source_url=item["source_url"],
        content_hash=item.get("_hash"),
    )


# ------------------------------------------------------------- commands

def cmd_login():
    import login

    sys.exit(login.main())


def cmd_sources(cfg):
    from publisher.x_publisher import XSession

    db = _make_db(cfg)
    session = XSession(cfg["paths"])
    session.start()
    try:
        result = pick_item(cfg, db, session)
    finally:
        session.stop()
    if result is None:
        print("No postable item right now (no accounts configured, or everything already posted).")
        return
    print(f"Top pick: [{result['source']}] score={result['score']:.0f}")
    print(f"  title : {result.get('title', '')[:80]}")
    print(f"  url   : {result['source_url']}")
    print(f"  media : {result['_media_path']}")
    print(f"  caption: {result['_caption']}")


def cmd_once(cfg):
    from publisher.x_publisher import XSession

    db = _make_db(cfg)
    session = XSession(cfg["paths"])
    session.start()
    try:
        item = pick_item(cfg, db, session)
        if item is None:
            alert(cfg, "no item available to post")
            return
        res = session.post(item["_caption"], [item["_media_path"]])
        db.add_post(
            item["_caption"], item["_media_path"], item["source"],
            item["source_url"], item["_hash"], "posted" if res["ok"] else "failed",
            res["reason"],
        )
        if res["ok"]:
            mark_item_published(db, item)
            logging.getLogger("post").info("POSTED: %s | %s", item["source_url"], item["_caption"])
        else:
            alert(cfg, f"post failed: {res['reason']} | {item['source_url']}")
    finally:
        session.stop()


def cmd_stats(cfg, offline: bool):
    from tracker import get_follower_count, maybe_check_followers, write_csv

    db = _make_db(cfg)
    handle = cfg["tracking"].get("own_handle", "average_pocka")
    session = None
    if not offline:
        from publisher.x_publisher import XSession

        session = XSession(cfg["paths"])
        session.start()
        maybe_check_followers(db, cfg, session)
    history = db.follower_history()
    csv_path = BASE / cfg["paths"]["logs_dir"] / "followers.csv"
    write_csv(str(csv_path), history)
    if not history:
        print("No follower data yet. First check happens automatically once the")
        print("daemon runs while logged in (or re-run `main.py stats` online).")
        return
    print(f"{'time (UTC)':<20} followers")
    for ts, count in history:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts)):<20} {count}")
    if len(history) >= 2:
        delta = history[-1][1] - history[0][1]
        days = max((history[-1][0] - history[0][0]) / 86400.0, 1 / 1440.0)
        print(f"\ngrowth: {delta:+d} followers over {days:.1f} days "
              f"({delta / days:+.1f}/day)")
    print(f"\nhistory CSV: {csv_path}")


def cmd_daemon(cfg):
    from publisher.x_publisher import XSession
    import scheduler

    db = _make_db(cfg)
    session = XSession(cfg["paths"])
    session.start()
    log = logging.getLogger("daemon")
    posting = cfg["posting"]
    safety = cfg["safety"]
    log.info("daemon started; %d-%d posts/day between %02d:00-%02d:00",
             posting["min_posts_per_day"], posting["max_posts_per_day"],
             posting["active_hours_start"], posting["active_hours_end"])
    while True:
        from tracker import maybe_check_followers

        maybe_check_followers(db, cfg, session)
        times = scheduler.compute_post_times(
            posting["min_posts_per_day"], posting["max_posts_per_day"],
            posting["active_hours_start"], posting["active_hours_end"],
        )
        if not times:
            log.info("no remaining slots today; waiting for tomorrow")
            time.sleep(60)
            continue
        for t in times:
            scheduler.sleep_until(t)
            if db.posts_today() >= safety["max_daily_posts_absolute"]:
                log.info("daily cap reached; sleeping")
                break
            item = pick_item(cfg, db, session)
            if item is None:
                log.info("no item found; skipping slot")
                continue
            res = session.post(item["_caption"], [item["_media_path"]])
            db.add_post(
                item["_caption"], item["_media_path"], item["source"],
                item["source_url"], item["_hash"], "posted" if res["ok"] else "failed",
                res["reason"],
            )
            if res["ok"]:
                mark_item_published(db, item)
                log.info("POSTED: %s", item["source_url"])
            else:
                alert(cfg, f"post failed: {res['reason']} | {item['source_url']}")
                if res["reason"] in ("login", "captcha") and safety["stop_on_login_failure"]:
                    alert(cfg, "stopping daemon due to login/captcha failure")
                    session.stop()
                    return
                time.sleep(safety["retry_backoff_minutes"] * 60)
        time.sleep(60)


def _make_db(cfg):
    from storage.db import Database

    return Database(str(BASE / cfg["paths"]["db_file"]))


# ------------------------------------------------------------- selftest / dry-run

def _demo_image() -> str:
    assets = BASE / "assets" / "demo"
    assets.mkdir(parents=True, exist_ok=True)
    out = assets / "demo1.jpg"
    if not out.exists():
        img = Image.new("RGB", (900, 700), (24, 24, 40))
        d = ImageDraw.Draw(img)
        d.text((350, 330), "demo shitpost", fill=(255, 255, 255))
        img.save(out, "JPEG", quality=90)
    return str(out)


def cmd_selftest(cfg) -> int:
    checks = []
    ok = True

    def check(name, passed, detail=""):
        nonlocal ok
        checks.append((name, passed, detail))
        if not passed:
            ok = False

    check("python>=3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    for mod in ("playwright", "googleapiclient", "yt_dlp", "PIL", "requests"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as e:
            check(f"import {mod}", False, str(e))

    check("config.json", cfg is not None)
    ffmpeg = BASE / cfg["paths"]["ffmpeg"]
    ffprobe = BASE / cfg["paths"]["ffprobe"]
    check("ffmpeg exists", ffmpeg.exists(), str(ffmpeg))
    check("ffprobe exists", ffprobe.exists(), str(ffprobe))
    brave = Path(str(BASE / cfg["paths"]["brave"]))
    check("brave exists", brave.exists(), str(brave))
    try:
        db = _make_db(cfg)
        check("database init", True, str(db.stats()))
    except Exception as e:
        check("database init", False, str(e))
    log_dir = BASE / cfg["paths"]["logs_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
    check("logs dir writable", log_dir.is_dir())

    missing = [k for k, v in cfg["secrets"].items() if not v]
    check("secrets (warn only)", True, f"missing (optional for now): {', '.join(missing) or 'none'}")

    for name, passed, detail in checks:
        print(f"[{'OK' if passed else 'FAIL'}] {name} {('— ' + detail) if detail else ''}")
    print("SELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def cmd_dry_run(cfg, seed_demo: bool) -> int:
    """Full selection pipeline without posting."""
    db = _make_db(cfg)
    if seed_demo:
        demo = {
            "source": "demo",
            "source_id": "demo-1",
            "source_url": "https://example.invalid/demo",
            "title": "demo shitpost test item",
            "media_url": None,
            "media_path": _demo_image(),
            "score": 99999.0,
            "created_utc": time.time(),
            "nsfw": False,
            "kind": "image",
        }
        item = prepare_item(demo, cfg, cfg["paths"])
        if not item:
            print("DRY-RUN FAILED: demo item could not be prepared")
            return 1
        from pipeline.media import hash_file
        from pipeline.filters import pick_caption

        h = hash_file(item)
        caption = pick_caption(
            demo["title"], cfg["posting"]["caption_style"], cfg["posting"]["caption_pool"],
            cfg["posting"]["random_caption_chance"], cfg["posting"]["max_caption_len"],
        )
        print(f"[OK] demo media prepared: {item}")
        print(f"[OK] caption: {caption!r}")
        print(f"[OK] content hash: {h}")
        print("DRY-RUN PASSED")
        return 0

    print("Scraping real sources (requires credentials)...")
    item = pick_item(cfg, db)
    if item is None:
        print("No postable item found (credentials missing or queue empty).")
        print("DRY-RUN PASSED (selection pipeline ran cleanly)")
        return 0
    print(f"[OK] would post: {item['source']} score={item['score']:.0f}")
    print(f"     caption: {item['_caption']!r}")
    print(f"     media:   {item['_media_path']}")
    print("DRY-RUN PASSED")
    return 0


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="X Shitpost Bot")
    parser.add_argument("command", nargs="?", default=None,
                        help="login | sources | once | daemon | stats")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed-demo", action="store_true")
    parser.add_argument("--offline", action="store_true",
                        help="stats: read stored data without opening a browser")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg)

    if args.selftest:
        sys.exit(cmd_selftest(cfg))
    if args.dry_run:
        sys.exit(cmd_dry_run(cfg, args.seed_demo))

    if args.command == "login":
        cmd_login()
    elif args.command == "sources":
        cmd_sources(cfg)
    elif args.command == "once":
        cmd_once(cfg)
    elif args.command == "daemon":
        cmd_daemon(cfg)
    elif args.command == "stats":
        cmd_stats(cfg, args.offline)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
