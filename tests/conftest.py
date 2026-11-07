"""Daemon test harness: a job registry on temporary paths and a client on top of the app."""

import asyncio
import atexit
import contextlib
import errno
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
from pathlib import Path

import httpx
import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

# The path override sits at module level and runs BEFORE any `import helper.*`.
# helper.env computes CACHE_DIR and CONFIG_DIR once, at import, and everything else
# is built on them already: JobManager() in its constructor wipes PAUSED_AUDIO_DIR (that is where the audio
# of paused jobs of the real daemon lives), TrackCache writes to the real
# index.sqlite and tracks/, and importing helper.app itself builds such a JobManager and reads
# the real config. Were the override one import late, the very first run would touch
# all of that.
#
# tmp_path_factory is no good here: it exists only inside fixtures, while conftest
# is imported before the test modules. For the same reason helper.* below is
# imported lazily, inside fixtures, and not at the top of the file.
#
# The assignment is unconditional: for a person whose daemon runs with these same variables,
# they are in the shell as well, and setdefault would leave the run on the real cache.
os.environ["YT_TRANS_CACHE"] = tempfile.mkdtemp(prefix="yt-trans-cache-")
os.environ["YT_TRANS_CONFIG"] = tempfile.mkdtemp(prefix="yt-trans-config-")

# /tmp on Fedora is tmpfs, that is memory: over hundreds of runs the directories would pile up.
for _variable in ("YT_TRANS_CACHE", "YT_TRANS_CONFIG"):
    atexit.register(shutil.rmtree, os.environ[_variable], ignore_errors=True)


@pytest.fixture
def manager(monkeypatch):
    """A job registry on temporary paths that does not start the pipeline.

    Guarantees the registry and endpoint tests rely on:
      * the cache and config paths are temporary (see the top of the file);
      * Job.start and Job.resume start nothing but append video_id
        to manager.started — a plain list[str] in call order;
      * helper.app.manager — the same object the test received.
    """
    from helper import app as helper_app
    from helper import jobs
    from helper.config import AsrConfig, Config, TranslatorConfig

    started: list[str] = []

    # The real start and resume call asyncio.create_task(runner.run(job)). In an ordinary
    # test that is "no running event loop", and in an async test it is starting the real pipeline:
    # yt-dlp would go to YouTube, and the GPU lease would be taken from the real card. The tests
    # check the logic of the registry and the states, not the pipeline, so both methods only
    # record that they were called: resuming a paused job appends
    # its video_id to started once more, on a par with the first start.
    def record(job, runner) -> None:
        started.append(job.video_id)

    monkeypatch.setattr(jobs.Job, "start", record)
    monkeypatch.setattr(jobs.Job, "resume", record)

    # The configuration is built in memory from defaults: load() would go read config.toml.
    registry = jobs.JobManager(config=Config(asr=AsrConfig(), translator=TranslatorConfig()))
    registry.started = started

    # The app's handlers read the module-level helper.app.manager — a separate
    # JobManager built at import. We replace it with the same object we give to
    # the test: otherwise a request through client and a direct manager call would go to two registries.
    monkeypatch.setattr(helper_app, "manager", registry)

    yield registry

    # TrackCache has no close(), and only the garbage collector would close the sqlite connection —
    # with a ResourceWarning on Python 3.13+, which under -W error would crash
    # on a random neighbouring test where the collection happens to run.
    registry.cache._db.close()


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock that stands still until the test calls advance(seconds).

    The override is at the module level of time: jobs.py reads time.monotonic() through the module.
    For ordinary tests only, not async: the event loop runs on these same clocks, and a standing
    clock would stop its timers.

    The count starts from a whole number, not from the real clock reading. The age of a record is
    the difference of two floats, and "start plus term minus start" is not always exactly the term:
    a sum that crossed a power of two is rounded. Such noise would make the "exactly
    PLAYHEAD_TTL" boundary check random.
    """
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def advance(seconds: float) -> None:
        now[0] += seconds

    return advance


@pytest.fixture
def audio_dir(manager):
    """The paused audio directory: clean at the start of the test, removed at its end.

    After manager, not before: JobManager() wipes this directory when created.
    It must be cleaned for gigabyte sparse files too: they take no space, but
    they would stay in the cache directory until the end of the run.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    yield PAUSED_AUDIO_DIR

    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)


