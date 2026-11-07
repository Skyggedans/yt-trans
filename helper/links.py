"""Saved links of the tablet page: YouTube only, no duplicates, with a cap.

The file sits next to the settings and survives a daemon restart. This is the only
page state on the server: the viewing settings themselves live in the browser.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from helper.env import CONFIG_DIR

LINKS_PATH = CONFIG_DIR / "links.json"
# The cap is deliberately small: this is a home list of what's being watched, not a
# library. Two hundred links is already more than a person scrolls through from a tablet.
MAX_LINKS = 200


class TooManyLinks(RuntimeError):
    pass


@dataclass(frozen=True)
class Link:
    id: str
    url: str
    title: str
    kind: str  # video | playlist
    # The duration the daemon saw when parsing the link. A playlist has no single
    # duration, old records don't either — then None.
    duration: int | None = None

    def wire(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "kind": self.kind,
            "duration": self.duration,
        }


def is_youtube_url(url: str) -> bool:
    """A YouTube address and only it: the daemon downloads on command from the page.

    The host is compared whole (or as a subdomain of youtube.com), not "contains":
    youtube.com.evil.example is a foreign site, and yt-dlp must not learn about it.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False

    if parts.scheme not in ("http", "https"):
        return False

    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def link_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def clean_duration(raw: object) -> int | None:
    """Seconds from what came from yt-dlp or from disk; garbage is None.

    The duration is needed by the list row, so a garbage value is better lost than
    showing "NaN:NaN" or a negative time.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None

    return int(raw) if raw > 0 else None


def load(path: Path | None = None) -> list[Link]:
    """Links from disk. A broken or missing file is an empty list, not a crash."""
    path = LINKS_PATH if path is None else path

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("links") or []

        return [
            Link(
                id=str(item["id"]),
                url=str(item["url"]),
                title=str(item.get("title") or ""),
                kind=str(item.get("kind") or "video"),
                duration=clean_duration(item.get("duration")),
            )
            for item in items
            if isinstance(item, dict) and item.get("id") and item.get("url")
        ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []


def save(items: list[Link], path: Path | None = None) -> None:
    path = LINKS_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(
        json.dumps({"links": [link.wire() for link in items]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(staging, path)


def add(
    url: str,
    title: str,
    kind: str,
    duration: int | None = None,
    path: Path | None = None,
) -> Link:
    """Add a link. The same link a second time returns the already saved record."""
    items = load(path)
    known = link_id(url)

    for link in items:
        if link.id == known:
            return link

    if len(items) >= MAX_LINKS:
        raise TooManyLinks(f"no more than {MAX_LINKS} links")

    link = Link(id=known, url=url, title=title, kind=kind, duration=clean_duration(duration))
    save([link, *items], path)

    return link


def remove(link_id_to_remove: str, path: Path | None = None) -> bool:
    items = load(path)
    kept = [link for link in items if link.id != link_id_to_remove]

    if len(kept) == len(items):
        return False

    save(kept, path)

    return True
