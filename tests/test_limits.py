"""Two limits for an open port: no more unfinished jobs than MAX_ACTIVE_JOBS,
and no more paused audio on disk than PAUSED_AUDIO_MAX_BYTES."""

import errno
import logging
import os
import shutil
from pathlib import Path

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

# Exactly eleven characters, like a real video id.
OVERFLOW = "overflowxyz"


def watch(video_id, **fields):
    return {"op": "watch", "videoId": video_id, **fields}


@pytest.fixture
def full(manager):
    """A registry with exactly MAX_ACTIVE_JOBS unfinished jobs: one more no longer fits."""
    return [manager.get_or_create(f"video{i:05d}a", "ru", True) for i in range(MAX_ACTIVE_JOBS)]


@pytest.fixture
def sentence(manager, full):
    """The sentence the daemon refuses with: the endpoints check against it, not against a copy of the text."""
    with pytest.raises(CapacityError) as refused:
        manager.get_or_create(OVERFLOW, "ru", True)

    return str(refused.value)


# ── the limit on unfinished jobs ─────────────────────────────────────────


def test_active_jobs_are_capped(manager, full):
    with pytest.raises(CapacityError) as refused:
        manager.get_or_create(OVERFLOW, "ru", True)

    message = str(refused.value)

    # A sentence for the viewer: how many videos are already in progress and that a slot will free up by itself.
    assert str(MAX_ACTIVE_JOBS) in message
    assert "will free up" in message
    # The refusal created nothing and started nothing.
    assert manager.get(OVERFLOW) is None
    assert manager.started == [job.video_id for job in full]


def test_overflow_is_not_a_permanent_refusal():
    # The PermanentJobError contract is a refusal that will not pass by itself, and the runner
    # sets retryable=False for the job because of it. Overflow passes as soon as any
    # job finishes, and it reaches the client as retryable.
    assert not issubclass(CapacityError, PermanentJobError)


@pytest.mark.parametrize("stage", list(Stage), ids=lambda stage: stage.value)
def test_a_fresh_job_holds_a_slot_unless_it_is_finished(manager, monkeypatch, stage):
    # Finished ones stay in the registry: with more than MAX_RETAINED_JOBS jobs _evict would
    # drop them itself, and the count of unfinished ones could not be told apart from the count of the whole registry.
    monkeypatch.setattr(jobs, "MAX_RETAINED_JOBS", 100)
    # A fresh pause holds a slot on a par with a running job: the viewer may have only stepped away, and
    # their poll will resume it. It is resumed only on a client request, so an abandoned one would
    # hold the slot forever, and one idle longer than PAUSED_AUDIO_TTL (the same
    # term as its audio) does not count: see test_stale_pauses_do_not_hold_a_slot.
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

    # The limit guards only creation. A viewer's poll for a job that already exists is not met
    # with a refusal, however many there are.
    for job in full:
        assert manager.get_or_create(job.video_id, "ru", True) is job

    # The paused one was resumed along the way: it was already in the count, and its resumption
    # does not change the number of jobs.
    assert manager.started == started + [paused.video_id]


def test_stale_pauses_do_not_hold_a_slot(manager, clock):
    # A pause is resumed only by a client request, it will not continue by itself. Abandoned,
    # it would stay in the registry until the daemon restarts, and sixteen of them would close
    # it to new videos for good. That is why one idle longer than PAUSED_AUDIO_TTL, that
    # is longer than the term by which _evict removes its audio, does not count.
    paused = [manager.get_or_create(f"video{i:05d}a", "ru", True) for i in range(MAX_ACTIVE_JOBS)]

    for job in paused:
        job.stage = Stage.PAUSED

    # Exactly the term is not old age yet: the comparison is strict, like that of the audio in _evict.
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

    # The other slots are taken by running jobs: the abandoned pause takes nothing from them.
    for i in range(MAX_ACTIVE_JOBS):
        manager.get_or_create(f"video{i:05d}a", "ru", True)

    with pytest.raises(CapacityError):
        manager.get_or_create(OVERFLOW, "ru", True)

    started = list(manager.started)

    # The pause stayed in the registry with its cues, and the returning viewer resumes it
    # like any existing job: the limit guards creation, not resumption.
    assert manager.get("abcdefghijk", True) is stale
    assert [cue.src for cue in stale.cues] == ["Zdravo"]
    assert manager.get_or_create("abcdefghijk", "ru", True) is stale
    assert manager.started == started + ["abcdefghijk"]


