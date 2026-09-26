"""Задания: от идентификатора ролика до русских реплик в браузере.

Порядок стадий не произволен. Нормализация письменности идёт до сегментации, потому
что Whisper колеблется между алфавитами внутри одной записи. Сегментация идёт до
перевода, потому что Whisper отдаёт тридцатисекундные окна по ~300 знаков: их нельзя
ни показать зрителю, ни отправить в модель — батч таких окон не помещается в ответ,
и грамматика не даёт закрыть JSON, теряя весь батч целиком.

Реплики вокруг плейхеда переводятся первыми. На медианном двадцатиминутном ролике
получается около 290 реплик и примерно 75 секунд перевода: ждать их целиком, глядя
в пустой экран, незачем, когда зритель смотрит одно конкретное место.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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

from helper.asr.worker import SAMPLE_RATE
from helper.cache import CacheKey, TrackCache
from helper.config import Config, load as load_config
from helper.env import YTDLP_CACHE
from helper.gpulease import GpuLease, Preempted
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

log = logging.getLogger(__name__)

BATCH_SIZE = 40
CONTEXT_CUES = 3

# Сбои, после которых имеет смысл дробить батч и пробовать снова: и отказ модели,
# и битый JSON, и обрыв связи с ollama лечатся одинаково. Ошибка вытеснения сюда
# намеренно НЕ входит — она должна пройти насквозь и приостановить задание.
RETRYABLE = (TranslateError, json.JSONDecodeError, httpx.HTTPError)

# Предел длины ролика. Аудио декодируется в память целиком: час записи в 16 кГц
# float32 — это 230 МБ, и ещё столько же на копию в tobytes() при передаче воркеру.
# Медиана корпуса 20 минут, самый длинный найденный ролик 78 минут.
MAX_VIDEO_SECONDS = 3 * 3600

# Сколько завершённых заданий держать в памяти. Реплики двадцатиминутного ролика —
# это сотни объектов, и без вытеснения реестр рос бы всю жизнь процесса.
MAX_RETAINED_JOBS = 12

# Длина журнала событий. Реплики дублируются в job.cues, поэтому отставшему
# клиенту дешевле отдать снимок, чем хранить всю историю ради него одного.
MAX_EVENT_LOG = 2000

# Повторы метаданных и звука внутри одного задания — только для временных сбоев
# (YtDlpError.transient). Паузы растут: первый повтор ловит разовый 403 от
# googlevideo, второй — моргнувшую сеть, которой нужно время подняться.
FETCH_ATTEMPTS = 3
FETCH_BACKOFF = (5.0, 20.0)

# Сколько секунд после начала загрузки звука ещё можно начать новую попытку.
# Звук качается под пропуском на карту, а пропуск один на весь демон: пока
# идут повторы, чужие фазы на карте ждут. Обычная загрузка укладывается
# в секунды, и разовый 403 бюджет пропускает с запасом, а затяжной обрыв
# сети пусть лучше провалит задание — повторяемо, — чем держит всех.
FETCH_RETRY_BUDGET = 90.0

# Через сколько секунд после отказа опрос может начать задание заново. Только
# для повторяемых отказов — см. JobManager.get_or_create.
RETRY_COOLDOWN = 60.0

# Звук задания, которое карту отдало уже после загрузки. На диске и рядом с кэшем,
# а не в /tmp: в Fedora /tmp — это tmpfs, то есть та же память, а держать в ней
# десятки и сотни мегабайт на приостановленный ролик и нужно было избежать.
PAUSED_AUDIO_DIR = YTDLP_CACHE.parent / "paused-audio"

# Сколько хранить отложенный звук паузы, к которой никто не обращается.
# Открытая вкладка опрашивает демон каждые двадцать пять секунд и каждый раз
# продлевает срок; полчаса тишины значат, что вкладку закрыли или ушли на другой
# ролик. Задание при этом остаётся — пропадает только звук, и если к нему всё же
# вернутся, возобновление скачает его заново.
PAUSED_AUDIO_TTL = 30 * 60.0

# Потолок на распознавание одного ролика. С запасом: самый длинный допустимый
# ролик (MAX_VIDEO_SECONDS) large-v3 на карте проходит за минуты.
ASR_TIMEOUT = 3600.0

# Сколько видеопамяти просить под каждую фазу, берётся из конфигурации вместе
# с именем модели: см. helper/config.py. Цифра не может жить отдельно от модели —
# у turbo расход втрое меньше, чем у large-v3, и постоянная здесь означала бы,
# что контроль допуска врёт при любой смене модели.


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
    """Отказ, который не пройдёт сам: эфир, слишком длинный ролик.

    Наследник RuntimeError, чтобы любой обработчик, ловящий RuntimeError, ловил
    и его. Отдельный тип нужен ровно для одного — задание с таким отказом
    не пересоздаётся опросом, сколько бы времени ни прошло.
    """


@dataclass
class JobCue:
    index: int
    start: float
    end: float
    src: str
    lines: list[str] = field(default_factory=list)
    # Вторая строка для изучения языка: черногорский перевод той же реплики.
    # Пусто, если режим выключен.
    study: str = ""
    # Перевод не удался ни батчем, ни половинами, ни поштучно: на экран уходит
    # исходный сербский. Расширение показывает такие реплики иначе, чтобы зритель
    # видел, что это не перевод.
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


class Job:
    """Одно задание на один ролик."""

    def __init__(
        self, video_id: str, target_lang: str = "ru", use_author_track: bool = True
    ) -> None:
        self.video_id = video_id
        self.target_lang = target_lang
        # Брать ли готовую дорожку автора вместо распознавания. Выключается
        # из настроек расширения: дорожка бывает не транскриптом, а вольным
        # пересказом или переводом, и тогда своё распознавание точнее.
        self.use_author_track = use_author_track
        self.stage = Stage.QUEUED
        # Чего задание ждёт в очереди: "gpu" — видеокарту под распознавание.
        self.waiting: str | None = None
        self.error: str | None = None
        # Может ли отказ пройти сам: сеть, 403, ограничение частоты — да; эфир,
        # удалённый ролик — нет. От этого зависит, предложит ли расширение
        # повтор и пересоздаст ли задание опрос. Время отказа монотонное:
        # остывание не должно зависеть от перевода системных часов.
        self.retryable = False
        self.failed_at: float | None = None
        self.cues: list[JobCue] = []
        self.info: VideoInfo | None = None
        self.playhead = 0.0
        # Откуда взяты реплики: 'asr' или 'manual:<язык>'. Входит в ключ кэша —
        # дорожка автора и результат распознавания это разные дорожки.
        self.source = "asr"
        # Дорожка автора есть в метаданных, но не скачалась. Запоминаем, чтобы
        # возобновление после паузы не обращалось за ней к YouTube снова: пока
        # карта занята, пауза повторяется каждые две минуты.
        self.track_failed = False
        self.created_at = time.time()

        # Журнал событий с курсором. Нужен для длинных опросов: клиент говорит,
        # что видел до позиции N, и получает всё, что появилось после. Очередей
        # на подписчика для этого мало — они одноразовые, а опрос переподключается
        # после каждого ответа и должен продолжать ровно с того места.
        self.events: list[dict] = []
        # Сколько событий уже выброшено из начала журнала. Курсор у клиента
        # абсолютный, а обрезка сдвигает индексы списка — без этого счётчика
        # после переполнения журнала курсор указывал бы не туда.
        self._dropped = 0
        self._wakeup = asyncio.Event()

        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        # Ручка на процесс распознавания. Без неё его нечем убить: subprocess.run
        # внутри asyncio.to_thread не прерывается отменой задачи, поэтому Whisper
        # пережил бы и отмену, и вытеснение, продолжая держать ~6 ГБ уже без сторожа.
        self._asr_proc: subprocess.Popen | None = None
        # Просьба убить распознавание, которая переживает гонку с его запуском.
        # Вытеснение может прийти, пока воркер ещё только создаётся и ручки нет:
        # одна ручка тогда не убила бы ничего, и Whisper отработал бы до конца на
        # отданной карте. Флаг ставится до чтения ручки, а воркер читает его после
        # того, как ручку выставил, — так хотя бы одна сторона видит другую.
        # Он же отличает наше убийство от чужого: см. JobRunner._run_asr.
        self._asr_kill = False
        # Звук, отложенный на диск, когда карту отобрали после загрузки: см.
        # stash_audio. Файл свой у каждого задания — пересозданное задание
        # по тому же ролику чужой звук не подхватит.
        self._audio_path: Path | None = None
        # Задание сброшено: откладывать звук больше незачем. См. cancel.
        self._audio_dead = False
        # Когда к заданию последний раз обращались — опрос или возобновление.
        # Отложенный звук брошенной паузы по нему убирает _evict.
        self.touched_at = time.monotonic()

    # ── подписка на события ──────────────────────────────────────────────

    def subscribe(self, queue: asyncio.Queue) -> asyncio.Queue:
        """Подписать очередь вызывающего на события задания.

        Очередь передаётся снаружи, а не создаётся здесь, чтобы одно соединение
        могло слушать несколько заданий через одну очередь. Иначе читателю пришлось
        бы ждать сразу на нескольких очередях, а брошенные задачи `get()` успевают
        забрать сообщение и потерять его.
        """
        self._subscribers.add(queue)

        # Догоняющий снимок: подключившийся позже получает всё, что уже готово,
        # и дальше следит за потоком наравне с остальными.
        queue.put_nowait(self.snapshot())

        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _emit(self, event: dict) -> None:
        self.events.append(event)

        # Журнал ограничен: реплики всё равно целиком лежат в job.cues, поэтому
        # отставший клиент получит их снимком, а не будет держать нас за хвост.
        if len(self.events) > MAX_EVENT_LOG:
            excess = len(self.events) - MAX_EVENT_LOG
            del self.events[:excess]
            self._dropped += excess

        self._wakeup.set()
        self._wakeup.clear()

        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def poll(self, cursor: int, timeout: float) -> dict:
        """События после позиции cursor. Ждёт до timeout, если новых пока нет.

        Отрицательный или слишком старый курсор означает, что клиент отстал или
        подключился впервые: отдаём снимок и текущую позицию, чтобы он начал
        с согласованного состояния, а не с середины.

        Курсор ЗА концом журнала означает обратное — он из другого задания на тот
        же ролик, пересозданного под сменившийся переключатель дорожки. Это тоже
        рассинхронизация, и лечится тем же снимком: без него клиент молча ждал бы,
        пока журнал дорастёт до его позиции, потерял бы все события до неё и
        подмешал бы новые реплики к показанным старым.
        """
        end = self._dropped + len(self.events)

        if cursor < self._dropped or cursor > end:
            return {"cursor": end, "snapshot": self.snapshot(), "events": []}

        if cursor >= end:
            # Ждём появления новых. Таймаут возвращает пустой ответ — это норма,
            # клиент тут же спросит снова, и соединение не висит дольше лимита.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout)

            end = self._dropped + len(self.events)

        return {"cursor": end, "events": self.events[cursor - self._dropped :]}

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "videoId": self.video_id,
            "stage": self.stage.value,
            "error": self.error,
            "retryable": self.retryable,
            # Снимок получает и тот, кто подключился посреди ожидания карты:
            # без поля он видел бы обычную очередь, а не ожидание видеокарты.
            "waiting": self.waiting,
            "title": self.info.title if self.info else "",
            "total": len(self.cues),
            "translated": sum(c.translated for c in self.cues),
            "cues": [c.payload() for c in self.cues if c.translated],
        }

    def _set_stage(self, stage: Stage, **extra) -> None:
        self.stage = stage
        # Любая следующая стадия ожидание снимает, поэтому берётся из каждой.
        self.waiting = extra.get("waiting")
        self._emit({"type": "state", "stage": stage.value, **extra})

    def _fail(self, message: str, retryable: bool) -> None:
        self.error = message
        self.retryable = retryable
        self.failed_at = time.monotonic()
        self._set_stage(Stage.ERROR, message=message, retryable=retryable)

    # ── жизненный цикл ───────────────────────────────────────────────────

    def start(self, runner: JobRunner) -> None:
        if self._task is None:
            self._task = asyncio.create_task(runner.run(self))

    def resume(self, runner: JobRunner) -> None:
        """Продолжить после вытеснения, сохранив уже готовые реплики.

        Задание то же самое: job.cues уже содержит сегментированные реплики
        и часть переводов, поэтому _pipeline пойдёт сразу к недостающим.
        """
        if self._task and not self._task.done():
            return

        self._task = asyncio.create_task(runner.run(self))

    def kill_asr(self) -> None:
        """Убить процесс распознавания, если он идёт.

        Вызывается и при отмене, и при вытеснении: пропуск на GPU отпускается
        сразу после отмены задачи, поэтому память надо освободить до этого,
        а не через непредсказуемое время планировщика потоков.
        """
        self._asr_kill = True
        proc = self._asr_proc

        if proc and proc.poll() is None:
            proc.kill()

    # ── отложенный звук ──────────────────────────────────────────────────

    def stash_audio(self, audio) -> None:
        """Отложить скачанный звук до возобновления.

        Нужно, когда карту отбирают уже после загрузки — посреди распознавания
        или между ним и загрузкой. Иначе возобновление качало бы ролик заново.
        Пишется один раз: повторное вытеснение того же задания файл уже застаёт.
        """
        if self._audio_dead:
            return

        if self._audio_path is not None and self._audio_path.exists():
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
            # Недописанный файл — это обрезанный звук. Прочитанный при
            # возобновлении, он распознался бы и ушёл в кэш как целый ролик.
            self.discard_audio()
            raise

        # Задание сбросили, пока файл писался: сброс ручки ещё не видел,
        # и файл остался бы ничьим до перезапуска демона.
        if self._audio_dead:
            self.discard_audio()

    def take_audio(self):
        """Отложенный звук, если он есть. Файл остаётся до конца распознавания.

        Не прочитался — не беда: None, и возобновление скачает звук заново,
        как если бы файла не было вовсе.
        """
        self.touched_at = time.monotonic()

        if self._audio_path is None or not self._audio_path.exists():
            return None

        try:
            return np.fromfile(self._audio_path, dtype="<f4")
        except OSError as exc:
            log.warning("отложенный звук %s не прочитан: %s", self.video_id, exc)
            self.discard_audio()

            return None

    def discard_audio(self) -> None:
        path, self._audio_path = self._audio_path, None

        if path is not None:
            path.unlink(missing_ok=True)

    def cancel(self) -> None:
        # Приостановленное задание своей задачи не имеет — она уже завершилась
        # паузой, — и убрать его звук, кроме как здесь, некому. Флаг ставится
        # до сброса ручки, а запись читает его после того, как ручку выставила:
        # так файл, дописанный уже после сброса, тоже не останется ничьим.
        self._audio_dead = True
        self.discard_audio()
        self.kill_asr()

        if self._task:
            self._task.cancel()

    @property
    def finished(self) -> bool:
        return self.stage in (Stage.DONE, Stage.ERROR)


class JobRunner:
    """Выполняет задание, соблюдая дисциплину GPU."""

    def __init__(self, lease: GpuLease, cache: TrackCache, config: Config) -> None:
        self._lease = lease
        self._cache = cache
        self._config = config
        # Заявка обязана делаться настроенной моделью, а не константой. Сторож
        # считает своей ровно объявленную: разойдись эти две вещи, и демон увидит
        # собственного переводчика как чужую работу и вытеснит сам себя.
        self._lease.claim_model(config.translator.model)

    @property
    def _mt_model(self) -> str:
        return self._config.translator.model

    def _cache_key(self, job: Job) -> CacheKey:
        # Обе модели входят в ключ, поэтому смена любой из них обесценивает старые
        # дорожки автоматически, а не подсовывает перевод от другой модели.
        return CacheKey(
            video_id=job.video_id,
            source_lang=self._config.asr.language,
            target_lang=job.target_lang,
            asr_model=f"{self._config.asr.model}/{self._config.asr.compute_type}",
            translator_model=self._mt_model,
            study_lang=self._config.translator.study_language,
            track_fingerprint=job.source,
        )

    async def run(self, job: Job) -> None:
        try:
            await self._pipeline(job)
        except asyncio.CancelledError:
            raise
        except Preempted as exc:
            # Не отказ. Пользователь занял карту, мы её отдали — реплики, которые
            # успели перевести, остаются на месте, распознавание повторять не нужно.
            # Возобновлением занимается _pipeline: он видит готовые job.cues и идёт
            # сразу к переводу недостающих.
            job._set_stage(Stage.PAUSED, message=str(exc),
                           translated=sum(c.translated for c in job.cues))
            log.info("задание %s приостановлено: %s", job.video_id, exc)
        except YtDlpError as exc:
            job._fail(str(exc), retryable=not exc.permanent)
            log.warning("задание %s провалено: %s", job.video_id, exc)

            if exc.diagnostics:
                log.warning("задание %s, вывод yt-dlp:\n%s", job.video_id, exc.diagnostics)
        except PermanentJobError as exc:
            job._fail(str(exc), retryable=False)
            log.warning("задание %s провалено: %s", job.video_id, exc)
        except (TranslateError, RuntimeError) as exc:
            # Переводчик, распознавание, воркер — всё это машина, а не ролик:
            # ollama перезапустили, память кончилась, процесс упал. Следующая
            # попытка вполне может пройти, поэтому отказ повторяемый. Из ролика
            # сюда попадает только эфир, запись которого ещё собирается, — и он
            # тоже проходит сам.
            job._fail(str(exc), retryable=True)
            log.warning("задание %s провалено: %s", job.video_id, exc)
        except Exception as exc:  # noqa: BLE001 — падение одного задания не роняет демон
            job._fail(repr(exc), retryable=True)
            log.exception("задание %s упало", job.video_id)
        finally:
            # Отложенный звук нужен только паузе. Готовое, упавшее и отменённое
            # задание к нему больше не вернётся, а файл — это десятки мегабайт.
            if job.stage is not Stage.PAUSED:
                job.discard_audio()

    async def _pipeline(self, job: Job) -> None:
        # Возобновление после вытеснения: реплики уже распознаны и сегментированы,
        # остаётся дописать перевод. Повторять ASR было бы тратой карты впустую.
        if job.cues:
            await self._finish_translation(job)

            return

        # Метаданные уже есть, если задание возобновляется после паузы: пока карта
        # занята, пауза повторяется каждые две минуты, и спрашивать YouTube о том же
        # ролике на каждом витке незачем. Проверки ниже они тогда тоже уже прошли —
        # иначе задание упало бы, а не встало на паузу.
        if job.info is None:
            job._set_stage(Stage.PROBING)
            job.info = await self._with_retries(job, Stage.PROBING, probe)

            # Проверка раньше is_live, который включает и этот случай. Эфир уже
            # закончился, YouTube собирает из него запись, и через минуты или часы
            # это обычный ролик. Отказ поэтому не окончательный: остывание в
            # get_or_create и кнопка повтора подхватят ролик, когда запись будет готова.
            if job.info.live_status == "post_live":
                raise RuntimeError("эфир закончился, запись ещё обрабатывается")

            if job.info.is_live:
                raise PermanentJobError("прямые эфиры не поддерживаются")

            if job.info.duration and job.info.duration > MAX_VIDEO_SECONDS:
                raise PermanentJobError(
                    f"ролик длиннее {MAX_VIDEO_SECONDS // 3600} ч "
                    f"({job.info.duration / 3600:.1f} ч): аудио не поместится в память"
                )

        # Авторская дорожка, если она есть, снимает распознавание целиком: и
        # минуты работы карты, и её ошибки. Реплики размечены человеком, тайминги
        # его же — пересобирать их незачем, в отличие от тридцатисекундных окон Whisper.
        #
        # Источник определяется ДО обращения к кэшу, потому что входит в ключ.
        # Считай мы ключ раньше, дорожка автора не попадала бы в кэш никогда —
        # клали бы под один ключ, спрашивали под другой, — а с выключенным
        # переключателем демон отдавал бы прежний результат распознавания,
        # и переключатель выглядел бы не работающим вовсе.
        track = (
            usable_manual_track(job.info)
            if job.use_author_track and not job.track_failed
            else None
        )

        if track:
            job.source = f"manual:{track.lang}"

        if await self._serve_cached(job):
            return

        if track:
            source = f"дорожку автора ({track.lang})"
            job._set_stage(Stage.FETCHING, source=source)

            try:
                # С повторами, как метаданные и звук: отказ запоминается на всё
                # задание (track_failed), и разовый 403 иначе навсегда променял бы
                # дорожку автора на распознавание.
                segments = await self._with_retries(
                    job,
                    Stage.FETCHING,
                    lambda video_id: fetch_manual_track(video_id, track.lang),
                    source=source,
                )
            except YtDlpError as exc:
                # Дорожка есть в метаданных, но не скачалась — это не повод
                # проваливать задание: звук на месте, распознаем сами. Источник
                # возвращаем обратно, иначе распознанное легло бы в кэш под ключом
                # дорожки, а кэш распознавания для этого ролика остался бы нетронут
                # и карта молола бы одно и то же на каждом просмотре.
                log.info("дорожка %s недоступна (%s), иду через распознавание", track.lang, exc)
                job.source = "asr"
                job.track_failed = True

                if await self._serve_cached(job):
                    return
            else:
                job.cues = self._to_job_cues_from_track(
                    normalize_cues(segments, track.lang)
                )
                job._emit({"type": "segmented", "total": len(job.cues)})
                await self._finish_translation(job)

                return

        # Фазы берут пропуск по очереди, а не одним куском: им нужно разное
        # количество памяти (6 ГБ Whisper против 17.8 ГБ переводчика), и общий
        # захват под меньшую из потребностей означал бы, что допуск ни разу
        # не проверил, поместится ли переводчик.
        # Выгрузка обязана идти ДО контроля допуска. Переводчик мог остаться
        # резидентным с прошлого ролика, и тогда свободно 1–3 ГБ: допуск ждал бы
        # памяти, которую сам же и не освобождает, пока не истечёт keep_alive.
        await asyncio.to_thread(self._lease.unload, self._mt_model)

        # Ждём мы карту, а не сеть, и зритель должен видеть именно это.
        job._set_stage(Stage.QUEUED, waiting="gpu")

        # Звук качается уже под пропуском, а не до него. Не дождавшись карты,
        # задание встаёт на паузу, и возобновление начинает всё заново —
        # скачанный до допуска звук пропадал бы на каждом витке. Так и было:
        # пока карту держал пользователь, один ролик скачался 43 раза подряд
        # за полтора часа, по разу на две минуты ожидания, и на сорок четвёртый
        # googlevideo ответил 403. Скачивать заранее и держать звук до допуска
        # тоже не выход: приостановленное задание может ждать сколько угодно,
        # а это десятки и сотни мегабайт на ролик. Откладывается на диск только
        # звук, уже скачанный под пропуском, — когда карту отобрали после
        # загрузки; см. Job.stash_audio.
        #
        # Цена нового порядка: пропуск один на весь демон, и пока идёт загрузка,
        # чужие фазы на карте ждут. Обычно это секунды. Повторы после 403 и обрыва
        # сети добавляют до двадцати пяти секунд пауз, поэтому они ограничены
        # FETCH_RETRY_BUDGET и прерываются вытеснением; мёртвое соединение
        # обрывает таймаут сокета в audio_pcm. Сама попытка, если yt-dlp тянет
        # медленно, но тянет, ограничена только его общим таймаутом. И ещё одна
        # загрузка, если карту отберут посреди неудавшейся попытки: скачанного
        # звука тогда нет, и откладывать нечего.
        async with self._lease.acquire(self._config.asr_vram_mib) as held:
            audio = await asyncio.to_thread(job.take_audio)

            if audio is None:
                job._set_stage(Stage.FETCHING)
                audio = await self._with_retries(job, Stage.FETCHING, audio_pcm, held=held)

            duration = audio.size / SAMPLE_RATE

            try:
                # Пользователь мог занять карту, пока шла загрузка. Проверяем до
                # запуска воркера: иначе он успел бы начать грузить в память
                # Whisper только для того, чтобы его тут же убили.
                held.check()

                job._set_stage(Stage.ASR, duration=duration)
                result = await self._run_asr_watched(audio, job, held)
            except Preempted:
                # Звук уже скачан, и возобновление должно начаться с него, а не
                # с новой загрузки. На диск, а не в памяти задания: сколько ждать
                # карту, заранее неизвестно.
                #
                # Отложить — только выгода, не обязанность. Полный диск не должен
                # превращать паузу в отказ: не вышло — задание всё равно встаёт
                # на паузу, а возобновление просто скачает звук заново.
                try:
                    await asyncio.to_thread(job.stash_audio, audio)
                except OSError as exc:
                    log.warning("звук %s не отложен: %s", job.video_id, exc)
                    job.discard_audio()

                raise

            # Распознано — отложенный звук больше не нужен. Вытеснение ниже
            # уводит в паузу уже с репликами, и возобновление начнётся с перевода.
            job.discard_audio()
            segments = result["cues"]
            spoken = result.get("language") or "sr"

            if result.get("detected") and result.get("detected") != spoken:
                log.info("язык речи: %s (определитель дал %s, уверенность %.2f)",
                         spoken, result["detected"], result.get("confidence") or 0.0)

            # Присваиваем реплики ДО проверки вытеснения. Транскрипт уже получен,
            # и выбрасывать его только потому, что карту забрали секундой позже,
            # значит потерять минуты работы GPU: возобновление начнётся с перевода.
            job.cues = self._to_job_cues(build_cues(normalize_cues(segments, spoken)))
            job._emit({"type": "segmented", "total": len(job.cues)})
            held.check()

        await self._finish_translation(job)

    async def _with_retries(self, job: Job, stage: Stage, fetch, *, held=None, **extra):
        """Метаданные или звук, с повтором временных сбоев на месте.

        Повторять надо здесь, внутри задания. Отказ задания прилипает, и без
        этого разовый 403 от googlevideo — а он случается примерно на каждой
        пятидесятой загрузке — стоил бы зрителю провала, хотя следующая же
        ссылка скачивается. Каждая попытка — новый вызов yt-dlp, то есть
        и новое извлечение со свежей ссылкой.

        Паузы — asyncio.sleep в цикле событий, а не сон в рабочем потоке. Сам
        вызов yt-dlp отменой не прерывается, но между попытками drop и отмена
        должны снимать задание сразу, а не после двадцати секунд ожидания.

        Неудачную попытку, за которой идёт повтор, пишем в журнал здесь, вместе
        с выводом yt-dlp. Последнюю пишет run, когда проваливает задание.

        held — пропуск на карту, если загрузка идёт под ним. Тогда повторы
        сверяются с вытеснением: карту отобрали — задание уходит в паузу сразу,
        а не качает снова то, что всё равно будет выброшено. И новая попытка
        начинается только в пределах FETCH_RETRY_BUDGET: пропуск один на весь
        демон, и затянувшаяся загрузка держала бы чужие фазы на карте.

        extra уходит в событие о повторе вместе со стадией — например, источник,
        чтобы кнопка не превращала «качаю дорожку автора» в «качаю звук».
        """
        started = time.monotonic()

        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(fetch, job.video_id)
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
                    "%s %s: попытка %d из %d не удалась, повтор через %.0f с: %s%s",
                    stage.value, job.video_id, attempt, FETCH_ATTEMPTS, delay, exc,
                    f"\n{exc.diagnostics}" if exc.diagnostics else "",
                )

                # Та же стадия со счётчиком повтора, и сразу, до паузы: иначе
                # зритель двадцать секунд смотрел бы на «качаю звук…», не зная,
                # что первая попытка уже не удалась.
                job._set_stage(stage, retry=attempt, **extra)
                await self._backoff(delay, held)

    @staticmethod
    async def _backoff(delay: float, held) -> None:
        """Пауза между попытками, которую прерывает вытеснение."""
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
        """Распознавание, которое действительно прерывается.

        `asyncio.to_thread` отменой не прерывается, поэтому единственный способ
        освободить карту — убить сам процесс. Гоняем ожидание результата против
        флага вытеснения и по срабатыванию убиваем воркер; тот завершается
        по сигналу, и `_run_asr` поднимает Preempted.
        """
        job._asr_kill = False
        work = asyncio.create_task(asyncio.to_thread(self._run_asr, audio, job))
        preempt = asyncio.create_task(held.wait_preempted())

        try:
            done, _ = await asyncio.wait({work, preempt}, return_when=asyncio.FIRST_COMPLETED)

            if work not in done:
                log.info("вытеснение во время распознавания — убиваю воркер")
                job.kill_asr()

            return await work
        except asyncio.CancelledError:
            # Отмена задания: процесс надо убить здесь, иначе пропуск отпустится
            # раньше, чем освободится память.
            job.kill_asr()
            raise
        finally:
            preempt.cancel()

    async def _finish_translation(self, job: Job) -> None:
        """Перевести недостающие реплики и, если всё готово, закэшировать дорожку."""
        job._set_stage(Stage.TRANSLATING, total=len(job.cues))

        # Переводить нечего — например, в ролике не распознано ни слова. Пропуск
        # под переводчик тогда не нужен, а взятый, он мог бы увести задание
        # в паузу без единой реплики, и возобновление начиналось бы с загрузки
        # звука и распознавания заново — на каждом витке, пока карта занята.
        if any(not c.translated for c in job.cues):
            try:
                # model= обязателен: если наш переводчик уже резидентен, память под него
                # просить не нужно — иначе допуск требовал бы 17.8 ГБ свободных ровно
                # тогда, когда 17.3 из них занял он сам.
                async with self._lease.acquire(
                    self._config.translator_vram_mib, model=self._mt_model
                ) as held:
                    await self._translate(job, held)
            except Preempted:
                # Уступая карту, надо её и освободить. keep_alive у нас 60 секунд, то
                # есть без явной выгрузки 17.3 ГБ оставались бы нашими ещё минуту после
                # того, как мы объявили, что уходим, — ровно то поведение враждебного
                # соседа, против которого написан gpulease.
                await asyncio.to_thread(self._lease.unload, self._mt_model)
                raise

        pending = sum(not c.translated for c in job.cues)
        failed = sum(c.failed for c in job.cues)

        if pending:
            # Работа не доделана — только это и есть пауза. Возобновление найдёт
            # непереведённые реплики и продолжит с них.
            job._set_stage(Stage.PAUSED, translated=len(job.cues) - pending)

            return

        if failed:
            # Все реплики обработаны, но часть уходит на экран исходным сербским.
            # Задание завершено — повторять нечего, лестница починки уже отработала
            # батчем, половинами и поштучно. Но в кэш такая дорожка не идёт: иначе
            # неудача закрепилась бы навсегда под валидным ключом, и следующий
            # просмотр отдал бы её же вместо новой попытки.
            job._set_stage(Stage.DONE, total=len(job.cues), failed=failed, cached=False)

            return

        await asyncio.to_thread(
            self._cache.put,
            self._cache_key(job),
            [c.payload() for c in job.cues],
            job.info.duration if job.info else None,
        )
        job._set_stage(Stage.DONE, total=len(job.cues))

    def _run_asr(self, audio, job: Job) -> dict:
        """Распознавание отдельным процессом — выход процесса гарантирует возврат VRAM."""
        prompt = _initial_prompt(job.info)
        args = [
            sys.executable,
            "-m",
            "helper.asr.worker",
            "--model",
            self._config.asr.model,
            "--compute-type",
            self._config.asr.compute_type,
            "--language",
            self._source_language(job),
        ]

        if prompt:
            args += ["--initial-prompt", prompt]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).resolve().parent.parent,
        )
        job._asr_proc = proc

        # Убить велели, пока процесс создавался, — ручки тогда ещё не было.
        if job._asr_kill:
            proc.kill()

        try:
            out, err = proc.communicate(input=audio.tobytes(), timeout=ASR_TIMEOUT)
        except subprocess.TimeoutExpired:
            # communicate по таймауту процесс не убивает. Без этого Whisper
            # продолжал бы держать ~6 ГБ, когда пропуск на карту уже отпущен,
            # и следующее задание получило бы допуск на память, которой нет.
            proc.kill()
            proc.communicate()

            raise RuntimeError(f"распознавание не уложилось в {ASR_TIMEOUT:.0f} с") from None
        finally:
            job._asr_proc = None

        if proc.returncode != 0:
            # Убитый по вытеснению или отмене процесс — не ошибка распознавания:
            # его прервали намеренно, и задание должно уйти в паузу, а не в отказ.
            if proc.returncode < 0 and job._asr_kill:
                raise Preempted(f"распознавание прервано сигналом {-proc.returncode}")

            # Сигнал пришёл не от нас: память кончилась, и процесс убило ядро,
            # или воркер упал сам. Это отказ, а не пауза. Пауза возобновляется
            # на каждом опросе, а реплик ещё нет, так что каждое возобновление
            # заново качало бы звук и снова падало бы тем же сигналом. Отказ
            # повторяемый и пересоздаётся не чаще раза в минуту.
            if proc.returncode < 0:
                raise RuntimeError(f"распознавание убито сигналом {-proc.returncode}")

            raise RuntimeError(f"ASR не справился: {err.decode(errors='replace')[-500:]}")

        result = json.loads(out)

        if "error" in result:
            raise RuntimeError(result["error"])

        return result

    def _source_language(self, job: Job) -> str:
        """Язык речи: сначала бесплатно из метаданных, и только потом моделью.

        yt-dlp выводит язык аудио из оригинальной ASR-дорожки YouTube, поэтому
        для английских и русских роликов поле заполнено, а для сербских пусто —
        автосубтитров для BCMS не существует. Замерено: en и ru отдаются верно,
        сербский корпус из 60 роликов дал None во всех шестидесяти.

        Настройка со значением языка перекрывает всё: она означает «я знаю, что
        тут, не гадай». Значение "auto" оставляет решение метаданным и модели.
        """
        configured = self._config.asr.language

        if configured and configured != "auto":
            return configured

        declared = (job.info.audio_language or "").split("-")[0].lower() if job.info else ""

        if declared:
            log.info("язык %s взят из метаданных, распознаватель определять не будет", declared)

            return declared

        # Метаданных нет — пусть решает Whisper по окнам, разложенным по ролику.
        return "auto"

    async def _serve_cached(self, job: Job) -> bool:
        """Отдать готовые реплики из кэша. True — задание закончено, идти дальше не нужно.

        Ключ зависит от job.source, поэтому звать это можно только после того,
        как источник реплик выбран.
        """
        cached = await asyncio.to_thread(
            self._cache.get, self._cache_key(job), job.info.duration
        )

        if not cached:
            return False

        job.cues = [
            JobCue(
                index=i,
                start=c["start"],
                end=c["end"],
                src=c["src"],
                lines=c["lines"],
                study=c.get("study", ""),
                # Флаг обязан пережить круг через кэш: без него реплика
                # с непереведённым сербским вернулась бы как полноценный перевод.
                failed=c.get("failed", False),
            )
            for i, c in enumerate(cached["cues"])
        ]
        job._emit({"type": "cues", "cues": [c.payload() for c in job.cues]})
        job._set_stage(Stage.DONE, cached=True, total=len(job.cues))

        return True

    def _to_job_cues_from_track(self, segments: list[dict]) -> list[JobCue]:
        """Реплики из авторской дорожки: тайминги берём как есть.

        Пересегментация здесь была бы вредна. Она нужна выходу Whisper, где
        реплика — это тридцатисекундное окно на триста знаков; у автора же
        границы расставлены под чтение, и переставлять их значит портить
        то, ради чего этот путь и существует.
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
        """Перевести все реплики, начиная с тех, что рядом с плейхедом."""
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
                # Ловим широко, а не только TranslateError: обрыв соединения с ollama,
                # её перезапуск или битый JSON — такие же поводы для лестницы починки.
                # Пропущенные мимо, они роняли всё задание вместе с готовым транскриптом,
                # на который уже потрачены минуты работы карты.
                #
                # Дробление идёт тем же путём, что и батч: если учебный режим включён,
                # половины и поштучные попытки тоже просят обе строки. Раньше здесь
                # был одноязычный откат — «учебная строка в такой ситуации роскошь», —
                # и он тихо оставлял её пустой. На русском ролике это выглядело так,
                # будто вместо черногорского пошёл русский: оверлей подставлял вместо
                # пустой строки текст распознавания, а он у такого ролика русский.
                log.warning("батч не перевёлся (%s), пробую половинами", exc)
                pairs = await self._translate_halves(batch_cues, job, held)

            for cue, (study_line, text) in zip(batch_cues, pairs):
                if text.strip():
                    cue.lines = text.split("\n")[:2]

                if study_line.strip():
                    cue.study = study_line.strip()

            # Гарантия сходимости цикла: всё, что не пережило ни батч, ни половины,
            # ни поштучную попытку, помечается исходным текстом и флагом. Без этого
            # неподдающаяся реплика держала бы pending непустым вечно.
            #
            # Показывать сербский вместо русского плохо, но молча пропустить кусок
            # речи хуже, поэтому реплика уходит на экран помеченной.
            for cue in batch_cues:
                if not cue.translated:
                    cue.lines = [cue.src]
                    cue.failed = True

            job._emit({"type": "cues", "cues": [c.payload() for c in batch_cues]})

    async def _translate_once(
        self, batch: Batch, cues: list[JobCue] | None = None
    ) -> list[tuple[str, str]]:
        """Один заход в модель. Пара строк, если учебный режим включён.

        Единая форма ответа на обоих режимах избавляет лестницу починки от
        необходимости знать, какой из них сейчас: она просто передаёт пары дальше.
        """
        if self._config.translator.study_language:
            pairs = await asyncio.to_thread(translate_dual, batch, self._mt_model)

            for reason in validate_dual(pairs, batch.cues):
                log.info("батч: %s", reason)

            return pairs

        lines = await asyncio.to_thread(translate_batch, batch, self._mt_model)

        for index, reason in validate(batch.cues, lines):
            number = cues[index].index if cues else index
            log.info("реплика %d: %s", number, reason)

        # Учебный режим выключен: второй строкой идёт текст распознавания, как и
        # раньше. Подставляет его демон, а не оверлей — только здесь известно,
        # включён ли режим, и пустая учебная строка означает именно неудачу,
        # а не «показать оригинал».
        return [(src, ru) for src, ru in zip(batch.cues, lines)]

    async def _translate_halves(
        self, cues: list[JobCue], job: Job, held
    ) -> list[tuple[str, str]]:
        """Батч не прошёл целиком — делим пополам. Обычно виноват один длинный кусок.

        Проверка вытеснения обязательна на каждом уровне рекурсии: на батче в сорок
        реплик спуск до поштучных попыток — это до семидесяти запросов к модели,
        то есть десятки минут удержания карты. Без проверки лестница чинила бы
        перевод, игнорируя, что пользователь давно ждёт свою очередь.
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
        """Ближайшие к плейхеду реплики, подряд идущие.

        Подряд — обязательно: соседние реплики переводятся вместе, потому что
        контекст нужен для согласования рода и терминологии.
        """
        nearest = min(pending, key=lambda c: abs(c.start - job.playhead))
        start = max(0, nearest.index - BATCH_SIZE // 4)
        window = [c for c in job.cues[start : start + BATCH_SIZE] if not c.translated]

        return window or pending[:BATCH_SIZE]

    def _context_for(self, job: Job, index: int) -> list[tuple[str, str]]:
        """Несколько уже переведённых реплик перед батчем.

        И сербский, и русский маркируют род в прошедшем времени. Без антецедента
        модель угадает пол говорящего и перевернёт его посреди сцены.
        """
        context = []

        for cue in job.cues[max(0, index - CONTEXT_CUES) : index]:
            if cue.translated:
                context.append((cue.src, " ".join(cue.lines)))

        return context


def _initial_prompt(info: VideoInfo | None) -> str:
    """Затравка на латинице: она же фиксирует письменность вывода.

    Whisper обусловливается на предыдущий текст, поэтому связная фраза работает
    лучше списка через запятую. Держим короткой: затравка режется примерно на
    половине 224-токенного контекста, а переполнение — известный триггер галлюцинаций.
    """
    if not info:
        return ""

    names = _proper_nouns(info)

    if not names:
        return "Ovo je transkript video snimka na srpskom jeziku."

    return "Ovo je transkript video snimka na srpskom jeziku. Pominju se: " + ", ".join(names[:12]) + "."


def _proper_nouns(info: VideoInfo) -> list[str]:
    """Имена собственные из метаданных — бесплатная подсказка для ASR.

    Регексом, а не моделью: это происходит до захвата GPU, и платить за него
    ещё одним свопом модели нельзя.
    """
    import re

    from helper.script import to_latin

    text = " ".join([info.title, info.description[:1500], " ".join(info.tags[:20])])
    words = re.findall(r"\b[A-ZČĆŽŠĐ][\wčćžšđ]{2,}", to_latin(text))
    seen: dict[str, None] = {}

    for word in words:
        seen.setdefault(word, None)

    return list(seen)[:20]


class JobManager:
    """Реестр заданий: одно на ролик, общий пропуск на GPU."""

    def __init__(self, config: Config | None = None) -> None:
        # Конфигурация читается один раз, при старте. Смена модели требует
        # перезапуска демона: модель резидентна в ollama, и менять её посреди
        # задания значит переоткрывать всю дисциплину свопа.
        self.config = config or load_config()
        self.lease = GpuLease()
        self.cache = TrackCache()
        self.runner = JobRunner(self.lease, self.cache, self.config)
        self._jobs: dict[str, Job] = {}

        # Реестр живёт в памяти, и после перезапуска отложенный звук не нужен
        # никому. А без чистки копился бы: приостановленное задание, чью вкладку
        # закрыли, не возобновится и само его не уберёт.
        shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)

    def attach(self, video_id: str) -> Job | None:
        """Существующее задание. В отличие от get_or_create, НИЧЕГО не создаёт.

        Нужно опросу после восстановления. Обычный опрос идёт через get_or_create,
        потому что подписка на ролик и есть согласие на работу. Но вкладка, которую
        всего лишь перезагрузили, согласия не давала: она подхватила уже сделанное.
        Если задание к этому моменту исчезло — демон перезапустили, реестр вытеснил
        его, — создавать новое нельзя, иначе перезагрузка страницы сама запускала бы
        скачивание и карту.
        """
        job = self._jobs.get(video_id)

        if job is not None:
            job.touched_at = time.monotonic()

        return job

    def get_or_create(
        self, video_id: str, target_lang: str = "ru", use_author_track: bool = True
    ) -> Job:
        job = self._jobs.get(video_id)
        replaced: Job | None = None

        if job is not None and job.use_author_track != use_author_track:
            # Переключатель сменили: готовое задание отвечает уже не на тот
            # вопрос. Пересоздаём, иначе настройка выглядела бы сломанной —
            # перезагрузка страницы возвращала бы прежний результат. Шторма,
            # как с прилипшим отказом, тут не будет: значение приходит из
            # настроек и между опросами не меняется, так что расхождение
            # случается ровно один раз на переключение.
            log.info("дорожка автора %s — начинаю %s заново",
                     "включена" if use_author_track else "выключена", video_id)
            self.drop(video_id)
            replaced, job = job, None
        elif (
            job is not None
            and job.stage is Stage.ERROR
            and job.retryable
            and job.failed_at is not None
            and time.monotonic() - job.failed_at >= RETRY_COOLDOWN
        ):
            # Отказ прилипает, но повторяемый отказ — не навсегда. Пересоздавать
            # задание на каждом запросе нельзя: опрос зовёт get_or_create
            # постоянно, и это давало шторм — отказ → пересоздание → отказ,
            # восемь раз за тридцать секунд, с обращением к YouTube на каждом
            # витке. Но и держать отказ вечно нельзя: сбой сети или 403 от
            # googlevideo проходит сам, а зритель, нажавший запуск после F5,
            # получал бы всё тот же старый отказ без единой новой попытки.
            #
            # Остывание разводит эти два случая: от опроса — не чаще попытки
            # в минуту на ролик, и это уже не шторм, а зрителю — настоящая
            # новая попытка. Окончательные отказы (эфир, длина, удалённый или
            # закрытый ролик) не пересоздаются вовсе: их повтор ничего не даст.
            # Немедленный повтор по кнопке идёт через drop, он в API есть.
            log.info("повторяю %s: отказ повторяемый и остыл (%s)", video_id, job.error)
            self.drop(video_id)
            replaced, job = job, None

        if job is None:
            job = Job(video_id, target_lang, use_author_track)
            self._jobs[video_id] = job

            # Подписчики заменённого задания переходят к новому. Сокет подписывается
            # на ролик один раз, на первом watch, и его очередь остаётся на том
            # задании, что было тогда. Без переноса пересоздание оставило бы его
            # слушать отменённое задание, а unwatch снимал бы очередь с нового,
            # где её никогда не было.
            if replaced is not None:
                for queue in list(replaced._subscribers):
                    replaced.unsubscribe(queue)
                    job.subscribe(queue)

            job.start(self.runner)
        elif job.stage is Stage.ERROR:
            # Отказ окончательный или ещё не остыл — отдаём его как есть, см. выше.
            pass
        elif job.stage is Stage.PAUSED:
            # Карту забирал пользователь. Реплики никуда не делись — продолжаем
            # то же задание, а не начинаем заново с распознавания.
            job.resume(self.runner)

        job.touched_at = time.monotonic()

        self._evict()

        return job

    def _key_for(self, video_id: str, target_lang: str, fingerprint: str) -> CacheKey:
        return CacheKey(
            video_id=video_id,
            source_lang=self.config.asr.language,
            target_lang=target_lang,
            asr_model=f"{self.config.asr.model}/{self.config.asr.compute_type}",
            translator_model=self.config.translator.model,
            study_lang=self.config.translator.study_language,
            track_fingerprint=fingerprint,
        )

    def restore(
        self,
        video_id: str,
        target_lang: str = "ru",
        use_author_track: bool = True,
        duration: float | None = None,
    ) -> Job | None:
        """Что уже есть по этому ролику. Работы НЕ начинает — ни сети, ни карты.

        Нужно для перезагрузки страницы. Согласие зрителя спрашивают один раз
        за ролик, и переспрашивать его на F5 незачем: показать уже сделанное —
        не то же самое, что начать качать аудио и занимать карту. Поэтому здесь
        строго чтение: идущее задание отдаётся как есть, готовое поднимается
        из кэша, а если нет ни того ни другого — возвращается None, и оверлей
        честно показывает кнопку запуска.
        """
        job = self._jobs.get(video_id)

        if job is not None:
            # Задание с ДРУГИМ переключателем отдавать нельзя. Оно отвечает не на тот
            # вопрос, и следующий же опрос /events пересоздал бы его через
            # get_or_create — то есть перезагрузка страницы запустила бы полный
            # конвейер со скачиванием и картой, без единого нажатия.
            if job.use_author_track != use_author_track:
                return None

            # Приостановленное и провалившееся не восстанавливаем. Возобновление
            # паузы — это захват карты, а прилипший отказ, поднятый в оверлей,
            # закрывает зрителю кнопку: она прячется под сообщением об ошибке,
            # и повторить попытку становится нечем.
            if job.stage in (Stage.PAUSED, Stage.ERROR):
                return None

            return job

        payload = self._cached_variant(video_id, target_lang, use_author_track, duration)

        # Пустая дорожка — это не «всё готово», а испорченная запись. Отдай мы её,
        # оверлей включился бы, спрятал кнопку по стадии done и не показал ничего,
        # причём нажать было бы уже нечего.
        if payload is None or not payload.get("cues"):
            return None

        job = Job(video_id, target_lang, use_author_track)
        job.source = payload.get("track_fingerprint", "asr")
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
        job._emit({"type": "cues", "cues": [c.payload() for c in job.cues]})
        job._set_stage(Stage.DONE, cached=True, total=len(job.cues))

        self._jobs[video_id] = job
        self._evict()

        log.info("восстановил %s из кэша (%s, %d реплик)", video_id, job.source, len(job.cues))

        return job

    def _cached_variant(
        self,
        video_id: str,
        target_lang: str,
        use_author_track: bool,
        duration: float | None = None,
    ) -> dict | None:
        """Сохранённая дорожка, отвечающая текущим настройкам.

        Отпечаток источника берём из самого файла, а ключ по нему пересчитываем:
        совпадение ключа и означает, что дорожка сделана теми же моделями и тем же
        промптом, что стоят сейчас. Иначе восстановление подсовывало бы результат
        от другой модели, обходя ровно ту проверку, ради которой ключ и заведён.
        """
        found: dict[str, dict] = {}

        for stored in self.cache.variants(video_id):
            fingerprint = stored.get("track_fingerprint", "asr")
            # Длительность приходит от самого плеера: страница её знает точно,
            # и запрашивать метаданные у YouTube на каждой загрузке не нужно.
            # Без этой сверки перезалитый под тем же идентификатором ролик
            # молча получал бы съехавшие тайминги.
            hit = self.cache.get(
                self._key_for(video_id, target_lang, fingerprint), duration
            )

            if hit is not None:
                found[fingerprint] = hit

        if use_author_track:
            # Дорожка автора приоритетнее — при включённом переключателе конвейер
            # выбрал бы её же. Её отсутствие означает, что у ролика её просто нет,
            # и распознавание для этой настройки такой же законный результат.
            manual = next((v for k, v in found.items() if k.startswith("manual:")), None)

            return manual or found.get("asr")

        return found.get("asr")

    def _evict(self) -> None:
        """Выбросить самые старые завершённые задания.

        Реплики двадцатиминутного ролика — это сотни объектов, и реестр без
        вытеснения рос бы всё время жизни процесса. Выбрасываем только то, что
        уже никуда не движется: незавершённое задание держит транскрипт, ради
        которого потрачена работа карты.
        """
        # Сами приостановленные задания не вытесняются — в них работа карты.
        # Но их отложенный звук, если к заданию полчаса никто не обращался,
        # убираем: иначе брошенная пауза держала бы на диске десятки и сотни
        # мегабайт до перезапуска демона, а он живёт неделями.
        now = time.monotonic()

        for job in self._jobs.values():
            if (
                job.stage is Stage.PAUSED
                and (job._task is None or job._task.done())
                and now - job.touched_at > PAUSED_AUDIO_TTL
            ):
                job.discard_audio()

        finished = [j for j in self._jobs.values() if j.finished]

        for job in sorted(finished, key=lambda j: j.created_at)[
            : max(0, len(self._jobs) - MAX_RETAINED_JOBS)
        ]:
            self._jobs.pop(job.video_id, None)

    def get(self, video_id: str) -> Job | None:
        return self._jobs.get(video_id)

    def drop(self, video_id: str) -> None:
        job = self._jobs.pop(video_id, None)

        if job:
            job.cancel()

    def stats(self) -> dict:
        return {
            "jobs": len(self._jobs),
            "stages": {v: sum(j.stage is v for j in self._jobs.values()) for v in Stage},
        }
