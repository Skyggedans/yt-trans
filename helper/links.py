"""Сохранённые ссылки страницы планшета: только YouTube, без дублей, с потолком.

Файл лежит рядом с токеном и переживает перезапуск демона. Это единственное
состояние страницы на сервере: сами настройки просмотра живут в браузере.
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
# Потолок маленький намеренно: это домашний список того, что смотрят, а не
# библиотека. Двести ссылок — это уже больше, чем человек листает с планшета.
MAX_LINKS = 200


class TooManyLinks(RuntimeError):
    pass


@dataclass(frozen=True)
class Link:
    id: str
    url: str
    title: str
    kind: str  # video | playlist
    # Метраж, который демон увидел при разборе ссылки. У плейлиста одного метража
    # нет, у старых записей его тоже нет — тогда None.
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
    """Адрес YouTube и только он: демон качает по команде со страницы.

    Хост сравнивается целиком (или как поддомен youtube.com), а не «содержит»:
    youtube.com.evil.example — чужой сайт, и yt-dlp не должен о нём узнать.
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
    """Секунды из того, что пришло от yt-dlp или с диска; мусор — это None.

    Метраж нужен строке списка, поэтому мусорное значение лучше потерять, чем
    показать «NaN:NaN» или отрицательное время.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None

    return int(raw) if raw > 0 else None


def load(path: Path | None = None) -> list[Link]:
    """Ссылки с диска. Битый или отсутствующий файл — пустой список, а не падение."""
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
    """Добавить ссылку. Та же ссылка второй раз возвращает уже сохранённую запись."""
    items = load(path)
    known = link_id(url)

    for link in items:
        if link.id == known:
            return link

    if len(items) >= MAX_LINKS:
        raise TooManyLinks(f"не больше {MAX_LINKS} ссылок")

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
