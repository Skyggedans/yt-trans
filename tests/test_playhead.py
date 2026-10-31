"""Плейхед на клиента: окно перевода ходит по кругу между различными позициями живых
зрителей, а клиент, что замолчал, рулить перестаёт."""

import time

import pytest

from helper.jobs import PLAYHEAD_TTL, JobCue

VIDEO = "abcdefghijk"
OTHER = "zzzzzzzzzzz"


def test_two_clients_take_turns(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("a", 120.0)
    job.report_playhead("b", 1200.0)

    aims = [job.aim() for _ in range(4)]

    assert aims == [120.0, 1200.0, 120.0, 1200.0]


def test_one_client_always_gets_its_own_position(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("a", 42.0)

    assert [job.aim() for _ in range(3)] == [42.0, 42.0, 42.0]


def test_client_without_id_keeps_todays_behaviour(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead(None, 7.0)

    assert job.aim() == 7.0
    assert job.playhead == 7.0


def test_silent_client_stops_steering_after_the_ttl(manager, monkeypatch):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("gone", 1200.0)
    job.report_playhead("here", 60.0)

    later = time.monotonic() + PLAYHEAD_TTL + 1
    monkeypatch.setattr(time, "monotonic", lambda: later)
    job.report_playhead("here", 61.0)

    assert [job.aim() for _ in range(2)] == [61.0, 61.0]


def test_absurd_client_id_is_refused_not_stored(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("x" * 500, 5.0)
    job.report_playhead("", 6.0)

    assert job.aim() == 6.0


def test_job_nobody_has_reported_on_aims_at_the_start(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    # Свежее задание переводит с начала ролика, как и раньше.
    assert job.aim() == 0.0
    assert job.playhead == 0.0


def test_last_known_position_survives_when_every_slot_has_expired(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("only", 900.0)

    clock(PLAYHEAD_TTL + 1)

    # Фоновую вкладку браузер замораживает, и её отчёты прекращаются. Раньше последняя
    # позиция оставалась в задании навсегда, и перевод продолжал идти вокруг неё.
    # Вернуть начало ролика значило бы дёрнуть окно с того места, где человек остановился.
    assert [job.aim() for _ in range(3)] == [900.0, 900.0, 900.0]


def test_last_known_position_is_the_most_recent_report(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    clock(10)
    job.report_playhead("b", 200.0)
    clock(10)
    job.report_playhead("c", 900.0)
    clock(10)
    job.report_playhead("b", 150.0)

    clock(PLAYHEAD_TTL + 31)

    # Молчат все трое, берётся самое свежее сообщение. Свежее всех писал «b»: он ни первый,
    # ни последний слот по порядку появления и не самый дальний по ролику, поэтому выбор
    # по любому из этих признаков дал бы другое число.
    assert [job.aim() for _ in range(2)] == [150.0, 150.0]


def test_expired_slots_never_compete_with_a_live_one(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("gone", 1200.0)
    clock(PLAYHEAD_TTL + 1)
    job.report_playhead("here", 60.0)

    # Запасная позиция нужна, пока живых нет вовсе. Есть хоть один живой — круг
    # идёт только между живыми.
    assert [job.aim() for _ in range(3)] == [60.0, 60.0, 60.0]


def test_slot_exactly_the_ttl_old_is_still_live(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("edge", 1200.0)
    clock(PLAYHEAD_TTL)
    job.report_playhead("fresh", 60.0)

    # «Старше» значит строго старше: запись ровно PLAYHEAD_TTL возраста ещё жива и ходит
    # в круге наравне со свежей. Сделай сравнение строгим, и «edge» выпала бы на одно
    # мгновение раньше срока: круг стоял бы на 60.0.
    assert [job.aim() for _ in range(4)] == [1200.0, 60.0, 1200.0, 60.0]

    clock(1)

    # Секундой позже она старше срока и из круга уходит.
    assert [job.aim() for _ in range(2)] == [60.0, 60.0]


def test_live_slots_are_the_clients_not_older_than_the_ttl(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("edge", 1200.0)
    clock(PLAYHEAD_TTL)
    job.report_playhead("fresh", 60.0)

    # Порядок записей — порядок появления клиентов, на нём держится очерёдность круга.
    assert list(job.live_slots()) == ["edge", "fresh"]

    clock(1)

    assert list(job.live_slots()) == ["fresh"]

    clock(PLAYHEAD_TTL)

    # Просрочены все: живых нет, хотя записи в задании остались (см. aim).
    assert job.live_slots() == {}


def test_asking_who_is_live_does_not_turn_the_circle(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)

    # Вопрос «кто жив» задаёт и JobManager.drop. Был бы он ходом, сброс чужого клиента
    # отнимал бы у зрителей батч, не принеся перевода.
    for _ in range(3):
        job.live_slots()

    assert [job.aim() for _ in range(2)] == [100.0, 900.0]


def test_reports_between_batches_do_not_reshuffle_the_circle(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)

    # В жизни каждый клиент шлёт позицию раз в секунду, то есть между батчами. Новая
    # позиция обязана заменять прежнюю на месте: переставь её запись в конец, порядок
    # в круге менялся бы с каждым сообщением, и один зритель мог бы получать два батча
    # подряд, а другой — ни одного.
    assert job.aim() == 100.0

    job.report_playhead("a", 101.0)

    assert job.aim() == 900.0

    job.report_playhead("b", 901.0)

    assert job.aim() == 101.0

    job.report_playhead("a", 102.0)

    assert job.aim() == 901.0


def test_clients_at_the_same_position_share_one_turn(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 100.0)
    job.report_playhead("c", 900.0)

    # Круг идёт между различными позициями. Два зрителя на одном месте — один ход,
    # иначе третий получал бы треть перевода вместо половины.
    assert [job.aim() for _ in range(4)] == [100.0, 900.0, 100.0, 900.0]


@pytest.mark.parametrize(
    "client_id",
    [None, "", "x" * 65, 123, True, ["a"], {"a": 1}],
    ids=["none", "empty", "too-long", "int", "bool", "list", "dict"],
)
def test_unusable_client_id_is_the_legacy_client(manager, client_id):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead(client_id, 5.0)
    job.report_playhead(None, 6.0)

    # Один слот на двоих: вторая позиция заменила первую. Попади непригодный ключ
    # в словарь как есть, слотов было бы два, и круг начался бы с 5.0. Список и словарь
    # заодно показывают, что ключ проверяется до того, как его хэшируют.
    assert [job.aim() for _ in range(2)] == [6.0, 6.0]


def test_client_id_of_exactly_the_limit_is_a_client_of_its_own(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("x" * 64, 5.0)
    job.report_playhead(None, 6.0)

    # Предел включительно: ключ в 64 знака годится, в 65 уже нет (см. выше).
    assert [job.aim() for _ in range(4)] == [5.0, 6.0, 5.0, 6.0]


def test_forgotten_client_no_longer_takes_a_turn(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 120.0)
    job.report_playhead("b", 1200.0)

    job.forget_client("a")

    assert [job.aim() for _ in range(3)] == [1200.0, 1200.0, 1200.0]


def test_forgetting_none_and_reporting_none_address_the_same_slot(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead(None, 20.0)

    job.forget_client(None)

    # Клиент без ключа и клиент, которого снимают без ключа, — одна запись "legacy":
    # снялась она, а не «tablet».
    assert [job.aim() for _ in range(2)] == [10.0, 10.0]

    job.report_playhead("legacy", 30.0)
    job.forget_client(None)

    # Явный ключ "legacy" — тот же слот, что и отсутствие ключа.
    assert [job.aim() for _ in range(2)] == [10.0, 10.0]


def test_forgetting_an_unknown_client_is_quiet(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.forget_client("nobody")

    assert job.aim() == 0.0

    job.report_playhead("a", 120.0)
    job.forget_client("nobody")
    job.forget_client("")
    job.forget_client(None)

    # Чужие слоты на месте: тихий пропуск не снял ничего лишнего.
    assert [job.aim() for _ in range(2)] == [120.0, 120.0]


def test_forgetting_the_only_client_after_it_expired_leaves_nothing_to_aim_at(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 900.0)

    clock(PLAYHEAD_TTL + 1)

    # Пока клиент числится, его просроченная позиция остаётся запасной.
    assert job.aim() == 900.0

    job.forget_client("a")

    # Снятого клиента запасная позиция не помнит: записей нет, и это единственный путь
    # к началу ролика у задания, о позиции которого когда-то сообщали. Отдельного
    # «последнего за всё время» поля нет намеренно, иначе снятый продолжал бы рулить.
    assert job.aim() == 0.0
    assert job.playhead == 0.0


def test_forgetting_the_newer_of_two_expired_clients_leaves_the_older_one(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("older", 100.0)
    clock(10)
    job.report_playhead("newer", 200.0)

    clock(PLAYHEAD_TTL + 1)

    # Молчат оба, и берётся самое свежее сообщение.
    assert job.aim() == 200.0

    job.forget_client("newer")

    # Свежее сняли, и запасной становится позиция второго, а не начало ролика.
    assert job.aim() == 100.0
    assert job.playhead == 100.0


def test_playhead_is_read_only(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    # Присваивание, оставшееся где-то в коде, упадёт сразу и громко, а не молча
    # перестанет что-либо двигать: позицию теперь пишут через report_playhead.
    with pytest.raises(AttributeError):
        job.playhead = 5.0


def test_reading_the_playhead_does_not_spend_a_turn(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)

    # Чтение — только взгляд. Окажись оно ходом, каждая строка лога или поле снимка, что
    # прочитали свойство, отнимали бы у кого-то из зрителей очередь: круг проворачивался
    # бы без единого переведённого батча.
    assert [job.playhead for _ in range(3)] == [100.0, 100.0, 100.0]
    assert [job.aim() for _ in range(2)] == [100.0, 900.0]


def test_playhead_shows_the_position_aim_serves_next(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)
    seen = []

    for _ in range(4):
        ahead = job.playhead

        assert job.aim() == ahead
        seen.append(ahead)

    assert seen == [100.0, 900.0, 100.0, 900.0]


def test_playhead_of_a_job_whose_clients_all_went_silent_is_the_last_known_position(
    manager, clock
):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("only", 900.0)

    clock(PLAYHEAD_TTL + 1)

    # Тот же запасной ответ, что у aim(): замороженная вкладка не дёргает окно к нулю.
    assert job.playhead == 900.0


def cues_for_batches(job):
    """Двести реплик с шагом в десять секунд: реплика i начинается на секунде 10·i."""
    job.cues = [
        JobCue(index=i, start=i * 10.0, end=i * 10.0 + 5.0, src=f"реплика {i}")
        for i in range(200)
    ]

    return [c for c in job.cues if not c.translated]


def test_next_batch_spends_one_turn_per_batch(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    pending = cues_for_batches(job)
    job.report_playhead("tablet", 100.0)  # реплика 10
    job.report_playhead("desktop", 1500.0)  # реплика 150

    batches = [{c.index for c in manager.runner._next_batch(job, pending)} for _ in range(4)]

    # Батчи идут по очереди: планшету, настольному, снова планшету. Позицию надо брать
    # ОДИН раз на батч. Спроси её _next_batch на каждой реплике внутри min() или дважды
    # за батч, круг проворачивался бы за батч на чётное число ходов (ожидающих реплик
    # двести), и все батчи доставались бы одному и тому же зрителю.
    assert [10 in batch for batch in batches] == [True, False, True, False]
    assert [150 in batch for batch in batches] == [False, True, False, True]


def test_next_batch_follows_a_single_client(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    pending = cues_for_batches(job)
    # Так ведёт себя расширение: ключа нет, клиент один.
    job.report_playhead(None, 1500.0)

    batches = [{c.index for c in manager.runner._next_batch(job, pending)} for _ in range(3)]

    assert [150 in batch for batch in batches] == [True, True, True]
    assert all(10 not in batch for batch in batches)


def playhead_message(video_id, t, **fields):
    return {"op": "playhead", "videoId": video_id, "t": t, **fields}


async def test_playhead_endpoint_hands_the_client_id_to_every_variant(client, manager):
    with_track = manager.get_or_create(VIDEO, "ru", True)
    without = manager.get_or_create(VIDEO, "ru", False)
    url = f"/v1/jobs/{VIDEO}/playhead"

    first = await client.post(url, json={"t": 33.0, "clientId": "tablet"})
    second = await client.post(url, json={"t": 900.0, "clientId": "desktop"})

    assert first.status_code == second.status_code == 200

    # Два разных ключа — два слота, и на каждом из вариантов ролика: положи обработчик
    # всё в один слот "legacy", позиции вытесняли бы друг друга, и круга не вышло бы.
    for job in (with_track, without):
        assert [job.aim() for _ in range(4)] == [33.0, 900.0, 33.0, 900.0]


async def test_playhead_endpoint_without_a_client_id_is_the_legacy_client(client, manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    url = f"/v1/jobs/{VIDEO}/playhead"

    # Так шлёт расширение: тело {"t": ...}. Одному клиенту — один слот, и вторая
    # позиция заменяет первую, а не встаёт рядом.
    await client.post(url, json={"t": 20.0})
    await client.post(url, json={"t": 30.0})

    assert [job.aim() for _ in range(4)] == [10.0, 30.0, 10.0, 30.0]

    job.forget_client("legacy")

    assert [job.aim() for _ in range(2)] == [10.0, 10.0]


async def test_playhead_endpoint_tolerates_a_body_without_t(client, manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead(None, 50.0)

    response = await client.post(f"/v1/jobs/{VIDEO}/playhead", json={})

    # Как и прежде, отсутствие t значит начало ролика, а не отказ.
    assert response.status_code == 200
    assert job.aim() == 0.0


async def test_ws_playhead_reaches_the_clients_slot_on_every_variant(manager, connection):
    with_track = manager.get_or_create(VIDEO, "ru", True)
    without = manager.get_or_create(VIDEO, "ru", False)

    await connection.say(playhead_message(VIDEO, 33.0, clientId="tablet"))
    await connection.say(playhead_message(VIDEO, 900.0, clientId="desktop"))

    # Признака дорожки у операции нет, поэтому позиция уходит на все варианты ролика,
    # и на каждом — в слот своего клиента: см. аналогичный тест для POST.
    for job in (with_track, without):
        assert [job.aim() for _ in range(4)] == [33.0, 900.0, 33.0, 900.0]


async def test_ws_playhead_without_a_client_id_lands_in_the_legacy_slot(manager, connection):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)

    await connection.say(playhead_message(VIDEO, 20.0))
    await connection.say(playhead_message(VIDEO, 30.0))

    # Безымянный клиент один, и вторая позиция заменила первую.
    assert [job.aim() for _ in range(4)] == [10.0, 30.0, 10.0, 30.0]

    job.forget_client("legacy")

    assert [job.aim() for _ in range(2)] == [10.0, 10.0]


async def test_ws_playhead_tolerates_a_message_without_t(manager, connection):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead(None, 50.0)

    await connection.say({"op": "playhead", "videoId": VIDEO})

    assert job.aim() == 0.0


async def test_ws_playhead_for_an_unknown_video_is_quiet(manager, connection):
    # say возвращается, только когда обработчик разобрал сообщение и снова ждёт
    # следующее: упади он на неизвестном ролике, тест получил бы TimeoutError.
    await connection.say(playhead_message(OTHER, 5.0, clientId="tablet"))

    # Позиция не заводит ни задания, ни запуска конвейера.
    assert manager.jobs_for(OTHER) == []
    assert manager.started == []
