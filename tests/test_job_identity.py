"""Тождество задания: один ролик — одно задание, какие бы настройки ни прислали."""

import asyncio


def test_second_client_with_other_author_track_flag_reuses_job(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    assert second is first
    assert first.use_author_track is True


def test_hundred_disagreeing_polls_create_one_job(manager):
    for i in range(100):
        manager.get_or_create("abcdefghijk", "ru", i % 2 == 0)

    assert manager.stats()["jobs"] == 1
    assert manager.get("abcdefghijk").generation == 0
    # Один уцелевший объект ещё не доказательство: пересоздание каждый раз зовёт
    # start, а за ним идёт скачивание, которое отмена не останавливает. Конвейер
    # обязан стартовать ровно один раз.
    assert manager.started == ["abcdefghijk"]


def test_snapshot_reports_the_settings_the_track_was_made_with(manager):
    job = manager.get_or_create("abcdefghijk", "sr", False)
    snapshot = job.snapshot()

    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0


def test_second_client_with_other_target_lang_reuses_job_and_can_see_it(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "sr", True)

    assert second is first
    assert second.snapshot()["targetLang"] == "ru"


async def test_cursor_ahead_of_the_journal_resyncs_once(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    ahead = await job.poll(cursor=9999, timeout=0.01)

    assert "snapshot" in ahead
    assert ahead["cursor"] == 0

    again = await job.poll(cursor=ahead["cursor"], timeout=0.01)

    assert "snapshot" not in again


def test_cooled_retryable_failure_recreates_job_as_next_generation(manager):
    from helper import jobs

    first = manager.get_or_create("abcdefghijk", "ru", True)
    # Сокет подписан на ролик один раз, на первом задании: пересоздание обязано
    # перенести его очередь, иначе он так и слушал бы отменённое задание.
    queue: asyncio.Queue = asyncio.Queue()
    first.subscribe(queue)
    first._fail("сеть моргнула", retryable=True)
    # Пересоздаёт только остывший отказ: сдвигаем время отказа на срок остывания.
    first.failed_at -= jobs.RETRY_COOLDOWN

    second = manager.get_or_create("abcdefghijk", "ru", True)

    assert second is not first
    assert manager.get("abcdefghijk") is second
    assert second.generation == 1
    assert manager.started == ["abcdefghijk", "abcdefghijk"]
    assert first._subscribers == set()
    assert second._subscribers == {queue}

    seen = []

    while not queue.empty():
        seen.append(queue.get_nowait())

    # Догоняющий снимок при переносе несёт уже новый номер: поколение должно быть
    # выставлено до переноса подписчиков, а не после.
    assert seen[-1]["type"] == "snapshot"
    assert seen[-1]["generation"] == 1
