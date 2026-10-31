"""Тождество задания: пара «ролик и дорожка автора»; язык перевода в ключ не входит."""

import asyncio


def emit_events(job, count):
    """Дописать в журнал задания count событий тем же способом, что и конвейер."""
    for number in range(count):
        job._emit({"type": "state", "stage": "probing", "n": number})


def test_client_with_other_author_track_flag_gets_its_own_job(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    assert second is not first
    assert second.use_author_track is False
    # Первое задание то же самое, флаг у него цел, и заново оно не запускалось:
    # два запуска на два задания, третьего не было бы, будь первое пересоздано.
    assert manager.get("abcdefghijk", True) is first
    assert first.use_author_track is True
    assert first.generation == 0
    assert manager.started == ["abcdefghijk", "abcdefghijk"]


def test_hundred_disagreeing_polls_create_exactly_two_jobs(manager):
    for i in range(100):
        manager.get_or_create("abcdefghijk", "ru", i % 2 == 0)

    assert manager.stats()["jobs"] == 2
    assert manager.get("abcdefghijk", True).generation == 0
    assert manager.get("abcdefghijk", False).generation == 0
    # Считаем запуски, а не объекты: пересоздание каждый раз зовёт start, а за ним
    # идёт скачивание, которое отмена не останавливает. Каждый вариант обязан
    # стартовать ровно один раз и дальше только отвечать на опросы.
    assert manager.started == ["abcdefghijk", "abcdefghijk"]


def test_snapshot_reports_the_settings_the_track_was_made_with(manager):
    job = manager.get_or_create("abcdefghijk", "sr", False)
    snapshot = job.snapshot()

    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0


def test_second_client_with_other_target_lang_reuses_job_and_can_see_it(manager):
    # Язык перевода в ключ не входит: он один на всех, и задание делается на том,
    # с каким его создал первый обратившийся.
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "sr", True)

    assert second is first
    assert second.snapshot()["targetLang"] == "ru"


