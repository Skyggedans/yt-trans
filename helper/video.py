"""Скачанные ролики на диске: одна загрузка на ролик, потолок и вытеснение по давности.

Готовность определяется файлом, а не памятью процесса: после перезапуска демона
скачанное остаётся готовым, и заново его никто не тянет.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from helper.env import CACHE_DIR
from helper.sources.ytdlp import YtDlpError, fetch_video

VIDEO_DIR = CACHE_DIR / "video"
# Двадцать гибибайт — это примерно пятьдесят часовых роликов в 1080p. Потолок нужен
# не ради места на диске, а чтобы кэш не рос вечно: смотреть планшетом «ещё одну
# серию» можно годами, и без вытеснения диск кончился бы молча.
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
        self.directory.mkdir(parents=True, exist_ok=True)
        self._states: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Одна загрузка за раз на весь демон: две параллельные качалки делят
        # полосу и обе не успевают к началу просмотра.
        self._one_at_a_time = asyncio.Semaphore(1)

    def path(self, video_id: str) -> Path:
        return self.directory / f"{video_id}.mp4"

    def status(self, video_id: str) -> VideoStatus:
        target = self.path(video_id)

        if target.exists():
            return VideoStatus("ready", "", target)

        state = self._states.get(video_id, "absent")

        # Файл мог исчезнуть из-под нас: готовность — это файл, а не запись в памяти.
        if state == "ready":
            state = "absent"

        return VideoStatus(state, self._errors.get(video_id, ""))

    def ensure(self, video_id: str, *, cookies: Path | None = None) -> VideoStatus:
        """Начать подготовку, если её ещё нет. Возвращается сразу, без ожидания."""
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
                    # Анонимно, как и всё прочее: аккаунт yt-dlp YouTube может забанить,
                    # поэтому cookies идут только туда, где без них никак.
                    await asyncio.to_thread(
                        self._fetch, video_id, target, VIDEO_TIMEOUT, cookies=None
                    )
                except YtDlpError as exc:
                    # Ролик 18+ без входа не отдаётся вовсе. Если аккаунт есть —
                    # одна вторая попытка с ним; без аккаунта отказ окончательный.
                    if not (exc.age_restricted and cookies):
                        raise

                    await asyncio.to_thread(
                        self._fetch, video_id, target, VIDEO_TIMEOUT, cookies=cookies
                    )

            self._states[video_id] = "ready"
            self.evict()
        except Exception as exc:  # noqa: BLE001 — любая причина отказа уходит зрителю
            self._states[video_id] = "error"
            self._errors[video_id] = str(exc) or exc.__class__.__name__
        finally:
            self._tasks.pop(video_id, None)

    def evict(self) -> int:
        """Удалить самые давние ролики, пока кэш не влезет в потолок. Возвращает байты.

        Самый свежий файл не трогается никогда: его только что скачали, чтобы
        смотреть, и вытеснение ради арифметики стёрло бы именно его.
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
