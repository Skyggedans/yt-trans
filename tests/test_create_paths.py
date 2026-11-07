"""The paths that create a job — POST /v1/jobs and the watch operation — pass the
author-track flag the client sent, not the default, and refuse a non-boolean."""

import pytest

VIDEO = "abcdefghijk"
OTHER = "zzzzzzzzzzz"

# What the client could send instead of true or false. Strings and null would land in the
# registry key as a ghost third variant, and 0 and 1 would pass a loose int check.
NOT_A_BOOLEAN = [None, "false", "true", 0, 1]
REFUSAL = "authorTrack must be true or false"


def watch(video_id, **fields):
    return {"op": "watch", "videoId": video_id, **fields}


async def test_post_jobs_honours_author_track_false(client, manager):
    response = await client.post("/v1/jobs", json={"videoId": VIDEO, "authorTrack": False})

    assert response.status_code == 200
    job = manager.get(VIDEO, False)
    # Exactly one variant, and it is the client's variant. The default would have created a variant
    # with an author track, i.e. a whole pipeline wasted for a client with it off.
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
    # The refusal comes before the registry: no job, no started pipeline.
    assert manager.jobs_for(VIDEO) == []
    assert manager.started == []


async def test_watch_passes_the_flag_to_the_job(manager, connection):
    await connection.say(watch(VIDEO, authorTrack=False))
    snapshot = await connection.hear()

    job = manager.get(VIDEO, False)
    # Exactly one variant, and it is the client's variant: see POST /v1/jobs above.
    assert job is not None
    assert manager.jobs_for(VIDEO) == [job]
    assert manager.started == [VIDEO]
    # The socket is subscribed to this very job: the catch-up snapshot carries its setting.
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
    # A job for the video already exists: another client created it. It shows that the refusal did not
    # subscribe the socket. get reads None as "no flag" and would return the freshest
    # job of the video, so a loose check would subscribe the socket to someone else's variant.
    job = manager.get_or_create(VIDEO, "ru", True)

    await connection.say(watch(VIDEO, authorTrack=flag))

    # The same kind of event as the other refusals for a video, not a new message type.
    assert await connection.hear() == {
        "type": "state",
        "stage": "error",
        "videoId": VIDEO,
        "message": REFUSAL,
        "retryable": False,
    }

    # A job event the socket could have ended up subscribed to, and then a snapshot
    # of another video. The socket's queue is one and goes in order: if it were subscribed, the foreign
    # event would arrive first, whereas this way the snapshot is exactly what comes next.
    job._emit({"type": "state", "stage": "probing"})
    await connection.say(watch(OTHER, authorTrack=False))

    assert (await connection.hear())["videoId"] == OTHER
    # The refusal created and started nothing: VIDEO was created by the test itself, OTHER by the second watch.
    assert manager.jobs_for(VIDEO) == [job]
    assert manager.started == [VIDEO, OTHER]


async def test_watching_the_other_variant_moves_the_socket_to_it(manager, connection):
    await connection.say(watch(VIDEO, authorTrack=True))
    first = await connection.hear()
    await connection.say(watch(VIDEO, authorTrack=False))
    second = await connection.hear()

    # Every watch sends a catch-up snapshot of the variant it subscribed to.
    assert (first["type"], first["authorTrack"]) == ("snapshot", True)
    assert (second["type"], second["authorTrack"]) == ("snapshot", False)

    with_track = manager.get(VIDEO, True)
    without = manager.get(VIDEO, False)
    # First the event of the previous variant, then of the new one. The socket's queue is one and goes
    # in order: if the socket stayed subscribed to the previous one too, its event would arrive first.
    with_track._emit({"type": "state", "stage": "probing", "variant": "with-track"})
    without._emit({"type": "state", "stage": "probing", "variant": "without"})

    assert (await connection.hear())["variant"] == "without"
    # Nothing was cancelled or restarted: the previous variant goes on as it was, it simply
    # has no listener. One start per each of the two jobs.
    assert manager.get(VIDEO, True) is with_track
    assert manager.started == [VIDEO, VIDEO]

    # And back: the queue returns to the previous variant and leaves the new one.
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
    # Another client polled the previous variant, and it became the freshest, not the one
    # the socket is on: cleanup must not stop at the freshest job of the video.
    manager.get_or_create(VIDEO, "ru", True)

    await connection.disconnect()

    # The connection's queue is created by the handler itself and not exposed, so we judge
    # that nothing is left of it by the jobs' own subscribers. Cleanup on
    # disconnect goes by video and removes the queue from all its variants: the socket does not
    # remember which of them it ended up on.
    assert manager.get(VIDEO, True)._subscribers == set()
    assert manager.get(VIDEO, False)._subscribers == set()


async def test_unwatch_after_a_move_stops_events_from_both_variants(manager, connection):
    await connection.say(watch(VIDEO, authorTrack=True))
    await connection.hear()
    await connection.say(watch(VIDEO, authorTrack=False))
    await connection.hear()
    # Another client polled the previous variant, and it became the freshest, not the one
    # the socket is on: unsubscribe must not stop at the freshest job of the video.
    manager.get_or_create(VIDEO, "ru", True)

    await connection.say({"op": "unwatch", "videoId": VIDEO})

    # Both variants send an event each, and then a snapshot of another video arrives. The socket's
    # queue is one and goes in order: if the socket stayed subscribed to even one variant, its
    # event would arrive before the snapshot.
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

    # The socket had already watched this video, but its queue stayed on the dropped job.
    # A condition like "the video is already in watched" would skip the subscription, and the new job would go
    # past the socket: you have to ask the job itself.
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
    # The user took the card: the job is paused, and the socket knows about it.
    job.stage = Stage.PAUSED

    await connection.say(watch(VIDEO))

    # watch goes through get_or_create EVERY time: this is the only place from which
    # a paused job is resumed. A start and a resume.
    assert manager.started == [VIDEO, VIDEO]

    # The queue was already on this job, so there must be no second catch-up snapshot.
    # Then a snapshot of another video: if there were an extra one, it would come first.
    await connection.say(watch(OTHER))

    assert (await connection.hear())["videoId"] == OTHER