def test_each_variant_of_a_video_counts_once(manager):
    # A job is the pair "video and author track", and the second variant of a known video
    # takes its own slot rather than sharing it with the first.
    for i in range(MAX_ACTIVE_JOBS - 1):
        manager.get_or_create(f"video{i:05d}a", "ru", True)

    manager.get_or_create("video00000a", "ru", False)

    assert manager.stats()["jobs"] == MAX_ACTIVE_JOBS

    # The last slot is taken, and a new variant of another known video no longer fits.
    with pytest.raises(CapacityError):
        manager.get_or_create("video00001a", "ru", False)

    # Both variants of the first video still answer a poll.
    assert manager.get_or_create("video00000a", "ru", True) is manager.get("video00000a", True)
    assert manager.get_or_create("video00000a", "ru", False) is manager.get("video00000a", False)


def test_cooled_failure_is_retried_only_when_there_is_room(manager, monkeypatch):
    # The limit is lowered so that the failed job lives until the poll: with more than MAX_RETAINED_JOBS
    # jobs _evict drops finished ones at once, and at the real limit this case
    # occurs only if nobody has polled the failed job since the failure.
    monkeypatch.setattr(jobs, "MAX_ACTIVE_JOBS", 2)
    failed = manager.get_or_create("abcdefghijk", "ru", True)
    failed._fail("сеть моргнула", retryable=True)
    failed.failed_at -= jobs.RETRY_COOLDOWN
    busy = [manager.get_or_create(f"video{i:05d}a", "ru", True) for i in range(2)]
    started = list(manager.started)

    # A retry is a new job, and when overflowing it is not created. But a poll for an
    # existing job is not met with a refusal either: it is returned as is, and the previous one
    # is not removed, otherwise its subscribers would be left on a cancelled one.
    assert manager.get_or_create("abcdefghijk", "ru", True) is failed
    assert manager.get("abcdefghijk", True) is failed
    assert failed.generation == 0
    assert manager.started == started

    busy[0].stage = Stage.DONE
    retried = manager.get_or_create("abcdefghijk", "ru", True)

    assert retried is not failed
    assert retried.generation == 1
    assert manager.started == started + ["abcdefghijk"]


# ── refusal on the paths that create a job ───────────────────────────────


async def test_post_jobs_refuses_with_429_and_the_sentence(client, manager, full, sentence):
    response = await client.post("/v1/jobs", json={"videoId": OVERFLOW})

    assert response.status_code == 429
    assert response.json() == {"detail": sentence}
    assert manager.get(OVERFLOW) is None
    assert manager.started == [job.video_id for job in full]

    # A job that already exists still answers: a repeated POST does not create it.
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
    # The connection's queue is not exposed, so we judge the subscription by the jobs: the
    # socket is on none of them.
    assert all(not job._subscribers for job in full)

    # Nothing is left in the socket's queue after the refusal: next comes the snapshot
    # of an existing job, which watch still subscribes to at the limit as well.
    await connection.say(watch(full[0].video_id))
    heard = await connection.hear()

    assert (heard["type"], heard["videoId"]) == ("snapshot", full[0].video_id)
    assert heard["stage"] == "queued"


async def test_events_with_create_answers_200_with_an_error_snapshot(
    client, manager, full, sentence
):
    params = {"create": "true", "authorTrack": "false", "target": "sr", "cursor": "123"}

    response = await client.get(f"/v1/jobs/{OVERFLOW}/events", params=params)

    # 200, not 429: for any other answer the extension writes "the daemon answered …" and
    # repeats the request after a pause, while a snapshot with the error stage shows the sentence and
    # stops the poll.
    assert response.status_code == 200

    body = response.json()
    snapshot = body["snapshot"]

    assert body["cursor"] == 123
    assert body["events"] == []
    assert snapshot["stage"] == "error"
    assert snapshot["error"] == sentence
    assert snapshot["retryable"] is True
    # The shape of the snapshot is exactly what Job.snapshot() produces: the same fields, not a copy of them.
    assert snapshot.keys() == full[0].snapshot().keys()
    assert snapshot["type"] == "snapshot"
    assert snapshot["videoId"] == OVERFLOW
    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0
    assert snapshot["waiting"] is None
    assert snapshot["language"] is None
    assert snapshot["title"] == ""
    assert (snapshot["total"], snapshot["translated"], snapshot["cues"]) == (0, 0, [])
    # The job for the snapshot did not enter the registry and was not started.
    assert manager.get(OVERFLOW) is None
    assert manager.jobs_for(OVERFLOW) == []
    assert manager.started == [job.video_id for job in full]


async def test_events_for_an_existing_job_are_served_at_the_cap(client, manager, full):
    # A cursor that is too old: the answer comes as a snapshot at once, without waiting for new events.
    response = await client.get(f"/v1/jobs/{full[0].video_id}/events", params={"cursor": "-1"})

    assert response.status_code == 200

    snapshot = response.json()["snapshot"]

    # This is a snapshot of a real job, not a refusal.
    assert snapshot["videoId"] == full[0].video_id
    assert snapshot["stage"] == "queued"
    assert snapshot["error"] is None


