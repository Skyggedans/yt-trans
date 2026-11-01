"""Два предела на случай открытого порта: незавершённых заданий не больше MAX_ACTIVE_JOBS,
а отложенного звука на диске не больше PAUSED_AUDIO_MAX_BYTES."""

import os
import shutil

import numpy as np
import pytest

from helper import jobs
from helper.jobs import (
    MAX_ACTIVE_JOBS,
    PAUSED_AUDIO_DIR,
    PAUSED_AUDIO_MAX_BYTES,
    PAUSED_AUDIO_TTL,
    CapacityError,
    JobCue,
    PermanentJobError,
    Stage,
)

# Ровно одиннадцать знаков, как у настоящего идентификатора ролика.
OVERFLOW = "overflowxyz"


def watch(video_id, **fields):
    return {"op": "watch", "videoId": video_id, **fields}


@pytest.fixture
def full(manager):
    """Реестр, где незавершённых заданий ровно MAX_ACTIVE_JOBS: ещё одно уже не помещается."""
    return [manager.get_or_create(f"video{i:05d}a", "ru", True) for i in range(MAX_ACTIVE_JOBS)]


@pytest.fixture
def sentence(manager, full):
    """Фраза, которой демон отказывает: эндпоинты сверяются с ней, а не с копией текста."""
    with pytest.raises(CapacityError) as refused:
        manager.get_or_create(OVERFLOW, "ru", True)

    return str(refused.value)


# ── предел на незавершённые задания ──────────────────────────────────────


def test_active_jobs_are_capped(manager, full):
    with pytest.raises(CapacityError) as refused:
        manager.get_or_create(OVERFLOW, "ru", True)

    message = str(refused.value)

    # Фраза для зрителя: сколько роликов уже в работе и что место освободится само.
    assert str(MAX_ACTIVE_JOBS) in message
    assert "освободится" in message
    # Отказ ничего не завёл и ничего не запустил.
    assert manager.get(OVERFLOW) is None
    assert manager.started == [job.video_id for job in full]


def test_overflow_is_not_a_permanent_refusal():
    # Контракт PermanentJobError — отказ, который не пройдёт сам, и бегун по нему
    # ставит заданию retryable=False. Переполнение проходит, как только закончится
    # любое задание, и клиенту оно уходит как повторяемое.
    assert not issubclass(CapacityError, PermanentJobError)


@pytest.mark.parametrize("stage", list(Stage), ids=lambda stage: stage.value)
def test_a_fresh_job_holds_a_slot_unless_it_is_finished(manager, monkeypatch, stage):
    # Готовые остаются в реестре: при заданиях сверх MAX_RETAINED_JOBS _evict выбросил
    # бы их сам, и счёт по незавершённым нельзя было бы отличить от счёта по всему реестру.
    monkeypatch.setattr(jobs, "MAX_RETAINED_JOBS", 100)
    # Свежая пауза держит место наравне с идущим заданием: зритель мог лишь отойти, и
    # его опрос возобновит её. Возобновляется она только по запросу клиента, поэтому
    # брошенная держала бы место вечно, и простоявшая дольше PAUSED_AUDIO_TTL (тот же
    # срок, что у её звука) в счёт не идёт: см. test_stale_pauses_do_not_hold_a_slot.
    first = manager.get_or_create("video00000a", "ru", True)
    first.stage = stage

    for i in range(1, MAX_ACTIVE_JOBS):
        manager.get_or_create(f"video{i:05d}a", "ru", True)

    if stage in (Stage.DONE, Stage.ERROR):
        assert manager.get_or_create(OVERFLOW, "ru", True) is not None
    else:
        with pytest.raises(CapacityError):
            manager.get_or_create(OVERFLOW, "ru", True)


def test_existing_jobs_are_still_served_at_the_cap(manager, full):
    paused = full[3]
    paused.stage = Stage.PAUSED
    started = list(manager.started)

    # Предел стережёт только создание. Опрос зрителя по заданию, что уже есть, отказом
    # не встречают, сколько бы их ни было.
    for job in full:
        assert manager.get_or_create(job.video_id, "ru", True) is job

    # Приостановленное при этом возобновилось: оно уже было в счёте, и его возобновление
    # числа заданий не меняет.
    assert manager.started == started + [paused.video_id]


def test_stale_pauses_do_not_hold_a_slot(manager, clock):
    # Пауза возобновляется только запросом клиента, сама она не продолжится. Брошенная,
    # она осталась бы в реестре до перезапуска демона, а шестнадцать таких закрыли бы
    # его для новых роликов насовсем. Поэтому простоявшая дольше PAUSED_AUDIO_TTL, то
    # есть дольше срока, по которому _evict убирает её звук, в счёт не идёт.
    paused = [manager.get_or_create(f"video{i:05d}a", "ru", True) for i in range(MAX_ACTIVE_JOBS)]

    for job in paused:
        job.stage = Stage.PAUSED

    # Ровно срок ещё не старость: сравнение строгое, как у звука в _evict.
    clock(PAUSED_AUDIO_TTL)

    with pytest.raises(CapacityError):
        manager.get_or_create(OVERFLOW, "ru", True)

    clock(1)

    assert manager.get_or_create(OVERFLOW, "ru", True) is not None


