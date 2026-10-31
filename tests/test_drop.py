"""Сброс по клиенту: уходящий снимает свою регистрацию, а работа, которую ещё смотрит
другой, остаётся. Без ключа сброс отменяет всё, как до появления ключей."""

import pytest

from helper import jobs
from helper.jobs import PLAYHEAD_TTL

VIDEO = "abcdefghijk"
OTHER = "zzzzzzzzzzz"


def test_drop_by_one_of_two_clients_keeps_the_job(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    assert manager.drop(VIDEO, "desktop") is False
    assert manager.get(VIDEO) is job


def test_drop_by_the_last_client_cancels(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)

    assert manager.drop(VIDEO, "tablet") is True
    assert manager.get(VIDEO) is None


def test_drop_without_client_id_cancels_as_today(manager):
    manager.get_or_create(VIDEO, "ru", True)

    assert manager.drop(VIDEO) is True
    assert manager.get(VIDEO) is None


def test_drop_of_an_unknown_video_is_quiet(manager):
    assert manager.drop(OTHER, "tablet") is False


async def test_drop_endpoint_reports_whether_it_cancelled(client, manager):
    manager.get_or_create(VIDEO, "ru", True)
    response = await client.post(f"/v1/jobs/{VIDEO}/drop", json={})

    assert response.json() == {"dropped": VIDEO, "cancelled": True}


def test_client_that_dropped_stops_steering_the_job_that_stays(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 1200.0)

    manager.drop(VIDEO, "desktop")

    # Снятая регистрация — это и снятая позиция: ушедший зритель больше не оттягивает
    # перевод от оставшихся и не ждёт, пока его запись протухнет за PLAYHEAD_TTL.
    assert [job.aim() for _ in range(3)] == [10.0, 10.0, 10.0]


def test_drop_cancels_the_job_only_when_nobody_is_left(manager, monkeypatch):
    cancelled = []
    monkeypatch.setattr(jobs.Job, "cancel", lambda job: cancelled.append(job))
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    manager.drop(VIDEO, "desktop")

    # Снять регистрацию — не отменить: скачивание и распознавание идут для планшета.
    assert cancelled == []

    manager.drop(VIDEO, "tablet")

    # Отмена настоящая, а не один вынос из реестра: без неё качалка и распознавание
    # продолжали бы работать, никому не нужные.
    assert cancelled == [job]


def test_repeated_drop_by_a_client_does_not_cancel_for_the_others(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    # Повтор запроса (обрыв сети, нетерпеливое нажатие) снимает уже снятого клиента.
    # Это тихий пропуск, а не повод отменить задание, которое ещё смотрит другой.
    assert manager.drop(VIDEO, "tablet") is False
    assert manager.drop(VIDEO, "tablet") is False
    assert manager.get(VIDEO) is job
    assert [job.aim() for _ in range(2)] == [20.0, 20.0]


def test_drop_leaves_every_variant_while_somebody_watches(manager):
    with_track = manager.get_or_create(VIDEO, "ru", True)
    without = manager.get_or_create(VIDEO, "ru", False)

    for job in (with_track, without):
        job.report_playhead("tablet", 10.0)
        job.report_playhead("desktop", 20.0)

    # Сброс приходит без признака дорожки, и какой из вариантов смотрел клиент, демон не
    # знает: ключ снимается со всех, а отменяются те, где никого не осталось.
    assert manager.drop(VIDEO, "desktop") is False
    assert manager.get(VIDEO, True) is with_track
    assert manager.get(VIDEO, False) is without
    assert [with_track.aim(), without.aim()] == [10.0, 10.0]

    assert manager.drop(VIDEO, "tablet") is True
    assert manager.jobs_for(VIDEO) == []


def test_drop_cancels_only_the_variants_nobody_watches_any_more(manager):
    watched = manager.get_or_create(VIDEO, "ru", True)
    abandoned = manager.get_or_create(VIDEO, "ru", False)
    watched.report_playhead("tablet", 10.0)
    watched.report_playhead("desktop", 20.0)
    abandoned.report_playhead("desktop", 20.0)

    # Настольный смотрел оба варианта, переключая настройку, планшет — один. Вариант,
    # где кроме ушедшего никого нет, отменяется, а второй продолжает идти для планшета.
    assert manager.drop(VIDEO, "desktop") is True
    assert manager.get(VIDEO, True) is watched
    assert manager.get(VIDEO, False) is None


def test_drop_without_client_id_cancels_every_variant_and_every_client(manager):
    with_track = manager.get_or_create(VIDEO, "ru", True)
    without = manager.get_or_create(VIDEO, "ru", False)

    for job in (with_track, without):
        job.report_playhead("tablet", 10.0)

    # Намеренно: расширение ключа не шлёт, а сбросить задание целиком уже умело — так
    # работает его кнопка повтора после отказа. Добавляющее изменение не должно отнимать
    # у старого клиента то, что он умел, поэтому «без ключа» остаётся «отменить всё», даже
    # когда у задания есть живой планшет.
    assert manager.drop(VIDEO) is True
    assert manager.jobs_for(VIDEO) == []


def test_drop_does_not_count_a_client_that_went_silent(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    clock(PLAYHEAD_TTL + 1)
    job.report_playhead("desktop", 21.0)

    # Планшет молчит дольше срока: вкладку закрыли или усыпили, и снять её некому. Её
    # просроченная запись не держит задание, иначе оно жило бы после ухода последнего
    # живого зрителя.
    assert manager.drop(VIDEO, "desktop") is True
    assert manager.get(VIDEO) is None


def test_drop_counts_a_client_exactly_the_ttl_silent_as_live(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)

    clock(PLAYHEAD_TTL)
    job.report_playhead("desktop", 20.0)

    # Тот же срок, что у круга в aim(): «старше» значит строго старше. Вместе с тестом
    # выше граница зажата с обеих сторон.
    assert manager.drop(VIDEO, "desktop") is False
    assert manager.get(VIDEO) is job


async def test_drop_endpoint_accepts_a_request_without_a_body(client, manager):
    manager.get_or_create(VIDEO, "ru", True)

    # Ровно то, что шлёт расширение (extension/background.js, повтор после отказа): POST
    # без тела и без Content-Type, только с Authorization. Сделай тело обязательным, и
    # этот повтор превратится в 422.
    response = await client.post(f"/v1/jobs/{VIDEO}/drop")

    assert response.request.content == b""
    assert "content-type" not in response.request.headers
    assert response.status_code == 200
    assert response.json() == {"dropped": VIDEO, "cancelled": True}
    assert manager.get(VIDEO) is None


async def test_drop_endpoint_with_a_client_id_keeps_the_job_for_the_others(client, manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)
    url = f"/v1/jobs/{VIDEO}/drop"

    first = await client.post(url, json={"clientId": "desktop"})

    assert first.status_code == 200
    assert first.json() == {"dropped": VIDEO, "cancelled": False}
    assert manager.get(VIDEO) is job

    second = await client.post(url, json={"clientId": "tablet"})

    assert second.json() == {"dropped": VIDEO, "cancelled": True}
    assert manager.get(VIDEO) is None


async def test_drop_endpoint_for_an_unknown_video_is_quiet(client, manager):
    response = await client.post(f"/v1/jobs/{OTHER}/drop", json={"clientId": "tablet"})

    # Как и раньше, 200, а не 404: сбросить то, чего нет, не ошибка. И ничего не создаётся.
    assert response.status_code == 200
    assert response.json() == {"dropped": OTHER, "cancelled": False}
    assert manager.jobs_for(OTHER) == []
    assert manager.started == []


@pytest.mark.parametrize(
    "client_id",
    ["", "x" * 65, 123, True, ["a"], {"a": 1}],
    ids=["empty", "too-long", "int", "bool", "list", "dict"],
)
async def test_drop_with_an_unusable_client_id_cannot_cancel_for_everyone(
    client, manager, client_id
):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)

    response = await client.post(f"/v1/jobs/{VIDEO}/drop", json={"clientId": client_id})

    # Присланный негодный ключ — не «ключа нет». Он сводится к "legacy", как у позиции
    # просмотра (см. _client_key), а право отменить всё остаётся за теми, кто ключа не
    # присылает вовсе. Список и словарь заодно показывают, что падения на хэшировании нет.
    assert response.status_code == 200
    assert response.json() == {"dropped": VIDEO, "cancelled": False}
    assert manager.get(VIDEO) is job
