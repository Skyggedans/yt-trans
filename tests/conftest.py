"""Оснастка тестов демона: реестр заданий на временных путях и клиент поверх приложения."""

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

# Подмена путей стоит на уровне модуля и выполняется ДО любого `import helper.*`.
# helper.env считает CACHE_DIR и CONFIG_DIR один раз, при импорте, а всё остальное
# строится уже на них: JobManager() в конструкторе сносит PAUSED_AUDIO_DIR (там звук
# приостановленных заданий настоящего демона), TrackCache пишет в настоящие
# index.sqlite и tracks/, а импорт helper.app сам строит такой JobManager и читает
# настоящий токен установки. Опоздай подмена на один импорт — первый же прогон
# тронул бы всё это.
#
# tmp_path_factory здесь не годится: он существует только внутри фикстур, а conftest
# импортируется раньше тестовых модулей. По той же причине helper.* ниже
# импортируется лениво, внутри фикстур, а не наверху файла.
#
# Присваивание безусловное: у человека, чей демон запущен с этими же переменными,
# они есть и в оболочке, и setdefault оставил бы прогон на настоящем кэше.
os.environ["YT_TRANS_CACHE"] = tempfile.mkdtemp(prefix="yt-trans-cache-")
os.environ["YT_TRANS_CONFIG"] = tempfile.mkdtemp(prefix="yt-trans-config-")

# /tmp в Fedora — tmpfs, то есть память: за сотни прогонов каталоги накопились бы.
for _variable in ("YT_TRANS_CACHE", "YT_TRANS_CONFIG"):
    atexit.register(shutil.rmtree, os.environ[_variable], ignore_errors=True)


@pytest.fixture
def manager(monkeypatch):
    """Реестр заданий на временных путях, который не запускает конвейер.

    Гарантии, на которые опираются тесты реестра и эндпоинтов:
      * пути кэша и конфига временные (см. начало файла);
      * Job.start и Job.resume ничего не запускают, а дописывают video_id
        в manager.started — обычный list[str] в порядке вызовов;
      * helper.app.manager — тот же объект, что получил тест.
    """
    from helper import app as helper_app
    from helper import jobs
    from helper.config import AsrConfig, Config, TranslatorConfig

    started: list[str] = []

    # Настоящие start и resume делают asyncio.create_task(runner.run(job)). В обычном
    # тесте это «no running event loop», а в async-тесте — запуск настоящего конвейера:
    # yt-dlp пошёл бы на YouTube, а пропуск на GPU взялся бы у настоящей карты. Тесты
    # проверяют логику реестра и состояний, не конвейер, поэтому оба метода лишь
    # записывают, что их позвали: возобновление приостановленного задания дописывает
    # его video_id в started ещё раз, наравне с первым запуском.
    def record(job, runner) -> None:
        started.append(job.video_id)

    monkeypatch.setattr(jobs.Job, "start", record)
    monkeypatch.setattr(jobs.Job, "resume", record)

    # Конфигурация собрана в памяти из умолчаний: load() полез бы читать config.toml.
    registry = jobs.JobManager(config=Config(asr=AsrConfig(), translator=TranslatorConfig()))
    registry.started = started

    # Обработчики приложения читают модульный helper.app.manager — отдельный
    # JobManager, собранный при импорте. Подменяем его тем же объектом, что отдаём
    # тесту: иначе запрос через client и прямой вызов manager шли бы в два реестра.
    monkeypatch.setattr(helper_app, "manager", registry)

    yield registry

    # У TrackCache нет close(), и соединение sqlite закрыл бы только сборщик мусора —
    # с ResourceWarning на Python 3.13+, который при запуске с -W error падал бы
    # на случайном соседнем тесте, где как раз сработает сборка.
    registry.cache._db.close()


@pytest.fixture
def clock(monkeypatch):
    """Монотонные часы, что стоят на месте, пока тест не позовёт advance(секунды).

    Подмена на уровне модуля time: jobs.py читает time.monotonic() через модуль.
    Только для обычных тестов, не async: по этим же часам ходит цикл событий, и стоящие
    часы остановили бы его таймеры.

    Отсчёт идёт от целого числа, а не от настоящего показания часов. Возраст записи —
    разность двух вещественных, и «начало плюс срок минус начало» не всегда ровно срок:
    сумма, перешагнувшая степень двойки, округляется. Проверку границы «ровно
    PLAYHEAD_TTL» такой шум сделал бы случайной.
    """
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def advance(seconds: float) -> None:
        now[0] += seconds

    return advance