def test_a_stale_pause_stays_registered_and_is_resumed_at_the_cap(manager, clock):
    stale = manager.get_or_create("abcdefghijk", "ru", True)
    stale.stage = Stage.PAUSED
    stale.cues = [JobCue(index=0, start=0.0, end=1.5, src="Zdravo", lines=["Привет"])]
    clock(PAUSED_AUDIO_TTL + 1)

    # Остальные места заняты идущими заданиями: брошенная пауза у них ничего не отнимает.
    for i in range(MAX_ACTIVE_JOBS):
        manager.get_or_create(f"video{i:05d}a", "ru", True)

    with pytest.raises(CapacityError):
        manager.get_or_create(OVERFLOW, "ru", True)

    started = list(manager.started)

    # Пауза осталась в реестре со своими репликами, и вернувшийся зритель возобновляет её
    # как любое существующее задание: предел стережёт создание, а не возобновление.
    assert manager.get("abcdefghijk", True) is stale
    assert [cue.src for cue in stale.cues] == ["Zdravo"]
    assert manager.get_or_create("abcdefghijk", "ru", True) is stale
    assert manager.started == started + ["abcdefghijk"]


def test_each_variant_of_a_video_counts_once(manager):
    # Задание — пара «ролик и дорожка автора», и второй вариант известного ролика
    # занимает своё место, а не делит с первым.
    for i in range(MAX_ACTIVE_JOBS - 1):
        manager.get_or_create(f"video{i:05d}a", "ru", True)

    manager.get_or_create("video00000a", "ru", False)

    assert manager.stats()["jobs"] == MAX_ACTIVE_JOBS

    # Место последнее, и новый вариант другого известного ролика уже не помещается.
    with pytest.raises(CapacityError):
        manager.get_or_create("video00001a", "ru", False)

    # Оба варианта первого ролика по-прежнему отвечают на опрос.
    assert manager.get_or_create("video00000a", "ru", True) is manager.get("video00000a", True)
    assert manager.get_or_create("video00000a", "ru", False) is manager.get("video00000a", False)


def test_cooled_failure_is_retried_only_when_there_is_room(manager, monkeypatch):
    # Предел уменьшен, чтобы упавшее задание дожило до опроса: сверх MAX_RETAINED_JOBS
    # заданий _evict выбрасывает завершённые сразу, и при настоящем пределе этот случай
    # наступает, только если упавшее задание никто не опрашивал со времени отказа.
    monkeypatch.setattr(jobs, "MAX_ACTIVE_JOBS", 2)
    failed = manager.get_or_create("abcdefghijk", "ru", True)
    failed._fail("сеть моргнула", retryable=True)
    failed.failed_at -= jobs.RETRY_COOLDOWN
    busy = [manager.get_or_create(f"video{i:05d}a", "ru", True) for i in range(2)]
    started = list(manager.started)

    # Повтор — это новое задание, и при переполнении его не заводят. Но и отказом
    # опрос по существующему заданию не встречают: оно отдаётся как есть, а прежнее
    # не снимается, иначе его подписчики остались бы на отменённом.
    assert manager.get_or_create("abcdefghijk", "ru", True) is failed
    assert manager.get("abcdefghijk", True) is failed
    assert failed.generation == 0
    assert manager.started == started

    busy[0].stage = Stage.DONE
    retried = manager.get_or_create("abcdefghijk", "ru", True)

    assert retried is not failed
    assert retried.generation == 1
    assert manager.started == started + ["abcdefghijk"]


# ── отказ на путях, что создают задание ──────────────────────────────────


async def test_post_jobs_refuses_with_429_and_the_sentence(client, manager, full, sentence):
    response = await client.post("/v1/jobs", json={"videoId": OVERFLOW})

    assert response.status_code == 429
    assert response.json() == {"detail": sentence}
    assert manager.get(OVERFLOW) is None
    assert manager.started == [job.video_id for job in full]

    # Задание, что уже есть, по-прежнему отвечает: повторный POST его не создаёт.
    existing = await client.post("/v1/jobs", json={"videoId": full[0].video_id})

    assert existing.status_code == 200


