"""Пути, что создают задание, — POST /v1/jobs и операция watch, — передают признак
дорожки автора, который прислал клиент, а не умолчание, и отказывают не-boolean."""

import pytest

VIDEO = "abcdefghijk"
OTHER = "zzzzzzzzzzz"

# То, что клиент мог прислать вместо true или false. Строки и null легли бы в ключ
# реестра призрачным третьим вариантом, а 0 и 1 прошли бы нестрогую проверку на int.
NOT_A_BOOLEAN = [None, "false", "true", 0, 1]
REFUSAL = "authorTrack должен быть true или false"


def watch(video_id, **fields):
    return {"op": "watch", "videoId": video_id, **fields}


async def test_post_jobs_honours_author_track_false(client, manager):
    response = await client.post("/v1/jobs", json={"videoId": VIDEO, "authorTrack": False})

    assert response.status_code == 200
    job = manager.get(VIDEO, False)
    # Ровно один вариант, и это вариант клиента. Умолчание завело бы вариант с
    # дорожкой автора, то есть целый конвейер впустую для клиента с выключенной.
    assert job is not None
    assert manager.jobs_for(VIDEO) == [job]
    assert manager.started == [VIDEO]


async def test_post_jobs_defaults_author_track_to_true(client, manager):
    response = await client.post("/v1/jobs", json={"videoId": VIDEO})

    assert response.status_code == 200
    job = manager.get(VIDEO, True)
    assert job is not None
    assert manager.jobs_for(VIDEO) == [job]


@pytest.mark.parametrize("flag", NOT_A_BOOLEAN, ids=repr)
async def test_post_jobs_refuses_a_flag_that_is_not_a_boolean(client, manager, flag):
    response = await client.post("/v1/jobs", json={"videoId": VIDEO, "authorTrack": flag})

    assert response.status_code == 400
    assert response.json() == {"detail": REFUSAL}
    # Отказ приходит до реестра: ни задания, ни запущенного конвейера.
    assert manager.jobs_for(VIDEO) == []
    assert manager.started == []


async def test_watch_passes_the_flag_to_the_job(manager, connection):
    await connection.say(watch(VIDEO, authorTrack=False))
    snapshot = await connection.hear()

    job = manager.get(VIDEO, False)
    # Ровно один вариант, и это вариант клиента: см. POST /v1/jobs выше.
    assert job is not None
    assert manager.jobs_for(VIDEO) == [job]
    assert manager.started == [VIDEO]
    # Сокет подписан на это самое задание: догоняющий снимок несёт его настройку.
    assert snapshot["type"] == "snapshot"
    assert snapshot["authorTrack"] is False


async def test_watch_defaults_author_track_to_true(manager, connection):
    await connection.say(watch(VIDEO))
    snapshot = await connection.hear()

    job = manager.get(VIDEO, True)
    assert job is not None
    assert manager.jobs_for(VIDEO) == [job]
    assert snapshot["authorTrack"] is True


@pytest.mark.parametrize("flag", NOT_A_BOOLEAN, ids=repr)
async def test_watch_refuses_a_flag_that_is_not_a_boolean(manager, connection, flag):
    # Задание ролика уже есть: его завёл другой клиент. На нём и видно, что отказ не
    # подписал сокет. get читает None как «признака нет» и отдал бы самое свежее
    # задание ролика, так что нестрогая проверка подписала бы сокет на чужой вариант.
    job = manager.get_or_create(VIDEO, "ru", True)

    await connection.say(watch(VIDEO, authorTrack=flag))

    # Тот же вид события, что и другие отказы по ролику, а не новый тип сообщения.
    assert await connection.hear() == {
        "type": "state",
        "stage": "error",
        "videoId": VIDEO,
        "message": REFUSAL,
        "retryable": False,
    }

    # Событие задания, на которое сокет мог оказаться подписан, и следом снимок
    # другого ролика. Очередь сокета одна и идёт по порядку: будь он подписан, чужое
    # событие пришло бы раньше, а так следующим приходит именно снимок.
    job._emit({"type": "state", "stage": "probing"})
    await connection.say(watch(OTHER, authorTrack=False))

    assert (await connection.hear())["videoId"] == OTHER
    # Отказ не создал и не запустил ничего: VIDEO завёл сам тест, OTHER — второй watch.
    assert manager.jobs_for(VIDEO) == [job]
    assert manager.started == [VIDEO, OTHER]


