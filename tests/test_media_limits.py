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


# ---- Defect A: filesystem write/open failures never leave a partial file ----

class _FlakyWriter:
    """Wraps a real file so ``write`` fails on the Nth call with OSError."""

    def __init__(self, real_file, fail_on_write=2):
        self._f = real_file
        self._n = 0
        self._fail_on = fail_on_write

    def write(self, data):
        self._n += 1
        if self._n == self._fail_on:
            raise OSError("disk write failed")
        return self._f.write(data)

    def flush(self):
        return self._f.flush()

    def close(self):
        return self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _flaky_open_wrapper(writer_cls=_FlakyWriter, **kw):
    real_open = open

    def _open(path, mode="r", *a, **k):
        raw = real_open(path, mode, *a, **k)
        return writer_cls(raw, **kw)

    return _open


# Test A — an OSError from f.write() leaves no partial file at any name.
def test_download_write_error_removes_partial(tmp_path):
    dest = tmp_path / "big.bin"
    with mock.patch("builtins.open", _flaky_open_wrapper()), \
         mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 64 * 3))):  # >= 2 chunks written
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=100_000)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []  # no .part leftover either


# Test B: the write failure surfaces as MediaError with the OSError as cause.
def test_download_write_error_preserves_cause(tmp_path):
    dest = tmp_path / "big.bin"
    with mock.patch("builtins.open", _flaky_open_wrapper()), \
         mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 200))):
        with pytest.raises(MediaError) as ei:
            media.download("https://x/img", str(dest), max_bytes=100_000)
    assert isinstance(ei.value.__cause__, OSError)
    assert not (tmp_path / "big.bin.part").exists()


# Test C: file-open failure (PermissionError) creates no output at all.
def test_download_write_open_failure_no_output(tmp_path):
    dest = tmp_path / "locked.bin"

    def _deny_open(*a, **k):
        raise PermissionError("denied")

    with mock.patch("builtins.open", _deny_open), \
         mock.patch("pipeline.media.requests.get", _stub_get(_FakeResp(body=b"a" * 64))):
        with pytest.raises(MediaError) as ei:
            media.download("https://x/img", str(dest), max_bytes=100_000)
    assert isinstance(ei.value.__cause__, PermissionError)
    assert not dest.exists()
    assert not (tmp_path / "locked.bin.part").exists()


# Test A2: a flush()/close failure also cleans up.
def test_download_flush_failure_removes_partial(tmp_path):
    class _FlushFail(_FlakyWriter):
        def flush(self):
            raise OSError("flush failed")

    dest = tmp_path / "flush.bin"
    with mock.patch("builtins.open", _flaky_open_wrapper(writer_cls=_FlushFail)), \
         mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 64))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=100_000)
    assert not dest.exists()
    assert not (tmp_path / "flush.bin.part").exists()


# Test H: the final path is not visible while the transfer is incomplete.
def test_download_final_path_not_visible_prematurely(tmp_path):
    dest = tmp_path / "x.bin"
    assert not dest.exists()

    class _Slow(_FakeResp):
        def iter_content(self, chunk_size):
            yield b"a" * 64
            raise OSError("device error")

    with mock.patch("builtins.open", _flaky_open_wrapper(fail_on_write=2)), \
         mock.patch("pipeline.media.requests.get", _stub_get(_Slow(body=b"a" * 300))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=10_000)
    assert not dest.exists()


# Defect A / Test D: existing size-overflow cleanup still removes the partial.
def test_download_size_overflow_still_cleans_partial(tmp_path):
    dest = tmp_path / "grow.bin"
    with mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 101))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=100)
    assert not dest.exists()
    assert not (tmp_path / "grow.bin.part").exists()


# Test F: a successful download leaves the final file byte-exact, no .part.
def test_download_success_leaves_no_part(tmp_path):
    dest = tmp_path / "ok.bin"
    payload = bytes(range(256)) * 3
    with mock.patch("pipeline.media.requests.get", _stub_get(_FakeResp(body=payload))):
        got = media.download("https://x/img", str(dest), max_bytes=10_000)
    assert got == str(dest)
    assert dest.read_bytes() == payload
    assert not (tmp_path / "ok.bin.part").exists()


# A failed re-download does not destroy a pre-existing valid destination file.
def test_download_failure_preserves_preexisting_destination(tmp_path):
    dest = tmp_path / "keep.bin"
    dest.write_bytes(b"precious")
    with mock.patch("pipeline.media.requests.get",
                    _stub_get(_FakeResp(body=b"a" * 300))):
        with pytest.raises(MediaError):
            media.download("https://x/img", str(dest), max_bytes=100)
    assert dest.read_bytes() == b"precious"
    assert not (tmp_path / "keep.bin.part").exists()