@pytest.fixture
def audio_dir(manager):
    """Каталог отложенного звука: к началу теста чистый, по окончании убранный.

    После manager, а не раньше: JobManager() при создании сносит этот каталог.
    Убирать надо и ради разреженных файлов на гигабайты: места они не занимают, но
    остались бы в каталоге кэша до конца прогона.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    yield PAUSED_AUDIO_DIR

    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)


@pytest.fixture
def refuse_in_audio_dir(monkeypatch):
    """Фабрика refuse(method): метод Path внутри каталога звука пауз отказывает правами.

    Отказ такой же, как у настоящего каталога без прав: PermissionError с именем пути.
    Права на сам каталог тесту не годятся: под root их не проверяет никто, и тест
    проходил бы там, где ничего не проверил. Пути вне каталога работают как обычно, чтобы
    подмена не задела ни остальной демон, ни pytest. Подмена держится до конца теста.
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
    """HTTP-клиент поверх настоящего приложения, с действующим токеном установки."""
    from helper import app as helper_app

    # manager в аргументах не ради значения, а ради порядка: подмена
    # helper.app.manager должна быть на месте до первого запроса.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {helper_app.TOKEN}"},
    ) as http:
        yield http


# Предел ожидания в тестах сокета. Всё идёт в одном цикле событий и без сети, так что
# в норме ответ приходит за доли миллисекунды. Секунды нужны лишь затем, чтобы
# сломанный тест падал с TimeoutError, а не висел.
SOCKET_TIMEOUT = 2.0


class FakeSocket:
    """Подмена WebSocket для helper.app.websocket: ровно то, чем пользуется обработчик.

    TestClient.websocket_connect здесь не годится по двум причинам. Модуль testclient
    при импорте печатает StarletteDeprecationWarning, и один раз на процесс: фильтр на
    отдельных тестах поймал бы его только у того, кто импортирует первым. И он гоняет
    приложение в потоке портала anyio, а тесту нужно класть события прямо в задания
    (job._emit) и видеть, что дошло до сокета: asyncio.Queue из чужого потока не
    будят, и читатель проснулся бы неизвестно когда. Здесь обработчик и тест живут
    в одном цикле, и порядок событий определён.

    receive_json, send_json и accept — то, что обработчик зовёт у настоящего сокета.
    say, hear и disconnect — сторона клиента, для теста.
    """

    def __init__(self, token: str) -> None:
        # Задача обработчика: её ставит фикстура connection, а disconnect дожидается.
        self.handler: asyncio.Task | None = None
        # Без Origin: check_origin пускает такие запросы, как curl. Токен настоящий.
        self.headers: dict[str, str] = {}
        self.query_params = {"token": token}
        # Что клиент сказал демону; None — разрыв соединения.
        self._incoming: asyncio.Queue = asyncio.Queue()
        # Что демон отправил клиенту, строго по порядку.
        self._outgoing: asyncio.Queue = asyncio.Queue()
        # Обработчик разобрал всё сказанное и ждёт следующего сообщения.
        self._idle = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def receive_json(self) -> dict:
        # Очередь пуста: всё, что клиент сказал, уже разобрано, и обработчик ждёт.
        # Верно, пока между разбором сообщения и следующим receive_json обработчик
        # ни на чём не ждёт: появись там await на внешнем, say вернулся бы раньше,
        # чем разбор закончен.
        if self._incoming.empty():
            self._idle.set()

        message = await self._incoming.get()

        if message is None:
            raise WebSocketDisconnect()

        return message

    async def send_json(self, event: dict) -> None:
        self._outgoing.put_nowait(event)

    async def say(self, message: dict) -> None:
        """Клиент говорит демону. Возвращается, когда демон разобрал сказанное."""
        self._idle.clear()
        self._incoming.put_nowait(message)
        await asyncio.wait_for(self._idle.wait(), SOCKET_TIMEOUT)

    async def hear(self) -> dict:
        """Следующее событие, отправленное демоном клиенту. Порядок — как в очереди."""
        return await asyncio.wait_for(self._outgoing.get(), SOCKET_TIMEOUT)

    async def disconnect(self) -> None:
        """Разрыв, как у закрытой вкладки. Возвращается, когда обработчик закончил.

        Обработчик выходит штатно, глушит насос и снимает подписки. Можно звать
        повторно: готовую задачу ожидание не трогает.
        """
        self._incoming.put_nowait(None)
        await asyncio.wait_for(self.handler, SOCKET_TIMEOUT)