async def test_watching_the_other_variant_moves_the_socket_to_it(manager, connection):
    await connection.say(watch(VIDEO, authorTrack=True))
    first = await connection.hear()
    await connection.say(watch(VIDEO, authorTrack=False))
    second = await connection.hear()

    # Каждый watch шлёт догоняющий снимок того варианта, на который подписал.
    assert (first["type"], first["authorTrack"]) == ("snapshot", True)
    assert (second["type"], second["authorTrack"]) == ("snapshot", False)

    with_track = manager.get(VIDEO, True)
    without = manager.get(VIDEO, False)
    # Сначала событие прежнего варианта, потом нового. Очередь сокета одна и идёт по
    # порядку: останься сокет подписанным и на прежний, первым пришло бы его событие.
    with_track._emit({"type": "state", "stage": "probing", "variant": "with-track"})
    without._emit({"type": "state", "stage": "probing", "variant": "without"})

    assert (await connection.hear())["variant"] == "without"
    # Ничего не отменено и не запущено заново: прежний вариант идёт как шёл, у него
    # просто нет слушателя. По одному запуску на каждое из двух заданий.
    assert manager.get(VIDEO, True) is with_track
    assert manager.started == [VIDEO, VIDEO]

    # И обратно: очередь возвращается на прежний вариант и уходит с нового.
    await connection.say(watch(VIDEO, authorTrack=True))
    back = await connection.hear()

    assert (back["type"], back["authorTrack"]) == ("snapshot", True)

    without._emit({"type": "state", "stage": "probing", "variant": "without-again"})
    with_track._emit({"type": "state", "stage": "probing", "variant": "with-track-again"})

    assert (await connection.hear())["variant"] == "with-track-again"


async def test_disconnect_after_a_move_leaves_no_subscription_on_either_variant(
    manager, connection
):
    await connection.say(watch(VIDEO, authorTrack=True))
    await connection.say(watch(VIDEO, authorTrack=False))
    # Другой клиент опросил прежний вариант, и самым свежим стал он, а не тот, на
    # котором стоит сокет: очистка не вправе ограничиться самым свежим заданием ролика.
    manager.get_or_create(VIDEO, "ru", True)

    await connection.disconnect()

    # Очередь соединения создаёт сам обработчик и наружу не отдаёт, поэтому о том,
    # что от неё ничего не осталось, судим по подписчикам самих заданий. Очистка при
    # разрыве идёт по ролику и снимает очередь со всех его вариантов: на каком из них
    # сокет оказался, он не помнит.
    assert manager.get(VIDEO, True)._subscribers == set()
    assert manager.get(VIDEO, False)._subscribers == set()


async def test_unwatch_after_a_move_stops_events_from_both_variants(manager, connection):
    await connection.say(watch(VIDEO, authorTrack=True))
    await connection.hear()
    await connection.say(watch(VIDEO, authorTrack=False))
    await connection.hear()
    # Другой клиент опросил прежний вариант, и самым свежим стал он, а не тот, на котором
    # стоит сокет: отписка не вправе ограничиться самым свежим заданием ролика.
    manager.get_or_create(VIDEO, "ru", True)

    await connection.say({"op": "unwatch", "videoId": VIDEO})

    # Оба варианта шлют по событию, а следом приходит снимок другого ролика. Очередь
    # сокета одна и идёт по порядку: останься сокет подписан хоть на один вариант, его
    # событие пришло бы раньше снимка.
    manager.get(VIDEO, True)._emit({"type": "state", "stage": "probing", "variant": "with-track"})
    manager.get(VIDEO, False)._emit({"type": "state", "stage": "probing", "variant": "without"})
    await connection.say(watch(OTHER))
    heard = await connection.hear()

    assert (heard["type"], heard.get("videoId")) == ("snapshot", OTHER)


async def test_watch_after_a_drop_subscribes_the_socket_to_the_new_job(manager, connection):
    await connection.say(watch(VIDEO))
    await connection.hear()
    old = manager.get(VIDEO, True)

    manager.drop(VIDEO)
    await connection.say(watch(VIDEO))
    snapshot = await connection.hear()
    new = manager.get(VIDEO, True)

    # Сокет этот ролик уже наблюдал, но его очередь осталась на сброшенном задании.
    # Условие «ролик уже в watched» подписку пропустило бы, и новое задание шло бы
    # мимо сокета: спрашивать надо у самого задания.
    assert new is not old
    assert snapshot["type"] == "snapshot"

    new._emit({"type": "state", "stage": "probing", "variant": "new"})

    assert (await connection.hear())["variant"] == "new"


async def test_repeated_watch_resumes_a_paused_job_but_sends_no_second_snapshot(
    manager, connection
):
    from helper.jobs import Stage

    await connection.say(watch(VIDEO))
    await connection.hear()
    job = manager.get(VIDEO, True)
    # Карту забрал пользователь: задание приостановлено, и сокет об этом знает.
    job.stage = Stage.PAUSED

    await connection.say(watch(VIDEO))

    # watch идёт через get_or_create КАЖДЫЙ раз: это единственное место, откуда
    # возобновляется приостановленное задание. Запуск и возобновление.
    assert manager.started == [VIDEO, VIDEO]

    # Очередь уже стояла на этом задании, второго догоняющего снимка быть не должно.
    # Следом снимок другого ролика: будь лишний, он пришёл бы раньше.
    await connection.say(watch(OTHER))

    assert (await connection.hear())["videoId"] == OTHER