@pytest.fixture
def refuse_in_audio_dir(monkeypatch):
    """The refuse(method) factory: a Path method inside the pause audio directory refuses on permissions.

    The refusal is the same as that of a real directory without permissions: PermissionError with the path name.
    Permissions on the directory itself are no good for the test: under root nobody checks them, and the test
    would pass where it checked nothing. Paths outside the directory work as usual, so that
    the override touches neither the rest of the daemon nor pytest. The override lasts until the end of the test.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    def refuse(method: str) -> None:
        original = getattr(Path, method)

        def refusing(self, *args, **kwargs):
            if PAUSED_AUDIO_DIR in (self, self.parent):
                raise PermissionError(errno.EACCES, "Permission denied", str(self))

            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, refusing)

    return refuse


@pytest.fixture
async def client(manager):
    """An HTTP client on top of the real app. No token: the daemon is open to whoever Origin lets in."""
    from helper import app as helper_app

    # manager is in the arguments not for its value but for the order: the helper.app.manager
    # override must be in place before the first request.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app),
        base_url="http://test",
    ) as http:
        yield http


# The wait limit in socket tests. Everything runs in one event loop and without the network, so
# normally an answer comes within fractions of a millisecond. The seconds are needed only so that
# a broken test fails with TimeoutError rather than hanging.
SOCKET_TIMEOUT = 2.0


class FakeSocket:
    """A WebSocket stand-in for helper.app.websocket: exactly what the handler uses.

    TestClient.websocket_connect is no good here for two reasons. The testclient module
    prints a StarletteDeprecationWarning on import, once per process: a filter on
    individual tests would catch it only for whoever imports first. And it runs
    the app in an anyio portal thread, while the test needs to put events straight into the jobs
    (job._emit) and see what reached the socket: an asyncio.Queue from a foreign thread is not
    woken, and the reader would wake at an unknown time. Here the handler and the test live
    in one loop, and the order of events is defined.

    receive_json, send_json and accept are what the handler calls on a real socket.
    say, hear and disconnect are the client side, for the test.
    """

    def __init__(self) -> None:
        # The handler's task: the connection fixture sets it, and disconnect waits for it.
        self.handler: asyncio.Task | None = None
        # Without Origin: check_origin lets such requests, like curl's, through — the daemon has no
        # other exceptions, the token was removed.
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        # What the client said to the daemon; None is a broken connection.
        self._incoming: asyncio.Queue = asyncio.Queue()
        # What the daemon sent to the client, strictly in order.
        self._outgoing: asyncio.Queue = asyncio.Queue()
        # The handler has parsed everything that was said and is waiting for the next message.
        self._idle = asyncio.Event()
        # What the handler did with the connection: accepted it or closed it, and with what code.
        self.accepted = False
        self.closed: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def receive_json(self) -> dict:
        # The queue is empty: everything the client said has been parsed, and the handler is waiting.
        # True as long as the handler does not wait on anything external between parsing a message
        # and the next receive_json: if there were an await on something external there, say would return
        # before parsing was finished.
        if self._incoming.empty():
            self._idle.set()

        message = await self._incoming.get()

        if message is None:
            raise WebSocketDisconnect()

        return message

    async def send_json(self, event: dict) -> None:
        self._outgoing.put_nowait(event)

    async def say(self, message: dict) -> None:
        """The client speaks to the daemon. Returns when the daemon has parsed what was said."""
        self._idle.clear()
        self._incoming.put_nowait(message)
        await asyncio.wait_for(self._idle.wait(), SOCKET_TIMEOUT)

    async def hear(self) -> dict:
        """The next event the daemon sent to the client. The order is the queue's."""
        return await asyncio.wait_for(self._outgoing.get(), SOCKET_TIMEOUT)

    async def disconnect(self) -> None:
        """A disconnect, like a closed tab. Returns when the handler has finished.

        The handler exits cleanly, stops the pump and removes the subscriptions. It can be called
        again: the wait does not touch a finished task.
        """
        self._incoming.put_nowait(None)
        await asyncio.wait_for(self.handler, SOCKET_TIMEOUT)


@pytest.fixture
async def connection(manager):
    """A WebSocket client connected to the real /v1/ws handler without the network."""
    from helper import app as helper_app

    # manager is in the arguments for the order, as with client: the helper.app.manager
    # override must be in place before the first message.
    socket = FakeSocket()
    socket.handler = asyncio.create_task(helper_app.websocket(socket))

    yield socket

    # Without the wait the handler's task would outlive the test.
    await socket.disconnect()


# ── the pipeline on fakes ────────────────────────────────────────────────


