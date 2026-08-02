import hashlib
import json
import os
import subprocess
from pathlib import Path

import requests
import yt_dlp
from PIL import Image, ImageOps

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 90
DOWNLOAD_CHUNK = 65536


class MediaError(Exception):
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


def download(url: str, dest_path: str, referer: str = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Stream-download a file. Raises MediaError on any failure."""
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(DOWNLOAD_CHUNK):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise MediaError(f"download failed for {url}: {e}") from e
    if not Path(dest_path).exists() or Path(dest_path).stat().st_size == 0:
        raise MediaError(f"empty download for {url}")
    return dest_path


def prepare_image(src_path: str, dest_dir: str, max_bytes: int) -> str:
    """Re-encode image as JPEG (strips metadata/EXIF). Animated GIFs pass through."""
    src = Path(src_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(src) as im:
            if getattr(im, "is_animated", False) and src.suffix.lower() == ".gif":
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
    except Exception as e:
        raise MediaError(f"cannot read image {src_path}: {e}") from e

    content_hash = hash_file(src_path)
    out = dest_dir / f"{content_hash}.jpg"
    if out.exists():
        return str(out)

    quality = 90
    while True:
        im.save(out, "JPEG", quality=quality, optimize=True)
        size = out.stat().st_size
        if size <= max_bytes or quality <= 55:
            break
        quality -= 10
    return str(out)


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


def trim_video(src_path: str, dest_dir: str, ffmpeg: str, ffprobe: str, max_seconds: float, min_seconds: float = 8.0) -> str:
    """Trim/compress video to <= max_seconds (middle chunk), H.264 + AAC. Returns output path."""
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
        raise MediaError(f"ffmpeg failed: {e}") from e
    if proc.returncode != 0 or not out.exists():
        raise MediaError(f"ffmpeg trim failed for {src_path}: {proc.stderr[-500:]}")
    return str(out)