async def test_watch_refuses_with_an_error_event_and_subscribes_nothing(
    manager, connection, full, sentence
):
    await connection.say(watch(OVERFLOW))

    assert await connection.hear() == {
        "type": "state",
        "stage": "error",
        "videoId": OVERFLOW,
        "message": sentence,
        "retryable": True,
    }
    assert manager.get(OVERFLOW) is None
    assert manager.started == [job.video_id for job in full]
    # Очередь соединения наружу не отдаётся, поэтому о подписке судим по заданиям: ни
    # на одном из них сокет не стоит.
    assert all(not job._subscribers for job in full)

    # В очереди сокета после отказа ничего не осталось: следующим приходит снимок
    # существующего задания, на которое watch по-прежнему подписывает и при пределе.
    await connection.say(watch(full[0].video_id))
    heard = await connection.hear()

    assert (heard["type"], heard["videoId"]) == ("snapshot", full[0].video_id)
    assert heard["stage"] == "queued"


async def test_events_with_create_answers_200_with_an_error_snapshot(
    client, manager, full, sentence
):
    params = {"create": "true", "authorTrack": "false", "target": "sr", "cursor": "123"}

    response = await client.get(f"/v1/jobs/{OVERFLOW}/events", params=params)

    # 200, а не 429: расширение на любой другой ответ пишет «демон ответил …» и
    # повторяет запрос через паузу, а снимок со стадией error показывает фразу и
    # останавливает опрос.
    assert response.status_code == 200

    body = response.json()
    snapshot = body["snapshot"]

    assert body["cursor"] == 123
    assert body["events"] == []
    assert snapshot["stage"] == "error"
    assert snapshot["error"] == sentence
    assert snapshot["retryable"] is True
    # Вид снимка — ровно тот, что выдаёт Job.snapshot(): те же поля, а не их копия.
    assert snapshot.keys() == full[0].snapshot().keys()
    assert snapshot["type"] == "snapshot"
    assert snapshot["videoId"] == OVERFLOW
    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0
    assert snapshot["waiting"] is None
    assert snapshot["title"] == ""
    assert (snapshot["total"], snapshot["translated"], snapshot["cues"]) == (0, 0, [])
    # Задание для снимка в реестр не попало и не запускалось.
    assert manager.get(OVERFLOW) is None
    assert manager.jobs_for(OVERFLOW) == []
    assert manager.started == [job.video_id for job in full]


async def test_events_for_an_existing_job_are_served_at_the_cap(client, manager, full):
    # Слишком старый курсор: ответ приходит снимком сразу, без ожидания новых событий.
    response = await client.get(f"/v1/jobs/{full[0].video_id}/events", params={"cursor": "-1"})

    assert response.status_code == 200

    snapshot = response.json()["snapshot"]

    # Это снимок настоящего задания, а не отказ.
    assert snapshot["videoId"] == full[0].video_id
    assert snapshot["stage"] == "queued"
    assert snapshot["error"] is None


# ── предел на отложенный звук ────────────────────────────────────────────

# Звук тестовых пауз: тысяча отсчётов float32. Настоящий бюджет на таких объёмах не
# проверить, поэтому тесты ниже подменяют его на несколько таких кусков.
CHUNK = 1000
CHUNK_BYTES = CHUNK * 4


@pytest.fixture
def audio_dir(manager):
    """Каталог отложенного звука: к началу теста чистый, по окончании убранный.

    Убирать надо и ради разреженных файлов на гигабайты: места они не занимают, но
    остались бы в каталоге кэша до конца прогона.
    """
    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    yield PAUSED_AUDIO_DIR

    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)


def stash(job, mtime):
    """Приостановить задание с настоящим отложенным звуком и задним числом записи."""
    job.stage = Stage.PAUSED
    job.stash_audio(np.zeros(CHUNK, dtype="<f4"))
    os.utime(job._audio_path, (mtime, mtime))

    return job._audio_path


def test_paused_audio_over_budget_is_discarded_oldest_first(manager, audio_dir):
    half = PAUSED_AUDIO_MAX_BYTES // 2 + 1
    made = []

    # Файлы без хозяина: задания в реестре у них нет, как у звука, оставшегося от
    # погибшего задания.
    for i, name in enumerate(("old.f32", "middle.f32", "fresh.f32")):
        path = audio_dir / name

        with path.open("wb") as handle:  # разреженный файл: места не занимает
            handle.truncate(half)

        os.utime(path, (1000 + i, 1000 + i))
        made.append(path)

    manager._evict()

    assert not made[0].exists()
    # Убирают, пока сумма выше предела: после первого файла остаются два по половине
    # с лишним, и они всё ещё не помещаются.
    assert not made[1].exists()
    assert made[2].exists()
    assert sum(f.stat().st_size for f in audio_dir.iterdir()) <= PAUSED_AUDIO_MAX_BYTES


