"""Perceptual fingerprinting (Issue 4).

Exact-byte dedup (``hashes`` table, MD5) catches identical files but misses a
video re-encoded by ``ffmpeg`` or a GIF re-saved by a different encoder —
content that is *perceptually the same* but byte-different. This module adds a
cheap, deterministic perceptual pass on top of exact hashing:

* Images — a 64-bit average-without-average difference hash (``dHash``):
  each pixel column is compared with its right neighbour, so the fingerprint
  is invariant to palette changes, resizing and mild recompression while fully
  deterministic (no random, no hash seed).
* Videos — three frames sampled at deterministic fractions of the duration
  (re-encoding a video with identical frames yields the same 3 hashes).

``is_near_duplicate`` compares a candidate fingerprint set to one historical
media group. A video is a near-duplicate when a *majority* (>= ceil(n/2)) of
its sampled frames can be matched one-to-one within the Hamming threshold.

Determinism & ordering guarantees: nothing in this module uses ``random`` or
wall-clock time, thresholds are module constants, and fingerprint tables are
region-first/location-first, so the same input always produces the same output
and results are reproducible run over run.

Safety: fingerprint failures are never a posting blocker. If the fingerprint
cannot be computed (unreadable media, ffmpeg missing, frame extraction error)
the pipeline logs and proceeds as "no near-duplicate evidence" — the exact
-byte dedup and source dedup still apply.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from pipeline.media import MediaError, video_duration

log = __import__("logging").getLogger("perceptual")

HASH_SIZE = 8            # dHash is an (HASH_SIZE+1) x HASH_SIZE grayscale image.
PERCEPTUAL_DISTANCE = 8  # max Hamming distance between two 64-bit dHashes still
                         # considered "near duplicate".
FRAME_FRACTIONS = (0.2, 0.5, 0.8)  # deterministic video sample points


def dhash(path: str, hash_size: int = HASH_SIZE) -> str:
    """64-bit dHash of an image file, as a 16-hex-char string.

    Every row of the ``(hash_size+1) x hash_size`` grayscale version is encoded
    as bits indicating whether each pixel's right neighbour is brighter; the
    row's hash is appended. Deterministic for identical visual content.
    """
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("L")
        im = im.resize((hash_size + 1, hash_size))
    px = im.load()
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            left = px[col, row]
            right = px[col + 1, row]
            bits.append(1 if left > right else 0)
    raw = sum(bit << (len(bits) - 1 - i) for i, bit in enumerate(bits))
    return f"{raw:0{hash_size * 8 // 4}x}"


def hamming(a: str, b: str) -> int:
    """Number of differing bits between two hex dHash strings."""
    if len(a) != len(b):
        return 2 ** 64
    n = int(a, 16) ^ int(b, 16)
    return bin(n).count("1")


def _frames(
    path: str,
    ffmpeg: str,
    ffprobe: str,
    work: Path,
    frame_count: int = 3,
) -> list[str]:
    """Extract ``frame_count`` deterministic frames from a video as PNG paths.

    Frame times are the fractions in ``FRAME_FRACTIONS`` of the total duration.
    A failure (ffprobe/ffmpeg missing, non-video input) raises ``MediaError``
    so callers can treat "no fingerprint" as a non-blocking missing signal.
    """
    if frame_count < 1:
        return []
    duration = video_duration(ffprobe, path)
    if duration <= 0:
        raise MediaError(f"cannot fingerprint {path}: zero duration")
    outputs: list[str] = []
    steps = FRAME_FRACTIONS[:frame_count]
    for i, frac in enumerate(steps):
        out = work / f"frame_{i:02d}.png"
        t = max(0.0, min(duration - 0.05, duration * frac))
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{t:.3f}",
            "-i", path,
            "-vframes", "1",
            "-vf", f"scale={HASH_SIZE + 1}:{HASH_SIZE}",
            str(out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise MediaError(f"ffmpeg frame extraction failed for {path}: {e}") from e
        if proc.returncode != 0 or not out.exists():
            raise MediaError(f"ffmpeg frame extraction failed for {path}: {proc.stderr[-500:]}")
        outputs.append(str(out))
    return outputs


def video_fingerprints(path: str, ffmpeg: str, ffprobe: str, frame_count: int = 3) -> list[str]:
    """Hash sampled frames and always remove this operation's exact work dir.

    Cleanup errors are warnings: they must not replace a more useful ffmpeg or
    image-decoding exception, nor discard fingerprints that were computed
    successfully. Only the directory allocated by this invocation is removed.
    """
    work = Path(tempfile.mkdtemp(prefix=".dhash_"))
    try:
        frames = _frames(path, ffmpeg, ffprobe, work, frame_count=frame_count)
        return [dhash(frame) for frame in frames]
    finally:
        try:
            shutil.rmtree(work)
        except OSError as exc:
            log.warning("could not remove perceptual temp directory %s: %s", work, exc)


def image_fingerprints(path: str) -> list[str]:
    """Perceptual fingerprint of an image: exactly one dHash."""
    return [dhash(path)]


def medium_fingerprints(path: str, kind: str, ffmpeg: str | None, ffprobe: str | None) -> list[str]:
    """Perceptual fingerprint for a prepared media file by kind.

    Returns a list of dHash strings (one per image, up to 3 for video). If the
    media cannot be fingerprinted (bad file, missing ffmpeg/ffprobe for video)
    an empty list is returned — the caller should treat that as "no fingerprint
    signal", never as a reason to block or as a record.
    """
    if kind == "image":
        try:
            return image_fingerprints(path)
        except Exception as e:
            log.debug("image fingerprint skipped for %s: %s", path, e)
            return []
    if kind == "video" and ffmpeg and ffprobe:
        try:
            return video_fingerprints(path, ffmpeg, ffprobe)
        except Exception as e:
            log.debug("video fingerprint skipped for %s: %s", path, e)
            return []
    return []


def is_near_duplicate(candidate: list[str], known: list[str], distance: int = PERCEPTUAL_DISTANCE) -> bool:
    """Match a candidate against one historical media group.

    Matching uses maximum one-to-one bipartite matching. This tolerates small
    sample timing shifts while preventing one generic historical frame from
    satisfying multiple candidate samples. Callers compare same-kind groups
    separately, so evidence can never accumulate across historical media.
    """
    if not candidate or not known:
        return False

    matched_history: dict[int, int] = {}

    def assign(candidate_index: int, visited: set[int]) -> bool:
        for history_index, historical in enumerate(known):
            if history_index in visited:
                continue
            if hamming(candidate[candidate_index], historical) > distance:
                continue
            visited.add(history_index)
            previous = matched_history.get(history_index)
            if previous is None or assign(previous, visited):
                matched_history[history_index] = candidate_index
                return True
        return False

    hits = sum(assign(index, set()) for index in range(len(candidate)))
    return hits * 2 >= len(candidate)