# ── the limit on paused audio ────────────────────────────────────────────

# The audio of test pauses: a thousand float32 samples. The real budget cannot be
# checked at such sizes, so the tests below replace it with a few such chunks.
CHUNK = 1000
CHUNK_BYTES = CHUNK * 4


def stash(job, mtime):
    """Pause a job with real paused audio, with a record time in the past."""
    job.stage = Stage.PAUSED
    job.stash_audio(np.zeros(CHUNK, dtype="<f4"))
    os.utime(job._audio_path, (mtime, mtime))

    return job._audio_path


def test_paused_audio_over_budget_is_discarded_oldest_first(manager, audio_dir):
    half = PAUSED_AUDIO_MAX_BYTES // 2 + 1
    made = []

    # Files without an owner: no job in the registry points at them, like audio left over from
    # a dead job.
    for i, name in enumerate(("old.f32", "middle.f32", "fresh.f32")):
        path = audio_dir / name

        with path.open("wb") as handle:  # a sparse file: takes no space
            handle.truncate(half)

        os.utime(path, (1000 + i, 1000 + i))
        made.append(path)

    manager._evict()

    assert not made[0].exists()
    # They are removed while the sum is above the limit: after the first file two of half
    # a chunk plus a bit remain, and they still do not fit.
    assert not made[1].exists()
    assert made[2].exists()
    assert sum(f.stat().st_size for f in audio_dir.iterdir()) <= PAUSED_AUDIO_MAX_BYTES


def test_budget_eviction_goes_through_the_owning_job(manager, audio_dir, monkeypatch):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", CHUNK_BYTES)
    # Jobs are created before any audio is written: get_or_create evicts, and the budget would
    # fire before everything is laid out.
    first = manager.get_or_create("video00000a", "ru", True)
    last = manager.get_or_create("video00001a", "ru", True)
    # Seniority is one for files of both kinds: the oldest belongs to a job, the middle one —
    # to nobody, the freshest again to a job.
    first_file = stash(first, mtime=1000)
    orphan = audio_dir / "orphan.f32"
    orphan.write_bytes(bytes(CHUNK_BYTES))
    os.utime(orphan, (2000, 2000))
    last_file = stash(last, mtime=3000)

    manager._evict()

    # The job's file was removed through the job: the handle did not stay hanging on a vanished file.
    assert first._audio_path is None
    assert first.take_audio() is None
    assert not first_file.exists()
    # Next the oldest of the remaining ones, which has no owner — it is simply erased.
    assert not orphan.exists()
    # The fresh one is in place and still readable.
    assert last._audio_path == last_file
    assert last.take_audio().size == CHUNK


def test_paused_audio_exactly_at_the_budget_stays(manager, audio_dir, monkeypatch):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", 2 * CHUNK_BYTES)
    first = manager.get_or_create("video00000a", "ru", True)
    second = manager.get_or_create("video00001a", "ru", True)
    paths = [stash(first, mtime=1000), stash(second, mtime=2000)]

    manager._evict()

    # "While the sum is above the limit": equal to the limit already fits.
    assert all(path.exists() for path in paths)
    assert first._audio_path is not None
    assert second._audio_path is not None


def test_time_cleanup_runs_before_the_budget(manager, audio_dir, monkeypatch):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", CHUNK_BYTES + 1)
    watched = manager.get_or_create("video00000a", "ru", True)
    abandoned = manager.get_or_create("video00001a", "ru", True)
    # The polled pause has an older file, the abandoned one a newer file: judging by file age alone,
    # the budget would remove the live one.
    stash(watched, mtime=1000)
    stash(abandoned, mtime=2000)
    abandoned.touched_at -= PAUSED_AUDIO_TTL + 1

    manager._evict()

    # Time removed the abandoned one, after which the sum fit into the limit, and the budget has
    # nothing left to do.
    assert abandoned._audio_path is None
    assert watched._audio_path is not None


class FakeTask:
    """The job's task within the limits _evict and the slot count ask about: whether it has finished."""

    def __init__(self, finished):
        self._finished = finished

    def done(self):
        return self._finished


# What counts as an abandoned pause: paused, no task or a finished one, and nobody has touched
# the job for longer than PAUSED_AUDIO_TTL. The other rows differ from
# the first by exactly one feature each.
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
    # One feature for the audio and for the slot: an abandoned pause loses both, and any
    # other job keeps both. Were they to diverge, a pause's audio would vanish while the slot for it
    # stayed, or the other way round.
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
    # The directory appears at the first audio deferral, and after startup it does not exist at all.
    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)

    manager._evict()

    assert not PAUSED_AUDIO_DIR.exists()


# ── audio housekeeping does not fail a request ──────────────────────────