async def test_cursor_ahead_of_the_journal_resyncs_once(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    # Журнал у каждого задания отсчитывается от своего основания, а не от нуля,
    # поэтому «за концом» считаем от конца самого журнала.
    ahead = await job.poll(cursor=job.journal_end + 9999, timeout=0.01)

    assert "snapshot" in ahead
    assert ahead["cursor"] == job.journal_end

    again = await job.poll(cursor=ahead["cursor"], timeout=0.01)

    assert "snapshot" not in again


async def test_fresh_client_at_cursor_zero_gets_one_snapshot_and_then_waits(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)

    # С нуля начинает каждый свежий клиент. Основание журнала случайное, поэтому
    # нуль для него слишком стар: снимок приходит сразу, а не после ожидания первого
    # события. Дальше клиент идёт с курсором из ответа и ждёт как обычно, плотного
    # цикла нет.
    first = await job.poll(cursor=0, timeout=0.01)

    assert "snapshot" in first
    assert first["cursor"] == job.journal_end

    again = await job.poll(cursor=first["cursor"], timeout=0.01)

    assert "snapshot" not in again
    assert again["events"] == []


async def test_cursor_from_the_other_variant_gets_a_snapshot(manager):
    source = manager.get_or_create("abcdefghijk", "ru", True)
    destination = manager.get_or_create("abcdefghijk", "ru", False)
    emit_events(source, 2)
    emit_events(destination, 5)
    # Курсор, с которым клиент уходит с источника на соседний вариант (так делает
    # расширение при смене переключателя у идущего задания). Он короче журнала
    # назначения, и при общем нуле отсчёта сошёл бы там за свой: клиент получил бы
    # хвост чужого журнала и никогда не увидел бы его начала.
    held = source.journal_end

    answer = await destination.poll(cursor=held, timeout=0.01)

    assert "snapshot" in answer
    assert answer["events"] == []
    assert answer["cursor"] == destination.journal_end


async def test_cursor_from_a_dropped_job_gets_a_snapshot_after_recreation(manager):
    dropped = manager.get_or_create("abcdefghijk", "ru", True)
    emit_events(dropped, 3)
    held = dropped.journal_end

    manager.drop("abcdefghijk")
    recreated = manager.get_or_create("abcdefghijk", "ru", True)
    emit_events(recreated, 5)

    answer = await recreated.poll(cursor=held, timeout=0.01)

    # Тот же ключ, но журнал другой: позиция из прежнего здесь ничего не значит.
    assert recreated is not dropped
    assert "snapshot" in answer
    assert answer["events"] == []


def test_fresh_jobs_start_their_journals_at_different_positions(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    # Конец журнала без событий и есть его основание. Не значение проверяем, а то,
    # что у двух заданий оно разное: именно поэтому чужой курсор не годится.
    assert first.journal_end != second.journal_end


def test_journal_positions_stay_exact_in_javascript(monkeypatch):
    from helper import jobs

    asked = []

    def widest(bits):
        asked.append(bits)

        return (1 << bits) - 1

    # Генератор подменён на худший случай: наибольшее значение, какое он может
    # выдать для запрошенной ширины. Настоящий выбор случаен, и по нему нельзя
    # детерминированно поймать слишком широкое основание.
    monkeypatch.setattr(jobs.secrets, "randbits", widest)

    job = jobs.Job("abcdefghijk")

    # Курсор уходит в расширение числом JSON и возвращается query-параметром, а
    # целые в JavaScript точны только до 2^53 - 1. Основание шире исказил бы браузер,
    # и курсор перестал бы совпадать с тем, что выдал демон.
    assert asked, "основание журнала должно браться из secrets.randbits"
    assert 0 <= job.journal_end <= 2**53 - 1


async def test_trimmed_journal_keeps_its_positions_relative_to_the_base(manager):
    from helper.jobs import MAX_EVENT_LOG

    job = manager.get_or_create("abcdefghijk", "ru", True)
    base = job.journal_end
    emit_events(job, MAX_EVENT_LOG + 10)

    assert len(job.events) == MAX_EVENT_LOG
    assert job.journal_end == base + MAX_EVENT_LOG + 10

    # Позиция из выброшенного начала журнала устарела: снимок.
    stale = await job.poll(cursor=base + 5, timeout=0.01)

    assert "snapshot" in stale

    # Позиция внутри оставшегося окна отдаёт ровно хвост после неё.
    inside = await job.poll(cursor=job.journal_end - 3, timeout=0.01)

    assert "snapshot" not in inside
    assert len(inside["events"]) == 3


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


def test_cooled_failure_of_one_variant_leaves_the_other_alone(manager, monkeypatch):
    from helper import jobs

    cancelled = []
    monkeypatch.setattr(jobs.Job, "cancel", lambda job: cancelled.append(job))

    failing = manager.get_or_create("abcdefghijk", "ru", True)
    steady = manager.get_or_create("abcdefghijk", "ru", False)
    failing._fail("сеть моргнула", retryable=True)
    failing.failed_at -= jobs.RETRY_COOLDOWN

    retried = manager.get_or_create("abcdefghijk", "ru", True)

    assert retried is not failing
    assert retried.generation == 1
    # Повтор снимает и отменяет только упавшее задание. Второй вариант ролика, может
    # быть, как раз идёт у другого клиента: он остаётся в реестре, не отменён и не
    # запускался заново (по одному запуску на каждое из трёх заданий).
    assert cancelled == [failing]
    assert manager.get("abcdefghijk", False) is steady
    assert steady.generation == 0
    assert manager.started == ["abcdefghijk"] * 3


def test_flipping_the_toggle_on_a_finished_job_starts_the_other_variant(manager):
    from helper.jobs import Stage

    # Так ведёт себя расширение: готовое задание не опрашивается, а смена
    # переключателя шлёт новый опрос с другим authorTrack. Он обязан начать работу,
    # а не получить назад готовое задание с прежней настройкой.
    done = manager.get_or_create("abcdefghijk", "ru", True)
    done.stage = Stage.DONE

    flipped = manager.get_or_create("abcdefghijk", "ru", False)

    assert flipped is not done
    assert flipped.use_author_track is False
    assert manager.started == ["abcdefghijk", "abcdefghijk"]
    # Прежний вариант остаётся: возврат к нему не начинает работу заново.
    assert manager.get("abcdefghijk", True) is done


def test_get_without_a_flag_returns_the_most_recently_touched_variant(manager):
    with_track = manager.get_or_create("abcdefghijk", "ru", True)
    without = manager.get_or_create("abcdefghijk", "ru", False)

    assert manager.get("abcdefghijk") is without
    assert manager.jobs_for("abcdefghijk") == [without, with_track]

    # Опрос первого варианта делает самым свежим уже его.
    manager.get_or_create("abcdefghijk", "ru", True)

    assert manager.get("abcdefghijk") is with_track
    assert manager.jobs_for("abcdefghijk") == [with_track, without]
    # С признаком ищется точный ключ, как бы давно к заданию ни обращались.
    assert manager.get("abcdefghijk", False) is without
    assert manager.get("zzzzzzzzzzz") is None
    assert manager.jobs_for("zzzzzzzzzzz") == []


def test_drop_removes_and_cancels_every_variant_of_the_video(manager, monkeypatch):
    from helper import jobs

    cancelled = []
    monkeypatch.setattr(jobs.Job, "cancel", lambda job: cancelled.append(job))

    with_track = manager.get_or_create("abcdefghijk", "ru", True)
    without = manager.get_or_create("abcdefghijk", "ru", False)
    neighbour = manager.get_or_create("zzzzzzzzzzz", "ru", True)

    manager.drop("abcdefghijk")

    assert manager.get("abcdefghijk") is None
    assert manager.get("abcdefghijk", True) is None
    assert manager.get("abcdefghijk", False) is None
    # Отменены оба, и каждый один раз: снятое с реестра, но не отменённое задание
    # продолжало бы качать звук без хозяина.
    assert sorted(map(id, cancelled)) == sorted([id(with_track), id(without)])
    assert manager.get("zzzzzzzzzzz") is neighbour
    assert manager.stats()["jobs"] == 1


def test_attach_follows_the_flag_and_never_creates(manager):
    assert manager.attach("abcdefghijk") is None
    assert manager.attach("abcdefghijk", False) is None
    assert manager.stats()["jobs"] == 0
    assert manager.started == []

    with_track = manager.get_or_create("abcdefghijk", "ru", True)
    without = manager.get_or_create("abcdefghijk", "ru", False)

    assert manager.attach("abcdefghijk", True) is with_track
    assert manager.attach("abcdefghijk", False) is without
    # Без признака — самое свежее, а сам attach и есть касание.
    assert manager.attach("abcdefghijk") is without
    assert manager.attach("abcdefghijk", True) is with_track
    assert manager.attach("abcdefghijk") is with_track
    assert manager.started == ["abcdefghijk", "abcdefghijk"]

    with_track.touched_at = 0.0
    manager.attach("abcdefghijk", True)

    assert with_track.touched_at > 0.0


def test_restore_looks_up_the_variant_the_viewer_asked_for(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)

    assert manager.restore("abcdefghijk", "ru", True) is job
    # Другой вариант ролика не готов — ни в реестре, ни в кэше. Для зрителя с такой
    # настройкой ничего нет, а restore только читает и работы под неё не начинает.
    assert manager.restore("abcdefghijk", "ru", False) is None
    assert manager.get("abcdefghijk", False) is None
    assert manager.started == ["abcdefghijk"]


def test_restore_from_cache_registers_the_variant_under_its_own_key(manager):
    from helper.jobs import Stage

    # Свой ролик: кэш на время прогона общий, а у остальных тестов «abcdefghijk»
    # должен оставаться без сохранённых дорожек.
    video = "cachedvideo"
    cues = [{"start": 0.0, "end": 1.5, "src": "Zdravo", "lines": ["Привет"]}]
    manager.cache.put(manager._key_for(video, "ru", "asr"), cues, 10.0)
    live = manager.get_or_create(video, "ru", True)

    restored = manager.restore(video, "ru", False, 10.0)

    # Для зрителя без дорожки автора готов вариант из кэша, хотя вариант с дорожкой
    # ещё идёт. Он встал в реестр под своим ключом и живое задание не вытеснил.
    assert restored is not live
    assert restored.use_author_track is False
    assert restored.stage is Stage.DONE
    assert manager.get(video, False) is restored
    assert manager.get(video, True) is live
    assert manager.stats()["jobs"] == 2


def test_eviction_removes_finished_jobs_by_their_pair_key(manager):
    from helper.jobs import MAX_RETAINED_JOBS, Stage

    # Ключ реестра — пара, и вытеснение, которое снимало бы задание по одному
    # video_id, не находило бы его и молча не вытесняло ничего: реестр рос бы всё
    # время жизни демона.
    for i in range(MAX_RETAINED_JOBS + 3):
        job = manager.get_or_create(f"video{i:05d}a", "ru", i % 2 == 0)
        job.stage = Stage.DONE

    # Вытеснение идёт в конце каждого get_or_create, так что последнее готовое
    # задание оно увидит только при следующем обращении.
    manager.get_or_create("zzzzzzzzzzz", "ru", True)

    assert manager.stats()["jobs"] == MAX_RETAINED_JOBS
    # Уходят самые старые готовые, и обе разновидности ключа.
    assert manager.get("video00000a") is None
    assert manager.get("video00001a") is None
    assert manager.get(f"video{MAX_RETAINED_JOBS + 2:05d}a") is not None


async def test_playhead_endpoint_moves_every_variant(client, manager):
    with_track = manager.get_or_create("abcdefghijk", "ru", True)
    without = manager.get_or_create("abcdefghijk", "ru", False)

    assert with_track is not without

    response = await client.post("/v1/jobs/abcdefghijk/playhead", json={"t": 42.5})

    assert response.status_code == 200
    # Позицию присылают без признака дорожки, и какой из вариантов смотрит клиент,
    # демон не знает: двигаем оба.
    assert with_track.playhead == 42.5
    assert without.playhead == 42.5

    unknown = await client.post("/v1/jobs/zzzzzzzzzzz/playhead", json={"t": 1.0})

    assert unknown.status_code == 404


async def test_events_without_create_find_only_their_own_variant(client, manager):
    manager.get_or_create("abcdefghijk", "ru", True)
    # Отрицательный курсор слишком стар для любого журнала: опрос отвечает снимком
    # сразу, а не ждёт 25 секунд.
    params = {"create": "false", "authorTrack": "false", "cursor": "-1"}

    missing = await client.get("/v1/jobs/abcdefghijk/events", params=params)

    # Вкладка, подхватившая готовое после F5, просит вариант без дорожки автора.
    # Его нет, и опрос создавать его не вправе: перезагрузка страницы не должна
    # сама начинать работу.
    assert missing.json() == {"gone": True, "cursor": -1, "events": []}
    assert manager.get("abcdefghijk", False) is None
    assert manager.started == ["abcdefghijk"]

    manager.get_or_create("abcdefghijk", "ru", False)
    found = await client.get("/v1/jobs/abcdefghijk/events", params=params)

    assert found.json()["snapshot"]["authorTrack"] is False


async def test_restore_cursor_continues_into_the_same_journal(
    client, manager, monkeypatch
):
    from helper import app as helper_app

    # Ожидание длинного опроса сокращаем: курсор из restore стоит на конце журнала,
    # и опрос по нему честно ждёт новых событий.
    monkeypatch.setattr(helper_app, "POLL_TIMEOUT", 0.01)
    job = manager.get_or_create("abcdefghijk", "ru", True)
    emit_events(job, 3)

    restored = (await client.get("/v1/jobs/abcdefghijk/restore")).json()
    params = {"create": "false", "cursor": str(restored["cursor"])}
    polled = (await client.get("/v1/jobs/abcdefghijk/events", params=params)).json()

    # Так расширение подхватывает идущее задание после F5. Курсор обязан быть
    # абсолютной позицией этого журнала: отсчитай его кто-то от нуля, первый же
    # опрос получил бы снимок, а позиция дальше разошлась бы с настоящей.
    assert restored["found"] is True
    assert restored["cursor"] == job.journal_end
    assert "snapshot" not in polled
    assert polled["events"] == []
    assert polled["cursor"] == job.journal_end