# --------------------------------------------------------------- x download_media

class _FakeXContext:
    """Minimal fake of the Playwright BrowserContext: ONLY cookies().

    Deliberately has no ``request.get(...)`` — if production code still tried
    the old Playwright API-request path it would fail with AttributeError.
    """

    def __init__(self, cookies=None):
        self._cookies = cookies or []

    def cookies(self, *a, **k):
        return self._cookies


def _x_session(cookies=None):
    context = _FakeXContext(cookies)
    return SimpleNamespace(cookies=context.cookies)


def _x_item(url="https://pbs.twimg.com/media/photo.jpg", source_id="tweet123"):
    return {
        "source": "x",
        "source_id": source_id,
        "media_url": url,
        "kind": "image",
    }


def _x_download(resp, item=None, max_bytes=None, cookies=None, tmp_path=None, dest_dir=None):
    dest = dest_dir or str(tmp_path)
    with mock.patch("pipeline.media.requests.get", _stub_get(resp)):
        return x_scraper.download_media(_x_session(cookies), item or _x_item(), dest, max_bytes=max_bytes)


# Defect 2 Test A — oversized Content-Length rejected before transfer.
def test_x_download_media_rejects_over_content_length(tmp_path):
    resp = _FakeResp(headers={"Content-Length": "999999"})
    got = _x_download(resp, _x_item(), max_bytes=100, tmp_path=tmp_path)
    assert got is None
    assert list(tmp_path.iterdir()) == []


# Defect 2 Test B — missing Content-Length cannot bypass; no body materialization.
def test_x_download_media_missing_cl_stops_at_limit(tmp_path):
    resp = _FakeResp(body=b"a" * 200)
    got = _x_download(resp, _x_item(), max_bytes=100, tmp_path=tmp_path)
    assert got is None
    assert list(tmp_path.iterdir()) == []


# Defect 2 Test C — lying low Content-Length cannot bypass the ceiling.
def test_x_download_media_lying_low_content_length_rejected(tmp_path):
    resp = _FakeResp(headers={"Content-Length": "50"}, body=b"a" * 200)
    got = _x_download(resp, _x_item(), max_bytes=100, tmp_path=tmp_path)
    assert got is None
    assert list(tmp_path.iterdir()) == []


# Defect 2 Test D — exact max boundary succeeds.
def test_x_download_media_exact_limit_succeeds(tmp_path):
    resp = _FakeResp(body=b"a" * 100)
    got = _x_download(resp, _x_item(), max_bytes=100, tmp_path=tmp_path)
    assert got is not None
    assert (tmp_path / "tweet123.jpg").read_bytes() == b"a" * 100


# Defect 2 Test E — max + 1 rejected.
def test_x_download_media_max_plus_one_rejected(tmp_path):
    resp = _FakeResp(body=b"a" * 101)
    got = _x_download(resp, _x_item(), max_bytes=100, tmp_path=tmp_path)
    assert got is None
    assert list(tmp_path.iterdir()) == []


# Defect 2 Test F — multi-chunk success preserves exact byte sequence.
def test_x_download_media_multichunk_content_correct(tmp_path):
    payload = bytes(range(256)) * 3  # several chunks across the 64-byte fake iterator
    resp = _FakeResp(body=payload)
    got = _x_download(resp, _x_item(), max_bytes=10_000, tmp_path=tmp_path)
    assert got is not None
    assert (tmp_path / "tweet123.jpg").read_bytes() == payload


# Defect 2 Test G — cleanup on network exception mid-stream.
def test_x_download_media_cleans_partial_on_error(tmp_path):
    class _Burst(_FakeResp):
        def iter_content(self, chunk_size):
            yield b"a" * 64
            raise requests.exceptions.ConnectionError("socket gone")

    got = _x_download(_Burst(body=b"a" * 300), _x_item(), max_bytes=10_000, tmp_path=tmp_path)
    assert got is None
    assert list(tmp_path.iterdir()) == []


def test_x_download_media_rejects_http_error(tmp_path):
    got = _x_download(_FakeResp(ok=False), _x_item(), max_bytes=100, tmp_path=tmp_path)
    assert got is None
    assert list(tmp_path.iterdir()) == []


