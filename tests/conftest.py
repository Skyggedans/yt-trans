"""Оснастка тестов демона: реестр заданий на временных путях и клиент поверх приложения."""

import asyncio
import atexit
import os
import shutil
import tempfile
import time

import httpx
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
