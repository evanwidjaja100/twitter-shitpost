"""Perceptual near-duplicate detection tests (Issue 4).

Purely deterministic PIL-based fingerprint tests (no network, no browser) plus
DB-cooldown integration and pick/finalize wiring. Video frame extraction is
exercised with a mocked ``subprocess.run`` that writes a known PNG to the
ffmpeg output path, so no real ffmpeg is needed.

Guarantees proven here:
  * identical / re-encoded images share the same dHash; a different image does not
  * Hamming distance works on 64-bit dHashes
  * ``is_near_duplicate`` is conservative: a single surviving frame of a video
    never decides a match, and an empty fingerprint never matches
  * fingerprints are recorded atomically on finalize and roll back on failure
  * cooldown windows for fingerprints mirror the hash cooldown semantics
  * ``pick_item`` skips candidates whose fingerprint collides with a recent one
    (the near-dup image is never posted), while distinct media passes through
"""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

import main
from pipeline import perceptual
from storage.db import Database


# ------------------------------------------------------------- helpers

def _gradient(path, width=128, height=128):
    """A deterministic luminosity ramp: row colour steps predictably."""
    im = Image.new("L", (width, height))
    for y in range(height):
        for x in range(width):
            im.putpixel((x, y), (x * 255 // width + y * 255 // height) % 256)
    im.save(path)
    return path


def _checkerboard(path, width=128, height=128):
    """A deterministic high-frequency pattern — visually unrelated to gradient."""
    im = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            c = (0, 0, 0) if (x // 8 + y // 8) % 2 == 0 else (255, 255, 255)
            im.putpixel((x, y), c)
    im.save(path)
    return path


def _fake_ffmpeg_sequence(frame_paths):
    """A subprocess.run that writes frame i's PNG to cmd[-1], deterministically."""
    counter = {"i": 0}

    def _run(cmd, *a, **k):
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        src = Path(frame_paths[counter["i"] % len(frame_paths)])
        out.write_bytes(src.read_bytes())
        counter["i"] += 1
        return SimpleNamespace(returncode=0, stderr="")
    return _run


# ------------------------------------------------------------- raw dHash

def test_dhash_is_deterministic_and_identical_for_reencode(tmp_path):
    a = _gradient(tmp_path / "a.png")
    b = _gradient(tmp_path / "b.png")
    assert perceptual.dhash(str(a)) == perceptual.dhash(str(b))


def test_dhash_hamming_distance_zero_for_identical_files(tmp_path):
    a = _gradient(tmp_path / "a.png")
    b = _gradient(tmp_path / "b.png")
    assert perceptual.hamming(perceptual.dhash(str(a)), perceptual.dhash(str(b))) == 0


def test_dhash_different_content_far_apart(tmp_path):
    grad = perceptual.dhash(str(_gradient(tmp_path / "grad.png")))
    checker = perceptual.dhash(str(_checkerboard(tmp_path / "chk.png")))
    assert perceptual.hamming(grad, checker) > perceptual.PERCEPTUAL_DISTANCE


def test_image_fingerprints_returns_single_hash(tmp_path):
    path = _gradient(tmp_path / "img.png")
    assert perceptual.image_fingerprints(str(path)) == [perceptual.dhash(str(path))]


def test_medium_fingerprints_image(tmp_path):
    path = _gradient(tmp_path / "img.png")
    assert perceptual.medium_fingerprints(str(path), "image", None, None) == [
        perceptual.dhash(str(path))
    ]


def test_medium_fingerprints_video_without_ffmpeg_returns_empty(tmp_path):
    assert perceptual.medium_fingerprints(str(tmp_path / "no.mp4"), "video", None, None) == []


def test_medium_fingerprints_unreadable_image_is_empty_and_safe(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"not an image")
    assert perceptual.medium_fingerprints(str(bad), "image", None, None) == []


# ---------------------------------------------------------- video frames

def test_video_fingerprints_uses_deterministic_frame_timestamps(tmp_path):
    frame_a = _gradient(tmp_path / "frameA.png")
    frame_b = _gradient(tmp_path / "frameB.png")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fake video bytes")

    with mock.patch("pipeline.perceptual.video_duration", return_value=60.0), \
            mock.patch("pipeline.perceptual.subprocess.run",
                       _fake_ffmpeg_sequence([frame_a, frame_b, frame_a])):
        fps = perceptual.video_fingerprints(str(src), "ffmpeg", "ffprobe")

    expected_a = perceptual.dhash(str(frame_a))
    expected_b = perceptual.dhash(str(frame_b))
    # Frames at 0.2/0.5/0.8 -> a, b, a.
    assert fps == [expected_a, expected_b, expected_a]


def test_video_fingerprints_raises_when_frame_extraction_fails(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"bytes")
    with mock.patch("pipeline.perceptual.video_duration", return_value=10.0), \
            mock.patch("pipeline.perceptual.subprocess.run",
                       return_value=SimpleNamespace(returncode=1, stderr="boom")):
        with pytest.raises(Exception):
            perceptual.video_fingerprints(str(src), "ffmpeg", "ffprobe")


def _owned_temp_factory(parent):
    real_mkdtemp = perceptual.tempfile.mkdtemp

    def make(*args, **kwargs):
        kwargs["dir"] = str(parent)
        return real_mkdtemp(*args, **kwargs)

    return make


def test_video_fingerprint_success_cleans_owned_temp_directory(tmp_path):
    frame = _gradient(tmp_path / "frame.png")
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    with mock.patch(
        "pipeline.perceptual.tempfile.mkdtemp",
        side_effect=_owned_temp_factory(temp_parent),
    ), mock.patch(
        "pipeline.perceptual.video_duration", return_value=10.0
    ), mock.patch(
        "pipeline.perceptual.subprocess.run",
        _fake_ffmpeg_sequence([frame, frame, frame]),
    ):
        assert len(perceptual.video_fingerprints("clip.mp4", "ffmpeg", "ffprobe")) == 3
    assert list(temp_parent.iterdir()) == []


@pytest.mark.parametrize("fail_at", [1, 2])
def test_video_fingerprint_extraction_failure_cleans_all_frames(tmp_path, fail_at):
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    calls = {"n": 0}

    def run(cmd, *args, **kwargs):
        calls["n"] += 1
        output = Path(cmd[-1])
        if calls["n"] == fail_at:
            return SimpleNamespace(returncode=1, stderr="induced failure")
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("L", (9, 8), 0).save(output)
        return SimpleNamespace(returncode=0, stderr="")

    with mock.patch(
        "pipeline.perceptual.tempfile.mkdtemp",
        side_effect=_owned_temp_factory(temp_parent),
    ), mock.patch(
        "pipeline.perceptual.video_duration", return_value=10.0
    ), mock.patch("pipeline.perceptual.subprocess.run", side_effect=run):
        with pytest.raises(Exception, match="induced failure"):
            perceptual.video_fingerprints("clip.mp4", "ffmpeg", "ffprobe")
    assert list(temp_parent.iterdir()) == []


def test_video_fingerprint_decode_failure_cleans_temp_directory(tmp_path):
    frame = _gradient(tmp_path / "frame.png")
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    with mock.patch(
        "pipeline.perceptual.tempfile.mkdtemp",
        side_effect=_owned_temp_factory(temp_parent),
    ), mock.patch(
        "pipeline.perceptual.video_duration", return_value=10.0
    ), mock.patch(
        "pipeline.perceptual.subprocess.run",
        _fake_ffmpeg_sequence([frame, frame, frame]),
    ), mock.patch("pipeline.perceptual.dhash", side_effect=OSError("decode boom")):
        with pytest.raises(OSError, match="decode boom"):
            perceptual.video_fingerprints("clip.mp4", "ffmpeg", "ffprobe")
    assert list(temp_parent.iterdir()) == []


def test_repeated_video_fingerprints_do_not_accumulate_temp_dirs(tmp_path):
    frame = _gradient(tmp_path / "frame.png")
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    with mock.patch(
        "pipeline.perceptual.tempfile.mkdtemp",
        side_effect=_owned_temp_factory(temp_parent),
    ), mock.patch(
        "pipeline.perceptual.video_duration", return_value=10.0
    ), mock.patch(
        "pipeline.perceptual.subprocess.run",
        _fake_ffmpeg_sequence([frame]),
    ):
        for _ in range(12):
            perceptual.video_fingerprints("clip.mp4", "ffmpeg", "ffprobe")
            assert list(temp_parent.iterdir()) == []


def test_cleanup_failure_is_logged_without_replacing_fingerprint_result(
    tmp_path, caplog
):
    frame = _gradient(tmp_path / "frame.png")
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    real_rmtree = perceptual.shutil.rmtree
    real_mkdtemp = perceptual.tempfile.mkdtemp
    owned = []

    def make(*args, **kwargs):
        kwargs["dir"] = str(temp_parent)
        path = real_mkdtemp(*args, **kwargs)
        owned.append(Path(path))
        return path

    with mock.patch(
        "pipeline.perceptual.tempfile.mkdtemp", side_effect=make
    ), mock.patch(
        "pipeline.perceptual.video_duration", return_value=10.0
    ), mock.patch(
        "pipeline.perceptual.subprocess.run", _fake_ffmpeg_sequence([frame])
    ), mock.patch(
        "pipeline.perceptual.shutil.rmtree", side_effect=OSError("cleanup denied")
    ):
        result = perceptual.video_fingerprints("clip.mp4", "ffmpeg", "ffprobe")

    assert len(result) == 3
    assert "cleanup denied" in caplog.text
    for path in owned:
        real_rmtree(path)


def test_cleanup_failure_does_not_replace_primary_fingerprint_error(tmp_path):
    frame = _gradient(tmp_path / "frame.png")
    temp_parent = tmp_path / "temps"
    temp_parent.mkdir()
    real_mkdtemp = perceptual.tempfile.mkdtemp
    real_rmtree = perceptual.shutil.rmtree
    owned = []

    def make(*args, **kwargs):
        kwargs["dir"] = str(temp_parent)
        path = real_mkdtemp(*args, **kwargs)
        owned.append(Path(path))
        return path

    with mock.patch(
        "pipeline.perceptual.tempfile.mkdtemp", side_effect=make
    ), mock.patch(
        "pipeline.perceptual.video_duration", return_value=10.0
    ), mock.patch(
        "pipeline.perceptual.subprocess.run", _fake_ffmpeg_sequence([frame])
    ), mock.patch(
        "pipeline.perceptual.dhash", side_effect=RuntimeError("primary hash error")
    ), mock.patch(
        "pipeline.perceptual.shutil.rmtree", side_effect=OSError("cleanup denied")
    ):
        with pytest.raises(RuntimeError, match="primary hash error"):
            perceptual.video_fingerprints("clip.mp4", "ffmpeg", "ffprobe")

    for path in owned:
        real_rmtree(path)


# ------------------------------------------------------------- near-dup rule

def test_is_near_duplicate_identical_image():
    h = "0123456789abcdef"
    assert perceptual.is_near_duplicate([h], [h]) is True


def test_is_near_duplicate_empty_candidate_never_matches():
    assert perceptual.is_near_duplicate([], ["abc"]) is False
    assert perceptual.is_near_duplicate(["abc"], []) is False


def test_is_near_duplicate_video_requires_majority():
    known = ["1111111111111111"]
    # Only one of three candidate frames matches -> majority (2 of 3) unmet.
    candidate = ["1111111111111111", "2222222222222222", "3333333333333333"]
    assert perceptual.is_near_duplicate(candidate, known) is False

    # Reusing one historical frame cannot create a majority.
    candidate2 = ["1111111111111111", "2222222222222222", "1111111111111111"]
    assert perceptual.is_near_duplicate(candidate2, known) is False

    # Two distinct historical samples matching two candidate samples does.
    assert perceptual.is_near_duplicate(candidate2, [
        "1111111111111111", "1111111111111111",
    ]) is True


def test_is_near_duplicate_single_frame_image_matches_itself():
    h = "abcdefabcdefabcd"
    assert perceptual.is_near_duplicate([h], [h]) is True


# --------------------------------------------------------- DB fingerprint rows

def test_record_fingerprints_preserves_separate_groups(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    db.record_fingerprints(["aa", "bb"], "youtube", "u", media_kind="video")
    db.record_fingerprints(["aa"], "youtube", "u2", media_kind="video")
    groups = db.fingerprint_groups("video", 30)
    assert [g["fingerprints"] for g in groups] == [["aa", "bb"], ["aa"]]


def test_fingerprint_groups_respect_cooldown_cutoff(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    now = 1_700_000_000.0
    db.record_fingerprints(["near"], "youtube", "u", now_ts=now)
    # A cooldown that still covers ``now + 3600`` keeps it; an expired one drops it.
    recent = db.fingerprint_groups("image", cooldown_days=1, now_ts=now + 3600)
    expired = db.fingerprint_groups("image", cooldown_days=0, now_ts=now + 1)
    assert [group["fingerprints"] for group in recent] == [["near"]]
    assert expired == []


def test_finalize_successful_post_records_fingerprints_atomically(tmp_path):
    db = Database(str(tmp_path / "bot.db"))
    db.finalize_successful_post(
        caption="c", media_path="m.mp4", source="youtube", source_id="vid",
        source_url="https://youtu.be/v", content_hash="h",
        fingerprints=["frame1", "frame2"],
        media_kind="video",
        now_ts=1_700_000_000.0,
    )
    fresh = Database(str(tmp_path / "bot.db"))
    groups = fresh.fingerprint_groups("video", 30, now_ts=1_700_000_001.0)
    assert [group["fingerprints"] for group in groups] == [["frame1", "frame2"]]


def test_finalize_failure_rolls_back_fingerprints_too(tmp_path):
    db = Database(str(tmp_path / "bot.db"))

    class _Fail3:
        def __init__(self, real):
            self._real = real
            self._n = 0

        def execute(self, *a, **k):
            self._n += 1
            if self._n == 3:   # posts ok, source ok, hash fails
                raise RuntimeError("boom")
            return self._real.execute(*a, **k)

        def commit(self):
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

    db._conn = _Fail3(db._conn)
    with pytest.raises(RuntimeError):
        db.finalize_successful_post(
            caption="c", media_path="m", source="youtube", source_id="vid",
            source_url="https://youtu.be/v", content_hash="h",
            fingerprints=["f1"],
            media_kind="image",
            now_ts=1_700_000_000.0,
        )
    fresh = Database(str(tmp_path / "bot.db"))
    assert fresh.fingerprint_groups("image", 30, now_ts=1_700_000_001.0) == []


# ------------------------------------------------------ pick_item integration

def _pick_cfg():
    return {
        "tiktok": {"foryou": True, "accounts": []},
        "secrets": {"youtube_api_key": ""},
        "youtube": {"shorts_feed": False},
        "x_sources": {"accounts": []},
        "paths": {"assets_dir": "assets"},
        "filters": {"blocked_keywords": [], "cooldown_days": 30},
        "posting": {
            "caption_style": "title",
            "caption_pool": [],
            "random_caption_chance": 0.0,
            "max_caption_len": 200,
        },
    }


def _media(tmp_path):
    return _gradient(tmp_path / "media.png")


def _seed_source_item():
    return {
        "source": "youtube",
        "source_id": "vid-9",
        "source_url": "https://youtu.be/vid-9",
        "title": "t",
        "score": 1.0,
        "kind": "image",
        "media_path": None,
        "media_url": None,
    }


def _selection_ctx(media, md5="unique-md5"):
    """Context for a full ``pick_item`` run seeded through the TikTok source."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(mock.patch("scrapers.tiktok_scraper.scrape",
                                   return_value=[_seed_source_item()]))
    stack.enter_context(mock.patch("main.prepare_item", return_value=str(media)))
    stack.enter_context(mock.patch("pipeline.media.hash_file", return_value=md5))
    return stack


def test_pick_item_skips_near_duplicate_fingerprint(tmp_path):
    media = _media(tmp_path)
    candidate_fp = perceptual.dhash(str(media))

    db = Database(str(tmp_path / "bot.db"))
    db.record_fingerprints([candidate_fp], "x", "https://x.com/old")

    with _selection_ctx(media):
        picked = main.pick_item(_pick_cfg(), db, mock.MagicMock())

    assert picked is None  # blocked by near-dup, even though MD5 is fresh


def test_pick_item_allows_fresh_nondup_fingerprint(tmp_path):
    media = _media(tmp_path)
    db = Database(str(tmp_path / "bot.db"))  # empty fingerprints

    with _selection_ctx(media):
        picked = main.pick_item(_pick_cfg(), db, mock.MagicMock())

    assert picked is not None
    assert picked["source_id"] == "vid-9"
    # Fingerprint is computed but nothing is recorded at pick time.
    assert db.fingerprint_groups("image", 30) == []


def test_pick_item_not_blocked_when_fingerprint_unavailable(tmp_path):
    bad = tmp_path / "bad.gif"
    bad.write_bytes(b"nope")

    db = Database(str(tmp_path / "bot.db"))
    with _selection_ctx(str(bad)):
        picked = main.pick_item(_pick_cfg(), db, mock.MagicMock())

    # The unreadable media yields no fingerprint signal -> not a blocker.
    assert picked is not None


def test_mark_item_published_passes_fingerprints_to_finalize():
    db = mock.MagicMock()
    item = {
        "source": "yt", "source_id": "1", "source_url": "https://youtu.be/1",
        "kind": "video",
        "_caption": "c", "_media_path": "m.mp4", "_hash": "h",
        "_fingerprints": ["fp-1", "fp-2"],
    }
    main.mark_item_published(db, item)
    db.finalize_successful_post.assert_called_once_with(
        caption="c", media_path="m.mp4", source="yt", source_id="1",
        source_url="https://youtu.be/1", content_hash="h",
        fingerprints=["fp-1", "fp-2"],
        media_kind="video",
    )
