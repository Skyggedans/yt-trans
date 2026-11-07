"""Job identity: the pair "video and author track"; the target language is not part of the key."""

import asyncio


def emit_events(job, count):
    """Append count events to the job's journal the same way the pipeline does."""
    for number in range(count):
        job._emit({"type": "state", "stage": "probing", "n": number})


def test_client_with_other_author_track_flag_gets_its_own_job(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    assert second is not first
    assert second.use_author_track is False
    # The first job is the same one, its flag is intact, and it was not started again:
    # two starts for two jobs; there would be no third if the first had been recreated.
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
    # We count starts, not objects: recreation calls start every time, and after it
    # comes a download that cancellation does not stop. Each variant must
    # start exactly once and from then on only answer polls.
    assert manager.started == ["abcdefghijk", "abcdefghijk"]


def test_snapshot_reports_the_settings_the_track_was_made_with(manager):
    job = manager.get_or_create("abcdefghijk", "sr", False)
    snapshot = job.snapshot()

    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0


def test_second_client_with_other_target_lang_reuses_job_and_can_see_it(manager):
    # The target language is not part of the key: it is one for all, and the job is made with the one
    # the first caller created it with.
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "sr", True)

    assert second is first
    assert second.snapshot()["targetLang"] == "ru"


async def test_cursor_ahead_of_the_journal_resyncs_once(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    # Each job's journal counts from its own base, not from zero,
    # so "past the end" is measured from the end of the journal itself.
    ahead = await job.poll(cursor=job.journal_end + 9999, timeout=0.01)

    assert "snapshot" in ahead
    assert ahead["cursor"] == job.journal_end

    again = await job.poll(cursor=ahead["cursor"], timeout=0.01)

    assert "snapshot" not in again


async def test_fresh_client_at_cursor_zero_gets_one_snapshot_and_then_waits(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)

    # Every fresh client starts from zero. The journal base is random, so
    # zero is too old for it: a snapshot comes at once, rather than after waiting for the first
    # event. After that the client goes with the cursor from the answer and waits as usual; there is no
    # busy loop.
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
    # The cursor with which a client moves from the source to the neighbouring variant (this is what
    # the extension does when toggling on a running job). It is shorter than the destination's
    # journal, and with a common zero count it would pass there for its own: the client would get
    # the tail of a foreign journal and would never see its beginning.
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

    # The same key, but a different journal: a position from the previous one means nothing here.
    assert recreated is not dropped
    assert "snapshot" in answer
    assert answer["events"] == []


def test_fresh_jobs_start_their_journals_at_different_positions(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    # The end of an empty journal is its base. We check not the value but the fact
    # that it differs between two jobs: that is exactly why a foreign cursor does not fit.
    assert first.journal_end != second.journal_end


def test_journal_positions_stay_exact_in_javascript(monkeypatch):
    from helper import jobs

    asked = []

    def widest(bits):
        asked.append(bits)

        return (1 << bits) - 1

    # The generator is replaced with the worst case: the largest value it can
    # produce for the requested width. The real choice is random, and one cannot
    # deterministically catch an overly wide base with it.
    monkeypatch.setattr(jobs.secrets, "randbits", widest)

    job = jobs.Job("abcdefghijk")

    # The cursor goes to the extension as a JSON number and comes back as a query parameter, while
    # integers in JavaScript are exact only up to 2^53 - 1. A wider base would be distorted by the browser,
    # and the cursor would stop matching what the daemon issued.
    assert asked, "the journal base must come from secrets.randbits"
    assert 0 <= job.journal_end <= 2**53 - 1


async def test_every_event_names_the_video_it_belongs_to(manager):
    # The socket is subscribed to several jobs at once (the stage and the copy going to the device),
    # and job events go in one stream. Without the video the client can neither
    # discard someone else's nor append subtitles to its own copy: the cues event went
    # unnamed, and what was saved stayed with what was in the snapshot.
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job._emit({"type": "cues", "cues": []})

    answer = await job.poll(cursor=job.journal_end - 1, timeout=0.01)

    assert answer["events"][0]["videoId"] == "abcdefghijk"


async def test_trimmed_journal_keeps_its_positions_relative_to_the_base(manager):
    from helper.jobs import MAX_EVENT_LOG

    job = manager.get_or_create("abcdefghijk", "ru", True)
    base = job.journal_end
    emit_events(job, MAX_EVENT_LOG + 10)

    assert len(job.events) == MAX_EVENT_LOG
    assert job.journal_end == base + MAX_EVENT_LOG + 10

    # A position from the discarded beginning of the journal is stale: a snapshot.
    stale = await job.poll(cursor=base + 5, timeout=0.01)

    assert "snapshot" in stale

    # A position inside the remaining window returns exactly the tail after it.
    inside = await job.poll(cursor=job.journal_end - 3, timeout=0.01)

    assert "snapshot" not in inside
    assert len(inside["events"]) == 3


def test_cooled_retryable_failure_recreates_job_as_next_generation(manager):
    from helper import jobs

    first = manager.get_or_create("abcdefghijk", "ru", True)
    # The socket is subscribed to the video once, on the first job: recreation must
    # move its queue, otherwise it would keep listening to the cancelled job.
    queue: asyncio.Queue = asyncio.Queue()
    first.subscribe(queue)
    first._fail("сеть моргнула", retryable=True)
    # Only a cooled-down failure recreates: we shift the failure time by the cooldown.
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

    # The catch-up snapshot on the move already carries the new number: the generation must be
    # set before moving the subscribers, not after.
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
    # The retry removes and cancels only the failed job. The video's second variant may
    # be running for another client right now: it stays in the registry, is not cancelled and
    # was not started again (one start per each of the three jobs).
    assert cancelled == [failing]
    assert manager.get("abcdefghijk", False) is steady
    assert steady.generation == 0
    assert manager.started == ["abcdefghijk"] * 3


def test_flipping_the_toggle_on_a_finished_job_starts_the_other_variant(manager):
    from helper.jobs import Stage

    # This is how the extension behaves: a finished job is not polled, and flipping
    # the toggle sends a new poll with a different authorTrack. It must start work,
    # not get the finished job with the previous setting back.
    done = manager.get_or_create("abcdefghijk", "ru", True)
    done.stage = Stage.DONE

    flipped = manager.get_or_create("abcdefghijk", "ru", False)

    assert flipped is not done
    assert flipped.use_author_track is False
    assert manager.started == ["abcdefghijk", "abcdefghijk"]
    # The previous variant remains: returning to it does not start work again.
    assert manager.get("abcdefghijk", True) is done


def test_get_without_a_flag_returns_the_most_recently_touched_variant(manager):
    with_track = manager.get_or_create("abcdefghijk", "ru", True)
    without = manager.get_or_create("abcdefghijk", "ru", False)

    assert manager.get("abcdefghijk") is without
    assert manager.jobs_for("abcdefghijk") == [without, with_track]

    # Polling the first variant makes it the freshest.
    manager.get_or_create("abcdefghijk", "ru", True)

    assert manager.get("abcdefghijk") is with_track
    assert manager.jobs_for("abcdefghijk") == [with_track, without]
    # With the flag an exact key is looked up, no matter how long ago the job was touched.
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
    # Both are cancelled, each once: a job removed from the registry but not cancelled
    # would keep downloading audio with no owner.
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
    # Without the flag — the freshest, and attach itself is the touch.
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
    # The video's other variant is not ready — neither in the registry nor in the cache. For a viewer with that
    # setting there is nothing, and restore only reads and does not start work for it.
    assert manager.restore("abcdefghijk", "ru", False) is None
    assert manager.get("abcdefghijk", False) is None
    assert manager.started == ["abcdefghijk"]


def test_restore_from_cache_registers_the_variant_under_its_own_key(manager):
    from helper.jobs import Stage

    # Its own video: the cache is shared for the run, and in the other tests "abcdefghijk"
    # must stay without saved tracks.
    video = "cachedvideo"
    cues = [{"start": 0.0, "end": 1.5, "src": "Zdravo", "lines": ["Привет"]}]
    manager.cache.put(manager._key_for(video, "asr"), cues, 10.0)
    live = manager.get_or_create(video, "ru", True)

    restored = manager.restore(video, "ru", False, 10.0)

    # For a viewer without an author track the cached variant is ready, although the variant with the track
    # is still running. It entered the registry under its own key and did not evict the live job.
    assert restored is not live
    assert restored.use_author_track is False
    assert restored.stage is Stage.DONE
    assert manager.get(video, False) is restored
    assert manager.get(video, True) is live
    assert manager.stats()["jobs"] == 2


def test_eviction_removes_finished_jobs_by_their_pair_key(manager):
    from helper.jobs import MAX_RETAINED_JOBS, Stage

    # The registry key is a pair, and eviction that removed a job by a single
    # video_id would not find it and would silently evict nothing: the registry would grow the whole
    # time the daemon lives.
    for i in range(MAX_RETAINED_JOBS + 3):
        job = manager.get_or_create(f"video{i:05d}a", "ru", i % 2 == 0)
        job.stage = Stage.DONE

    # Eviction runs at the end of every get_or_create, so it will see the last finished
    # job only on the next touch.
    manager.get_or_create("zzzzzzzzzzz", "ru", True)

    assert manager.stats()["jobs"] == MAX_RETAINED_JOBS
    # The oldest finished ones leave, and both kinds of key.
    assert manager.get("video00000a") is None
    assert manager.get("video00001a") is None
    assert manager.get(f"video{MAX_RETAINED_JOBS + 2:05d}a") is not None


async def test_playhead_endpoint_moves_every_variant(client, manager):
    with_track = manager.get_or_create("abcdefghijk", "ru", True)
    without = manager.get_or_create("abcdefghijk", "ru", False)

    assert with_track is not without

    response = await client.post("/v1/jobs/abcdefghijk/playhead", json={"t": 42.5})

    assert response.status_code == 200
    # The position is sent without the track flag, and the daemon does not know which variant the client
    # is watching: we move both.
    assert with_track.playhead == 42.5
    assert without.playhead == 42.5

    unknown = await client.post("/v1/jobs/zzzzzzzzzzz/playhead", json={"t": 1.0})

    assert unknown.status_code == 404


async def test_events_without_create_find_only_their_own_variant(client, manager):
    manager.get_or_create("abcdefghijk", "ru", True)
    # A negative cursor is too old for any journal: the poll answers with a snapshot
    # at once, rather than waiting 25 seconds.
    params = {"create": "false", "authorTrack": "false", "cursor": "-1"}

    missing = await client.get("/v1/jobs/abcdefghijk/events", params=params)

    # A tab picking up something finished after F5 asks for the variant without an author track.
    # It does not exist, and the poll has no right to create it: reloading the page must not
    # start work by itself.
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

    # We shorten the long-poll wait: the cursor from restore stands at the end of the journal,
    # and a poll by it honestly waits for new events.
    monkeypatch.setattr(helper_app, "POLL_TIMEOUT", 0.01)
    job = manager.get_or_create("abcdefghijk", "ru", True)
    emit_events(job, 3)

    restored = (await client.get("/v1/jobs/abcdefghijk/restore")).json()
    params = {"create": "false", "cursor": str(restored["cursor"])}
    polled = (await client.get("/v1/jobs/abcdefghijk/events", params=params)).json()

    # This is how the extension picks up a running job after F5. The cursor must be
    # an absolute position of this journal: if someone counted it from zero, the very first
    # poll would get a snapshot, and the position would then diverge from the real one.
    assert restored["found"] is True
    assert restored["cursor"] == job.journal_end
    assert "snapshot" not in polled
    assert polled["events"] == []
    assert polled["cursor"] == job.journal_end
