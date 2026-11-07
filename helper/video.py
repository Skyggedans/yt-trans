"""Downloaded videos on disk: one download per video, a cap and eviction by age.

Readiness is determined by a file, not by process memory: after a daemon restart
what was downloaded stays ready, and nobody pulls it again.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from helper.env import CACHE_DIR
from helper.sources.ytdlp import YtDlpError, fetch_video

VIDEO_DIR = CACHE_DIR / "video"
# Twenty gibibytes is about fifty hour-long videos in 1080p. The cap is needed not
# for disk space, but so the cache doesn't grow forever: one can watch "one more
# episode" on a tablet for years, and without eviction the disk would silently run out.
VIDEO_CACHE_MAX_BYTES = 20 * 1024**3
VIDEO_TIMEOUT = 1800.0


@dataclass(frozen=True)
class VideoStatus:
    state: str  # absent | preparing | ready | error
    message: str = ""
    path: Path | None = None

    def wire(self) -> dict:
        return {"state": self.state, "message": self.message}


class VideoStore:
    def __init__(
        self,
        directory: Path = VIDEO_DIR,
        max_bytes: int = VIDEO_CACHE_MAX_BYTES,
        fetch=fetch_video,
    ) -> None:
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._fetch = fetch
        # 0o700, not by umask: the directory may sit in /tmp where foreign processes
        # are nearby, and the file names are the video id, i.e. predictable.
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._states: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # One download at a time for the whole daemon: two parallel downloaders split
        # the bandwidth and both fail to make it by the start of viewing.
        self._one_at_a_time = asyncio.Semaphore(1)

    def path(self, video_id: str) -> Path:
        return self.directory / f"{video_id}.mp4"

    def status(self, video_id: str) -> VideoStatus:
        target = self.path(video_id)

        if target.exists():
            return VideoStatus("ready", "", target)

        state = self._states.get(video_id, "absent")

        # The file may have disappeared from under us: readiness is a file, not a
        # memory record.
        if state == "ready":
            state = "absent"

        return VideoStatus(state, self._errors.get(video_id, ""))

    def ensure(self, video_id: str, *, cookies: Path | None = None) -> VideoStatus:
        """Start preparing if it isn't already. Returns immediately, without waiting."""
        status = self.status(video_id)

        if status.state in ("ready", "preparing"):
            return status

        self._states[video_id] = "preparing"
        self._errors.pop(video_id, None)
        self._tasks[video_id] = asyncio.create_task(self._prepare(video_id, cookies))

        return VideoStatus("preparing")

    async def settled(self, video_id: str) -> VideoStatus:
        task = self._tasks.get(video_id)

        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        return self.status(video_id)

    async def _prepare(self, video_id: str, cookies: Path | None) -> None:
        target = self.path(video_id)

        try:
            async with self._one_at_a_time:
                try:
                    # Anonymously, like everything else: YouTube may ban a yt-dlp
                    # account, so cookies go only where it's impossible without them.
                    await asyncio.to_thread(
                        self._fetch, video_id, target, VIDEO_TIMEOUT, cookies=None
                    )
                except YtDlpError as exc:
                    # An 18+ video isn't served at all without signing in. If there's
                    # an account — one second attempt with it; without an account the
                    # refusal is final.
                    if not (exc.age_restricted and cookies):
                        raise

                    await asyncio.to_thread(
                        self._fetch, video_id, target, VIDEO_TIMEOUT, cookies=cookies
                    )

            self._states[video_id] = "ready"
            self.evict()
        except Exception as exc:  # noqa: BLE001 — any reason for refusal goes to the viewer
            self._states[video_id] = "error"
            self._errors[video_id] = str(exc) or exc.__class__.__name__
        finally:
            self._tasks.pop(video_id, None)

    def evict(self) -> int:
        """Delete the oldest videos until the cache fits the cap. Returns bytes.

        The freshest file is never touched: it was just downloaded to watch, and
        eviction for the sake of arithmetic would wipe exactly it.
        """
        files = sorted(
            (path for path in self.directory.glob("*.mp4") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        total = sum(path.stat().st_size for path in files)
        freed = 0

        for path in files[:-1]:
            if total <= self.max_bytes:
                break

            size = path.stat().st_size
            path.unlink()
            total -= size
            freed += size

        return freed
