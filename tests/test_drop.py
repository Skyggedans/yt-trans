"""Drop by client: the one leaving removes its registration, while work another
client is still watching stays. Without a key the drop cancels everything, as before keys existed."""

import logging

import numpy as np
import pytest

from helper import jobs
from helper.jobs import PLAYHEAD_TTL, Stage

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

    # A removed registration is also a removed position: the departed viewer no longer pulls
    # the translation away from those remaining, and does not wait for its record to expire after PLAYHEAD_TTL.
    assert [job.aim() for _ in range(3)] == [10.0, 10.0, 10.0]


def test_drop_cancels_the_job_only_when_nobody_is_left(manager, monkeypatch):
    cancelled = []
    monkeypatch.setattr(jobs.Job, "cancel", lambda job: cancelled.append(job))
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    manager.drop(VIDEO, "desktop")

    # Removing a registration is not cancelling: the download and recognition go on for the tablet.
    assert cancelled == []

    manager.drop(VIDEO, "tablet")

    # The cancellation is real, not just a removal from the registry: without it the downloader and recognition
    # would keep running, wanted by nobody.
    assert cancelled == [job]


def test_repeated_drop_by_a_client_does_not_cancel_for_the_others(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    # A repeated request (a network drop, an impatient press) removes a client already removed.
    # That is a quiet skip, not a reason to cancel a job another client is still watching.
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

    # The drop comes without the track flag, and the daemon does not know which variant the client
    # was watching: the key is removed from all, and those with nobody left are cancelled.
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

    # The desktop watched both variants while toggling the setting, the tablet one. The variant
    # where nobody but the departed one remains is cancelled, while the second keeps going for the tablet.
    assert manager.drop(VIDEO, "desktop") is True
    assert manager.get(VIDEO, True) is watched
    assert manager.get(VIDEO, False) is None


def test_drop_without_client_id_cancels_every_variant_and_every_client(manager):
    with_track = manager.get_or_create(VIDEO, "ru", True)
    without = manager.get_or_create(VIDEO, "ru", False)

    for job in (with_track, without):
        job.report_playhead("tablet", 10.0)

    # Deliberately: the extension does not send a key, and it already knew how to drop a whole job — that is
    # how its retry button works after a failure. An additive change must not take away
    # from an old client what it could do, so "without a key" stays "cancel everything", even
    # when the job has a live tablet.
    assert manager.drop(VIDEO) is True
    assert manager.jobs_for(VIDEO) == []


def test_drop_does_not_count_a_client_that_went_silent(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    clock(PLAYHEAD_TTL + 1)
    job.report_playhead("desktop", 21.0)

    # The tablet has been silent longer than the term: the tab was closed or slept, and there is nobody to remove it. Its
    # expired record does not hold the job, otherwise it would live on after the last
    # live viewer left.
    assert manager.drop(VIDEO, "desktop") is True
    assert manager.get(VIDEO) is None


def test_drop_counts_a_client_exactly_the_ttl_silent_as_live(manager, clock):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.report_playhead("tablet", 10.0)

    clock(PLAYHEAD_TTL)
    job.report_playhead("desktop", 20.0)

    # The same term as the round in aim(): "older" means strictly older. Together with the test
    # above, the boundary is pinned from both sides.
    assert manager.drop(VIDEO, "desktop") is False
    assert manager.get(VIDEO) is job


async def test_drop_endpoint_accepts_a_request_without_a_body(client, manager):
    manager.get_or_create(VIDEO, "ru", True)

    # Exactly what the extension sends (extension/background.js, retry after a failure): POST
    # with no body and no Content-Type, only with Authorization. Make the body mandatory, and
    # this retry turns into a 422.
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

    # As before, 200, not 404: dropping what does not exist is not an error. And nothing is created.
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

    # A bad key that was sent is not "no key". It is reduced to "legacy", as with the play
    # position (see _client_key), while the right to cancel everything stays with those who send no
    # key at all. The list and the dict also show that there is no crash on hashing.
    assert response.status_code == 200
    assert response.json() == {"dropped": VIDEO, "cancelled": False}
    assert manager.get(VIDEO) is job


# ── drop when the pause audio cannot be erased ───────────────────────────


class LiveTask:
    """The job's task, still running: it remembers cancellation, done() answers after it."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> bool:
        self.cancelled = True

        return True

    def done(self) -> bool:
        return self.cancelled


class RunningWhisper:
    """A recognition process still running: poll() is None until it is killed."""

    def __init__(self) -> None:
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


def resumed_in_asr(job):
    """A resumed job in the middle of recognition: the pause audio on disk, the task and Whisper alive.

    Only this holds both the card and the audio: on the first pass the audio is in memory, and a pause has
    neither a task nor a process.
    """
    job.stage = Stage.ASR
    job.stash_audio(np.zeros(1000, dtype="<f4"))
    job._task = LiveTask()
    job._asr_proc = RunningWhisper()

    return job._audio_path


def test_cancel_frees_the_card_even_if_the_paused_audio_stays(
    manager, audio_dir, refuse_in_audio_dir, caplog, monkeypatch
):
    job = manager.get_or_create(VIDEO, "ru", True)
    path = resumed_in_asr(job)
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")
    # What held the card at the moment the drop reached the audio.
    seen = []
    discard = job.discard_audio

    def watched():
        seen.append((job._asr_proc.killed, job._task.cancelled))
        discard()

    monkeypatch.setattr(job, "discard_audio", watched)

    job.cancel()

    # The card is held by the process and the task, and the drop removes both before the audio, whatever
    # happens to it: otherwise a job the registry has already forgotten would keep recognising and translating.
    assert seen == [(True, True)]
    assert job._asr_proc.killed
    assert job._task.cancelled
    # The audio was not erased and was left ownerless; the disk refusal is a line in the log, not an exception.
    assert job._audio_path is None
    assert path.exists()
    assert any(
        str(path) in record.getMessage() and "Permission denied" in record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    )


def test_drop_with_undeletable_audio_still_stops_every_variant(
    manager, audio_dir, refuse_in_audio_dir
):
    steady = manager.get_or_create(VIDEO, "ru", False)
    steady._task = LiveTask()
    stuck = manager.get_or_create(VIDEO, "ru", True)
    resumed_in_asr(stuck)
    # Touched last: jobs_for gives it to the drop first, and a failure on it must not
    # leave the neighbouring variant uncancelled. Explicitly, not by the clock: two calls in a row
    # could get the same reading, and the order would be decided by insertion into the registry.
    stuck.touched_at = steady.touched_at + 1.0
    refuse_in_audio_dir("unlink")

    assert manager.jobs_for(VIDEO)[0] is stuck
    assert manager.drop(VIDEO) is True
    assert manager.jobs_for(VIDEO) == []
    assert stuck._asr_proc.killed
    assert stuck._task.cancelled
    assert steady._task.cancelled


async def test_drop_endpoint_answers_even_if_the_paused_audio_cannot_be_removed(
    client, manager, audio_dir, refuse_in_audio_dir
):
    job = manager.get_or_create(VIDEO, "ru", True)
    resumed_in_asr(job)
    refuse_in_audio_dir("unlink")

    response = await client.post(f"/v1/jobs/{VIDEO}/drop")

    # A real server would answer 500 to an exception from here; the tests' ASGITransport
    # hands it to the test itself.
    assert response.status_code == 200
    assert response.json() == {"dropped": VIDEO, "cancelled": True}
    assert job._asr_proc.killed
    assert job._task.cancelled
