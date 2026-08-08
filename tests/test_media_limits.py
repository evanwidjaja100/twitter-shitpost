"""Media byte-limit regression tests (Issue 2).

Covers the authoritative ceilings introduced across every acquisition and
preparation path:
  * streamed ``download()`` MUST abort the moment the stream exceeds the budget
    and never leave a partial file (2A),
  * X media download validates Content-Length + the real body before any write (2B),
  * oversized GIFs are rejected instead of passed straight through (2C),
  * JPEG quality-floor oversize is a hard rejection, not a silent pass (2D),
  * a "short" X video is only returned untouched when its real size already
    fits the ceiling (2E), and FFmpeg output that still exceeds the limit is
    deleted + rejected (2F),
  * ``validate_final_media_size`` is the final invariant on the prepared path (2G),
  * a media prep failure records no permanent dedup/post state.

All downloads use local temp files and mocked HTTP/FFmpeg — no network.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import requests
from PIL import Image

import main
from pipeline import media
from pipeline.media import MediaError
from scrapers import x_scraper


# ------------------------------------------------------------------ download()

class _FakeResp:
    """A requests.Response stand-in for ``requests.get(url, stream=True)``."""

    def __init__(self, headers=None, body=b"", ok=True):
        self.headers = headers or {}
        self._body = body
        self.ok = ok
        self.status = 200 if ok else 500

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(self.status)

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), 64):
            yield self._body[i:i + 64]


def _stub_get(resp):
    def _get(*a, **k):
        return resp
    return _get


# Test 2A-1: Content-Length ahead of the ceiling is rejected before streaming.
def test_download_rejects_by_content_length(tmp_path):
    dest = tmp_path / "big.bin"
    with mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(headers={"Content-Length": "999999"}))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=100)
    assert not dest.exists()


# test 2A-2: the streamed byte counter is authoritative (no Content-Length).
def test_download_aborts_when_stream_exceeds(tmp_path):
    dest = tmp_path / "big.bin"
    with mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 300))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=100)
    assert not dest.exists()


# test 2A-3: exactly-at-limit is accepted.
def test_download_accepts_size_equal_to_limit(tmp_path):
    dest = tmp_path / "ok.bin"
    with mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 100))):
        got = media.download("https://x/img", str(dest), max_bytes=100)
    assert got == str(dest)
    assert dest.read_bytes() == b"a" * 100


# test 2A-4: a partial file is removed on mid-stream failure.
def test_download_removes_partial_on_error(tmp_path):
    class _Burst(_FakeResp):
        def iter_content(self, chunk_size):
            yield b"a" * 64
            raise requests.exceptions.ConnectionError("socket gone")

    dest = tmp_path / "partial.bin"
    with mock.patch("pipeline.media.requests.get", _stub_get(_Burst(body=b"a" * 300))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=10_000)
    assert not dest.exists()


# test 2A-5: an empty body is a failure, not a saved zero-byte file.
def test_download_raises_on_empty_body(tmp_path):
    dest = tmp_path / "empty.bin"
    with mock.patch("pipeline.media.requests.get", _stub_get(_FakeResp(body=b""))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=10_000)
    assert not dest.exists()


def test_download_raises_on_http_error(tmp_path):
    dest = tmp_path / "err.bin"
    with mock.patch("pipeline.media.requests.get", _stub_get(_FakeResp(ok=False))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=10_000)
    assert not dest.exists()


# --------------------------------------------------------------- x download_media

class _Resp:
    def __init__(self, body=b"", ok=True, status=200, headers=None):
        self._body = body
        self.ok = ok
        self.status = status
        self.headers = headers or {}

    def body(self):
        return self._body


def _x_session(resp):
    return SimpleNamespace(
        _context=SimpleNamespace(
            request=SimpleNamespace(get=lambda url, headers=None: resp)
        )
    )


def _x_item(url="https://pbs.twimg.com/media/photo.jpg", source_id="tweet123"):
    return {
        "source": "x",
        "source_id": source_id,
        "media_url": url,
        "kind": "image",
    }


def test_x_download_media_rejects_over_content_length(tmp_path):
    resp = _Resp(body=b"a" * 50, headers={"content-length": "999999"})
    got = x_scraper.download_media(_x_session(resp), _x_item(), str(tmp_path), max_bytes=100)
    assert got is None
    assert list(tmp_path.iterdir()) == []


def test_x_download_media_rejects_over_body_without_cl(tmp_path):
    resp = _Resp(body=b"a" * 500)
    got = x_scraper.download_media(_x_session(resp), _x_item(), str(tmp_path), max_bytes=100)
    assert got is None
    assert list(tmp_path.iterdir()) == []


def test_x_download_media_respects_body_within_limit(tmp_path):
    resp = _Resp(body=b"a" * 80)
    got = x_scraper.download_media(_x_session(resp), _x_item(), str(tmp_path), max_bytes=100)
    assert got == str(tmp_path / "tweet123.jpg")
    assert (tmp_path / "tweet123.jpg").read_bytes() == b"a" * 80


def test_x_download_media_rejects_http_error(tmp_path):
    got = x_scraper.download_media(_x_session(_Resp(ok=False, status=404)), _x_item(), str(tmp_path))
    assert got is None
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ images

def _tiny_gif(path):
    frames = [Image.new("RGB", (6, 6), c) for c in ("red", "blue")]
    frames[0].save(str(path), "GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    return Path(path)


def _tiny_jpg(path, size=(32, 32), color=(40, 40, 60)):
    Image.new("RGB", size, color).save(str(path), "JPEG", quality=90)
    return Path(path)


def test_gif_oversized_rejected(tmp_path):
    src = _tiny_gif(tmp_path / "anim.gif")
    src.write_bytes(src.read_bytes() + b"\x00" * 2048)  # push size over any sane floor
    with pytest.raises(MediaError):
        media.prepare_image(str(src), str(tmp_path / "out"), max_bytes=1)
    assert (tmp_path / "out").exists() is False or list((tmp_path / "out").iterdir()) == []


def test_gif_within_limit_passes_through(tmp_path):
    src = _tiny_gif(tmp_path / "tiny.gif")
    out = media.prepare_image(str(src), str(tmp_path / "out"), max_bytes=10_000_000)
    assert Path(out).suffix == ".gif"
    assert Path(out).exists()
    assert Path(out).stat().st_size <= 10_000_000
    assert Path(out) != src


def test_jpg_returns_encoded_within_limit(tmp_path):
    src = _tiny_jpg(tmp_path / "photo.jpg")
    out = media.prepare_image(str(src), str(tmp_path / "out"), max_bytes=1_000_000)
    assert Path(out).suffix == ".jpg"
    assert Path(out).stat().st_size <= 1_000_000


def test_jpg_cached_result_revalidated(tmp_path):
    src = _tiny_jpg(tmp_path / "photo.jpg")
    out = media.prepare_image(str(src), str(tmp_path / "out"), max_bytes=1_000_000)
    out2 = media.prepare_image(str(src), str(tmp_path / "out"), max_bytes=1_000_000)
    assert out == out2


def test_jpg_at_quality_floor_still_rejected_when_oversized(tmp_path):
    src = _tiny_jpg(tmp_path / "big.jpg", size=(800, 800))
    with pytest.raises(MediaError):
        media.prepare_image(str(src), str(tmp_path / "out"), max_bytes=1)
    out_dir = tmp_path / "out"
    if out_dir.exists():
        assert list(out_dir.iterdir()) == []


# ------------------------------------------------------------- trim_video

def _patch_ffmpeg(out_size=0, returncode=0, exc=None, missing=False):
    def fake_run(cmd, capture_output=True, text=True, timeout=1800):
        if exc is not None:
            raise exc
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if not missing:
            out.write_bytes(b"x" * out_size)
        return SimpleNamespace(returncode=returncode, stderr="boom")
    return fake_run


@pytest.fixture
def _short_source(tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"\x00" * 64)
    return src


def test_trim_video_output_over_limit_rejected_and_deleted(tmp_path, _short_source):
    with mock.patch("pipeline.media.video_duration", return_value=10.0), \
            mock.patch("pipeline.media.subprocess.run", _patch_ffmpeg(out_size=300)):
        with pytest.raises(MediaError):
            media.trim_video(str(_short_source), str(tmp_path), "ffmpeg", "ffprobe", 30.0,
                             max_bytes=100)
    assert not (tmp_path / "src_clip.mp4").exists()


def test_trim_video_output_at_limit_accepted(tmp_path, _short_source):
    with mock.patch("pipeline.media.video_duration", return_value=10.0), \
            mock.patch("pipeline.media.subprocess.run", _patch_ffmpeg(out_size=100)):
        out = media.trim_video(str(_short_source), str(tmp_path), "ffmpeg", "ffprobe", 30.0,
                               max_bytes=100)
    assert Path(out).exists()
    assert Path(out).stat().st_size <= 100


def test_trim_video_timeout_removes_output(tmp_path, _short_source):
    with mock.patch("pipeline.media.video_duration", return_value=10.0), \
            mock.patch("pipeline.media.subprocess.run",
                       _patch_ffmpeg(exc=TimeoutError("hung"))):
        with pytest.raises(MediaError):
            media.trim_video(str(_short_source), str(tmp_path), "ffmpeg", "ffprobe", 30.0,
                             max_bytes=100)
    assert not (tmp_path / "src_clip.mp4").exists()


def test_trim_video_missing_output_removed(tmp_path, _short_source):
    with mock.patch("pipeline.media.video_duration", return_value=10.0), \
            mock.patch("pipeline.media.subprocess.run", _patch_ffmpeg(out_size=0, missing=True)):
        with pytest.raises(MediaError):
            media.trim_video(str(_short_source), str(tmp_path), "ffmpeg", "ffprobe", 30.0,
                             max_bytes=100)
    assert not (tmp_path / "src_clip.mp4").exists()


# --------------------------------------------- final validation (Issue 2G)

def test_validate_final_media_size(tmp_path):
    small = tmp_path / "img.jpg"
    small.write_bytes(b"x" * 100)
    assert media.validate_final_media_size(str(small), "image", 1000, 100) == str(small)

    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * 200)
    with pytest.raises(MediaError):
        media.validate_final_media_size(str(big), "image", 100, 1000)

    with pytest.raises(MediaError):
        media.validate_final_media_size(str(tmp_path / "missing.jpg"), "image", 1000, 100)


def test_ytdl_passes_max_filesize_option(tmp_path):
    target = tmp_path / "vid123.mp4"
    target.write_bytes(b"\x00" * 10)
    captured = {}

    class _FakeYDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {"id": "vid123", "title": "t"}

        def prepare_filename(self, info):
            return str(target)

    with mock.patch("pipeline.media.yt_dlp.YoutubeDL", lambda opts: _FakeYDL(opts)):
        out = media.ytdl_download("https://youtu.be/vid123", str(tmp_path), 123456)
    assert captured["opts"]["max_filesize"] == 123456
    assert out == str(target)


# ------------------------------------------------- main.prepare_item

def _prep_cfg(tmp_path):
    return {
        "paths": {
            "ffmpeg": "tools/ffmpeg/ffmpeg.exe",
            "ffprobe": "tools/ffmpeg/ffprobe.exe",
            "assets_dir": str(tmp_path / "assets"),
        },
        "youtube": {"clip_max_seconds": 30, "clip_min_seconds": 8.0},
        "posting": {"max_image_bytes": 20000000, "max_video_bytes": 2000},
    }


def test_prepare_short_x_video_within_limit_not_trimmed(tmp_path):
    src = tmp_path / "assets" / "x" / "v1.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 1000)  # <= max_video_bytes (2000)
    item = {
        "source": "x", "source_id": "v1", "kind": "video",
        "media_url": "https://example.com/v.mp4", "media_path": str(src),
        "source_url": "https://x.com/status/1",
    }
    with mock.patch("pipeline.media.video_duration", return_value=10.0), \
            mock.patch("pipeline.media.trim_video") as trim:
        out = main.prepare_item(item, _prep_cfg(tmp_path), _prep_cfg(tmp_path)["paths"])
    assert out == str(src)
    trim.assert_not_called()


def test_short_x_video_oversized_not_returned_without_trim(tmp_path):
    src = tmp_path / "assets" / "x" / "v1.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 5000)  # over max_video_bytes (2000)
    fail = tmp_path / "assets" / "x" / "trimmed.mp4"
    fail.write_bytes(b"x" * 5000)
    item = {
        "source": "x", "source_id": "v1", "kind": "video",
        "media_url": "https://example.com/v.mp4", "media_path": str(src),
        "source_url": "https://x.com/status/1",
    }

    def _fake_trim(*a, **k):
        return str(fail)

    with mock.patch("pipeline.media.video_duration", return_value=10.0), \
            mock.patch("pipeline.media.trim_video", side_effect=_fake_trim):
        out = main.prepare_item(item, _prep_cfg(tmp_path), _prep_cfg(tmp_path)["paths"])
    assert out is None  # the oversized short source was never returned as-is


def test_prepare_failure_records_no_dedup(db):
    item = {
        "source": "youtube", "source_id": "vid-1",
        "source_url": "https://youtu.be/vid-1", "title": "clip",
        "score": 10.0, "_caption": "c",
    }
    cfg = {
        "tiktok": {"foryou": True, "accounts": []},
        "secrets": {"youtube_api_key": ""},
        "youtube": {"shorts_feed": False},
        "x_sources": {"accounts": []},
        "paths": {"assets_dir": "assets"},
        "filters": {"blocked_keywords": [], "cooldown_days": 30},
        "posting": {"caption_style": "title", "caption_pool": [],
                    "random_caption_chance": 0.0, "max_caption_len": 200},
    }
    with mock.patch("scrapers.tiktok_scraper.scrape", return_value=[item]), \
            mock.patch("main.prepare_item", return_value=None):
        picked = main.pick_item(cfg, db, mock.MagicMock())
    assert picked is None
    assert not db.is_source_seen("youtube", "vid-1")
    assert not db.is_hash_seen("deadbeef", 30)