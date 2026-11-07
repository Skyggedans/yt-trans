"""Cache of ready subtitle tracks.

It lives on the daemon side, not the extension's: it survives an extension reinstall,
doesn't hit the browser quota, and lies where the computations were performed. A repeat
viewing of a video must not touch the GPU at all.

The key is content-based, not time-based. A shelf life isn't needed here: if the model,
prompt or schema changed — the key changes, and the old record simply stops being found.
It doesn't leave the disk by itself: eviction by last-read age happens only inside put
and only when the records have outgrown the budget, so orphans lie there as long as it
lasts. Deleting them is the owner's decision.
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

# Version of everything that makes a track from audio or from the author's track,
# except the models themselves (they're in the key separately): the rules of choosing
# the recognition language together with the constants of helper/language.py, the seeds
# and filters of recognition, the cutting into utterances and the translator prompt.
# Bump on any edit of them: the version is part of the key, and previous records stop
# being found. They don't leave the disk for that, though: eviction triggers only when
# the cache outgrows the budget.
# 2 — "Source language": the detector always, seeding by language, silence filters,
# repeats and echo, abbreviations in the cutting.
PROMPT_VERSION = 2

# On-disk record format: bump when what put writes changes.
# 2 — the record carries language and pin instead of source_lang and target_lang.
SCHEMA_VERSION = 2

DEFAULT_BUDGET_MIB = 2048


@dataclass(frozen=True)
class CacheKey:
    video_id: str
    # The language pinned by a human in [asr] language, normalized; "" — not pinned.
    # Neither "auto" nor YouTube metadata appears here: the effective language and the
    # detector's decision live in the record itself, in the language field.
    pin: str
    asr_model: str
    translator_model: str
    # The study language of the second line. Mandatory in the key: without it, switching
    # the mode would return an old track where the second line simply isn't there.
    study_lang: str = ""
    # Fingerprint of the source track: for the ASR path it's the model and its parameters,
    # for a manual track — its language and origin. When it changes, the key changes too.
    track_fingerprint: str = "asr"

    def digest(self) -> str:
        raw = "|".join(
            [
                self.video_id,
                self.track_fingerprint,
                self.pin,
                self.asr_model,
                self.translator_model,
                self.study_lang,
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

    def variants(self, video_id: str) -> list[dict]:
        """All saved tracks of this video, with their fingerprints.

        Needed for recovery after a page reload. The cache key can't be computed in
        advance: it includes the source fingerprint, and that depends on whether the
        video has an author's track — which can only be learned by a request to
        YouTube. So we go from the other side: take what's lying there and look at the
        fingerprint recorded in the file itself. Then the caller recomputes the key
        from it and checks it against the current setting.
        """
        found = []

        for (path,) in self._db.execute(
            "SELECT path FROM tracks WHERE video_id = ?", (video_id,)
        ):
            try:
                found.append(json.loads(Path(path).read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                # The file may have disappeared past the index. That's no reason to
                # crash: recovery is a convenience, not an obligation.
                continue

        return found

    def get(self, key: CacheKey, length_seconds: float | None = None) -> dict | None:
        """A ready track, if it exists and still relates to this video."""
        path = self._path(key)

        if not path.exists():
            return None

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        # The duration isn't part of the key, so we check it separately: a re-uploaded
        # video keeps the identifier but its content is different.
        stored = payload.get("length_seconds")

        if length_seconds and stored and abs(stored - length_seconds) > 1.0:
            return None

        self._db.execute(
            "UPDATE tracks SET used_at = ? WHERE digest = ?", (time.time(), key.digest())
        )
        self._db.commit()

        return payload

    def put(
        self,
        key: CacheKey,
        cues: list[dict],
        length_seconds: float | None,
        *,
        language: dict | None = None,
    ) -> None:
        """Save a ready track.

        Only whole: a partially translated job is never cached, otherwise an interruption
        in the middle of work would permanently pin a leaky track under a valid key.

        language — the language decision, LanguageVerdict.wire(), or None. Readers take it
        via .get("language"), and a record without the field is returned with language null.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "video_id": key.video_id,
            # The key field is written to the file for the same reason as study_lang
            # below: without it the record, when parsed by eye, doesn't explain why the
            # key doesn't match.
            "pin": key.pin,
            "asr_model": key.asr_model,
            "translator_model": key.translator_model,
            # It's part of the key hash but wasn't written to the file — and when parsed
            # by eye it looked like None for all tracks at once, making it hard to
            # understand why the key doesn't match. It doesn't affect the comparison: it
            # goes by the hash, not by these fields.
            "study_lang": key.study_lang,
            "track_fingerprint": key.track_fingerprint,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "length_seconds": length_seconds,
            "language": language,
            "created_at": time.time(),
            "cues": cues,
        }

        # Write through a temporary file: a crash in the middle of writing must not leave
        # truncated JSON under a valid name.
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
        """Keep the cache within budget, discarding the least recently read."""
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
