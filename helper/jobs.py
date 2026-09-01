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
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx

from helper.asr.worker import SAMPLE_RATE
from helper.cache import CacheKey, TrackCache
from helper.config import Config, load as load_config
from helper.gpulease import GpuLease, Preempted
from helper.script import normalize_cues
from helper.segment import Cue, build_cues
from helper.sources.ytdlp import YtDlpError, VideoInfo, audio_pcm, probe, usable_manual_track
from helper.translate import Batch, TranslateError, translate_batch, validate

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


@dataclass
class JobCue:
    index: int
    start: float
    end: float
    src: str
    lines: list[str] = field(default_factory=list)
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
            "failed": self.failed,
        }


class Job:
    """Одно задание на один ролик."""

    def __init__(self, video_id: str, target_lang: str = "ru") -> None:
        self.video_id = video_id
        self.target_lang = target_lang
        self.stage = Stage.QUEUED
        self.error: str | None = None
        self.cues: list[JobCue] = []
        self.info: VideoInfo | None = None
        self.playhead = 0.0
        self.created_at = time.time()

        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        # Ручка на процесс распознавания. Без неё его нечем убить: subprocess.run
        # внутри asyncio.to_thread не прерывается отменой задачи, поэтому Whisper
        # пережил бы и отмену, и вытеснение, продолжая держать ~6 ГБ уже без сторожа.
        self._asr_proc: subprocess.Popen | None = None

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
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "videoId": self.video_id,
            "stage": self.stage.value,
            "error": self.error,
            "title": self.info.title if self.info else "",
            "total": len(self.cues),
            "translated": sum(c.translated for c in self.cues),
            "cues": [c.payload() for c in self.cues if c.translated],
        }

    def _set_stage(self, stage: Stage, **extra) -> None:
        self.stage = stage
        self._emit({"type": "state", "stage": stage.value, **extra})

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
        proc = self._asr_proc

        if proc and proc.poll() is None:
            proc.kill()

    def cancel(self) -> None:
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
        except (YtDlpError, TranslateError, RuntimeError) as exc:
            job.error = str(exc)
            job._set_stage(Stage.ERROR, message=str(exc))
            log.warning("задание %s провалено: %s", job.video_id, exc)
        except Exception as exc:  # noqa: BLE001 — падение одного задания не роняет демон
            job.error = repr(exc)
            job._set_stage(Stage.ERROR, message=repr(exc))
            log.exception("задание %s упало", job.video_id)

    async def _pipeline(self, job: Job) -> None:
        # Возобновление после вытеснения: реплики уже распознаны и сегментированы,
        # остаётся дописать перевод. Повторять ASR было бы тратой карты впустую.
        if job.cues:
            await self._finish_translation(job)

            return

        job._set_stage(Stage.PROBING)
        job.info = await asyncio.to_thread(probe, job.video_id)

        if job.info.is_live:
            raise RuntimeError("прямые эфиры не поддерживаются")

        if job.info.duration and job.info.duration > MAX_VIDEO_SECONDS:
            raise RuntimeError(
                f"ролик длиннее {MAX_VIDEO_SECONDS // 3600} ч "
                f"({job.info.duration / 3600:.1f} ч): аудио не поместится в память"
            )

        key = self._cache_key(job)
        cached = await asyncio.to_thread(self._cache.get, key, job.info.duration)

        if cached:
            job.cues = [
                JobCue(
                    index=i,
                    start=c["start"],
                    end=c["end"],
                    src=c["src"],
                    lines=c["lines"],
                    # Флаг обязан пережить круг через кэш: без него реплика
                    # с непереведённым сербским вернулась бы как полноценный перевод.
                    failed=c.get("failed", False),
                )
                for i, c in enumerate(cached["cues"])
            ]
            job._emit({"type": "cues", "cues": [c.payload() for c in job.cues]})
            job._set_stage(Stage.DONE, cached=True, total=len(job.cues))

            return

        job._set_stage(Stage.FETCHING)
        audio = await asyncio.to_thread(audio_pcm, job.video_id)
        duration = audio.size / SAMPLE_RATE

        # Фазы берут пропуск по очереди, а не одним куском: им нужно разное
        # количество памяти (6 ГБ Whisper против 17.8 ГБ переводчика), и общий
        # захват под меньшую из потребностей означал бы, что допуск ни разу
        # не проверил, поместится ли переводчик.
        # Выгрузка обязана идти ДО контроля допуска. Переводчик мог остаться
        # резидентным с прошлого ролика, и тогда свободно 1–3 ГБ: допуск ждал бы
        # памяти, которую сам же и не освобождает, пока не истечёт keep_alive.
        await asyncio.to_thread(self._lease.unload, self._mt_model)

        async with self._lease.acquire(self._config.asr_vram_mib) as held:
            job._set_stage(Stage.ASR, duration=duration)
            segments = await self._run_asr_watched(audio, job, held)

            # Присваиваем реплики ДО проверки вытеснения. Транскрипт уже получен,
            # и выбрасывать его только потому, что карту забрали секундой позже,
            # значит потерять минуты работы GPU: возобновление начнётся с перевода.
            job.cues = self._to_job_cues(build_cues(normalize_cues(segments)))
            job._emit({"type": "segmented", "total": len(job.cues)})
            held.check()

        await self._finish_translation(job)

    async def _run_asr_watched(self, audio, job: Job, held):
        """Распознавание, которое действительно прерывается.

        `asyncio.to_thread` отменой не прерывается, поэтому единственный способ
        освободить карту — убить сам процесс. Гоняем ожидание результата против
        флага вытеснения и по срабатыванию убиваем воркер; тот завершается
        по сигналу, и `_run_asr` поднимает Preempted.
        """
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

    def _run_asr(self, audio, job: Job) -> list[dict]:
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
            self._config.asr.language,
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

        try:
            out, err = proc.communicate(input=audio.tobytes(), timeout=3600)
        finally:
            job._asr_proc = None

        if proc.returncode != 0:
            # Убитый по вытеснению или отмене процесс — не ошибка распознавания:
            # его прервали намеренно, и задание должно уйти в паузу, а не в отказ.
            if proc.returncode < 0:
                raise Preempted(f"распознавание прервано сигналом {-proc.returncode}")

            raise RuntimeError(f"ASR не справился: {err.decode(errors='replace')[-500:]}")

        result = json.loads(out)

        if "error" in result:
            raise RuntimeError(result["error"])

        return result["cues"]

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
                translated = await asyncio.to_thread(translate_batch, batch, self._mt_model)
            except RETRYABLE as exc:
                # Ловим широко, а не только TranslateError: обрыв соединения с ollama,
                # её перезапуск или битый JSON — такие же поводы для лестницы починки.
                # Пропущенные мимо, они роняли всё задание вместе с готовым транскриптом,
                # на который уже потрачены минуты работы карты.
                log.warning("батч не перевёлся (%s), пробую половинами", exc)
                translated = await self._translate_halves(batch_cues, job, held)

            for problem_index, reason in validate(batch.cues, translated):
                log.info("реплика %d: %s", batch_cues[problem_index].index, reason)

            for cue, text in zip(batch_cues, translated):
                if text.strip():
                    cue.lines = text.split("\n")[:2]

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

    async def _translate_halves(self, cues: list[JobCue], job: Job, held) -> list[str]:
        """Батч не прошёл целиком — делим пополам. Обычно виноват один длинный кусок.

        Проверка вытеснения обязательна на каждом уровне рекурсии: на батче в сорок
        реплик спуск до поштучных попыток — это до семидесяти запросов к модели,
        то есть десятки минут удержания карты. Без проверки лестница чинила бы
        перевод, игнорируя, что пользователь давно ждёт свою очередь.
        """
        held.check()

        if len(cues) == 1:
            return [""]

        middle = len(cues) // 2
        out: list[str] = []

        for half in (cues[:middle], cues[middle:]):
            batch = Batch(cues=[c.src for c in half], title=job.info.title if job.info else "")

            try:
                out += await asyncio.to_thread(translate_batch, batch, self._mt_model)
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

    def get_or_create(self, video_id: str, target_lang: str = "ru") -> Job:
        job = self._jobs.get(video_id)

        if job is None or job.stage is Stage.ERROR:
            job = Job(video_id, target_lang)
            self._jobs[video_id] = job
            job.start(self.runner)
        elif job.stage is Stage.PAUSED:
            # Карту забирал пользователь. Реплики никуда не делись — продолжаем
            # то же задание, а не начинаем заново с распознавания.
            job.resume(self.runner)

        self._evict()

        return job

    def _evict(self) -> None:
        """Выбросить самые старые завершённые задания.

        Реплики двадцатиминутного ролика — это сотни объектов, и реестр без
        вытеснения рос бы всё время жизни процесса. Выбрасываем только то, что
        уже никуда не движется: незавершённое задание держит транскрипт, ради
        которого потрачена работа карты.
        """
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