@pytest.fixture
async def connection(manager):
    """Клиент WebSocket, подключённый к настоящему обработчику /v1/ws без сети."""
    from helper import app as helper_app

    # manager в аргументах ради порядка, как у client: подмена helper.app.manager
    # должна стоять до первого сообщения.
    socket = FakeSocket(helper_app.TOKEN)
    socket.handler = asyncio.create_task(helper_app.websocket(socket))

    yield socket

    # Без ожидания задача обработчика пережила бы тест.
    await socket.disconnect()


# ── конвейер на подделках ────────────────────────────────────────────────


def metadata(**overrides):
    """VideoInfo ролика abcdefghijk: все поля по умолчанию, overrides — поля по имени.

    Обычная функция, а не фикстура: тесты берут её `from conftest import metadata`.
    manual_tracks принимает и строки: "sr" — это CaptionTrack(lang="sr", url="",
    ext="json3", name=""); готовый CaptionTrack идёт как есть.
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
    """Сообщение watch от вкладки: подписка на задание ролика."""
    return {"op": "watch", "videoId": video_id, **fields}


async def hear_until_done(connection):
    """Всё, что сокет услышал до конца задания: до события state со стадией done или error."""
    heard = []

    while True:
        event = await connection.hear()
        heard.append(event)

        if event["type"] == "state" and event["stage"] in ("done", "error"):
            return heard


def preempt_after_asr(pipeline, monkeypatch):
    """Карту отбирают сразу после распознавания: первая проверка вытеснения после воркера."""
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
    """Пропуск на карту без NVML и ollama: записывает обращения и выдаёт настоящий _Holder.

    calls по порядку: ("claim", модель), ("unload", модель), ("acquire", МиБ, модель),
    ("release", МиБ). holders — выданные ручки; holders[-1].preempt() — то, что сделал бы
    сторож, увидев пользователя на карте.
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
    """Настоящий TrackCache, который помнит ключи: под какими искали (gets) и писали (puts).

    Пишет по-настоящему, в каталог теста, и variants() читает записанное с диска. Всё,
    кроме get и put, уходит к нему как есть.
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
    """subprocess.Popen для `python -m helper.asr.worker`: запоминает вызов, отвечает сценарием.

    args — argv целиком, как его собрал JobRunner._run_asr; options — флаги после
    `-m helper.asr.worker` парами; input — байты звука из communicate; killed — звали kill().
    Ответ берётся у конвейера в момент communicate: stdout — asr_result (dict уходит
    JSON-ом, bytes — как есть), код выхода — asr_exit, stderr — asr_stderr. Убитый
    отвечает кодом -9 и пустым выводом, как после SIGKILL.

    gate держит communicate, как долгий Whisper, а kill() его отпускает. communicate
    идёт в рабочем потоке (asyncio.to_thread), поэтому gate — threading.Event, не asyncio.
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
    """Настоящий JobRunner на подделках: сети, карты и ollama нет, конвейер свой.

    Сценарий (тест задаёт до run):
      info        — VideoInfo, которую отдаёт probe; по умолчанию metadata();
      audio       — звук от audio_pcm: две секунды нулей, float32, 16 кГц;
      track       — реплики дорожки автора от fetch_manual_track или исключение, которое
                    она бросит (YtDlpError уводит конвейер в распознавание);
      asr_result  — stdout воркера: dict уходит JSON-ом, bytes — как есть;
      asr_exit    — код выхода воркера (0); asr_stderr — его stderr (b"");
      gate        — threading.Event или None: держит воркер до set() или kill().
    Записи (тест читает после):
      calls          — обращения наружу по порядку: ("probe", id), ("track", id, язык),
                       ("audio", id), ("asr", флаги воркера), ("translate", реплики);
      workers        — запущенные FakeWorker; worker_started — Popen уже позван;
      lease          — FakeLease: calls и holders;
      cache          — RecordingCache: gets и puts (CacheKey), variants(id) с диска;
      runner, config — бегун и его настройка; их пересобирает configure().
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
        """Бегун на Config(asr=AsrConfig(**asr)); тот же Config и бегун получает реестр.

        Запись пропуска начинается заново: сразу после вызова в lease.calls только
        ("claim", модель переводчика) нового бегуна.
        """
        from helper import jobs
        from helper.config import AsrConfig, Config, TranslatorConfig

        self.config = Config(asr=AsrConfig(**asr), translator=TranslatorConfig())
        self.lease.calls.clear()
        self.runner = jobs.JobRunner(self.lease, self.cache, self.config)
        # restore считает ключ по manager.config: та же настройка, что у бегуна.
        self.manager.config = self.config
        self.manager.runner = self.runner

    def names(self) -> list[str]:
        """Имена обращений наружу по порядку: ["probe", "audio", "asr", "translate"]."""
        return [call[0] for call in self.calls]

    async def run(self, job) -> None:
        """Настоящий JobRunner.run(job): _pipeline со всеми его ветками и отказами."""
        await self.runner.run(job)


@pytest.fixture
def pipeline(manager, monkeypatch):
    """JobRunner на подделках: настоящий конвейер без сети, карты и ollama. См. Pipeline."""
    from helper import cache as track_cache
    from helper import env, jobs

    # Свой кэш у каждого теста. Общий кэш прогона наполнился бы готовыми дорожками, и
    # restore в чужих тестах их находил бы. CACHE_DIR подменён до TrackCache(): индекс
    # index.sqlite открывается в конструкторе.
    base = Path(tempfile.mkdtemp(prefix="pipeline-", dir=env.CACHE_DIR))
    monkeypatch.setattr(track_cache, "CACHE_DIR", base)
    rig = Pipeline(manager, RecordingCache(track_cache.TrackCache(root=base / "tracks")))
    # Простым присваиванием, не monkeypatch: его откат пришёл бы уже после того, как
    # manager закроет индекс своего кэша, и закрыл бы он тогда этот.
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
    # Подменён только subprocess модуля helper.jobs: yt-dlp и asyncio видят настоящий.
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


# ── воркер на подделке faster_whisper ────────────────────────────────────


class FakeWhisperModel:
    """WhisperModel без карты: помнит, с чем её построили, и всегда слышит сербский."""

    def __init__(self, name, device, compute_type) -> None:
        self.name = name
        self.device = device
        self.compute_type = compute_type

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        # С задачи 5 определитель работает при каждом transcribe. Ответ — как у настоящей:
        # лучший язык, его доля и всё распределение. Окна и голоса проверяет задача 5
        # своими тестами, на своей заглушке модели.
        return "sr", 1.0, [("sr", 1.0)]


class FakeBatchedPipeline:
    """BatchedInferencePipeline без карты: запоминает аргументы, отдаёт сегменты сценария."""

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
    """faster_whisper без карты для тестов воркера: transcribe и _detect настоящие.

    Сценарий (задаёт тест):
      segments — что вернёт BatchedInferencePipeline.transcribe; строить через segment().
    Записи (читает тест):
      model  — последняя модель, что построил воркер: name, device, compute_type;
      kwargs — именованные аргументы последнего BatchedInferencePipeline.transcribe.
    VAD не подменён: faster_whisper.vad.get_speech_timestamps — настоящий Silero, на
    процессоре и без сети; в нулях речи он не находит.
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
        """Настоящий faster_whisper.transcribe.Segment: имена полей проверяет сама библиотека.

        words — faster_whisper.transcribe.Word, как их отдаёт word_timestamps=True.
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
    """Подделка faster_whisper для тестов воркера, см. FakeWhisper. Карта не нужна."""
    # Сначала helper.env, как в самом воркере: он ставит HF_HOME во временный кэш до того,
    # как faster_whisper потянет huggingface_hub.
    import helper.env  # noqa: F401

    import faster_whisper

    fake = FakeWhisper()
    # transcribe импортирует оба класса внутри себя, при каждом вызове: подмена в модуле
    # faster_whisper до него доходит. VAD остаётся настоящим.
    monkeypatch.setattr(faster_whisper, "WhisperModel", fake.build_model)
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", fake.build_pipeline)

    return fake