@pytest.mark.parametrize("method", ["iterdir", "stat", "unlink"])
def test_disk_errors_in_audio_housekeeping_do_not_fail_a_request(
    manager, audio_dir, clock, monkeypatch, caplog, refuse_in_audio_dir, method
):
    # Housekeeping runs at the end of every get_or_create, that is on every poll that creates
    # jobs, and a disk refusal in it brought the request down when the job had already been created and started:
    # the client got a 500 while the job ran. A directory the daemon may not touch is not cleaned even by a
    # restart: rmtree with ignore_errors simply skips it.
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", CHUNK_BYTES)
    watched = manager.get_or_create("video00000a", "ru", True)
    abandoned = manager.get_or_create("video00001a", "ru", True)
    # More audio than the budget, and one pause abandoned: housekeeping reaches all three places —
    # the term, the directory listing and the budget.
    stash(watched, mtime=1000)
    stash(abandoned, mtime=2000)
    orphan = audio_dir / "orphan.f32"
    orphan.write_bytes(bytes(CHUNK_BYTES))
    os.utime(orphan, (500, 500))
    clock(PAUSED_AUDIO_TTL + 1)
    refuse_in_audio_dir(method)
    caplog.set_level(logging.WARNING, logger="helper.jobs")

    # A poll for an existing job returns it, and a new one is created, registered and
    # started: housekeeping failed, but the request does not depend on it.
    assert manager.get_or_create("video00000a", "ru", True) is watched

    fresh = manager.get_or_create("video00002a", "ru", True)

    assert manager.get("video00002a", True) is fresh
    assert manager.started[-1] == "video00002a"

    # The refusal is not swallowed silently: the log says which path and why.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    ]

    assert warnings
    assert all(str(PAUSED_AUDIO_DIR) in message for message in warnings)
    assert all("Permission denied" in message for message in warnings)


def test_audio_that_cannot_be_removed_still_counts_against_the_budget(
    manager, audio_dir, monkeypatch
):
    monkeypatch.setattr(jobs, "PAUSED_AUDIO_MAX_BYTES", CHUNK_BYTES)
    stuck, middle, fresh = (audio_dir / name for name in ("stuck.f32", "middle.f32", "fresh.f32"))

    for age, path in enumerate((stuck, middle, fresh)):
        path.write_bytes(bytes(CHUNK_BYTES))
        os.utime(path, (1000 + age, 1000 + age))

    original = Path.unlink

    def refusing(self, *args, **kwargs):
        if self == stuck:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))

        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing)

    manager._evict()

    # The budget is what lies on disk. The oldest one was not erased and still takes space,
    # so it stays in the sum, and the budget makes up the limit with the
    # next ones by seniority.
    assert stuck.exists()
    assert not middle.exists()
    assert not fresh.exists()


def test_a_handle_cleared_between_two_reads_does_not_fail_a_request(
    manager, audio_dir, monkeypatch
):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    reads = []

    def handle(self):
        # The first read sees the file, the following ones no longer: between them the worker thread called
        # discard_audio (take_audio and stash_audio go through to_thread).
        reads.append(self)

        return path if len(reads) == 1 else None

    # A property on the class shadows the instance field. Thus the handle disappears exactly between
    # two reads, without threads and without a race in the test itself.
    monkeypatch.setattr(
        jobs.Job, "_audio_path", property(handle, lambda self, value: None), raising=False
    )

    # Housekeeping reads the job's handle once: a second read after the None check would
    # crash with AttributeError on the poll.
    assert manager.get_or_create("video00000a", "ru", True) is job


def handle_gone_after_first_read(monkeypatch, path) -> None:
    """The job's handle sees the file only on the first read, after that it is None.

    This is what budget housekeeping (_trim_paused_audio → discard_audio in the event loop) looks like
    after it cleared the handle while take_audio or stash_audio ran in the worker thread. A property on
    the class shadows the instance field, and a write to it is swallowed.
    """
    reads = []

    def handle(self):
        reads.append(self)

        return path if len(reads) == 1 else None

    monkeypatch.setattr(
        jobs.Job, "_audio_path", property(handle, lambda self, value: None), raising=False
    )


def test_take_audio_survives_a_handle_cleared_between_two_reads(manager, audio_dir, monkeypatch):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    handle_gone_after_first_read(monkeypatch, path)

    # The handle is read once: the file that was in place at the check is the one that is read. A second
    # read after the reset would crash with AttributeError, and the resumption would fail with a
    # retryable refusal instead of taking the audio or downloading it again.
    assert job.take_audio().size == CHUNK


def test_stash_audio_survives_a_handle_cleared_between_two_reads(
    manager, audio_dir, monkeypatch
):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    handle_gone_after_first_read(monkeypatch, path)

    job.stash_audio(np.zeros(CHUNK, dtype="<f4"))

    # The file was caught on the first and only read of the handle, and the audio is not written a second time.
    assert list(audio_dir.iterdir()) == [path]