def test_budget_eviction_goes_through_the_owning_job(manager, audio_dir, monkeypatch):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", CHUNK_BYTES)
    # Задания заводятся все до записи звука: get_or_create вытесняет, и бюджет сработал
    # бы раньше, чем всё разложено.
    first = manager.get_or_create("video00000a", "ru", True)
    last = manager.get_or_create("video00001a", "ru", True)
    # Старшинство одно на файлы обоих видов: самый старый принадлежит заданию, средний —
    # никому, самый свежий снова заданию.
    first_file = stash(first, mtime=1000)
    orphan = audio_dir / "orphan.f32"
    orphan.write_bytes(bytes(CHUNK_BYTES))
    os.utime(orphan, (2000, 2000))
    last_file = stash(last, mtime=3000)

    manager._evict()

    # Файл задания убран через него: ручка не осталась висеть на пропавшем файле.
    assert first._audio_path is None
    assert first.take_audio() is None
    assert not first_file.exists()
    # Следом самый старый из оставшихся, у которого хозяина нет, — его просто стирают.
    assert not orphan.exists()
    # Свежий на месте и по-прежнему читается.
    assert last._audio_path == last_file
    assert last.take_audio().size == CHUNK


def test_paused_audio_exactly_at_the_budget_stays(manager, audio_dir, monkeypatch):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", 2 * CHUNK_BYTES)
    first = manager.get_or_create("video00000a", "ru", True)
    second = manager.get_or_create("video00001a", "ru", True)
    paths = [stash(first, mtime=1000), stash(second, mtime=2000)]

    manager._evict()

    # «Пока сумма выше предела»: равная пределу уже помещается.
    assert all(path.exists() for path in paths)
    assert first._audio_path is not None
    assert second._audio_path is not None


def test_time_cleanup_runs_before_the_budget(manager, audio_dir, monkeypatch):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", CHUNK_BYTES + 1)
    watched = manager.get_or_create("video00000a", "ru", True)
    abandoned = manager.get_or_create("video00001a", "ru", True)
    # У опрашиваемой паузы файл старше, у брошенной новее: по одному возрасту файлов
    # бюджет убрал бы живую.
    stash(watched, mtime=1000)
    stash(abandoned, mtime=2000)
    abandoned.touched_at -= PAUSED_AUDIO_TTL + 1

    manager._evict()

    # Брошенную убрало время, после чего сумма уложилась в предел, и бюджету делать
    # уже нечего.
    assert abandoned._audio_path is None
    assert watched._audio_path is not None


class FakeTask:
    """Задача задания в тех пределах, что спрашивают _evict и счёт мест: закончилась ли она."""

    def __init__(self, finished):
        self._finished = finished

    def done(self):
        return self._finished


# Что считается брошенной паузой: приостановлено, задачи нет или она закончилась, и
# к заданию дольше PAUSED_AUDIO_TTL никто не обращался. Остальные строки отличаются от
# первой ровно одним признаком каждая.
STALE_PAUSE_CASES = [
    pytest.param(Stage.PAUSED, PAUSED_AUDIO_TTL + 1, None, True, id="paused-without-a-task"),
    pytest.param(Stage.PAUSED, PAUSED_AUDIO_TTL + 1, FakeTask(True), True, id="paused-task-done"),
    pytest.param(Stage.PAUSED, PAUSED_AUDIO_TTL, None, False, id="paused-exactly-at-the-ttl"),
    pytest.param(
        Stage.PAUSED, PAUSED_AUDIO_TTL + 1, FakeTask(False), False, id="paused-task-running"
    ),
    pytest.param(Stage.TRANSLATING, PAUSED_AUDIO_TTL + 1, None, False, id="not-a-pause"),
]


@pytest.mark.parametrize(("stage", "age", "task", "stale"), STALE_PAUSE_CASES)
def test_one_staleness_decides_both_the_audio_and_the_slot(
    manager, audio_dir, clock, stage, age, task, stale
):
    # Один признак на звук и на место: брошенная пауза теряет и то и другое, а любое
    # другое задание сохраняет оба. Разойдись они, звук паузы пропал бы, а место за ней
    # осталось, или наоборот.
    subject = manager.get_or_create("video00000a", "ru", True)

    for i in range(1, MAX_ACTIVE_JOBS):
        manager.get_or_create(f"video{i:05d}a", "ru", True)

    stash(subject, mtime=1000)
    subject.stage = stage
    subject._task = task
    clock(age)

    manager._evict()

    assert (subject._audio_path is None) is stale

    if stale:
        assert manager.get_or_create(OVERFLOW, "ru", True) is not None
    else:
        with pytest.raises(CapacityError):
            manager.get_or_create(OVERFLOW, "ru", True)


def test_evict_without_a_paused_audio_directory_is_quiet(manager):
    # Каталог появляется при первой отсрочке звука, а после запуска его нет вовсе.
    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)

    manager._evict()

    assert not PAUSED_AUDIO_DIR.exists()
