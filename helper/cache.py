"""Кэш готовых дорожек субтитров.

Живёт на стороне демона, а не расширения: он переживает переустановку расширения,
не упирается в квоту браузера и лежит там, где производились вычисления. Повторный
просмотр ролика не должен трогать GPU вообще.

Ключ содержательный, а не временной. Срок годности здесь не нужен: если поменялась
модель, промпт или схема — поменяется ключ, и старая запись просто перестанет
находиться, а потом уедет по LRU.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from helper.env import CACHE_DIR, TRACKS_DIR

# Поднимать при любом изменении промпта или формата записи — это часть ключа,
# поэтому смена версии автоматически обесценивает старые записи.
PROMPT_VERSION = 1
SCHEMA_VERSION = 1

DEFAULT_BUDGET_MIB = 2048


@dataclass(frozen=True)
class CacheKey:
    video_id: str
    source_lang: str
    target_lang: str
    asr_model: str
    translator_model: str
    # Отпечаток исходной дорожки: для пути ASR это модель и её параметры, для ручной
    # дорожки — её язык и происхождение. Меняется он — меняется и ключ.
    track_fingerprint: str = "asr"

    def digest(self) -> str:
        raw = "|".join(
            [
                self.video_id,
                self.track_fingerprint,
                self.source_lang,
                self.target_lang,
                self.asr_model,
                self.translator_model,
                str(PROMPT_VERSION),
                str(SCHEMA_VERSION),
            ]
        )

        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TrackCache:
    def __init__(self, root: Path = TRACKS_DIR, budget_mib: int = DEFAULT_BUDGET_MIB) -> None:
        self._root = root
        self._budget = budget_mib * 1024 * 1024
        self._root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(CACHE_DIR / "index.sqlite", check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS tracks (
                   digest TEXT PRIMARY KEY,
                   video_id TEXT NOT NULL,
                   path TEXT NOT NULL,
                   bytes INTEGER NOT NULL,
                   length_seconds REAL,
                   used_at REAL NOT NULL
               )"""
        )
        self._db.commit()

    def _path(self, key: CacheKey) -> Path:
        digest = key.digest()

        return self._root / key.video_id[:2] / key.video_id / f"{digest}.json"

    def get(self, key: CacheKey, length_seconds: float | None = None) -> dict | None:
        """Готовая дорожка, если она есть и всё ещё относится к этому ролику."""
        path = self._path(key)

        if not path.exists():
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        # Длительность в ключ не входит, поэтому сверяем отдельно: перезалитый ролик
        # сохраняет идентификатор, но содержимое у него другое.
        stored = payload.get("length_seconds")

        if length_seconds and stored and abs(stored - length_seconds) > 1.0:
            return None

        self._db.execute(
            "UPDATE tracks SET used_at = ? WHERE digest = ?", (time.time(), key.digest())
        )
        self._db.commit()

        return payload

    def put(self, key: CacheKey, cues: list[dict], length_seconds: float | None) -> None:
        """Сохранить готовую дорожку.

        Только целиком: частично переведённое задание не кэшируется никогда, иначе
        обрыв посреди работы навсегда закрепил бы дырявую дорожку под валидным ключом.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "video_id": key.video_id,
            "source_lang": key.source_lang,
            "target_lang": key.target_lang,
            "asr_model": key.asr_model,
            "translator_model": key.translator_model,
            "track_fingerprint": key.track_fingerprint,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "length_seconds": length_seconds,
            "created_at": time.time(),
            "cues": cues,
        }

        # Пишем через временный файл: падение посреди записи не должно оставить
        # обрезанный JSON под валидным именем.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

        self._db.execute(
            """INSERT INTO tracks (digest, video_id, path, bytes, length_seconds, used_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(digest) DO UPDATE SET
                   bytes = excluded.bytes, used_at = excluded.used_at""",
            (
                key.digest(),
                key.video_id,
                str(path),
                path.stat().st_size,
                length_seconds,
                time.time(),
            ),
        )
        self._db.commit()
        self._evict()

    def _evict(self) -> None:
        """Держать кэш в рамках бюджета, выбрасывая самое давно не читанное."""
        total = self._db.execute("SELECT COALESCE(SUM(bytes), 0) FROM tracks").fetchone()[0]

        if total <= self._budget:
            return

        rows = self._db.execute(
            "SELECT digest, path, bytes FROM tracks ORDER BY used_at ASC"
        ).fetchall()

        for digest, path, size in rows:
            if total <= self._budget:
                break

            Path(path).unlink(missing_ok=True)
            self._db.execute("DELETE FROM tracks WHERE digest = ?", (digest,))
            total -= size

        self._db.commit()

    def stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM tracks"
        ).fetchone()

        return {"tracks": row[0], "bytes": row[1], "budget_bytes": self._budget}