# Cookie transfer: only cookies whose domain matches the media host are copied.
def test_x_download_media_copies_only_matching_domain_cookies(tmp_path):
    cookies = [
        {"name": "tw_cookie", "value": "cdnc-1", "domain": ".twimg.com"},
        {"name": "auth_token", "value": "sess", "domain": ".x.com"},
    ]
    seen = {}

    def _spy_get(url, headers=None, stream=True, timeout=90, allow_redirects=True, cookies=None):
        seen["cookies"] = cookies
        return _FakeResp(body=b"a" * 80)

    with mock.patch("pipeline.media.requests.get", _spy_get):
        got = x_scraper.download_media(
            _x_session(cookies), _x_item(), str(tmp_path), max_bytes=100
        )
    assert got is not None
    assert seen["cookies"] == {"tw_cookie": "cdnc-1"}  # session cookie NOT sent


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


# ------------------------------------------------------------- yt-dlp size check

def _ytdl_fake(files, info_id="vid123", template="vid123.mp4",
               requested_filepaths=None, captured=None):
    """Build a fake YoutubeDL that mirrors real yt-dlp behavior.

    Files are written into the per-operation working directory derived from
    ``opts['outtmpl']`` (exactly where real yt-dlp writes). ``requested_filepaths``
    (optional) populates ``info['requested_downloads'][*]['filepath']`` — the
    authoritative output metadata real yt-dlp returns after download + postprocessing.
    ``template`` is what ``prepare_filename`` returns (may differ from the real
    written file, simulating yt-dlp renaming/merging).
    """
    class _FakeYDL:
        def __init__(self, opts):
            if captured is not None:
                captured["opts"] = opts
            self._dir = Path(opts["outtmpl"]).parent

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            for name, content in files.items():
                (self._dir / name).write_bytes(content)
            info = {"id": info_id, "title": "t"}
            if requested_filepaths is not None:
                info["requested_downloads"] = [
                    {"filepath": str(self._dir / name)} for name in requested_filepaths
                ]
            return info

        def prepare_filename(self, info):
            return str(self._dir / template)

    return _FakeYDL


def _ytdl_run(fake, max_bytes, dest_dir):
    with mock.patch("pipeline.media.yt_dlp.YoutubeDL", lambda opts: fake(opts)):
        return media.ytdl_download("https://youtu.be/vid123", str(dest_dir), max_bytes)


# yt-dlp Test (config): max_filesize remains configured.
def test_ytdl_passes_max_filesize_option(tmp_path):
    captured = {}
    fake = _ytdl_fake({"vid123.mp4": b"x" * 10}, captured=captured)
    out = _ytdl_run(fake, 123456, tmp_path)
    assert captured["opts"]["max_filesize"] == 123456
    assert out == str(tmp_path / "vid123.mp4")


# yt-dlp Test A — under limit accepted.
def test_ytdl_under_limit_accepted(tmp_path):
    fake = _ytdl_fake({"vid123.mp4": b"x" * 80})
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mp4")
    assert (tmp_path / "vid123.mp4").stat().st_size == 80


# yt-dlp Test B — exact boundary accepted.
def test_ytdl_exact_boundary_accepted(tmp_path):
    fake = _ytdl_fake({"vid123.mp4": b"x" * 100})
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mp4")


# yt-dlp Test C — max + 1 rejected and removed.
def test_ytdl_max_plus_one_rejected_and_removed(tmp_path):
    fake = _ytdl_fake({"vid123.mp4": b"x" * 101})
    with pytest.raises(MediaError):
        _ytdl_run(fake, 100, tmp_path)
    assert not (tmp_path / "vid123.mp4").exists()


# yt-dlp Test D — downloader ignores max_filesize; the post-download stat catches it.
def test_ytdl_ignored_max_filesize_still_caught(tmp_path):
    captured = {}
    # Fake receives max_filesize=100 but deliberately writes 101 bytes.
    fake = _ytdl_fake({"vid123.mp4": b"x" * 101}, captured=captured)
    with pytest.raises(MediaError):
        _ytdl_run(fake, 100, tmp_path)
    assert captured["opts"]["max_filesize"] == 100
    assert not (tmp_path / "vid123.mp4").exists()


# yt-dlp Test E — the actual final/postprocessed file is the one validated.
def test_ytdl_actual_merged_filename_validated(tmp_path):
    # prepare_filename points at a name that never materializes; the real merged
    # file has a different name, and that file's size is what is checked.
    fake = _ytdl_fake({"vid123.mkv": b"x" * 80}, template="vid123.mp4")
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mkv")  # actual file returned, not template

    # A later oversized current output must be removed, and must NOT replace the
    # previously accepted file in the durable destination.
    fake_big = _ytdl_fake({"vid123.mkv": b"x" * 101}, template="vid123.mp4")
    with pytest.raises(MediaError):
        _ytdl_run(fake_big, 100, tmp_path)
    assert (tmp_path / "vid123.mkv").read_bytes() == b"x" * 80  # previous file intact
    assert not list(tmp_path.glob(".ytdl_*"))  # no temp dir / oversized output left