def metadata(**overrides):
    """VideoInfo of the video abcdefghijk: all fields at defaults, overrides — fields by name.

    An ordinary function, not a fixture: tests take it with `from conftest import metadata`.
    manual_tracks accepts strings too: "sr" is CaptionTrack(lang="sr", url="",
    ext="json3", name=""); a ready CaptionTrack goes as is.
    """
    from helper.sources.ytdlp import CaptionTrack, VideoInfo

    tracks = [
        track
        if isinstance(track, CaptionTrack)
        else CaptionTrack(lang=track, url="", ext="json3", name="")
        for track in overrides.pop("manual_tracks", [])
    ]
    fields = {
        "video_id": "abcdefghijk",
        "title": "Naslov",
        "description": "",
        "channel": "Kanal",
        "tags": [],
        "chapters": [],
        "duration": 60.0,
        "audio_language": None,
        "live_status": None,
        "has_automatic_captions": False,
    }

    return VideoInfo(**{**fields, **overrides}, manual_tracks=tracks)


def watch(video_id, **fields):
    """The watch message from a tab: a subscription to the video's job."""
    return {"op": "watch", "videoId": video_id, **fields}


async def hear_until_done(connection):
    """Everything the socket heard until the end of the job: up to the state event with a done or error stage."""
    heard = []

    while True:
        event = await connection.hear()
        heard.append(event)

        if event["type"] == "state" and event["stage"] in ("done", "error"):
            return heard


def preempt_after_asr(pipeline, monkeypatch):
    """The card is taken away right after recognition: the first preemption check after the worker."""
    from helper import gpulease

    real_check = gpulease._Holder.check
    taken = []

    def check(holder):
        if pipeline.workers and not taken:
            taken.append(holder)

            raise gpulease.Preempted("пользователь начал свою работу на GPU")

        real_check(holder)

    monkeypatch.setattr(gpulease._Holder, "check", check)


