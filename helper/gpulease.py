"""Единственный пропуск на работу с GPU.

На этой карте 24 ГБ, из которых десктоп держит 4–6, а переводчик 17.3. Свободного
остаётся 1–3 ГБ, поэтому две модели одновременно не помещаются — не «туго», а вообще.
Отсюда весь модуль: любая работа с картой берёт один и тот же семафор, а между фазами
модель выгружается.

Второе назначение — не быть враждебным соседом. Пользователь работает со своей ollama
на той же карте, и у ollama нет никакого механизма реакции на внешнее давление VRAM:
пока модель резидентна, свободная память не перечитывается вообще. Чужой аллокатор
получает жёсткий OOM (system-memory fallback на Linux не поддерживается), а при
runtime-OOM срабатывает expireRunnersForRuntimeOOM и выгружает ВСЕ модели разом,
убивая чужую сессию. Поэтому перед захватом идёт admission control, а пока пропуск
удерживается — сторож, который отдаёт карту при первых признаках чужой работы.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("YT_TRANS_OLLAMA", "http://127.0.0.1:11434")

# Запас сверх заявленной потребности. Щедрый намеренно: ollama в собственных
# комментариях признаёт, что отчётность CUDA о свободной памяти запаздывает,
# а десктоп раздувается именно тогда, когда мы работаем — Chrome декодирует видео.
GUARD_MIB = 2048

WATCHDOG_INTERVAL = 1.0

# Сколько подряд чистых опросов нужно, чтобы вернуться к работе ПОСЛЕ вытеснения.
# Без гистерезиса мы бились бы о чужой keep_alive, захватывая и отдавая карту по кругу.
# На свободной карте не применяется: там пропуск выдаётся сразу.
REARM_POLLS = 10

# Предел ожидания допуска. Ждать бесконечно нельзя: ожидание идёт под семафором,
# поэтому застрявшее задание заморозило бы все остальные, а снаружи это выглядело бы
# как зависшая стадия без единого события. По истечении срока задание уходит
# в PAUSED с внятной причиной и возобновляется при следующем обращении.
ADMISSION_TIMEOUT = 120.0

# Порог, отделяющий чужую вычислительную задачу от десктопа. NVML числит
# compute-процессами Chrome, VSCode, Telegram, gnome-shell и прочих — они используют
# CUDA для композитинга и декодирования видео. Замерено на этой машине: самый
# прожорливый из них берёт 938 МиБ, тогда как любая ML-задача берёт гигабайты.
# Без этого порога «карта занята» было бы истинно всегда.
FOREIGN_MIB = 2048

# Раннеры ollama. Их нельзя опознавать по PID: раннер порождается службой ollama,
# нашим потомком не является и занимает всю память модели — поэтому наша собственная
# загруженная модель выглядела бы чужой вычислительной задачей, и демон вытеснял бы
# сам себя сразу после загрузки переводчика (ровно это и произошло на первом сквозном
# прогоне). Принадлежность моделей ollama определяется точно и по имени, через /api/ps,
# так что из проверки по процессам они исключаются целиком.
OLLAMA_RUNNERS = frozenset({"llama-server", "ollama", "ollama_llama_server", "ollama-runner"})


class Preempted(Exception):
    """Карту пришлось отдать: пользователь начал свою работу."""


@dataclass
class GpuState:
    free_mib: int
    foreign_pids: list[int] = field(default_factory=list)
    foreign_models: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.foreign_pids or self.foreign_models)


def _nvml():
    try:
        import pynvml
    except ImportError:
        return None

    try:
        pynvml.nvmlInit()
    except Exception:
        return None

    return pynvml


class GpuLease:
    """Один пропуск на всю работу с картой."""

    def __init__(self, ollama_url: str = OLLAMA_URL, guard_mib: int = GUARD_MIB) -> None:
        self._semaphore = asyncio.Semaphore(1)
        self._ollama_url = ollama_url
        self._guard_mib = guard_mib
        self._nvml = _nvml()
        self._own_pid = os.getpid()
        self._own_models: set[str] = set()

    def claim_model(self, name: str) -> None:
        """Пометить модель как свою, чтобы сторож не принял её за чужую."""
        self._own_models.add(name)

    def state(self) -> GpuState:
        """Снимок карты: сколько свободно и не работает ли кто-то ещё."""
        free_mib = 0
        foreign_pids: list[int] = []

        if self._nvml:
            try:
                handle = self._nvml.nvmlDeviceGetHandleByIndex(0)
                free_mib = self._nvml.nvmlDeviceGetMemoryInfo(handle).free // (1024 * 1024)

                for proc in self._nvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    used_mib = (proc.usedGpuMemory or 0) // (1024 * 1024)

                    # Десктоп отсеиваем по объёму, свой ASR-воркер — по родословной,
                    # раннеры ollama — по имени: их принадлежность решается точнее
                    # через /api/ps, где видно, чья именно модель загружена.
                    if used_mib < FOREIGN_MIB or _process_name(proc.pid) in OLLAMA_RUNNERS:
                        continue

                    if proc.pid != self._own_pid and not _is_descendant(proc.pid, self._own_pid):
                        foreign_pids.append(proc.pid)
            except Exception as exc:
                # Не глотаем: пустой снимок неотличим от «карта свободна», а по
                # документированному правилу ослепший сторож обязан уступать карту.
                log.warning("NVML недоступен: %s", exc)
                raise

        loaded = self._loaded_models()

        return GpuState(
            free_mib=free_mib,
            foreign_pids=foreign_pids,
            foreign_models=[m for m in loaded if m not in self._own_models],
        )

    def _loaded_models(self) -> list[str]:
        """Что сейчас загружено в ollama.

        Ошибку намеренно не глотаем. Это единственный канал, по которому видна работа
        пользователя: раннеры ollama исключены из проверки по процессам, поэтому
        трактовать недоступность /api/ps как «моделей нет» означало бы докладывать
        «карта свободна» ровно тогда, когда мы ослепли.
        """
        response = httpx.get(f"{self._ollama_url}/api/ps", timeout=3.0)
        response.raise_for_status()

        return [m.get("name", "") for m in response.json().get("models", [])]

    def is_resident(self, model: str) -> bool:
        """Загружена ли уже эта модель. Ошибку опроса считаем за «нет»."""
        try:
            return model in self._loaded_models()
        except Exception:
            return False

    def unload(self, model: str) -> None:
        """Выгрузить свою модель из ollama немедленно.

        keep_alive: 0 — единственный способ освободить VRAM сразу. Отрицательное
        значение использовать нельзя ни при каких условиях: планировщик ollama
        сортирует кандидатов на выгрузку по sessionDuration, приведённой к uint64,
        поэтому -1 сортируется последним и делает модель невыгружаемой.
        """
        with contextlib.suppress(Exception):
            httpx.post(
                f"{self._ollama_url}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=30.0,
            )

    @contextlib.asynccontextmanager
    async def acquire(self, need_mib: int, *, model: str | None = None, wait: bool = True):
        """Взять пропуск под задачу, которой нужно need_mib видеопамяти.

        Возвращает объект с флагом `preempted`: долгие операции обязаны его
        проверять и сворачиваться, когда он поднят.
        """
        if not wait and self._semaphore.locked():
            raise Preempted("карта занята другой задачей демона")

        async with self._semaphore:
            await self._await_admission(need_mib, model)

            holder = _Holder()
            watchdog = asyncio.create_task(self._watch(holder))

            try:
                yield holder
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog

    async def _await_admission(self, need_mib: int, model: str | None = None) -> None:
        """Дождаться, пока карта действительно свободна.

        Если наша собственная модель уже резидентна, память под неё просить не надо:
        ollama её не перезагружает. Без этой поправки фаза перевода требовала бы
        17.8 ГБ свободных ровно тогда, когда 17.3 из них уже заняты нашим же
        переводчиком, и допуск не прошёл бы никогда.
        """
        needed = 0 if model and self.is_resident(model) else need_mib
        clean = 0
        contended = False
        deadline = time.monotonic() + ADMISSION_TIMEOUT

        while True:
            try:
                state = await asyncio.to_thread(self.state)
                free_enough = state.free_mib >= needed + self._guard_mib
                busy = state.busy
                detail = f"свободно {state.free_mib} МиБ, модели {state.foreign_models}"
            except Exception as exc:
                # Неизвестное состояние карты — это «занято», а не «свободно».
                log.warning("состояние GPU неизвестно, жду: %s", exc)
                free_enough, busy, detail = False, True, f"состояние неизвестно ({exc})"

            if free_enough and not busy:
                clean += 1

                # На свободной карте ждать нечего. Гистерезис нужен только чтобы
                # не биться о чужой keep_alive после того, как нас уже вытеснили.
                if not contended or clean >= REARM_POLLS:
                    return
            else:
                if clean:
                    log.info("карта снова занята, счётчик чистых опросов сброшен")

                contended = True
                clean = 0

            # Ждать вечно нельзя: ожидание идёт под семафором, поэтому одно
            # застрявшее задание заморозило бы все остальные, а клиент видел бы
            # стадию без единого события. Отдаём семафор и сообщаем причину.
            if time.monotonic() >= deadline:
                raise Preempted(
                    f"карта не освободилась под {needed} МиБ за "
                    f"{ADMISSION_TIMEOUT:.0f} с: {detail}"
                )

            await asyncio.sleep(WATCHDOG_INTERVAL)

    async def _watch(self, holder: _Holder) -> None:
        """Пока пропуск удерживается — следить, не появился ли рядом пользователь.

        Любая ошибка внутри трактуется как повод уступить карту. Сторож — это и есть
        механизм безопасности, и если он ослеп, продолжать работу вслепую хуже, чем
        лишний раз отдать карту: неизвестное состояние здесь означает риск чужого OOM,
        а он у ollama выгружает все модели разом и убивает сессию пользователя.
        """
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                state = await asyncio.to_thread(self.state)

                if state.busy:
                    log.info(
                        "уступаю карту: процессы %s, модели %s",
                        state.foreign_pids,
                        state.foreign_models,
                    )
                    holder.preempt()

                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сторож GPU упал — уступаю карту на всякий случай")
            holder.preempt()


class _Holder:
    """Ручка удерживаемого пропуска."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def preempted(self) -> bool:
        return self._event.is_set()

    def preempt(self) -> None:
        self._event.set()

    def check(self) -> None:
        """Бросить Preempted, если карту пора отдать."""
        if self.preempted:
            raise Preempted("пользователь начал свою работу на GPU")

    async def wait_preempted(self) -> None:
        await self._event.wait()


def _process_name(pid: int) -> str:
    """Короткое имя процесса из /proc. Пустая строка, если процесс уже исчез."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _is_descendant(pid: int, ancestor: int) -> bool:
    """Является ли pid потомком ancestor. Нужно, чтобы не принять свой ASR-воркер за чужого."""
    seen = 0

    while pid > 1 and seen < 20:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                # Четвёртое поле — PPID, но имя процесса в скобках может содержать пробелы,
                # поэтому режем по последней закрывающей скобке.
                fields = handle.read().rsplit(")", 1)[1].split()
                pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            return False

        if pid == ancestor:
            return True

        seen += 1

    return False
