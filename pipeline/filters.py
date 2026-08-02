import random
import re

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_NEWLINE_RE = re.compile(r"\s+")


def title_contains_blocked_keywords(title: str, keywords: list) -> bool:
    if not keywords:
        return False
    low = title.lower()
    return any(kw.lower() in low for kw in keywords if kw)


def clean_caption(title: str, max_len: int) -> str:
    if not title:
        return ""
    text = _URL_RE.sub("", title)
    text = _NEWLINE_RE.sub(" ", text).strip()
    if not text:
        return ""
    return text[:max_len]


def pick_caption(title, style: str, pool: list, random_chance: float, max_len: int) -> str:
    """Build a caption from title and/or caption pool according to config."""
    title = clean_caption(title, max_len)
    if style == "pool":
        if pool:
            return random.choice(pool)
        return title
    if style == "both":
        parts = [p for p in (title, random.choice(pool) if pool and random.random() < random_chance else None) if p]
        cap = " ".join(parts)
        return cap[:max_len] if cap else ""
    # default: "title"
    if title:
        return title
    if pool:
        return random.choice(pool)
    return ""


def image_passes_dims(img_path, min_width, min_height) -> bool:
    from PIL import Image

    try:
        with Image.open(img_path) as im:
            w, h = im.size
            return w >= min_width and h >= min_height
    except Exception:
        return False