class FakeLease:
    """A card lease without NVML and ollama: records calls and hands out a real _Holder.

    calls in order: ("claim", model), ("unload", model), ("acquire", MiB, model),
    ("release", MiB). holders — the handles handed out; holders[-1].preempt() is what the
    watchman would have done on seeing a user on the card.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.holders: list = []

    def claim_model(self, name: str) -> None:
        self.calls.append(("claim", name))

    def unload(self, model: str) -> None:
        self.calls.append(("unload", model))

    @contextlib.asynccontextmanager
    async def acquire(self, need_mib: int, *, model: str | None = None, wait: bool = True):
        from helper.gpulease import _Holder

        holder = _Holder()
        self.calls.append(("acquire", need_mib, model))
        self.holders.append(holder)

        try:
            yield holder
        finally:
            self.calls.append(("release", need_mib))


class RecordingCache:
    """The real TrackCache that remembers keys: which ones were looked up (gets) and written (puts).

    It writes for real, into the test's directory, and variants() reads what was written from disk. Everything
    except get and put goes to it as is.
    """

    def __init__(self, store) -> None:
        self.store = store
        self.gets: list = []
        self.puts: list = []

    def get(self, key, length_seconds=None):
        self.gets.append(key)

        return self.store.get(key, length_seconds)

    def put(self, key, cues, length_seconds, **kwargs) -> None:
        self.puts.append(key)
        self.store.put(key, cues, length_seconds, **kwargs)

    def __getattr__(self, name):
        return getattr(self.store, name)


class FakeWorker:
    """subprocess.Popen for `python -m helper.asr.worker`: remembers the call, answers with a scenario.

    args — the whole argv, as JobRunner._run_asr assembled it; options — the flags after
    `-m helper.asr.worker` in pairs; input — the audio bytes from communicate; killed — kill() was called.
    The answer is taken from the pipeline at communicate time: stdout is asr_result (a dict goes as
    JSON, bytes as is), the exit code is asr_exit, stderr is asr_stderr. A killed one
    answers with code -9 and empty output, as after SIGKILL.

    gate holds communicate, like a long Whisper, and kill() releases it. communicate
    runs in a worker thread (asyncio.to_thread), so gate is a threading.Event, not asyncio.
    """

    def __init__(self, rig, args, **kwargs) -> None:
        self.rig = rig
        self.args = list(args)
        self.input: bytes | None = None
        self.returncode: int | None = None
        self.killed = False
        rig.workers.append(self)
        rig.calls.append(("asr", self.options))
        rig.worker_started.set()

    @property
    def options(self) -> dict[str, str]:
        flags = self.args[3:]

        return dict(zip(flags[::2], flags[1::2]))

    def communicate(self, input=None, timeout=None):
        self.input = input

        if self.rig.gate is not None:
            self.rig.gate.wait(5)

        if self.killed:
            self.returncode = -9

            return b"", b""

        self.returncode = self.rig.asr_exit
        out = self.rig.asr_result

        if isinstance(out, dict):
            out = json.dumps(out, ensure_ascii=False).encode()

        return out, self.rig.asr_stderr

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True

        if self.rig.gate is not None:
            self.rig.gate.set()


class Pipeline:
    """The real JobRunner on fakes: no network, card or ollama, the pipeline is its own.

    Scenario (the test sets it before run):
      info        — the VideoInfo probe returns; metadata() by default;
      audio       — audio from audio_pcm: two seconds of zeros, float32, 16 kHz;
      track       — the author track's cues from fetch_manual_track, or the exception it
                    will raise (YtDlpError sends the pipeline to recognition);
      asr_result  — the worker's stdout: a dict goes as JSON, bytes as is;
      asr_exit    — the worker's exit code (0); asr_stderr — its stderr (b"");
      gate        — threading.Event or None: holds the worker until set() or kill().
    Records (the test reads them after):
      calls          — outside calls in order: ("probe", id), ("track", id, language),
                       ("audio", id), ("asr", worker flags), ("translate", cues);
      workers        — started FakeWorker; worker_started — Popen has been called;
      lease          — FakeLease: calls and holders;
      cache          — RecordingCache: gets and puts (CacheKey), variants(id) from disk;
      runner, config — the runner and its config; configure() rebuilds them.
    """

    def __init__(self, manager, cache) -> None:
        self.manager = manager
        self.cache = cache
        self.lease = FakeLease()
        self.calls: list[tuple] = []
        self.workers: list[FakeWorker] = []
        self.worker_started = threading.Event()
        self.gate: threading.Event | None = None
        self.info = metadata()
        self.audio = np.zeros(2 * 16000, dtype="<f4")
        self.track = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima.", "words": []}]
        self.asr_result: dict | bytes = {
            "language": "sr",
            "duration": 2.0,
            "model": "large-v3",
            "cues": [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}],
        }
        self.asr_exit = 0
        self.asr_stderr = b""
        self.configure()

    def configure(self, **asr) -> None:
        """A runner on Config(asr=AsrConfig(**asr)); the registry gets the same Config and runner.

        The lease record starts over: right after the call lease.calls holds only
        the new runner's ("claim", translator model).
        """
        from helper import jobs
        from helper.config import AsrConfig, Config, TranslatorConfig

        self.config = Config(asr=AsrConfig(**asr), translator=TranslatorConfig())
        self.lease.calls.clear()
        self.runner = jobs.JobRunner(self.lease, self.cache, self.config)
        # restore computes the key from manager.config: the same config as the runner's.
        self.manager.config = self.config
        self.manager.runner = self.runner

    def names(self) -> list[str]:
        """The names of outside calls in order: ["probe", "audio", "asr", "translate"]."""
        return [call[0] for call in self.calls]

    async def run(self, job) -> None:
        """The real JobRunner.run(job): _pipeline with all its branches and refusals."""
        await self.runner.run(job)


@pytest.fixture
def pipeline(manager, monkeypatch):
    """JobRunner on fakes: the real pipeline without network, card or ollama. See Pipeline."""
    from helper import cache as track_cache
    from helper import env, jobs

    # Each test has its own cache. The run's shared cache would fill up with ready tracks, and
    # restore in other tests would find them. CACHE_DIR is overridden before TrackCache(): the
    # index.sqlite index is opened in the constructor.
    base = Path(tempfile.mkdtemp(prefix="pipeline-", dir=env.CACHE_DIR))
    monkeypatch.setattr(track_cache, "CACHE_DIR", base)
    rig = Pipeline(manager, RecordingCache(track_cache.TrackCache(root=base / "tracks")))
    # By plain assignment, not monkeypatch: its rollback would come after
    # manager closes its own cache's index, and it would close this one instead.
    shared = manager.cache
    manager.cache = rig.cache

    def probe(video_id, *, cookies=None):
        rig.calls.append(("probe", video_id))

        return rig.info

    def audio_pcm(video_id, *, cookies=None):
        rig.calls.append(("audio", video_id))

        return rig.audio

    def fetch_manual_track(video_id, lang, *, cookies=None):
        rig.calls.append(("track", video_id, lang))

        if isinstance(rig.track, BaseException):
            raise rig.track

        return rig.track

    def translate_dual(batch, model, **kwargs):
        rig.calls.append(("translate", list(batch.cues)))

        return [(f"cnr {cue}", f"ru {cue}") for cue in batch.cues]

    def translate_batch(batch, model, **kwargs):
        rig.calls.append(("translate", list(batch.cues)))

        return [f"ru {cue}" for cue in batch.cues]

    monkeypatch.setattr(jobs, "probe", probe)
    monkeypatch.setattr(jobs, "audio_pcm", audio_pcm)
    monkeypatch.setattr(jobs, "fetch_manual_track", fetch_manual_track)
    monkeypatch.setattr(jobs, "translate_dual", translate_dual)
    monkeypatch.setattr(jobs, "translate_batch", translate_batch)
    monkeypatch.setattr(jobs, "FETCH_BACKOFF", (0.0, 0.0))
    # Only helper.jobs's subprocess is overridden: yt-dlp and asyncio see the real one.
    monkeypatch.setattr(
        jobs,
        "subprocess",
        types.SimpleNamespace(
            Popen=lambda args, **kwargs: FakeWorker(rig, args, **kwargs),
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    yield rig

    manager.cache = shared
    rig.cache.store._db.close()
    shutil.rmtree(base, ignore_errors=True)


# ── the worker on a faster_whisper fake ──────────────────────────────────


class FakeWhisperModel:
    """WhisperModel without a card: remembers what it was built with, and always hears Serbian."""

    def __init__(self, name, device, compute_type) -> None:
        self.name = name
        self.device = device
        self.compute_type = compute_type

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        # Since task 5 the detector runs on every transcribe. The answer is the real one's:
        # the best language, its share and the whole distribution. Task 5 checks windows and voices
        # with its own tests, on its own model stub.
        return "sr", 1.0, [("sr", 1.0)]


class FakeBatchedPipeline:
    """BatchedInferencePipeline without a card: remembers the arguments, returns the scenario's segments."""

    def __init__(self, whisper, model) -> None:
        self.whisper = whisper
        self.model = model

    def transcribe(self, audio, **kwargs):
        from faster_whisper.transcribe import TranscriptionInfo

        self.whisper.kwargs = kwargs
        seconds = len(audio) / 16000
        info = TranscriptionInfo(
            language=kwargs.get("language"),
            language_probability=1.0,
            duration=seconds,
            duration_after_vad=seconds,
            all_language_probs=None,
            transcription_options=None,
            vad_options=None,
        )

        return iter(self.whisper.segments), info


