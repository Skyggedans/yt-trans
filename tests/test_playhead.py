"""Playhead per client: the translation window cycles between the distinct positions of live
viewers, and a client that goes silent stops steering."""

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

    # A fresh job translates from the start of the video, as before.
    assert job.aim() == 0.0
    assert job.playhead == 0.0


def test_last_known_position_survives_when_every_slot_has_expired(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("only", 900.0)

    clock(PLAYHEAD_TTL + 1)

    # The browser freezes a background tab, and its reports stop. Previously the last
    # position stayed in the job forever, and the translation kept going around it.
    # Returning the start of the video would jerk the window from where the person stopped.
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

    # All three are silent, the freshest message is taken. "b" wrote most recently: it is neither the first
    # nor the last slot in order of appearance, and not the furthest through the video, so a choice
    # by any of these signs would give a different number.
    assert [job.aim() for _ in range(2)] == [150.0, 150.0]


def test_expired_slots_never_compete_with_a_live_one(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("gone", 1200.0)
    clock(PLAYHEAD_TTL + 1)
    job.report_playhead("here", 60.0)

    # The fallback position is needed only while there are no live ones at all. If there is at least one live one, the circle
    # goes only between the live ones.
    assert [job.aim() for _ in range(3)] == [60.0, 60.0, 60.0]


def test_slot_exactly_the_ttl_old_is_still_live(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("edge", 1200.0)
    clock(PLAYHEAD_TTL)
    job.report_playhead("fresh", 60.0)

    # "Older" means strictly older: a record exactly PLAYHEAD_TTL old is still live and takes
    # part in the circle on a par with the fresh one. Make the comparison strict, and "edge" would drop out one
    # moment early: the circle would sit on 60.0.
    assert [job.aim() for _ in range(4)] == [1200.0, 60.0, 1200.0, 60.0]

    clock(1)

    # A second later it is older than the term and leaves the circle.
    assert [job.aim() for _ in range(2)] == [60.0, 60.0]


def test_live_slots_are_the_clients_not_older_than_the_ttl(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("edge", 1200.0)
    clock(PLAYHEAD_TTL)
    job.report_playhead("fresh", 60.0)

    # The order of records is the order in which clients appeared, and the circle's turn order rests on it.
    assert list(job.live_slots()) == ["edge", "fresh"]

    clock(1)

    assert list(job.live_slots()) == ["fresh"]

    clock(PLAYHEAD_TTL)

    # All are expired: there are no live ones, although the records remain in the job (see aim).
    assert job.live_slots() == {}


def test_asking_who_is_live_does_not_turn_the_circle(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)

    # JobManager.drop also asks "who is live". If that were a turn, dropping someone else's client
    # would take a batch away from the viewers without bringing a translation.
    for _ in range(3):
        job.live_slots()

    assert [job.aim() for _ in range(2)] == [100.0, 900.0]


def test_reports_between_batches_do_not_reshuffle_the_circle(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)

    # In real life each client sends its position once a second, i.e. between batches. A new
    # position must replace the previous one in place: move its record to the end, and the order
    # in the circle would change with every message, and one viewer could get two batches
    # in a row while another gets none.
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

    # The circle goes between distinct positions. Two viewers at the same spot are one turn,
    # otherwise the third would get a third of the translation instead of half.
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

    # One slot for two: the second position replaced the first. If the unusable key landed
    # in the dict as is, there would be two slots, and the circle would start at 5.0. The list and the dict
    # also show that the key is checked before it is hashed.
    assert [job.aim() for _ in range(2)] == [6.0, 6.0]


def test_client_id_of_exactly_the_limit_is_a_client_of_its_own(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("x" * 64, 5.0)
    job.report_playhead(None, 6.0)

    # The limit is inclusive: a 64-character key is fine, 65 is not (see above).
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

    # A client without a key and a client removed without a key are one "legacy" record:
    # that is what was removed, not "tablet".
    assert [job.aim() for _ in range(2)] == [10.0, 10.0]

    job.report_playhead("legacy", 30.0)
    job.forget_client(None)

    # An explicit "legacy" key is the same slot as no key at all.
    assert [job.aim() for _ in range(2)] == [10.0, 10.0]


def test_forgetting_an_unknown_client_is_quiet(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.forget_client("nobody")

    assert job.aim() == 0.0

    job.report_playhead("a", 120.0)
    job.forget_client("nobody")
    job.forget_client("")
    job.forget_client(None)

    # The other slots are in place: the quiet skip removed nothing extra.
    assert [job.aim() for _ in range(2)] == [120.0, 120.0]


def test_forgetting_the_only_client_after_it_expired_leaves_nothing_to_aim_at(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 900.0)

    clock(PLAYHEAD_TTL + 1)

    # While the client is still listed, its expired position remains the fallback.
    assert job.aim() == 900.0

    job.forget_client("a")

    # The fallback position does not remember a removed client: there are no records, and this is the only path
    # to the start of the video for a job whose position was once reported. There is deliberately no separate
    # "last ever" field, otherwise the removed one would keep steering.
    assert job.aim() == 0.0
    assert job.playhead == 0.0


def test_forgetting_the_newer_of_two_expired_clients_leaves_the_older_one(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("older", 100.0)
    clock(10)
    job.report_playhead("newer", 200.0)

    clock(PLAYHEAD_TTL + 1)

    # Both are silent, and the freshest message is taken.
    assert job.aim() == 200.0

    job.forget_client("newer")

    # The fresher one was removed, and the second one's position becomes the fallback, not the start of the video.
    assert job.aim() == 100.0
    assert job.playhead == 100.0


def test_playhead_is_read_only(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    # An assignment left somewhere in the code will fail at once and loudly, rather than silently
    # stop moving anything: the position is now written through report_playhead.
    with pytest.raises(AttributeError):
        job.playhead = 5.0


def test_reading_the_playhead_does_not_spend_a_turn(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("a", 100.0)
    job.report_playhead("b", 900.0)

    # Reading is only a look. If it were a turn, every log line or snapshot field that
    # read the property would take someone's turn: the circle would spin
    # without a single translated batch.
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

    # The same fallback answer as aim(): a frozen tab does not jerk the window to zero.
    assert job.playhead == 900.0


def cues_for_batches(job):
    """Two hundred cues ten seconds apart: cue i starts at second 10·i."""
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

    # The batches go in turn: to the tablet, to the desktop, to the tablet again. The position must be taken
    # ONCE per batch. Ask for it in _next_batch on every cue inside min() or twice
    # per batch, and the circle would turn an even number of steps per batch (there are two hundred
    # pending cues), and all the batches would go to the same viewer.
    assert [10 in batch for batch in batches] == [True, False, True, False]
    assert [150 in batch for batch in batches] == [False, True, False, True]


def test_next_batch_follows_a_single_client(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    pending = cues_for_batches(job)
    # This is how the extension behaves: no key, one client.
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

    # Two different keys are two slots, on each of the video's variants: if the handler put
    # everything into one "legacy" slot, the positions would evict each other, and there would be no circle.
    for job in (with_track, without):
        assert [job.aim() for _ in range(4)] == [33.0, 900.0, 33.0, 900.0]


async def test_playhead_endpoint_without_a_client_id_is_the_legacy_client(client, manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    url = f"/v1/jobs/{VIDEO}/playhead"

    # This is how the extension sends it: the body {"t": ...}. One client is one slot, and the second
    # position replaces the first rather than standing next to it.
    await client.post(url, json={"t": 20.0})
    await client.post(url, json={"t": 30.0})

    assert [job.aim() for _ in range(4)] == [10.0, 30.0, 10.0, 30.0]

    job.forget_client("legacy")

    assert [job.aim() for _ in range(2)] == [10.0, 10.0]


async def test_playhead_endpoint_tolerates_a_body_without_t(client, manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead(None, 50.0)

    response = await client.post(f"/v1/jobs/{VIDEO}/playhead", json={})

    # As before, the absence of t means the start of the video, not a refusal.
    assert response.status_code == 200
    assert job.aim() == 0.0


async def test_ws_playhead_reaches_the_clients_slot_on_every_variant(manager, connection):
    with_track = manager.get_or_create(VIDEO, "ru", True)
    without = manager.get_or_create(VIDEO, "ru", False)

    await connection.say(playhead_message(VIDEO, 33.0, clientId="tablet"))
    await connection.say(playhead_message(VIDEO, 900.0, clientId="desktop"))

    # The operation has no track flag, so the position goes to all variants of the video,
    # and on each — into its own client's slot: see the analogous test for POST.
    for job in (with_track, without):
        assert [job.aim() for _ in range(4)] == [33.0, 900.0, 33.0, 900.0]


async def test_ws_playhead_without_a_client_id_lands_in_the_legacy_slot(manager, connection):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)

    await connection.say(playhead_message(VIDEO, 20.0))
    await connection.say(playhead_message(VIDEO, 30.0))

    # The unnamed client is one, and the second position replaced the first.
    assert [job.aim() for _ in range(4)] == [10.0, 30.0, 10.0, 30.0]

    job.forget_client("legacy")

    assert [job.aim() for _ in range(2)] == [10.0, 10.0]


async def test_ws_playhead_tolerates_a_message_without_t(manager, connection):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead(None, 50.0)

    await connection.say({"op": "playhead", "videoId": VIDEO})

    assert job.aim() == 0.0


async def test_ws_playhead_for_an_unknown_video_is_quiet(manager, connection):
    # say returns only when the handler has parsed the message and is waiting
    # for the next one: if it crashed on an unknown video, the test would get a TimeoutError.
    await connection.say(playhead_message(OTHER, 5.0, clientId="tablet"))

    # The position creates neither a job nor a pipeline start.
    assert manager.jobs_for(OTHER) == []
    assert manager.started == []
