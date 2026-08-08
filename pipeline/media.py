import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

import requests
import yt_dlp
from PIL import Image, ImageOps

log = logging.getLogger("media")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 90
DOWNLOAD_CHUNK = 65536


class MediaError(Exception):
    pass


def _remove_if_exists(path):
    """Best-effort delete of a partial/oversized output file."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def hash_file(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest_path: str, referer: str = None, timeout: int = DEFAULT_TIMEOUT,
             max_bytes: int | None = None) -> str:
    """Stream-download a file with an authoritative byte ceiling.

    ``max_bytes`` is an absolute safety budget: the running streamed byte count
    is authoritative (Content-Length may be absent, wrong, or chunked), and the
    download is aborted the moment it is exceeded. The partial file is removed
    on any failure, never left behind.

    Content-Length is used only as an early-rejection optimisation (rejected
    immediately when present and over the ceiling); it is never trusted as the
    sole enforcement.
    """
    dest = Path(dest_path)
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
            r.raise_for_status()
            if max_bytes is not None:
                cl = r.headers.get("Content-Length")
                if cl is not None:
                    try:
                        if int(cl) > max_bytes:
                            log.warning("rejecting download: %s Content-Length %s exceeds %d", url, cl, max_bytes)
                            return _abort_download(dest, f"Content-Length {cl} exceeds {max_bytes} byte limit for {url}")
                    except ValueError:
                        pass  # malformed Content-Length; streaming counter is authoritative
            written = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        log.warning("aborting download after exceeding %d bytes: %s", max_bytes, url)
                        raise MediaError(f"download exceeded {max_bytes} byte limit for {url}")
                    f.write(chunk)
    except requests.RequestException as e:
        _remove_if_exists(dest)
        raise MediaError(f"download failed for {url}: {e}") from e
    except MediaError:
        _remove_if_exists(dest)
        raise
    if not dest.exists() or dest.stat().st_size == 0:
        _remove_if_exists(dest)
        raise MediaError(f"empty download for {url}")
    return dest_path


def _abort_download(dest: Path, message: str) -> str:
    _remove_if_exists(dest)
    raise MediaError(message)


def prepare_image(src_path: str, dest_dir: str, max_bytes: int) -> str:
    """Re-encode image as JPEG (strips metadata/EXIF). Animated GIFs pass through."""
    src = Path(src_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(src) as im:
            if getattr(im, "is_animated", False) and src.suffix.lower() == ".gif":
                if src.stat().st_size > max_bytes:
                    raise MediaError(
                        f"rejecting GIF: {src.stat().st_size} bytes exceeds {max_bytes} byte limit"
                    )
                out = dest_dir / f"{im.info.get('name', hash_file(src_path)[:8])}.gif"
                out.write_bytes(src.read_bytes())
                return str(out)
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                im = background
            elif im.mode != "RGB":
                im = im.convert("RGB")
    except MediaError:
        raise
    except Exception as e:
        raise MediaError(f"cannot read image {src_path}: {e}") from e

    content_hash = hash_file(src_path)
    out = dest_dir / f"{content_hash}.jpg"
    if out.exists() and out.stat().st_size <= max_bytes:
        return str(out)

    quality = 90
    while True:
        im.save(out, "JPEG", quality=quality, optimize=True)
        size = out.stat().st_size
        if size <= max_bytes:
            return str(out)
        if quality <= 55:
            break
        quality -= 10
    # Quality floor reached and still too large: the image cannot be reduced
    # to the configured limit. Never return an oversized publishable path.
    log.warning("rejecting image %s: %d bytes exceeds %d byte limit at quality floor",
                src_path, size, max_bytes)
    try:
        out.unlink(missing_ok=True)
    except OSError:
        pass
    raise MediaError(f"cannot reduce image {src_path} below {max_bytes} byte limit")


def validate_final_media_size(path: str, kind: str, max_image_bytes: int, max_video_bytes: int) -> str:
    """Final authoritative byte-limit invariant before any media is publishable.

    Called on the actual prepared path right before it leaves the preparation
    pipeline. ``kind`` is ``"image"`` or ``"video"``. Raises
    :class:`MediaError` (never returns an oversized path) when the real
    filesystem size exceeds the applicable configured ceiling.
    """
    p = Path(path)
    if not p.exists():
        raise MediaError(f"prepared media missing for {kind}: {path}")
    size = p.stat().st_size
    limit = max_image_bytes if kind == "image" else max_video_bytes
    if size > limit:
        log.warning("rejecting prepared %s %s: %d bytes exceeds %d byte limit",
                    kind, path, size, limit)
        raise MediaError(f"prepared {kind} {path} exceeds {limit} byte limit")
    return str(path)


def _ffprobe_json(ffprobe: str, path: str) -> dict:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaError(f"ffprobe failed: {e}") from e
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise MediaError(f"cannot parse ffprobe output for {path}") from e


def video_duration(ffprobe: str, path: str) -> float:
    info = _ffprobe_json(ffprobe, path)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("duration"):
            return float(stream["duration"])
    fmt = info.get("format", {})
    if fmt.get("duration"):
        return float(fmt["duration"])
    raise MediaError(f"cannot determine duration for {path}")


def ytdl_download(url: str, dest_dir: str, max_bytes: int, ffmpeg_dir: str = None) -> str:
    """Download a video via yt-dlp. Returns path to downloaded file."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": max_bytes,
        "retries": 3,
    }
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        raise MediaError(f"yt-dlp failed for {url}: {e}") from e
    if not Path(path).exists():
        candidates = sorted(dest_dir.glob(f"{info.get('id', '')}.*"))
        if not candidates:
            raise MediaError(f"yt-dlp produced no file for {url}")
        path = str(candidates[0])
    return path


def trim_video(src_path: str, dest_dir: str, ffmpeg: str, ffprobe: str, max_seconds: float,
               min_seconds: float = 8.0, max_bytes: int | None = None) -> str:
    """Trim/compress video to <= max_seconds (middle chunk), H.264 + AAC.

    ``max_bytes`` enforces the authoritative ``max_video_bytes`` ceiling: after
    transcoding the actual filesystem size is checked and, if still oversized,
    the output is removed and a :class:`MediaError` is raised. The original
    source is never deleted.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    duration = video_duration(ffprobe, src_path)
    if duration < min_seconds:
        raise MediaError(f"video too short ({duration:.1f}s) for {src_path}")

    if duration <= max_seconds:
        clip_len = duration
        start = 0.0
    else:
        clip_len = max_seconds
        start = max(0.0, (duration - clip_len) / 2.0)

    out = dest_dir / f"{Path(src_path).stem}_clip.mp4"
    cmd = [
        ffmpeg, "-y",
        "-ss", f"{start:.2f}",
        "-i", src_path,
        "-t", f"{clip_len:.2f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a", "aac",
        "-b:a", "96k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        _remove_if_exists(out)
        raise MediaError(f"ffmpeg failed: {e}") from e
    if proc.returncode != 0 or not out.exists():
        _remove_if_exists(out)
        raise MediaError(f"ffmpeg trim failed for {src_path}: {proc.stderr[-500:]}")
    if max_bytes is not None and out.stat().st_size > max_bytes:
        log.warning("FFmpeg output %s still exceeds configured limit: %d > %d",
                    out, out.stat().st_size, max_bytes)
        _remove_if_exists(out)
        raise MediaError(
            f"ffmpeg output for {src_path} exceeds {max_bytes} byte video limit"
        )
    return str(out)
