"""Оснастка тестов демона: реестр заданий на временных путях и клиент поверх приложения."""

import atexit
import os
import shutil
import tempfile

import httpx
import pytest

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