# yt-dlp Test F — oversized output removed (also asserted in C/D/E).
def test_ytdl_oversized_output_removed(tmp_path):
    fake = _ytdl_fake({"vid123.webm": b"x" * 500}, template="vid123.mp4")
    with pytest.raises(MediaError):
        _ytdl_run(fake, 100, tmp_path)
    assert not (tmp_path / "vid123.webm").exists()


def test_ytdl_no_output_raises(tmp_path):
    fake = _ytdl_fake({})
    with pytest.raises(MediaError):
        _ytdl_run(fake, 100, tmp_path)


# ---- Defect B: current-output identity (stale same-ID file must never win) ----

# Test A — a stale LARGER same-ID file is not selected over the current output.
def test_ytdl_stale_larger_same_id_not_selected(tmp_path):
    (tmp_path / "vid123.webm").write_bytes(b"x" * 90)  # old stale file, larger
    fake = _ytdl_fake({"vid123.mkv": b"x" * 80}, requested_filepaths=["vid123.mkv"])
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mkv")  # current output, NOT the stale webm
    assert (tmp_path / "vid123.webm").exists()  # stale file untouched


# Test B — a stale SMALLER same-ID file does not matter either.
def test_ytdl_stale_smaller_same_id_not_selected(tmp_path):
    (tmp_path / "vid123.webm").write_bytes(b"x" * 20)
    fake = _ytdl_fake({"vid123.mkv": b"x" * 80}, requested_filepaths=["vid123.mkv"])
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mkv")
    assert (tmp_path / "vid123.webm").exists()


# Test C — authoritative requested_downloads filepath wins over directory guesses.
def test_ytdl_authoritative_filepath_metadata_wins(tmp_path):
    # Two current-looking files, but yt-dlp's metadata names exactly one as final.
    fake = _ytdl_fake({"vid123.mkv": b"x" * 80, "vid123.part.mp4": b"x" * 30},
                      requested_filepaths=["vid123.mkv"])
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mkv")


# Test D — postprocessed extension differs from the prepare_filename template.
def test_ytdl_postprocessed_extension_difference(tmp_path):
    # template says .webm, but the real postprocessed (converted) file is .mp4.
    fake = _ytdl_fake({"vid123.mp4": b"x" * 80}, template="vid123.webm")
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mp4")
    assert (tmp_path / "vid123.mp4").stat().st_size == 80


# Test E — current oversized output removed; the stale same-ID file is retained.
def test_ytdl_oversized_current_removed_stale_retained(tmp_path):
    (tmp_path / "vid123.webm").write_bytes(b"x" * 50)  # stale file, under limit
    fake = _ytdl_fake({"vid123.mkv": b"x" * 101}, requested_filepaths=["vid123.mkv"])
    with pytest.raises(MediaError):
        _ytdl_run(fake, 100, tmp_path)
    assert not (tmp_path / "vid123.mkv").exists()  # current output cleanup
    assert (tmp_path / "vid123.webm").exists()     # stale file NOT deleted
    assert (tmp_path / "vid123.webm").stat().st_size == 50


# Test I — unrelated destination files are untouched by the yt-dlp operation.
def test_ytdl_unrelated_files_untouched(tmp_path):
    (tmp_path / "other-video.mp4").write_bytes(b"x" * 12)
    (tmp_path / "notes.txt").write_bytes(b"hashed pen")
    (tmp_path / "some-image.jpg").write_bytes(b"j" * 7)
    fake = _ytdl_fake({"vid123.mp4": b"x" * 80})
    out = _ytdl_run(fake, 100, tmp_path)
    assert out == str(tmp_path / "vid123.mp4")
    assert (tmp_path / "other-video.mp4").read_bytes() == b"x" * 12
    assert (tmp_path / "notes.txt").read_bytes() == b"hashed pen"
    assert (tmp_path / "some-image.jpg").read_bytes() == b"j" * 7


# Test J — ambiguous current output (no authoritative metadata) fails safely.
def test_ytdl_ambiguous_output_fails_safely(tmp_path):
    fake = _ytdl_fake({"vid123.mkv": b"x" * 80, "vid123.webm": b"x" * 90})
    with pytest.raises(MediaError):
        _ytdl_run(fake, 100, tmp_path)
    # neither may be moved into the destination by a guessed pick
    assert not (tmp_path / "vid123.mkv").exists()
    assert not (tmp_path / "vid123.webm").exists()


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
