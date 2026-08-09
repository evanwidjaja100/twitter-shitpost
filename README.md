# Average Pocka — X Shitpost Bot

$0 shitposting bot: scrapes TikTok, YouTube, and X for top content, reposts to
X via a real Brave browser (Playwright). Everything lives on drive D.

## Quick start

```powershell
# 1. One-time login (Brave window opens; log in manually with @average_pocka,
#    turn Brave Shields OFF for x.com when asked)
.venv\Scripts\python.exe login.py

# 2. Health check (should print SELFTEST PASSED)
.venv\Scripts\python.exe main.py --selftest

# 3. Post one item now (first run: fill in credentials below first)
.venv\Scripts\python.exe main.py once

# 4. Run the bot (posts 3-6/day, randomized times)
.venv\Scripts\python.exe main.py daemon
```

## Free credentials to fill in (`config.json` → `secrets`)

| Key | Where to get it | Cost |
|---|---|---|
| `youtube_api_key` | console.cloud.google.com → enable *YouTube Data API v3* → API key | $0 |

TikTok needs **no credentials and no accounts** — the bot scrapes the general
"For You" feed with its own browser automatically (no API, so there's nothing
for TikTok to reject). To make it niche instead, optionally add gaming `@handles`
under `tiktok.accounts`. Either way, no key needed.

YouTube works the same now: set `youtube.shorts_feed: true` (default) to scrape
the Shorts feed automatically with no API key. Only if you want specific
channels' full videos do you need `youtube_api_key` + `youtube.channels`.

Without credentials the bot still works but only posts demo/test content —
YouTube scraping unlocks with the API key, and TikTok unlocks as soon as you
add accounts (no key needed).

## Configure sources (`config.json`)

- `tiktok.foryou` — `true` (default) = auto-scrape the general TikTok feed; `false` = scrape only your curated accounts below
- `tiktok.accounts` — optional list of gaming TikTok @handles (used when `foryou` is `false`, or in addition)
- `youtube.shorts_feed` — `true` (default) = auto-scrape the YouTube Shorts feed (no API key); `false` = use only the configured channels below
- `youtube.channels` — optional list of `{name, handle: "@channel"}` to pull clips from (needs `youtube_api_key`)
- `x_sources.accounts` — X accounts to scrape (e.g. `["@memelord", ...]`)
- `posting.min/max_posts_per_day` — daily volume (default 3-6)
- `publisher.image_ready_timeout_seconds` — maximum wait for an attached image post to become submit-ready (default 60 seconds)
- `publisher.video_ready_timeout_seconds` — maximum wait for X to finish accepting/processing an attached video before Post becomes enabled (default 180 seconds)
- `filters.blocked_keywords` — content never posted
- `filters.cooldown_days` — how long before a duplicate may appear again
  (both exact MD5 hashes and perceptual video/image fingerprints)

## Commands

```
python main.py login          one-time manual login (saves session in browser_profile/)
python main.py sources        preview the single best item without posting
python main.py once           pick best item and post it right now
python main.py daemon         scheduler loop (3-6 posts/day, randomized times)
python main.py stats          show follower history (opens browser to check)
python main.py stats --offline  show stored follower history (no browser)
python main.py --selftest     environment checks, exit 0 = healthy
python main.py --dry-run --seed-demo   offline end-to-end test, no posting
```

## Niche: gaming feed

The bot is configured to pull gaming content from curated TikTok @handles (and
YouTube channels) for the gaming niche and posts between 16:00 and 01:00 (gaming
peak hours, spans midnight — supported by the scheduler).

To add more sources:
- **TikTok**: leave `tiktok.foryou: true` to scrape the whole feed automatically,
  or add gaming `@handles` to `tiktok.accounts` for curated picks
- **YouTube**: leave `youtube.shorts_feed: true` to auto-scrape Shorts; or add
  `{"name": "...", "handle": "@channel", "playlist_id": ""}` to
  `youtube.channels` (needs `youtube_api_key`) for full videos
- **X**: add gaming shitpost account handles to `x_sources.accounts`

## Follower tracking

The daemon records the follower count weekly (see `tracking.follow_check_hours`)
into `data/bot.db` and writes `logs/followers.csv`. This history becomes your
account's sales sheet when you flip the account.

## Auto-start on Windows login

Run once (elevated PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File setup_task.ps1
```

Starts the daemon at logon with no console window. Manual start:
`.\run_bot.ps1`. Remove: `schtasks /Delete /TN AveragePockaBot /F`.

## Where things live (all on D)

```
assets/            downloaded + processed media
browser_profile/   the bot's isolated Brave profile (contains the login session)
data/bot.db        SQLite: dedup hashes + perceptual fingerprints, post history
logs/bot.log       activity log
logs/alerts.log    serious issues (post failures, login/captcha problems)
tools/ffmpeg/      ffmpeg + ffprobe (local, no PATH needed)
.venv/             Python environment
```

## Safety notes (read once)

- **New account risk**: `@average_pocka` posts reposted content 3-6x/day from
  day one. X can flag or suspend it. If that happens the bot stops and writes
  to `logs/alerts.log` — log back in and restart.
- **UI changes**: if X changes the composer, posting may fail. All X selectors
  live in `publisher/x_publisher.py` — fix them in one place.
- **Copyright**: content is reposted from public sources; dedup + keyword
  filters keep it defensible, but it is still third-party content.
- The bot never touches your personal Brave profile — it uses
  `browser_profile/`.