class FakeWhisper:
    """faster_whisper without a card for worker tests: transcribe and _detect are the real ones.

    Scenario (set by the test):
      segments — what BatchedInferencePipeline.transcribe returns; build with segment().
    Records (read by the test):
      model  — the last model the worker built: name, device, compute_type;
      kwargs — the keyword arguments of the last BatchedInferencePipeline.transcribe.
    VAD is not overridden: faster_whisper.vad.get_speech_timestamps is the real Silero, on
    the CPU and without the network; it finds no speech in zeros.
    """

    def __init__(self) -> None:
        self.segments: list = []
        self.model: FakeWhisperModel | None = None
        self.kwargs: dict | None = None

    def build_model(self, model_size_or_path, device="auto", compute_type="default", **kwargs):
        self.model = FakeWhisperModel(model_size_or_path, device, compute_type)

        return self.model

    def build_pipeline(self, model):
        return FakeBatchedPipeline(self, model)

    @staticmethod
    def segment(
        start,
        end,
        text,
        *,
        no_speech_prob=0.1,
        avg_logprob=-0.3,
        compression_ratio=1.5,
        words=(),
    ):
        """The real faster_whisper.transcribe.Segment: the library itself checks the field names.

        words — faster_whisper.transcribe.Word, as word_timestamps=True returns them.
        """
        from faster_whisper.transcribe import Segment

        return Segment(
            id=0,
            seek=0,
            start=start,
            end=end,
            text=text,
            tokens=[],
            avg_logprob=avg_logprob,
            compression_ratio=compression_ratio,
            no_speech_prob=no_speech_prob,
            words=list(words),
            temperature=0.0,
        )


@pytest.fixture
def whisper(monkeypatch):
    """A faster_whisper fake for worker tests, see FakeWhisper. No card needed."""
    # helper.env first, as in the worker itself: it points HF_HOME at a temporary cache before
    # faster_whisper pulls huggingface_hub.
    import helper.env  # noqa: F401

    import faster_whisper

    fake = FakeWhisper()
    # transcribe imports both classes inside itself, on every call: the override in the
    # faster_whisper module reaches it. VAD stays real.
    monkeypatch.setattr(faster_whisper, "WhisperModel", fake.build_model)
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", fake.build_pipeline)

    return fake
