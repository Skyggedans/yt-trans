"""Jobs: from a video identifier to Russian lines in the browser.

The order of the stages is not arbitrary. Script normalization goes before segmentation
because Whisper wavers between alphabets within one recording. Segmentation goes before
translation because Whisper returns thirty-second windows of ~300 characters: they can
neither be shown to the viewer nor sent to the model — a batch of such windows does not
fit in the answer, and the grammar does not let the JSON be closed, losing the whole
batch at once.

The cues around the playhead are translated first. On the median twenty-minute video
that is about 290 cues and roughly 75 seconds of translation: there is no reason to wait
for all of them staring at an empty screen when the viewer is watching one particular
place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx
import numpy as np

from helper import language
from helper.asr.prompt import (
    COMPRESSION_RATIO_THRESHOLD,
    compression_ratio,
    harvest_names,
    is_echo,
    segment_verdict,
)
from helper.asr.worker import SAMPLE_RATE
from helper.cache import CacheKey, TrackCache
from helper.config import Config, load as load_config
from helper.env import YTDLP_CACHE
from helper.gpulease import GpuLease, Preempted
from helper.language import LanguageVerdict
from helper.script import normalize_cues
from helper.segment import Cue, build_cues
from helper.sources.ytdlp import (
    YtDlpError,
    VideoInfo,
    audio_pcm,
    fetch_manual_track,
    probe,
    usable_manual_track,
)
from helper.translate import (
    Batch,
    TranslateError,
    translate_batch,
    translate_dual,
    validate,
    validate_dual,
)
from helper.video import VIDEO_DIR, VideoStore

log = logging.getLogger(__name__)

BATCH_SIZE = 40
CONTEXT_CUES = 3

# Failures after which it makes sense to split the batch and try again: a model refusal,
# broken JSON and a dropped connection to ollama are all cured the same way. The preemption
# error is deliberately NOT included — it must pass through and pause the job.
RETRYABLE = (TranslateError, json.JSONDecodeError, httpx.HTTPError)

# Video length limit. Audio is decoded into memory in full: an hour of 16 kHz float32
# recording is 230 MB, and as much again for the copy in tobytes() when handing it to the
# worker. The corpus median is 20 minutes, the longest video found is 78 minutes.
MAX_VIDEO_SECONDS = 3 * 3600

# How many finished jobs to keep in memory. The cues of a twenty-minute video are
# hundreds of objects, and without eviction the registry would grow for the whole life
# of the process.
MAX_RETAINED_JOBS = 12

# How many unfinished jobs the daemon holds at once. The limit is against abuse, not the
# queue depth: the port will be reachable from the home network, and a client that creates
# jobs without count would pile up any number of them, each of which is a task and a
# metadata request, and on pause also hundreds of megabytes of stashed audio. A waiting job
# is cheap by itself: its task sits on the GPU lease, and the audio is downloaded under the
# lease. That is why the limit is not small: the extension queues as many videos as the
# viewer has tabs open, and the fifth tab of an ordinary evening should not be refused. An
# abandoned pause does not count: see JobManager._unfinished.
MAX_ACTIVE_JOBS = 16

# Event log length. The cues are duplicated in job.cues, so it is cheaper to give a
# lagging client a snapshot than to keep the whole history for its sake alone.
MAX_EVENT_LOG = 2000

# Retries of metadata and audio within one job are only for transient failures
# (YtDlpError.transient). The delays grow: the first retry catches the one-off 403 from
# googlevideo, the second — a network that blinked and needs time to come up.
FETCH_ATTEMPTS = 3
FETCH_BACKOFF = (5.0, 20.0)

# How many seconds after the start of the audio download a new attempt may still begin.
# The audio is downloaded under the graphics card lease, and the lease is one for the whole
# daemon: while retries run, other phases on the card wait. An ordinary download fits into
# seconds, and the one-off 403 fits the budget with room to spare, while a prolonged network
# outage had better fail the job — repeatably — than hold everyone.
FETCH_RETRY_BUDGET = 90.0

# How many seconds after a failure the poll may start the job over. Only for retryable
# failures — see JobManager.get_or_create.
RETRY_COOLDOWN = 60.0

# Audio of a job that gave the card back already after the download. On disk and next to
# the cache, not in /tmp: on Fedora /tmp is tmpfs, that is, the same memory, and keeping
# tens and hundreds of megabytes in it for a paused video was what had to be avoided.
PAUSED_AUDIO_DIR = YTDLP_CACHE.parent / "paused-audio"

# How long to keep the stashed audio of a pause nobody touches.
# An open tab polls the daemon every twenty-five seconds and every time extends the term;
# half an hour of silence means the tab was closed or the viewer went to another video. The
# job stays — only the audio goes away, and if they do come back to it, resuming will
# download it anew.
#
# The same term also decides admission: a pause that has stood longer stops taking a place
# under MAX_ACTIVE_JOBS (see Job.is_stale_pause). Tuning the term for the disk, you also
# change when abandoned pauses free up places for new videos.
PAUSED_AUDIO_TTL = 30 * 60.0

# How many bytes of stashed audio to keep on disk in total. Pause audio is float32 at
# 16 kHz, that is about 659 MiB per video in MAX_VIDEO_SECONDS, and PAUSED_AUDIO_TTL only
# removes abandoned pauses: while a tab polls the daemon the term is extended, and the card
# can be occupied for hours. Without a ceiling the directory would be limited only by free
# disk. Four gigabytes is roughly six such paused jobs. Above the ceiling _evict removes
# the audio oldest by write time, and resuming such a job downloads it anew.
PAUSED_AUDIO_MAX_BYTES = 4 * 1024**3

# How many seconds the view position stays valid after the last report about it.
# The extension sends it once a second, and the browser may throttle a background tab to
# one timer firing a minute, so the threshold is twice a minute: a throttled tab stays in
# the circle, while a closed, sleeping or killed one falls out of it. There is nobody to
# remove such a client — it did not send /drop — and without the term its position would
# forever pull the translation away from those still watching. When everyone has gone
# silent, the last known position remains: see Job.aim.
PLAYHEAD_TTL = 120.0

# A client that did not send a key is an extension that knows nothing about keys. All such
# clients merge into one, as it was while the view position was a single number.
LEGACY_CLIENT = "legacy"

# Maximum client key length. The key comes from outside and becomes a dictionary key in
# every job of the video, so an absurd one (empty, not a string, too long) is not rejected
# but reduced to LEGACY_CLIENT: the view position is a priority hint, there is no reason to
# answer it with an error, and such a key has no place in the dictionary.
MAX_CLIENT_ID = 64

# Ceiling on the recognition of one video. With room to spare: the longest allowed video
# (MAX_VIDEO_SECONDS) takes minutes for large-v3 on the card.
ASR_TIMEOUT = 3600.0

# How much video memory to ask for each phase is taken from the configuration together
# with the model name: see helper/config.py. The figure cannot live apart from the model —
# turbo consumes three times less than large-v3, and a constant here would mean that
# admission control lies whenever the model changes.


class Stage(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    FETCHING = "fetching"
    ASR = "asr"
    TRANSLATING = "translating"
    DONE = "done"
    ERROR = "error"
    PAUSED = "paused"


class PermanentJobError(RuntimeError):
    """A failure that will not pass by itself: a live stream, a video that is too long.

    A RuntimeError subclass, so that any handler catching RuntimeError catches
    this one too. A separate type is needed for exactly one thing — a job with such
    a failure is not recreated by the poll, however much time passes.
    """


class CapacityError(RuntimeError):
    """We do not take a new job: there are already MAX_ACTIVE_JOBS unfinished (abandoned pauses do not count).

    Not a PermanentJobError: that one means a failure that will not pass by itself, and the
    runner sets retryable=False on it, while an overflow passes as soon as any job finishes.
    The text is a ready-made phrase for the viewer: how many videos are already in progress
    and that a place will free up.
    """


@dataclass
class JobCue:
    index: int
    start: float
    end: float
    src: str
    lines: list[str] = field(default_factory=list)
    # Second line for language study: the Montenegrin translation of the same cue.
    # Empty if the mode is off.
    study: str = ""
    # The translation failed both as a batch, in halves, and one by one: the screen gets
    # the original text of the cue, in the language of the video. The extension shows such
    # cues differently so that the viewer sees this is not a translation.
    failed: bool = False

    @property
    def translated(self) -> bool:
        return bool(self.lines)

    def payload(self) -> dict:
        return {
            "i": self.index,
            "start": self.start,
            "end": self.end,
            "src": self.src,
            "lines": self.lines,
            "study": self.study,
            "failed": self.failed,
        }


def _client_key(client_id: object) -> str:
    """The client key in the positions dictionary: the sent one if fit, otherwise LEGACY_CLIENT.

    Fit is a non-empty string no longer than MAX_CLIENT_ID. Anything else — None, a number,
    a list, an empty or too long string — is reduced to one client without a key.
    The type is checked first: a number has no length, and a list or dict as a dictionary
    key would raise TypeError on hashing.
    """
    if isinstance(client_id, str) and 0 < len(client_id) <= MAX_CLIENT_ID:
        return client_id

    return LEGACY_CLIENT


class Job:
    """One job for one video and one author track variant."""

    def __init__(
        self, video_id: str, target_lang: str = "ru", use_author_track: bool = True
    ) -> None:
        self.video_id = video_id
        self.target_lang = target_lang
        # Whether to take the ready author track instead of recognition. Turned off
        # from the extension settings: the track is sometimes not a transcript but a free
        # retelling or a translation, and then our own recognition is more accurate. Together
        # with the video this is the job key in the registry: each variant has its own job.
        self.use_author_track = use_author_track
        self.stage = Stage.QUEUED
        # What the job is waiting for in the queue: "gpu" — the graphics card for recognition.
        self.waiting: str | None = None
        self.error: str | None = None
        # Whether the failure can pass by itself: network, 403, rate limiting — yes; a live
        # stream, a removed video — no. This decides whether the extension offers
        # a retry and whether the poll recreates the job. The failure time is monotonic:
        # cooling must not depend on the system clock being changed.
        self.retryable = False
        self.failed_at: float | None = None
        self.cues: list[JobCue] = []
        self.info: VideoInfo | None = None
        # Where each client is watching: client key → (position, moment of the report by
        # the monotonic clock). The clock is monotonic for the same reason as failed_at:
        # the age of the record must not depend on the system clock being changed. Write only
        # through report_playhead and forget_client: they bring the key to one form.
        self._playheads: dict[str, tuple[float, float]] = {}
        # How many times aim() has handed out a position. The remainder modulo the number of
        # distinct positions decides whose turn it is to translate.
        self._turn = 0
        # Where the cues came from: 'asr' or 'manual:<language>'. Part of the cache key —
        # the author track and the recognition result are different tracks.
        self.source = "asr"
        # Which language the daemon decided on and why: what language the cues are in and
        # how that is known. None while there is no decision. Written only through
        # _set_language: it also tells the clients about the decision. Not part of the
        # registry key — the video variant is (video_id, use_author_track), and the language
        # is a property of the material, not of the variant.
        self.language: LanguageVerdict | None = None
        # The author track is in the metadata but did not download. Remembered so that
        # resuming after a pause does not go to YouTube for it again: while the card is
        # occupied, the pause repeats every two minutes.
        self.track_failed = False
        # The video is 18+, and YouTube did not give it out anonymously. Everything further
        # about it — metadata, track, audio, including after a pause — goes with the account
        # from the settings; no other video touches the account.
        self.account = False
        self.created_at = time.time()
        # Which number this job is under its key (video and track variant): 0 for the one
        # created for the first time, and one recreated after a failure gets one more
        # (see get_or_create). A recreated job may get a different language or take the
        # author track where the previous one fell back to recognition, and their cue
        # numbering differs, so the client needs a way to tell its snapshot from the
        # snapshot of the previous job — see snapshot. The registry forgets a job dropped
        # through /drop, so the next job under the same key again starts from 0.
        self.generation = 0

        # Event log with a cursor. Needed for long polls: the client says how far it has
        # seen up to position N, and receives everything that appeared after. Per-subscriber
        # queues are not enough for this — they are one-shot, while the poll reconnects
        # after every answer and must continue from exactly that place.
        self.events: list[dict] = []
        # The absolute position of the first event still in the log. The client's cursor is
        # absolute, while trimming shifts the list indices — without this position, after
        # the log overflows the cursor would point to the wrong place. The end of the log
        # is read through journal_end, not added up by hand.
        #
        # The count starts from a random base, not from zero. From zero all logs would have
        # matching positions: a cursor carried over from a neighbouring variant of the same
        # video or from a dropped job would turn out valid here too, if it is shorter than
        # the log, and the client would get the tail of someone else's log without seeing
        # its beginning. With its own base each log has its own range of positions,
        # someone else's cursor does not fall into it and gets a snapshot through the usual
        # poll branch.
        #
        # The base is 48-bit, not wider: the cursor goes into the extension as a JSON number
        # and comes back as a query parameter, and integers in JavaScript are exact only up
        # to 2^53, so the browser would distort a 62-bit base. Consequence: the first poll of
        # a fresh client with cursor 0 also gets a snapshot right away. This is the same
        # "cursor too old" branch, and it is harmless.
        self._dropped = secrets.randbits(48)
        self._wakeup = asyncio.Event()

        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        # A handle on the recognition process. Without it there is nothing to kill it with:
        # subprocess.run inside asyncio.to_thread is not interrupted by task cancellation,
        # so Whisper would survive both the cancellation and the preemption, continuing to
        # hold ~6 GB already without a watchdog.
        self._asr_proc: subprocess.Popen | None = None
        # A request to kill recognition that survives the race with its start.
        # Preemption may come while the worker is only being created and there is no handle
        # yet: one handle alone would then kill nothing, and Whisper would run to the end on
        # the card given up. The flag is set before reading the handle, and the worker reads
        # it after it has set the handle — so at least one side sees the other.
        # It also distinguishes our kill from someone else's: see JobRunner._run_asr.
        self._asr_kill = False
        # Audio stashed to disk when the card was taken away after the download: see
        # stash_audio. Each job has its own file — a recreated job for the same video will
        # not pick up someone else's audio.
        self._audio_path: Path | None = None
        # The job was dropped: there is no more point in stashing audio. See cancel.
        self._audio_dead = False
        # When the job was last touched — by a poll or a resume.
        # _evict uses it to remove the stashed audio of an abandoned pause.
        self.touched_at = time.monotonic()

    # ── subscribing to events ────────────────────────────────────────────

    def subscribe(self, queue: asyncio.Queue) -> asyncio.Queue:
        """Subscribe the caller's queue to the job events.

        The queue is passed from outside rather than created here so that one connection
        can listen to several jobs through one queue. Otherwise the reader would have to
        wait on several queues at once, and abandoned `get()` tasks manage to take a message
        and lose it.
        """
        self._subscribers.add(queue)

        # A catch-up snapshot: one who connected later gets everything already ready,
        # and then follows the stream on a par with the rest.
        queue.put_nowait(self.snapshot())

        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def is_subscribed(self, queue: asyncio.Queue) -> bool:
        """Whether the queue is among the subscribers.

        Needed so as not to subscribe it twice: every subscription sends a catch-up snapshot.
        """
        return queue in self._subscribers

    def _emit(self, event: dict) -> None:
        # The video is put on every event, not only on the snapshot: the socket
        # is subscribed to several jobs at once (the stage, the running copy), and without
        # the video the client cannot tell someone else's cue from its own, and so cannot
        # finish the subtitles of a saved copy either.
        event.setdefault("videoId", self.video_id)
        self.events.append(event)

        # The log is bounded: the cues lie in job.cues in full anyway, so
        # a lagging client will get them as a snapshot instead of holding us by the tail.
        if len(self.events) > MAX_EVENT_LOG:
            excess = len(self.events) - MAX_EVENT_LOG
            del self.events[:excess]
            self._dropped += excess

        self._wakeup.set()
        self._wakeup.clear()

        for queue in list(self._subscribers):
            queue.put_nowait(event)

    @property
    def journal_end(self) -> int:
        """The position right after the last event: from it the client gets only new things.

        The cursor is absolute, and the count starts from the log base, not from zero —
        see the comment at _dropped. Counting the position by hand, for example from
        len(events), will not do: it would not match the one poll understands.
        """
        return self._dropped + len(self.events)

    async def poll(self, cursor: int, timeout: float) -> dict:
        """Events after position cursor. Waits up to timeout if there are none yet.

        A negative or too old cursor means the client has lagged or
        connected for the first time: we give a snapshot and the current position so that it
        starts from an agreed state rather than from the middle.

        A cursor BEYOND the end of the log means the opposite — it is from another job for the
        same video: the poll moved to another track variant when the switch was changed, or
        the job was recreated after a failure or dropped through /drop. This is a
        desynchronization too, and is cured by the same snapshot: without it the client
        would silently wait until the log grows to its position, would lose all events
        before it and would mix new cues into the old ones shown.

        Someone else's cursor always falls into one of these two branches: the positions of
        each log are counted from its own random base (see Job.__init__), and a cursor of
        another job will not pass for a position inside this one. Zero, from which a fresh
        client starts, is also too old for such a log.
        """
        end = self.journal_end

        if cursor < self._dropped or cursor > end:
            return {"cursor": end, "snapshot": self.snapshot(), "events": []}

        if cursor >= end:
            # Waiting for new ones to appear. A timeout returns an empty answer — this is
            # normal, the client will ask again at once, and the connection does not hang
            # longer than the limit.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout)

            end = self.journal_end

        return {"cursor": end, "events": self.events[cursor - self._dropped :]}

    def readiness(self) -> dict:
        """What is already translated and what is still empty — for the page that waits in place.

        The viewer pauses the video where they wait: the translation goes from the current
        viewing place in batches, so "30 of 40 translated" says nothing about whether the
        subtitles are here. pending — the merged intervals of cues the hand has not reached
        yet. translatedThrough — the start of the first such interval, that is, up to which
        second the translation goes solidly; None means nothing untranslated is left at all.
        """
        pending: list[list[float]] = []

        for cue in self.cues:
            if cue.translated:
                continue

            if pending and cue.start <= pending[-1][1]:
                pending[-1][1] = cue.end
            else:
                pending.append([cue.start, cue.end])

        return {
            "translatedThrough": pending[0][0] if pending else None,
            "pending": pending,
        }

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "videoId": self.video_id,
            # The settings with which this track was made. authorTrack is part of the job
            # key, and by it the client sees which of the video variants is before it.
            # targetLang is not part of the key: the language is one for all, and it was set
            # by the first request to the job, so a client with a different language gets the
            # same job and sees the discrepancy here, see get_or_create.
            "authorTrack": self.use_author_track,
            "targetLang": self.target_lang,
            # From the change of the number the client learns that the job was recreated and
            # the cues with the cursor it holds referred to the previous one: see
            # Job.generation.
            "generation": self.generation,
            # The language decision as a whole, as one nested object. The top-level keys
            # source, message, waiting and retry are read by extension/content.js (building
            # the badge label), and "source: asr" at the top would turn the badge into
            # "downloading asr…". That is why the decision has its own key, and its fields
            # lie inside. The refusal snapshot (refusal_snapshot) is assembled from a real
            # Job and gets language: null by itself.
            "language": self.language.wire() if self.language else None,
            "stage": self.stage.value,
            "error": self.error,
            "retryable": self.retryable,
            # The snapshot is also received by one who connected in the middle of waiting
            # for the card: without the field it would see an ordinary queue rather than
            # waiting for the graphics card.
            "waiting": self.waiting,
            "title": self.info.title if self.info else "",
            "total": len(self.cues),
            "translated": sum(c.translated for c in self.cues),
            "cues": [c.payload() for c in self.cues if c.translated],
            **self.readiness(),
        }

    def _set_stage(self, stage: Stage, **extra) -> None:
        self.stage = stage
        # Any next stage removes the waiting, so it is taken from each of them.
        self.waiting = extra.get("waiting")
        self._emit({"type": "state", "stage": stage.value, **extra})

    def _set_language(self, verdict: LanguageVerdict) -> None:
        """The language decision onto the job and into the log: the clients hear it as a language event."""
        self.language = verdict
        # Not through _set_stage: that one removes waiting and sends state, by which
        # extension/content.js (updateChip) redraws the badge, and extension/background.js
        # (lastStage) decides whether to keep polling. The extension skips an event of a new
        # type: trackStage, mergeCues and updateChip act only on state, snapshot
        # and cues. Verified by running unchanged content.js and background.js
        # in Node: a snapshot with a nested language and a language event leave the badge
        # "translating… 2/290" as it was, while lastStage on a single language event yields
        # undefined — the poll goes on.
        self._emit({"type": "language", "language": verdict.wire()})

    def _fail(self, message: str, retryable: bool) -> None:
        self.error = message
        self.retryable = retryable
        self.failed_at = time.monotonic()
        self._set_stage(Stage.ERROR, message=message, retryable=retryable)

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self, runner: JobRunner) -> None:
        if self._task is None:
            self._task = asyncio.create_task(runner.run(self))

    def resume(self, runner: JobRunner) -> None:
        """Continue after preemption, keeping the cues already ready.

        The job is the same one: job.cues already contains the segmented cues
        and part of the translations, so _pipeline will go straight to the missing ones.
        """
        if self._task and not self._task.done():
            return

        self._task = asyncio.create_task(runner.run(self))

    def kill_asr(self) -> None:
        """Kill the recognition process if it is running.

        Called both on cancellation and on preemption: the GPU lease is released
        right after the task is cancelled, so the memory must be freed before that,
        and not after an unpredictable time of the thread scheduler.
        """
        self._asr_kill = True
        proc = self._asr_proc

        if proc and proc.poll() is None:
            proc.kill()

    # ── stashed audio ────────────────────────────────────────────────────

    def stash_audio(self, audio) -> None:
        """Stash the downloaded audio until the resume.

        Needed when the card is taken away already after the download — in the middle of
        recognition or between it and the download. Otherwise the resume would download the
        video again. Written once: a repeated preemption of the same job already finds the
        file there.
        """
        if self._audio_dead:
            return

        # Read the handle once: the write goes in a worker thread, and the budget cleanup in
        # the event loop may reset it between the None check and the file check.
        path = self._audio_path

        if path is not None and path.exists():
            return

        PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(
            dir=PAUSED_AUDIO_DIR, prefix=f"{self.video_id}-", suffix=".f32"
        )
        self._audio_path = Path(name)

        try:
            with open(handle, "wb") as out:
                audio.tofile(out)
        except BaseException:
            # A half-written file is truncated audio. Read on resume,
            # it would be recognized and go into the cache as a whole video.
            self.discard_audio()
            raise

        # The job was dropped while the file was being written: the drop had not seen the
        # handle yet, and the file would have remained ownerless until the daemon restarts.
        if self._audio_dead:
            self.discard_audio()

    def take_audio(self):
        """The stashed audio, if there is any. The file stays until recognition ends.

        Not read — no matter: None, and the resume will download the audio again,
        as if there had been no file at all.
        """
        self.touched_at = time.monotonic()

        # Once, for the same reason as in stash_audio: the read goes in a worker thread.
        path = self._audio_path

        if path is None or not path.exists():
            return None

        try:
            return np.fromfile(path, dtype="<f4")
        except OSError as exc:
            log.warning("stashed audio %s not read: %s", self.video_id, exc)
            self.discard_audio()

            return None

    def discard_audio(self) -> None:
        path, self._audio_path = self._audio_path, None

        if path is not None:
            path.unlink(missing_ok=True)

    def cancel(self) -> None:
        # A paused job has no task of its own — it already finished with a pause, — and
        # there is nobody to remove its audio except here. The flag is set
        # before the handle is reset, and the writer reads it after it has set the handle:
        # so a file written already after the drop will not remain ownerless either.
        self._audio_dead = True

        # First what holds the card — the recognition process and the task — and only
        # then the audio. The registry has already forgotten the dropped job
        # (JobManager._remove), and a disk error that used to fly out of here left it running
        # without an owner: Whisper on the card, translation and cache writes behind it, and
        # /drop answering 500.
        self.kill_asr()

        if self._task:
            self._task.cancel()

        # Audio that failed to be erased stays on disk without an owner: discard_audio resets
        # the handle before deleting. It will be removed by the budget (_trim_paused_audio)
        # when the audio grows past PAUSED_AUDIO_MAX_BYTES, or by a daemon restart. The drop
        # does not depend on this.
        try:
            self.discard_audio()
        except OSError as exc:
            log.warning("audio %s not removed on drop: %s", self.video_id, exc)

    @property
    def finished(self) -> bool:
        return self.stage in (Stage.DONE, Stage.ERROR)

    def is_stale_pause(self, now: float) -> bool:
        """Paused, no task, and nobody has touched the job for longer than PAUSED_AUDIO_TTL.

        A pause is resumed only by a client request, it will not continue by itself, and one
        that has been silent for half an hour is most likely abandoned: the tab was closed or
        the viewer went to another video. A live task means the resume is already under way.
        The sign is shared by two places: _evict uses it to remove the pause audio, and
        JobManager._unfinished does not count it as an occupied place. The job itself stays in
        the registry with its cues, and a returning viewer will resume it as usual.

        now is passed by the caller: the monotonic clock is read once for the whole registry
        walk. The comparison is strict: exactly PAUSED_AUDIO_TTL is not yet old age.
        """
        return (
            self.stage is Stage.PAUSED
            and (self._task is None or self._task.done())
            and now - self.touched_at > PAUSED_AUDIO_TTL
        )

    # ── playhead ─────────────────────────────────────────────────────────

    def report_playhead(self, client_id: str | None, t: float) -> None:
        """Remember where the client is watching now.

        One report — one record per client: the new position replaces the previous one, while
        the client's place in the circle stays the one it appeared at. Without a fit key the
        client is LEGACY_CLIENT, see _client_key.
        """
        self._playheads[_client_key(client_id)] = (t, time.monotonic())

    def forget_client(self, client_id: str | None) -> None:
        """Take the client off the wheel: its position is no longer taken into the circle.

        A client that gave up the job itself stops influencing the translation at once,
        rather than after PLAYHEAD_TTL. An unknown client is skipped silently: the key comes
        in the request from outside, and it may well turn out already removed or foreign.
        """
        self._playheads.pop(_client_key(client_id), None)

    def live_slots(self) -> dict[str, tuple[float, float]]:
        """The records of clients that reported themselves no later than PLAYHEAD_TTL ago.

        The only place where it is decided whether a client is alive. The circle in aim() and
        the question "is there still anyone watching" in JobManager.drop read this one answer,
        and they have nowhere to diverge. "Older" means strictly older: a record of exactly
        PLAYHEAD_TTL age is still alive.

        Only reads: it does not turn the circle and does not throw records away. The order is
        the order in which the clients appeared, and the order of the circle rests on it.
        """
        now = time.monotonic()

        return {
            client: slot
            for client, slot in self._playheads.items()
            if now - slot[1] <= PLAYHEAD_TTL
        }

    def _position_ahead(self, alive: dict[str, tuple[float, float]]) -> float:
        """The position aim() will hand out next. Does not turn the circle.

        The common part of aim() and playhead, so that "what next" is counted by one code.
        alive is the answer of live_slots(), already obtained by the caller: so aim() decides
        who is alive once per call, rather than twice from two clock readings.
        """
        if alive:
            positions = list(dict.fromkeys(position for position, _ in alive.values()))

            return positions[self._turn % len(positions)]

        if self._playheads:
            return max(self._playheads.values(), key=lambda slot: slot[1])[0]

        return 0.0

    def aim(self) -> float:
        """The position around which the next translation batch is taken.

        There is one translation window, while several clients may be watching at once, each
        at its own place. The positions go in a circle: each call hands out the next one
        among the distinct positions of live clients, in the order the clients appeared. The
        circle is chosen instead of "the most lagging": with viewers at the second and the
        twentieth minute, the minimum would make the second wait for the whole video, while
        the translation anyway overtakes the viewer about twenty times, so half the speed
        remains a tenfold margin.

        Who is alive is decided by live_slots. Those fallen silent are not taken into the
        circle while there is at least one alive. If everyone fell silent, the freshest of the
        reported positions is handed out: before, it stayed on the job forever, while a reset
        to zero would jerk the translation window to the start of the video for the only
        viewer whose tab is simply frozen. Zero — only when there are no records at all:
        nobody reported a position or everyone was removed.

        Each call turns the circle, so it must be called once per batch.
        """
        alive = self.live_slots()
        position = self._position_ahead(alive)

        if alive:
            # We throw expired records away, but only when there are live ones. The fallback
            # position is taken from the records themselves, and while there is nobody to
            # steer, they must not be touched. With live ones it is not needed, and throwing
            # them away does not change it: the freshest record is always among the alive.
            self._playheads = alive
            self._turn += 1

        return position

    @property
    def playhead(self) -> float:
        """The position aim() will hand out next. Does not turn the circle.

        The field is left from the times when this was a number for writing. Writing into it
        is now impossible, and assignment will raise AttributeError: the position is reported
        through report_playhead. Reading is just a look: it does not spend anyone's turn and
        does not throw records away, so it can be read as much as you like without taking the
        turn from the viewers. The batch takes the position through aim().
        """
        return self._position_ahead(self.live_slots())


def refusal_snapshot(
    video_id: str, target_lang: str, use_author_track: bool, message: str, retryable: bool
) -> dict:
    """A refusal snapshot for a video that is not in the registry and will not be.

    The client that was refused reads from the poll answer a snapshot of the usual kind. It
    is assembled from a real Job rather than by hand: the set of fields is determined by
    Job.snapshot(), and a second record of them would diverge from it at the first new field.
    The job is not registered or started anywhere — it is needed for exactly this call.
    """
    refused = Job(video_id, target_lang, use_author_track)
    refused._fail(message, retryable)

    return refused.snapshot()


def _log_language(job: Job, verdict: LanguageVerdict, windows: list) -> None:
    """The language decision into the log: an INFO line and warnings in words.

    The extension will not show the decision, and there is no tablet page yet: until then
    the log and the snapshot (GET /v1/jobs/{id}) are the only places where the decision is
    visible. windows — the detector windows from the worker answer: [code or None, share,
    seconds of speech].

    Everything absent is written with the "—" sign: the declaration as a whole, the heard
    language, the confidence, the code of a window that did not vote, and the window list
    itself (the author track does not listen to the detector).
    """
    if verdict.declared:
        declared = f"{verdict.declared} ({verdict.declared_by or '—'})"
    else:
        declared = "—"

    confidence = "—" if verdict.confidence is None else f"{verdict.confidence:.2f}"
    # The seconds are whole and rounded down, as on the wire: a window shown with three
    # seconds and more went into the model. Rounding to the nearest would show a window of
    # 2.6–2.9 s that did not reach the model as "3s" — and would not explain why there is no
    # warning about it below.
    shown = ", ".join(
        f"{code or '—'} {share:.2f}/{int(seconds)}s" for code, share, seconds in windows
    )

    log.info(
        "language %s: %s (%s), declared %s, by speech %s, confidence %s, windows: %s",
        job.video_id, verdict.effective, verdict.reason, declared,
        verdict.detected or "—", confidence, shown or "—",
    )

    note = verdict.note()

    if verdict.mismatch and note:
        log.warning("language %s: %s", job.video_id, note)

    # Without votes the decision silently goes to the YouTube label or to sr, and a broken
    # detector (failing on every window, for example after a faster-whisper update) would
    # look like a video without speech. A window shorter than the threshold did not go into
    # the model, and staying silent about it is right.
    silent = sum(
        1 for code, _, seconds in windows
        if code is None and seconds >= language.MIN_SPEECH_SECONDS
    )

    if silent:
        log.warning("language %s: detector did not answer, speech windows without an answer: %d",
                    job.video_id, silent)


def track_key(config: Config, video_id: str, fingerprint: str, pin: str | None) -> CacheKey:
    """The track cache key. One recipe: the pipeline searches and writes by it, and restore searches.

    The language in the key is the human pin (pin; None — not pinned), not the effective
    language, although the design prescribed otherwise: "the effective language goes into
    the cache key… search by the declared one, write under the effective one". The cache must
    answer before any work on the card (helper/cache.py docstring), while the effective
    language appears only after the worker. When a human has pinned the language, the
    effective one matches it, and the decision on an unpinned video lies in the record
    itself and is visible on the wire.

    YouTube metadata is not part of the key: the Juske channel is labelled en with Serbian
    speech, and the language of such a video is decided by the detector. targetLang is not
    part of it either: the output is always a Russian and a Montenegrin line, and a client
    with another targetLang gets the same track.

    Both models are part of the key, so changing either of them devalues the old tracks
    automatically rather than slipping in a translation from another model.
    """
    return CacheKey(
        video_id=video_id,
        pin=pin or "",
        asr_model=f"{config.asr.model}/{config.asr.compute_type}",
        translator_model=config.translator.model,
        study_lang=config.translator.study_language,
        track_fingerprint=fingerprint,
    )


class JobRunner:
    """Runs a job while observing GPU discipline."""

    def __init__(self, lease: GpuLease, cache: TrackCache, config: Config) -> None:
        self._lease = lease
        self._cache = cache
        self._config = config
        # The claim must be made by the configured model, not by a constant. The watchdog
        # counts as its own exactly the declared one: should these two diverge, the daemon
        # would see its own translator as someone else's work and preempt itself.
        self._lease.claim_model(config.translator.model)

    @property
    def _mt_model(self) -> str:
        return self._config.translator.model

    def _cache_key(self, job: Job) -> CacheKey:
        # The declaration goes through _declaration, not hint: _pipeline has already refused
        # an unfit pin through _pin(), and the declaration for the video and for the link will
        # come there too (see _declaration). A resume after a pause writes under the same key
        # the job searched under: the setting does not change until the daemon restarts.
        return track_key(self._config, job.video_id, job.source, self._declaration(job)[0])

    async def run(self, job: Job) -> None:
        try:
            await self._pipeline(job)
        except asyncio.CancelledError:
            raise
        except Preempted as exc:
            # Not a failure. The user took the card, we gave it away — the cues that managed
            # to get translated stay in place, recognition does not need repeating.
            # The resume is handled by _pipeline: it sees the ready job.cues and goes
            # straight to translating the missing ones.
            job._set_stage(Stage.PAUSED, message=str(exc),
                           translated=sum(c.translated for c in job.cues))
            log.info("job %s paused: %s", job.video_id, exc)
        except YtDlpError as exc:
            job._fail(str(exc), retryable=not exc.permanent)
            log.warning("job %s failed: %s", job.video_id, exc)

            if exc.diagnostics:
                log.warning("job %s, yt-dlp output:\n%s", job.video_id, exc.diagnostics)
        except PermanentJobError as exc:
            job._fail(str(exc), retryable=False)
            log.warning("job %s failed: %s", job.video_id, exc)
        except (TranslateError, RuntimeError) as exc:
            # The translator, recognition, the worker — all of this is the machine, not the
            # video: ollama was restarted, memory ran out, the process crashed. The next
            # attempt may well succeed, so the failure is retryable. From the video only a
            # live stream whose recording is still being assembled gets here — and it
            # passes by itself too.
            job._fail(str(exc), retryable=True)
            log.warning("job %s failed: %s", job.video_id, exc)
        except Exception as exc:  # noqa: BLE001 — one job crashing does not bring down the daemon
            job._fail(repr(exc), retryable=True)
            log.exception("job %s crashed", job.video_id)
        finally:
            # Stashed audio is needed only by a pause. A finished, failed or cancelled
            # job will not come back to it, and the file is tens of megabytes.
            if job.stage is not Stage.PAUSED:
                job.discard_audio()

    async def _pipeline(self, job: Job) -> None:
        # Resuming after preemption: the cues are already recognized and segmented, only
        # the translation remains to be finished. Repeating ASR would waste the card.
        if job.cues:
            await self._finish_translation(job)

            return

        # An unfit code in [asr] language — a refusal before the first request to YouTube and
        # to the card: no metadata, no audio, no lease. The refusal is final, otherwise the
        # cooling in get_or_create would recreate the job once a minute, as it was with the
        # Whisper traceback. The resume above does not ask about the language: recognition is
        # already behind.
        self._pin()

        # The metadata are already there if the job is resuming after a pause: while the card
        # is occupied, the pause repeats every two minutes, and there is no point in asking
        # YouTube about the same video on every round. The checks below have then already
        # passed too — otherwise the job would have failed instead of pausing.
        if job.info is None:
            job._set_stage(Stage.PROBING)
            job.info = await self._fetch(job, Stage.PROBING, probe)

            # The check is earlier than is_live, which covers this case too. The live stream
            # has already ended, YouTube is assembling a recording from it, and in minutes or
            # hours it is an ordinary video. The refusal is therefore not final: the cooling
            # in get_or_create and the retry button will pick the video up once the recording
            # is ready.
            if job.info.live_status == "post_live":
                raise RuntimeError("the live stream has ended, the recording is still processing")

            if job.info.is_live:
                raise PermanentJobError("live streams are not supported")

            if job.info.duration and job.info.duration > MAX_VIDEO_SECONDS:
                raise PermanentJobError(
                    f"the video is longer than {MAX_VIDEO_SECONDS // 3600} h "
                    f"({job.info.duration / 3600:.1f} h): the audio will not fit in memory"
                )

        # The author track, if there is one, saves recognition entirely: both minutes of card
        # work and its errors. The cues are marked up by a human, the timings are theirs too —
        # there is no point in reassembling them, unlike Whisper's thirty-second windows.
        #
        # The source is determined BEFORE the cache is consulted, because it is part of the
        # key. Had we counted the key earlier, the author track would never get into the cache
        # — we would put it under one key and ask under another, — and with the switch off the
        # daemon would return the previous recognition result, and the switch would look like
        # it does not work at all.
        # A pin outside the Serbian family does not take the track, see usable_manual_track.
        pin, _ = self._declaration(job)
        track = (
            usable_manual_track(job.info, pin)
            if job.use_author_track and not job.track_failed
            else None
        )

        if track:
            job.source = f"manual:{track.lang}"

        if await self._serve_cached(job):
            return

        if track:
            source = f"author track ({track.lang})"
            job._set_stage(Stage.FETCHING, source=source)

            try:
                # With retries, like metadata and audio: the failure is remembered for the
                # whole job (track_failed), and a one-off 403 would otherwise exchange the
                # author track for recognition forever.
                segments = await self._fetch(
                    job,
                    Stage.FETCHING,
                    lambda video_id, *, cookies: fetch_manual_track(
                        video_id, track.lang, cookies=cookies
                    ),
                    source=source,
                )
            except YtDlpError as exc:
                # The track is in the metadata but did not download — that is no reason to
                # fail the job: the audio is there, we will recognize it ourselves. We put the
                # source back, otherwise the recognized text would go into the cache under the
                # track key, while the recognition cache for this video would stay untouched
                # and the card would grind the same thing on every viewing.
                log.info("track %s unavailable (%s), going through recognition", track.lang, exc)
                job.source = "asr"
                job.track_failed = True

                if await self._serve_cached(job):
                    return
            else:
                # The same declaration as in decide: the human pin overrides the metadata.
                # The detector does not listen to the track, and whether it matches the speech
                # is unknown (verified=None).
                pin, hint = self._declaration(job)
                verdict = LanguageVerdict(
                    effective=language.hint(track.lang) or "sr",
                    declared=pin or hint,
                    declared_by="config" if pin else ("metadata" if hint else None),
                    detected=None,
                    confidence=None,
                    reason="track",
                    source="manual",
                    track=track.lang,
                    verified=None,
                )
                job._set_language(verdict)
                _log_language(job, verdict, [])
                # The script is by the effective language of the decision, not by the raw
                # label: otherwise Cyrillic under sr-Cyrl or SR would go to the translator.
                job.cues = self._to_job_cues_from_track(
                    normalize_cues(segments, verdict.effective)
                )
                job._emit({"type": "segmented", "total": len(job.cues), **job.readiness()})
                await self._finish_translation(job)

                return

        # The phases take the lease in turn rather than in one piece: they need different
        # amounts of memory (6 GB for Whisper versus 17.8 GB for the translator), and a common
        # capture for the smaller of the needs would mean that admission never once checked
        # whether the translator fits.
        # The unload must go BEFORE admission control. The translator may have remained
        # resident from the previous video, and then 1–3 GB are free: admission would wait for
        # memory it does not free itself until keep_alive expires.
        await asyncio.to_thread(self._lease.unload, self._mt_model)

        # We are waiting for the card, not the network, and the viewer must see exactly that.
        job._set_stage(Stage.QUEUED, waiting="gpu")

        # The audio is downloaded already under the lease, not before it. Without waiting for
        # the card, the job pauses, and the resume starts everything over — audio downloaded
        # before admission would be lost on every round. So it was: while the user held the
        # card, one video was downloaded 43 times in a row in an hour and a half, once per two
        # minutes of waiting, and on the forty-fourth googlevideo answered 403. Downloading in
        # advance and keeping the audio until admission is no way out either: a paused job may
        # wait indefinitely, and that is tens and hundreds of megabytes per video. Only the
        # audio already downloaded under the lease is stashed to disk — when the card was taken
        # away after the download; see Job.stash_audio.
        #
        # The price of the new order: there is one lease for the whole daemon, and while the
        # download runs, other phases on the card wait. Usually that is seconds. Retries after
        # a 403 and a network outage add up to twenty-five seconds of delays, so they are
        # limited by FETCH_RETRY_BUDGET and interrupted by preemption; a dead connection is cut
        # by the socket timeout in audio_pcm. The attempt itself, if yt-dlp is slow but is
        # pulling, is limited only by its overall timeout. And one more download, if the card
        # is taken away in the middle of a failed attempt: then there is no downloaded audio,
        # and there is nothing to stash.
        async with self._lease.acquire(self._config.asr_vram_mib) as held:
            audio = await asyncio.to_thread(job.take_audio)

            if audio is None:
                job._set_stage(Stage.FETCHING)
                audio = await self._fetch(job, Stage.FETCHING, audio_pcm, held=held)

            duration = audio.size / SAMPLE_RATE

            try:
                # The user may have taken the card while the download ran. We check before
                # starting the worker: otherwise it would manage to start loading Whisper into
                # memory only to be killed at once.
                held.check()

                job._set_stage(Stage.ASR, duration=duration)
                result = await self._run_asr_watched(audio, job, held)
            except Preempted:
                # The audio is already downloaded, and the resume must start from it rather
                # than from a new download. To disk, not into the job memory: how long to wait
                # for the card is not known in advance.
                #
                # Stashing is only a benefit, not an obligation. A full disk must not turn a
                # pause into a failure: if it did not work out, the job pauses anyway, and the
                # resume simply downloads the audio anew.
                try:
                    await asyncio.to_thread(job.stash_audio, audio)
                except OSError as exc:
                    log.warning("audio %s not stashed: %s", job.video_id, exc)
                    job.discard_audio()

                raise

            # Recognized — the stashed audio is no longer needed. A preemption below sends the
            # job into a pause already with cues, and the resume will start from translation.
            # A disk error here is no reason to throw away a ready transcript: the file stays
            # without an owner, as in cancel, and the job goes on.
            try:
                job.discard_audio()
            except OSError as exc:
                log.warning("audio %s not removed after recognition: %s", job.video_id, exc)

            # The language decision and the cues — before the preemption check. The transcript
            # is already obtained, and throwing it away only because the card was taken a
            # second later would mean losing minutes of GPU work: the resume will start from
            # translation, and the language decision by then must lie on the job together with
            # the cues.
            self._accept_asr(job, result)
            held.check()

        await self._finish_translation(job)

    async def _fetch(self, job: Job, stage: Stage, fetch, **kwargs):
        """A request to YouTube: anonymously, and for an 18+ video — with the account from the settings.

        First always anonymously, and the account is substituted only when YouTube refused
        specifically because of the age restriction. In advance an 18+ video cannot be told
        from an ordinary one, and an extra request under the account is an extra chance to
        lose it: according to yt-dlp itself, YouTube bans accounts it is used through.

        All account-related refusals are final. A retryable one would be recreated by the poll
        once a minute while the tab is open, and that would mean knocking on YouTube under the
        account every minute. Only a human can fix them anyway: connect an account or download
        fresh cookies — and restart the daemon.
        """
        try:
            return await self._with_retries(job, stage, fetch, **kwargs)
        except YtDlpError as exc:
            if not exc.age_restricted:
                raise

            diagnostics = "\n".join(filter(None, [str(exc), exc.diagnostics]))

            if job.account:
                raise YtDlpError(
                    "the video is 18+, and the account from the settings did not open it on "
                    "YouTube: the cookies have expired or the age of the account is not verified",
                    permanent=True,
                    diagnostics=diagnostics,
                ) from exc

            cookies = self._config.youtube.cookies_path

            if cookies is None:
                raise YtDlpError(
                    "the video is 18+: YouTube gives it out only to a signed-in user, and no "
                    "account is connected for such videos — [youtube] cookies in the daemon's "
                    "config.toml",
                    permanent=True,
                    diagnostics=diagnostics,
                ) from exc

            if not cookies.is_file():
                raise YtDlpError(
                    f"the video is 18+, and the cookies file from the settings is missing: {cookies}",
                    permanent=True,
                    diagnostics=diagnostics,
                ) from exc

            log.info("%s: the video is 18+, going for it with the account from the settings", job.video_id)
            job.account = True

            return await self._fetch(job, stage, fetch, **kwargs)

    async def _with_retries(self, job: Job, stage: Stage, fetch, *, held=None, **extra):
        """Metadata or audio, with transient failures retried in place.

        The retry must happen here, inside the job. A job failure sticks, and without
        this a one-off 403 from googlevideo — and it happens on roughly every
        fiftieth download — would cost the viewer a failure, although the very next
        link downloads. Each attempt is a new yt-dlp call, that is,
        also a new extraction with a fresh link.

        The delays are asyncio.sleep in the event loop, not a sleep in a worker thread. The
        yt-dlp call itself is not interrupted by cancellation, but between attempts drop and
        cancellation must remove the job at once, rather than after twenty seconds of waiting.

        A failed attempt followed by a retry is written to the log here, together
        with the yt-dlp output. The last one is written by run, when it fails the job.

        held — the lease on the card, if the download goes under it. Then the retries
        are checked against preemption: the card was taken away — the job pauses at once,
        rather than downloading again what will be thrown away anyway. And a new attempt
        begins only within FETCH_RETRY_BUDGET: the lease is one for the whole
        daemon, and a drawn-out download would hold other phases on the card.

        extra goes into the retry event together with the stage — for example, the source,
        so that the button does not turn "downloading author track" into "downloading audio".
        """
        started = time.monotonic()
        cookies = self._config.youtube.cookies_path if job.account else None

        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(fetch, job.video_id, cookies=cookies)
            except YtDlpError as exc:
                if held is not None:
                    held.check()

                if (
                    not exc.transient
                    or attempt == FETCH_ATTEMPTS
                    or (held is not None and time.monotonic() - started > FETCH_RETRY_BUDGET)
                ):
                    raise

                delay = FETCH_BACKOFF[min(attempt, len(FETCH_BACKOFF)) - 1]
                log.warning(
                    "%s %s: attempt %d of %d failed, retrying in %.0f s: %s%s",
                    stage.value, job.video_id, attempt, FETCH_ATTEMPTS, delay, exc,
                    f"\n{exc.diagnostics}" if exc.diagnostics else "",
                )

                # The same stage with the retry counter, and at once, before the delay:
                # otherwise the viewer would watch "downloading audio…" for twenty seconds
                # without knowing that the first attempt has already failed.
                job._set_stage(stage, retry=attempt, **extra)
                await self._backoff(delay, held)

    @staticmethod
    async def _backoff(delay: float, held) -> None:
        """The delay between attempts, which preemption interrupts."""
        if held is None:
            await asyncio.sleep(delay)

            return

        sleeper = asyncio.ensure_future(asyncio.sleep(delay))
        preempted = asyncio.ensure_future(held.wait_preempted())

        try:
            await asyncio.wait({sleeper, preempted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            preempted.cancel()

        held.check()

    async def _run_asr_watched(self, audio, job: Job, held):
        """Recognition that actually gets interrupted.

        `asyncio.to_thread` is not interrupted by cancellation, so the only way to
        free the card is to kill the process itself. We race the wait for the result against
        the preemption flag and on firing kill the worker; it exits
        on the signal, and `_run_asr` raises Preempted.
        """
        job._asr_kill = False
        work = asyncio.create_task(asyncio.to_thread(self._run_asr, audio, job))
        preempt = asyncio.create_task(held.wait_preempted())

        try:
            done, _ = await asyncio.wait({work, preempt}, return_when=asyncio.FIRST_COMPLETED)

            if work not in done:
                log.info("preemption during recognition — killing the worker")
                job.kill_asr()

            return await work
        except asyncio.CancelledError:
            # Job cancellation: the process must be killed here, otherwise the lease is released
            # before the memory is freed.
            job.kill_asr()
            raise
        finally:
            preempt.cancel()

    def _accept_asr(self, job: Job, result: dict) -> None:
        """The worker answer onto the job: first the language decision, then the cues and segmented."""
        verdict = LanguageVerdict.from_wire(result.get("verdict"))

        if verdict is None:
            # There is no decision in the answer or it is unfit: the language is taken from the
            # language field by the same rule as the pin. An unknown code — the refusal is
            # final: a retry in a minute would run recognition on the card again and get the
            # same answer.
            raw = result.get("language")

            try:
                effective = language.normalize(raw)
            except language.UnknownLanguage:
                effective = None

            if effective is None:
                raise PermanentJobError(f"the recognizer returned an unknown language \"{raw}\"")

            verdict = LanguageVerdict(
                effective=effective, declared=None, declared_by=None,
                detected=None, confidence=None, reason="detector",
            )

        job._set_language(verdict)
        _log_language(job, verdict, (result.get("detection") or {}).get("windows") or [])
        # The seed is composed by the worker, and its stderr is discarded on success: without
        # this line there is nothing to match the echo on the screen against. A line per
        # emission — because "silence" is up to 30 seconds of audio, and there is nothing to
        # check the thresholds against on this corpus: there are no metrics in the cache. The
        # log is the only check in the field.
        prompt = result.get("prompt")
        log.info("seed %s: %s", job.video_id, prompt or "none")
        cues, dropped = asr_cues(result["cues"], verdict.effective, prompt)

        for reason, text in dropped:
            log.info("dropped %s (%s): %s", job.video_id, reason, text)

        # The JobCue indices stay contiguous: the filtering goes before _to_job_cues, and
        # _next_batch and _context_for rely on index.
        job.cues = self._to_job_cues(cues)
        job._emit({"type": "segmented", "total": len(job.cues), **job.readiness()})

    async def _finish_translation(self, job: Job) -> None:
        """Translate the missing cues and, if everything is ready, cache the track."""
        job._set_stage(Stage.TRANSLATING, total=len(job.cues))

        # There is nothing to translate — for example, not a word was recognized in the video.
        # Then the lease for the translator is not needed, and if taken, it could send the job
        # into a pause without a single cue, and the resume would start with downloading
        # the audio and recognition anew — on every round while the card is occupied.
        if any(not c.translated for c in job.cues):
            try:
                # model= is mandatory: if our translator is already resident, there is no need
                # to ask for memory for it — otherwise admission would require 17.8 GB free
                # exactly when 17.3 of them are occupied by it itself.
                async with self._lease.acquire(
                    self._config.translator_vram_mib, model=self._mt_model
                ) as held:
                    await self._translate(job, held)
            except Preempted:
                # Giving up the card, we must free it too. Our keep_alive is 60 seconds, that
                # is, without an explicit unload, 17.3 GB would remain ours for another minute
                # after we announced we are leaving — exactly the behaviour of the hostile
                # neighbour gpulease was written against.
                await asyncio.to_thread(self._lease.unload, self._mt_model)
                raise

        pending = sum(not c.translated for c in job.cues)
        failed = sum(c.failed for c in job.cues)

        if pending:
            # The work is not finished — this is the only thing that a pause is. The resume
            # will find the untranslated cues and continue from them.
            job._set_stage(Stage.PAUSED, translated=len(job.cues) - pending)

            return

        if failed:
            # All the cues are processed, but part of them goes to the screen as the original
            # text. The job is finished — there is nothing to repeat, the repair ladder has
            # already worked through a batch, halves and one by one. But such a track does not
            # go into the cache: otherwise the failure would be fixed forever under a valid
            # key, and the next viewing would give it back instead of a new attempt.
            job._set_stage(Stage.DONE, total=len(job.cues), failed=failed, cached=False)

            return

        if not job.cues:
            # An empty track does not go into the cache. Written, it would be given out as
            # ready forever (there are three such in the daemon cache, one for a 41-minute
            # video), while restore considers it a corrupted record. The price — a video
            # without a single cue is not cached: a new job for it — when the registry evicts
            # this one (see MAX_RETAINED_JOBS) or the daemon is restarted — downloads the
            # audio and recognizes again under the lease.
            job._set_stage(Stage.DONE, total=0, cached=False)

            return

        await asyncio.to_thread(
            self._cache.put,
            self._cache_key(job),
            [c.payload() for c in job.cues],
            job.info.duration if job.info else None,
            language=job.language.wire() if job.language else None,
        )
        job._set_stage(Stage.DONE, total=len(job.cues))

    def _run_asr(self, audio, job: Job) -> dict:
        """Recognition in a separate process — the process exit guarantees VRAM return."""
        pin, hint = self._declaration(job)
        args = [
            sys.executable,
            "-m",
            "helper.asr.worker",
            "--model",
            self._config.asr.model,
            "--compute-type",
            self._config.asr.compute_type,
            "--language",
            pin or "auto",
        ]

        if hint:
            args += ["--hint", hint]

        # Names, not a ready seed: the language is decided by the worker after the detector,
        # and only it knows whether there is a phrase for this language. The description is not
        # passed (see harvest_names). ensure_ascii=False — the names are readable in ps and in
        # the log. The flag goes into every run, even with the list [].
        args += [
            "--prompt-names",
            json.dumps(harvest_names(job.info.title, job.info.tags), ensure_ascii=False),
        ]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parent.parent,
        )
        job._asr_proc = proc

        # The kill was ordered while the process was being created — there was no handle then.
        if job._asr_kill:
            proc.kill()

        try:
            out, err = proc.communicate(input=audio.tobytes(), timeout=ASR_TIMEOUT)
        except subprocess.TimeoutExpired:
            # communicate does not kill the process on timeout. Without this Whisper
            # would keep holding ~6 GB when the lease on the card has already been released,
            # and the next job would get admission for memory that does not exist.
            proc.kill()
            proc.communicate()

            raise RuntimeError(f"recognition did not fit into {ASR_TIMEOUT:.0f} s") from None
        finally:
            job._asr_proc = None

        if proc.returncode != 0:
            # A process killed by preemption or cancellation is not a recognition error:
            # it was interrupted deliberately, and the job must pause rather than fail.
            if proc.returncode < 0 and job._asr_kill:
                raise Preempted(f"recognition interrupted by signal {-proc.returncode}")

            # The signal did not come from us: memory ran out and the kernel killed the
            # process, or the worker crashed itself. This is a failure, not a pause. A pause
            # resumes on every poll, and there are no cues yet, so every resume would download
            # the audio again and fail again with the same signal. The failure is retryable and
            # is recreated no more often than once a minute.
            if proc.returncode < 0:
                raise RuntimeError(f"recognition killed by signal {-proc.returncode}")

            # The worker refused itself — the phrase is in stdout, not a traceback in stderr.
            # A refusal with "permanent" will not pass on a retry either (an unfit language
            # code), so it is final: the cooling does not recreate such a job. A phrase without
            # the flag ("audio shorter than half a second") is a retryable failure, but the
            # viewer sees it rather than an empty tail of stderr.
            try:
                reply = json.loads(out)
            except ValueError:
                reply = None

            if isinstance(reply, dict) and reply.get("error"):
                if reply.get("permanent") is True:
                    raise PermanentJobError(str(reply["error"]))

                raise RuntimeError(f"ASR failed: {reply['error']}")

            raise RuntimeError(f"ASR failed: {err.decode(errors='replace')[-500:]}")

        result = json.loads(out)

        if "error" in result:
            raise RuntimeError(result["error"])

        return result

    def _pin(self) -> str | None:
        """The language pinned by the owner in [asr] language: a Whisper code or None ("auto").

        load() brings the value to a code, and only logs an unfit one: the daemon must come
        up. A job with an unfit code gets a refusal in a human phrase, final: a retry with the
        same config would give the same thing.
        """
        try:
            return language.normalize(self._config.asr.language)
        except language.UnknownLanguage as exc:
            raise PermanentJobError(language.refusal(exc.raw)) from None

    def _declaration(self, job: Job) -> tuple[str | None, str | None]:
        """What is said about the language before the audio: (the owner's pin, the YouTube label).

        What decides is not this but the worker (helper.language.decide): the detector always
        listens. The YouTube label is only a hint. yt-dlp derives it from the YouTube ASR track,
        and that labels Serbian channels as English (Juske); Serbian videos usually have no
        label at all — a Serbian corpus of 60 videos gave None in all sixty. An unfamiliar
        label is just "we do not know".

        A declaration per video and per link will come here later (the "Deferred" section in
        docs/superpowers/plans/2026-11-01-source-language.md): the declaration for the decision,
        the cache key and the track choice is read only here. The exceptions are the pin check
        at the start of _pipeline (_pin) and restore (JobManager._key_for: there is no job
        there, it reads the setting through hint and changes together with this function).
        """
        return self._pin(), (language.hint(job.info.audio_language) if job.info else None)

    async def _serve_cached(self, job: Job) -> bool:
        """Give out the ready cues from the cache. True — the job is finished, no need to go on.

        The key depends on job.source, so this can only be called after the source of the cues
        has been chosen.
        """
        cached = await asyncio.to_thread(
            self._cache.get, self._cache_key(job), job.info.duration
        )

        # A record without cues is a miss, as in restore: such a track is never ready.
        if not cached or not cached.get("cues"):
            return False

        # The decision before the cues, as on fresh recognition: otherwise the mismatch flag
        # would disappear on every repeated viewing. A record without the field or with an
        # unfit one — language null and no language event, without crashing.
        verdict = LanguageVerdict.from_wire(cached.get("language"))

        if verdict is not None:
            job._set_language(verdict)

        job.cues = [
            JobCue(
                index=i,
                start=c["start"],
                end=c["end"],
                src=c["src"],
                lines=c["lines"],
                study=c.get("study", ""),
                # The flag must survive the round through the cache: without it a cue
                # with untranslated source text would come back as a full-fledged
                # translation.
                failed=c.get("failed", False),
            )
            for i, c in enumerate(cached["cues"])
        ]
        job._emit({"type": "cues", "cues": [c.payload() for c in job.cues], **job.readiness()})
        job._set_stage(Stage.DONE, cached=True, total=len(job.cues))

        return True

    def _to_job_cues_from_track(self, segments: list[dict]) -> list[JobCue]:
        """Cues from the author track: the timings are taken as they are.

        Resegmentation here would be harmful. It is needed for the Whisper output, where
        a cue is a thirty-second window of three hundred characters; with the author, the
        boundaries are placed for reading, and moving them would spoil
        what this path exists for.
        """
        return [
            JobCue(index=i, start=c["start"], end=c["end"], src=c["text"])
            for i, c in enumerate(segments)
            if c["text"].strip()
        ]

    def _to_job_cues(self, cues: list[Cue]) -> list[JobCue]:
        return [
            JobCue(index=i, start=c.start, end=c.end, src=c.text)
            for i, c in enumerate(cues)
        ]

    async def _translate(self, job: Job, held) -> None:
        """Translate all the cues, starting with those near the playhead."""
        while True:
            held.check()

            pending = [c for c in job.cues if not c.translated]

            if not pending:
                return

            batch_cues = self._next_batch(job, pending)

            batch = Batch(
                cues=[c.src for c in batch_cues],
                previous=self._context_for(job, batch_cues[0].index),
                title=job.info.title if job.info else "",
                channel=job.info.channel if job.info else "",
            )

            try:
                pairs = await self._translate_once(batch, batch_cues)
            except RETRYABLE as exc:
                # Caught broadly, not only TranslateError: a dropped connection to ollama,
                # its restart or broken JSON are just as much a reason for the repair
                # ladder. Missed past, they brought down the whole job together with the
                # ready transcript, on which minutes of card work had already been spent.
                #
                # The splitting goes the same way as the batch: if the study mode is on,
                # the halves and the one-by-one attempts ask for both lines too. There used
                # to be a monolingual fallback here — "a study line is a luxury in such a
                # situation" — and it silently left it empty. On a Russian video this looked
                # as if Russian had gone instead of Montenegrin: the overlay substituted the
                # recognition text for the empty line, and that is Russian for such a video.
                log.warning("batch did not translate (%s), trying halves", exc)
                pairs = await self._translate_halves(batch_cues, job, held)

            for cue, (study_line, text) in zip(batch_cues, pairs):
                if text.strip():
                    cue.lines = text.split("\n")[:2]

                if study_line.strip():
                    cue.study = study_line.strip()

            # The loop convergence guarantee: everything that survived neither the batch nor
            # the halves nor the one-by-one attempt is marked with the source text and the
            # flag. Without this an unyielding cue would keep pending non-empty forever.
            #
            # Showing the source text instead of Russian is bad, but silently skipping a
            # piece of speech is worse, so the cue goes to the screen marked.
            for cue in batch_cues:
                if not cue.translated:
                    cue.lines = [cue.src]
                    cue.failed = True

            job._emit({"type": "cues", "cues": [c.payload() for c in batch_cues], **job.readiness()})

    async def _translate_once(
        self, batch: Batch, cues: list[JobCue] | None = None
    ) -> list[tuple[str, str]]:
        """One go at the model. A pair of lines if the study mode is on.

        The uniform answer form in both modes saves the repair ladder from
        having to know which of them is on: it simply passes the pairs on.
        """
        if self._config.translator.study_language:
            pairs = await asyncio.to_thread(translate_dual, batch, self._mt_model)

            for reason in validate_dual(pairs, batch.cues):
                log.info("batch: %s", reason)

            return pairs

        lines = await asyncio.to_thread(translate_batch, batch, self._mt_model)

        for index, reason in validate(batch.cues, lines):
            number = cues[index].index if cues else index
            log.info("cue %d: %s", number, reason)

        # The study mode is off: the second line is the recognition text, as
        # before. The daemon substitutes it, not the overlay — only here is it known
        # whether the mode is on, and an empty study line means exactly a failure,
        # not "show the original".
        return [(src, ru) for src, ru in zip(batch.cues, lines)]

    async def _translate_halves(
        self, cues: list[JobCue], job: Job, held
    ) -> list[tuple[str, str]]:
        """The batch did not go through in full — we divide it in half. Usually one long piece is to blame.

        The preemption check is mandatory at every level of recursion: on a batch of forty
        cues, descending to one-by-one attempts is up to seventy model requests,
        that is, tens of minutes of holding the card. Without the check the ladder would
        repair the translation ignoring that the user has long been waiting for their turn.
        """
        held.check()

        if len(cues) == 1:
            return [("", "")]

        middle = len(cues) // 2
        out: list[tuple[str, str]] = []

        for half in (cues[:middle], cues[middle:]):
            batch = Batch(cues=[c.src for c in half], title=job.info.title if job.info else "")

            try:
                out += await self._translate_once(batch, half)
            except RETRYABLE:
                out += await self._translate_halves(half, job, held)

        return out

    def _next_batch(self, job: Job, pending: list[JobCue]) -> list[JobCue]:
        """The cues nearest to the playhead, running consecutively.

        Consecutively — mandatory: neighbouring cues are translated together, because
        context is needed to agree on gender and terminology.

        A job has several playheads, one per viewer, and each next batch
        goes around the next of them: see Job.aim.
        """
        # The position is taken ONCE per batch: every aim() turns the circle between
        # the viewers. Were we to call it from the min key, that is, on every pending
        # cue, the circle would turn once per cue per batch, and the turn would stop
        # alternating: with an even number of cues all batches would go to one viewer.
        aim = job.aim()
        nearest = min(pending, key=lambda c: abs(c.start - aim))
        start = max(0, nearest.index - BATCH_SIZE // 4)
        window = [c for c in job.cues[start : start + BATCH_SIZE] if not c.translated]

        return window or pending[:BATCH_SIZE]

    def _context_for(self, job: Job, index: int) -> list[tuple[str, str]]:
        """A few already translated cues before the batch.

        Both Serbian and Russian mark gender in the past tense. Without an antecedent
        the model would guess the speaker's gender and flip it in the middle of a scene.
        """
        context = []

        for cue in job.cues[max(0, index - CONTEXT_CUES) : index]:
            if cue.translated:
                context.append((cue.src, " ".join(cue.lines)))

        return context


def asr_cues(
    segments: list[dict], language: str, prompt: str | None
) -> tuple[list[Cue], list[tuple[str, str]]]:
    """Cues from the recognition segments and what was dropped along the way: (reason, text).

    "Silence" drops the segment entirely. In a segment with a "repeat" only the
    looped sentences go: the memory is each segment's own, the loop lives inside one
    piece, and the speech before it remains. In segments without the mark repeats are never
    dropped: "Da. Ne. Da." is ordinary speech. Echo is checked after the repeat and after
    normalize_cues: the Cyrillic output is compared with the Latin seed already in Latin.

    The filters are only for recognition — the author track branch in _pipeline is not
    changed. The drops are returned rather than written here because the log is kept by the
    job.
    """
    dropped: list[tuple[str, str]] = []
    kept = []

    for segment in segments:
        if segment_verdict(segment) == "тишина":
            dropped.append(("тишина", segment["text"]))
        else:
            kept.append(segment)

    # What has already been said in the segment: each has its own, the loop lives inside one piece.
    heard: dict[int, set[tuple[str, ...]]] = {}

    def drop(sentence: str, segment: dict) -> bool:
        if segment_verdict(segment) == "повтор":
            # Words without case and punctuation: "Idemo dalje" without a dot is the same repeat.
            words = tuple(re.findall(r"\w+", sentence.casefold()))
            seen = heard.setdefault(id(segment), set())
            looped = words in seen or compression_ratio(sentence) > COMPRESSION_RATIO_THRESHOLD
            seen.add(words)

            if looped:
                dropped.append(("повтор", sentence))

                return True

        # Echo is checked after normalize_cues: the Cyrillic output is compared
        # with the Latin seed already in Latin.
        if is_echo(sentence, prompt):
            dropped.append(("эхо затравки", sentence))

            return True

        return False

    return build_cues(normalize_cues(kept, language), drop=drop), dropped


class JobManager:
    """The job registry: one per video and track variant, a shared lease on the GPU."""

    def __init__(self, config: Config | None = None) -> None:
        # The configuration is read once, at startup. A model change requires
        # a daemon restart: the model is resident in ollama, and changing it in the middle
        # of a job means reopening the whole swap discipline.
        self.config = config or load_config()
        self.lease = GpuLease()
        self.cache = TrackCache()
        # The directory and the ceiling — from the config: the cache can also be kept in /tmp
        # (tmpfs), but then the ceiling is no longer measured by the disk.
        self.videos = VideoStore(
            self.config.video.directory or VIDEO_DIR,
            max_bytes=self.config.video.max_bytes,
        )
        self.runner = JobRunner(self.lease, self.cache, self.config)
        # The key is the pair (video, author track), not one video: see get_or_create.
        # The dictionary cannot be accessed by a single video_id — for that there are get and
        # jobs_for.
        self._jobs: dict[tuple[str, bool], Job] = {}

        # The registry lives in memory, and after a restart the stashed audio is needed by
        # nobody. And without cleanup it would pile up: a paused job whose tab was
        # closed will not resume, and it will not remove it itself.
        shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)

    def attach(self, video_id: str, use_author_track: bool | None = None) -> Job | None:
        """An existing job. Unlike get_or_create, it creates NOTHING.

        Needed by the poll after restore. An ordinary poll goes through get_or_create,
        because subscribing to a video is itself consent to the work. But a tab that was
        merely reloaded gave no consent: it picked up what was already done. If the job has
        disappeared by this moment — the daemon was restarted, the registry evicted
        it, — a new one must not be created, otherwise reloading the page would itself start
        the download and the card.

        Searches the same way as get: with the track flag — exactly this video variant,
        without it — the most recently touched. The tab picks up its own job rather than a
        neighbouring one: a video has a job per variant.
        """
        job = self.get(video_id, use_author_track)

        if job is not None:
            job.touched_at = time.monotonic()

        return job

    def _unfinished(self) -> int:
        """How many jobs take a place under MAX_ACTIVE_JOBS: unfinished, except abandoned pauses.

        A running one, one waiting for the card and a fresh pause take one: there is card work
        in a pause, and the viewer may have only stepped away. But a pause is resumed only by a
        client request, it will not continue by itself, and an abandoned one would hang in the
        registry until the daemon restarts, and such ones, having accumulated, would close it
        for new videos for good. That is why one that has stood longer than PAUSED_AUDIO_TTL
        (Job.is_stale_pause, the same term by which _evict removes its audio) does not count.
        It does not leave the registry: a returning viewer will resume it as an existing job,
        while the limit guards only creation. A finished and a failed job take no place.
        """
        now = time.monotonic()

        return sum(
            not job.finished and not job.is_stale_pause(now) for job in self._jobs.values()
        )

    def get_or_create(
        self, video_id: str, target_lang: str = "ru", use_author_track: bool = True
    ) -> Job:
        """The job for the video and track variant; if there is none — create and start it.

        CapacityError is raised only when a new job would have to be created and there is no
        place for it: MAX_ACTIVE_JOBS are already taken (see _unfinished). A job that already
        exists is given out even at the limit: a viewer's poll is not met with a refusal.
        """
        key = (video_id, use_author_track)
        job = self._jobs.get(key)
        replaced: Job | None = None

        # The identity of a job is the pair "video and author track". The author track and
        # recognition give different texts with different cue numbering, and two clients
        # with a different switch wait for different things: each gets its own job, and
        # nobody recreates someone else's. Before, the job was one per video and was
        # dropped on a mismatch, in the expectation that the value comes from the settings
        # and does not change between polls — true for one client, not for two: the polls
        # dropped each other's job on every round (113 times in 90 seconds), and every drop
        # started the download anew, and cancellation does not stop it: the downloader lives
        # in to_thread, and the yt-dlp processes remained orphans. The switch in the
        # extension now simply leads to another key: the previous job stays, and returning to
        # it does not start the work anew.
        #
        # The translation language is not part of the key: it is one for all, and the job is
        # made with the one the first caller created it with. The client sees the discrepancy
        # itself — the snapshot carries targetLang.
        #
        # The only thing that recreates a job without a client request is a retryable
        # failure, once it has cooled down, and only the job that failed.
        if (
            job is not None
            and job.stage is Stage.ERROR
            and job.retryable
            and job.failed_at is not None
            and time.monotonic() - job.failed_at >= RETRY_COOLDOWN
            # A retry is a new job, and it is not created on overflow. The failed one then
            # stays as it is and is given out below, as before cooling: a poll on an existing
            # job is not met with a refusal, and removing it and then refusing would mean
            # losing the generation number and leaving the subscribers on a cancelled one.
            and self._unfinished() < MAX_ACTIVE_JOBS
        ):
            # A failure sticks, but a retryable one — not forever. Recreating
            # the job on every request is impossible: the poll calls get_or_create
            # constantly, and that gave a storm — failure → recreation → failure,
            # eight times in thirty seconds, with a request to YouTube on every
            # round. But keeping the failure forever is impossible either: a network
            # problem or a 403 from googlevideo passes by itself, while a viewer who pressed
            # start after F5 would get the same old failure without a single new attempt.
            #
            # The cooling separates these two cases: from the poll — no more often than an
            # attempt a minute per video, and that is no longer a storm, while for the viewer
            # — a real new attempt. Final failures (a live stream, length, a removed or
            # private video) are not recreated at all: repeating them will give nothing.
            # An immediate retry by the button goes through drop, it is in the API.
            log.info("retrying %s: the failure is retryable and has cooled down (%s)", video_id, job.error)
            # Not drop: that one removes all variants of the video, while the second one may
            # be running for another client.
            self._remove(job)
            replaced, job = job, None

        if job is None:
            # The limit guards only creation, so the check is here rather than at the start:
            # a poll on an existing job does not reach it. The retry of a failed job comes
            # here too: the removed job was finished and did not count, and the place for the
            # retry was checked above.
            unfinished = self._unfinished()

            if unfinished >= MAX_ACTIVE_JOBS:
                raise CapacityError(
                    f"too many videos in progress ({unfinished}): a place will free up "
                    "when one of them finishes"
                )

            job = Job(video_id, target_lang, use_author_track)
            self._jobs[key] = job

            if replaced is not None:
                # The next generation of the same video. The number is set BEFORE the
                # subscribers are moved: the move sends each a catch-up snapshot, and in it
                # it must already be the new one, otherwise the client would take the
                # recreated job for the previous one.
                job.generation = replaced.generation + 1

                # The subscribers of the replaced job move to the new one. The socket
                # resubscribes only through its own watch — when moving to another
                # variant or when its queue is not on the job, as after a
                # drop, — while the job is recreated by the first request after the failure
                # has cooled, most often someone else's poll. Until the next watch, which may
                # never come, the socket queue stays on the previous job. Without the move,
                # the recreation would leave it listening to the cancelled job, while
                # unwatch would remove the queue from the new one, where it never was.
                for queue in list(replaced._subscribers):
                    replaced.unsubscribe(queue)
                    job.subscribe(queue)

            job.start(self.runner)
        elif job.stage is Stage.ERROR:
            # The failure is final or has not cooled yet — we give it out as it is, see above.
            pass
        elif job.stage is Stage.PAUSED:
            # The user was taking the card. The cues have not gone anywhere — we continue
            # the same job rather than starting over from recognition.
            job.resume(self.runner)

        job.touched_at = time.monotonic()

        self._evict()

        return job

    def _key_for(self, video_id: str, fingerprint: str) -> CacheKey:
        # The same recipe as the pipeline (track_key), otherwise restore would silently fail
        # to find what was done. The pin — through hint, not _pin: restore only reads and does
        # not refuse. An unfit pin makes the job fail in the pipeline, while videos cached
        # without a pin still open: restore reads an unfit code as "not pinned"; those made
        # under the previous fit pin are not found until the code is fixed.
        return track_key(
            self.config, video_id, fingerprint, language.hint(self.config.asr.language)
        )

    def restore(
        self,
        video_id: str,
        target_lang: str = "ru",
        use_author_track: bool = True,
        duration: float | None = None,
    ) -> Job | None:
        """What is already there for this video. Does NOT start work — neither network nor card.

        Needed for a page reload. The viewer's consent is asked once
        per video, and there is no point in asking again on F5: showing what is already done is
        not the same as starting to download audio and occupy the card. That is why this is
        strictly a read: a running job is given out as it is, a ready one is raised
        from the cache, and if there is neither — None is returned, and the overlay
        honestly shows the start button.
        """
        # We look for exactly the video variant the viewer asked for: a job with a DIFFERENT
        # switch lies under another key and is not found here, no separate
        # check is needed. A track made with another setting is not what they
        # asked for, and nothing is ready for them until there is their own.
        key = (video_id, use_author_track)
        job = self._jobs.get(key)

        if job is not None:
            # We do not restore a paused or failed one. Resuming a
            # pause is capturing the card, while a stuck failure raised into the overlay
            # hides the button from the viewer: it hides under the error message,
            # and there is nothing left to retry with.
            if job.stage in (Stage.PAUSED, Stage.ERROR):
                return None

            return job

        payload = self._cached_variant(video_id, use_author_track, duration)

        # An empty track is not "everything is ready" but a corrupted record. Were we to give
        # it out, the overlay would turn on, hide the button by the done stage and show
        # nothing, and there would be nothing left to press.
        if payload is None or not payload.get("cues"):
            return None

        job = Job(video_id, target_lang, use_author_track)
        job.source = payload.get("track_fingerprint", "asr")
        # The decision before the cues, as in _serve_cached; a record without it or with an
        # unfit one — language null and no language event.
        verdict = LanguageVerdict.from_wire(payload.get("language"))

        if verdict is not None:
            job._set_language(verdict)

        job.cues = [
            JobCue(
                index=i,
                start=c["start"],
                end=c["end"],
                src=c["src"],
                lines=c["lines"],
                study=c.get("study", ""),
                failed=c.get("failed", False),
            )
            for i, c in enumerate(payload["cues"])
        ]
        job._emit({"type": "cues", "cues": [c.payload() for c in job.cues], **job.readiness()})
        job._set_stage(Stage.DONE, cached=True, total=len(job.cues))

        self._jobs[key] = job
        self._evict()

        log.info("restored %s from the cache (%s, %d cues)", video_id, job.source, len(job.cues))

        return job

    def translated(self, video_id: str, use_author_track: bool = True) -> bool:
        """Whether the translation of this video is already done — read only, no work.

        The link list needs the same as the picture readiness: to answer at once, without
        network and without the card. The key is counted by the current settings, so a track
        from a previous model does not count as a ready translation. An empty track is
        a corrupted record, not a ready translation: with a checkmark the viewer would wait
        for subtitles that do not exist.
        """
        payload = self._cached_variant(video_id, use_author_track)

        return bool(payload and payload.get("cues"))

    def _cached_variant(
        self, video_id: str, use_author_track: bool, duration: float | None = None
    ) -> dict | None:
        """The saved track matching the current settings.

        The source fingerprint is taken from the file itself, and the key is recomputed from
        it: a key match means the track was made by the same models and the same
        prompt as the current ones. Otherwise restore would slip in a result
        from another model, bypassing exactly the check the key was introduced for.

        The pin in the recomputed key is the current one, from the setting, not the one
        recorded in the file. That is why a record made under another pin, another model
        or version is not found, but is not deleted either.
        """
        # Only the fingerprint is taken from the record. A fingerprint has one key, while get
        # writes used_at, so there is one get per fingerprint, however many records carry it.
        fingerprints = dict.fromkeys(
            stored.get("track_fingerprint", "asr")
            for stored in self.cache.variants(video_id)
        )
        found: dict[str, dict] = {}

        for fingerprint in fingerprints:
            # The duration comes from the player itself: the page knows it exactly,
            # and there is no need to request metadata from YouTube on every load.
            # Without this check a video re-uploaded under the same identifier
            # would silently get shifted timings.
            hit = self.cache.get(self._key_for(video_id, fingerprint), duration)

            if hit is not None:
                found[fingerprint] = hit

        if use_author_track:
            # The author track takes priority — with the switch on, the pipeline
            # would have chosen it too. Its absence means the video simply does not have it,
            # and recognition for this setting is an equally legitimate result.
            #
            # Of several author tracks (manual:sr and manual:sr-Latn) — the freshest
            # by created_at, the last one made, not the first by the index row order.
            manual = max(
                (v for k, v in found.items() if k.startswith("manual:")),
                key=lambda v: v["created_at"]
                if isinstance(v.get("created_at"), (int, float))
                else 0.0,
                default=None,
            )

            return manual or found.get("asr")

        return found.get("asr")

    def _evict(self) -> None:
        """Throw out the oldest finished jobs and keep the stashed audio within the budget.

        The cues of a twenty-minute video are hundreds of objects, and the registry without
        eviction would grow for the whole life of the process. We throw out only what
        is no longer moving anywhere: an unfinished job holds the transcript for
        which card work was spent.
        """
        # The paused jobs themselves are not evicted — there is card work in them.
        # But their stashed audio, if nobody has touched the job for half an hour,
        # we remove: otherwise an abandoned pause would hold tens and hundreds of
        # megabytes on disk until the daemon restarts, and it lives for weeks. The same sign
        # takes such a pause off the count under MAX_ACTIVE_JOBS: see Job.is_stale_pause.
        #
        # A disk failure here does not bring down the request. _evict goes at the end of
        # get_or_create (a poll with create=true, watch, POST /v1/jobs) and restore, when the
        # job is already created and the new one started, — an exception from here would turn
        # the answer about a working job into a 500. Audio that failed to be removed stays on
        # disk without an owner: discard_audio resets the handle before deleting, and the next
        # walk will not come back to it. Such a file will be removed by the budget
        # (_trim_paused_audio) when the audio grows past PAUSED_AUDIO_MAX_BYTES, or by a
        # daemon restart.
        now = time.monotonic()

        for job in self._jobs.values():
            if job.is_stale_pause(now):
                try:
                    job.discard_audio()
                except OSError as exc:
                    log.warning("audio of the abandoned pause %s not removed: %s", job.video_id, exc)

        # The budget after the term, not instead of it: the term has already removed what was
        # abandoned, and the budget must not touch the audio of pauses that are being accessed
        # because of it.
        self._trim_paused_audio()

        finished = [j for j in self._jobs.values() if j.finished]

        for job in sorted(finished, key=lambda j: j.created_at)[
            : max(0, len(self._jobs) - MAX_RETAINED_JOBS)
        ]:
            self._jobs.pop((job.video_id, job.use_author_track), None)

    def _trim_paused_audio(self) -> None:
        """Remove the oldest stashed audio while its total is above PAUSED_AUDIO_MAX_BYTES.

        Seniority is by file write time, one for all files in the directory. A file that is
        listed under a registry job is removed through the job itself: its handle
        (_audio_path) stays correct. A file without an owner — the remnant of a dead job —
        is simply erased. A job left without audio does not break: take_audio will return
        None, and the resume will download the audio anew.

        A disk error does not come out, for the same reason as in _evict: the cleanup goes with
        every get_or_create. The directory was not read — the budget is not checked this time;
        a file was not measured — it does not count towards the budget; it failed to erase —
        it stays both in the directory and in the sum, and the budget is made up by the next
        one in seniority. Each such refusal is a line in the log with the path and the reason.
        """
        try:
            entries = list(PAUSED_AUDIO_DIR.iterdir())
        except FileNotFoundError:
            # The directory is created at the first audio stashing, and after startup it is
            # absent.
            return
        except OSError as exc:
            log.warning(
                "stashed audio directory %s not read, budget not checked: %s",
                PAUSED_AUDIO_DIR,
                exc,
            )

            return

        files = []

        for entry in entries:
            try:
                info = entry.stat()
            except FileNotFoundError:
                # The file may have been removed between listing and measuring: a worker thread
                # both stashes and removes audio. Such a file is no longer in the budget.
                continue
            except OSError as exc:
                log.warning(
                    "stashed audio %s not measured, not counted towards the budget: %s", entry.name, exc
                )

                continue

            files.append((info.st_mtime, info.st_size, entry))

        total = sum(size for _, size, _ in files)
        # We identify by name, not by path: mkstemp returns an absolute path, while the
        # directory could also be given as relative. The name in the directory is one per file.
        #
        # The handle of each job is read once. It is reset both by a worker thread
        # (discard_audio from take_audio and stash_audio, which go through to_thread) and
        # between the None check and reading the name it could disappear: a second read would
        # fail with AttributeError.
        owners = {}

        for job in self._jobs.values():
            path = job._audio_path

            if path is not None:
                owners[path.name] = job

        for _, size, entry in sorted(files, key=lambda f: f[0]):
            if total <= PAUSED_AUDIO_MAX_BYTES:
                break

            owner = owners.get(entry.name)

            try:
                if owner is not None:
                    owner.discard_audio()
                else:
                    entry.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("stashed audio %s not removed: %s", entry.name, exc)

                continue

            total -= size
            log.info(
                "stashed audio %s removed: pause audio must not take more than %d MiB",
                entry.name,
                PAUSED_AUDIO_MAX_BYTES // 2**20,
            )

    def jobs_for(self, video_id: str) -> list[Job]:
        """All jobs for the video, the most recently touched first.

        Needed by those who name the video without a track flag: the view position,
        unwatching, dropping. Which of the variants they need they do not say, and a video
        has two of them.
        """
        return sorted(
            (job for (video, _), job in self._jobs.items() if video == video_id),
            key=lambda job: job.touched_at,
            reverse=True,
        )

    def get(self, video_id: str, use_author_track: bool | None = None) -> Job | None:
        """The job for the video. With the track flag — exactly this variant.

        Without the flag — the most recently touched: it was accessed last, and it
        is the current one for the video.
        """
        if use_author_track is not None:
            return self._jobs.get((video_id, use_author_track))

        found = self.jobs_for(video_id)

        return found[0] if found else None

    def drop(self, video_id: str, client_id: str | None = None) -> bool:
        """Take the client off the video and cancel the jobs nobody is watching any more.

        Acts on ALL variants of the video: the drop comes without a track flag, and the daemon
        does not know which of them the client was watching. With a key the client is taken
        off each variant (Job.forget_client), while only the one with no live records left is
        cancelled and removed from the registry (Job.live_slots): work still watched by another
        client must not disappear because one of the two changed their mind. One who has fallen
        silent for longer than PLAYHEAD_TTL does not count as a viewer: there is nobody to
        remove a closed tab, and its record would hold the job forever.

        A sent but unfit key (empty, not a string, too long) is still a key: forget_client
        reduces it to LEGACY_CLIENT, as it does the view position. "No key" is only None.
        Without a key everything is cancelled, as it was while there was one client: why,
        it is said in the loop.

        Known limitations accepted deliberately:

        1. A viewer is a live position record: a subscription and a long poll do not count as
           one, and a client that has not yet reported where it is watching is not protected
           from someone else's drop. So an extension tab with a frozen page (no positions for
           longer than PLAYHEAD_TTL) whose service worker is still polling with create=true
           loses the job from a tablet drop with a key, and the worker's next poll creates it
           again.
        2. The records are in fact shared for the video, not for the variant: the position comes
           without a track flag and lands on all variants. It matters only when clients
           differ in authorTrack: a variant nobody watches will not be cancelled while the
           neighbouring one is watched — extra work, but not lost work.
        3. A drop without a key cancels all variants, including a healthy one watched by
           another client. The extension sends exactly that: the retry button reads the most
           recently touched variant and, if it failed, drops the video without a body.

        Returns whether at least one job was cancelled. An unknown video — a silent False.
        """
        cancelled = False

        for job in self.jobs_for(video_id):
            # Without a key there is no check, and the job is cancelled even if someone's
            # tablet is watching it. This is deliberate, not unfinished work: the extension
            # does not know the key, and it has already been able to drop a job entirely —
            # that is how its retry button works after a failure. An additive change must not
            # take from the old client what it could do. The tablet sends the key, and it has
            # no such power.
            if client_id is not None:
                job.forget_client(client_id)

                if job.live_slots():
                    continue

            self._remove(job)
            cancelled = True

        return cancelled

    def _remove(self, job: Job) -> None:
        """Remove and cancel one job. The neighbouring variant of the video stays."""
        self._jobs.pop((job.video_id, job.use_author_track), None)
        job.cancel()

    def stats(self) -> dict:
        return {
            "jobs": len(self._jobs),
            "stages": {v: sum(j.stage is v for j in self._jobs.values()) for v in Stage},
        }
