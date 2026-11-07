"""The language decision on the wire: the language field in the snapshot and the language event before the cues."""

import logging

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import jobs
from helper.jobs import Stage, refusal_snapshot
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack

# Eleven characters, like a real video.
VIDEO = "langwire001"

# Serbian speech on a channel YouTube marked English (Juske, Q2QcYOBk0Jo):
# the detector is sure of Serbian, the declaration disagrees with the speech.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# An English video without metadata: there is nothing to disagree with, nothing to say.
ENGLISH = LanguageVerdict(
    effective="en", declared=None, declared_by=None,
    detected="en", confidence=0.99, reason="detector",
)
# No window voted, and the language was taken from YouTube.
HINTED = LanguageVerdict(
    effective="en", declared="en", declared_by="metadata",
    detected=None, confidence=None, reason="hint",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
TRACK = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
# The snapshot keys before this plan. There is one new field, and the extension does not read its name.
OLD_SNAPSHOT_KEYS = {
    "type", "videoId", "authorTrack", "targetLang", "generation", "stage", "error",
    "retryable", "waiting", "title", "total", "translated", "cues",
}
# Event types the extension responds to with an action (content.js, background.js).
EXTENSION_TYPES = {"state", "snapshot", "cues", "helper", "restored", "gone"}


def author_track(lang):
    return CaptionTrack(lang=lang, url="", ext="json3", name="")


def answer(verdict, windows=(), cues=CUES):
    """The worker's answer according to the task 5 protocol: the decision, the detector windows, the cues."""
    return {
        "language": verdict.effective,
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.0,
            "votes": sum(code is not None for code, _, _ in windows),
            "probed": len(windows),
            "windows": [list(window) for window in windows],
        },
        "duration": 2.0,
        "model": "large-v3",
        "cues": cues,
    }


def language_lines(caplog):
    """Log lines about the language decision for this video: level and text."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "helper.jobs" and record.getMessage().startswith(f"language {VIDEO}:")
    ]


def test_snapshot_has_no_language_until_the_daemon_decides(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert job.language is None
    assert job.snapshot()["language"] is None
    assert refusal_snapshot(VIDEO, "ru", True, "отказ", True)["language"] is None


def test_the_language_event_lands_in_the_journal_and_the_snapshot(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_stage(Stage.QUEUED, waiting="gpu")

    job._set_language(JUSKE)

    assert job.language is JUSKE
    assert job.events[-1] == {"type": "language", "language": JUSKE.wire(), "videoId": VIDEO}
    assert job.snapshot()["language"] == JUSKE.wire()
    # Not through _set_stage: the stage and the card wait remain, the badge label is the same.
    assert job.stage is Stage.QUEUED
    assert job.waiting == "gpu"


def test_the_language_reuses_no_name_the_extension_reads(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_language(JUSKE)
    event = job.events[-1]
    snapshot = job.snapshot()

    assert event.keys() == {"type", "language", "videoId"}
    assert event["type"] not in EXTENSION_TYPES
    # The snapshot keys before the language plan. pending and translatedThrough were added by the
    # tablet page plan; the extension reads none of the three, so any fourth
    # new key is an error.
    assert snapshot.keys() - OLD_SNAPSHOT_KEYS == {"language", "pending", "translatedThrough"}
    assert snapshot.keys() >= OLD_SNAPSHOT_KEYS


async def test_the_socket_hears_the_language_before_segmented_after_asr(
    manager, connection, pipeline
):
    pipeline.asr_result = answer(JUSKE)
    await connection.say(watch(VIDEO))
    caught_up = await connection.hear()

    assert (caught_up["type"], caught_up["language"]) == ("snapshot", None)

    await pipeline.run(manager.get(VIDEO, True))
    heard = await hear_until_done(connection)
    types = [event["type"] for event in heard]
    at = types.index("language")

    assert at < types.index("segmented")
    # The decision arrives right after recognition: the worker makes it.
    assert heard[at - 1]["stage"] == "asr"
    assert heard[at] == {"type": "language", "language": JUSKE.wire(), "videoId": VIDEO}
    assert heard[at]["language"]["mismatch"] is True
    assert heard[at]["language"]["note"] == "YouTube indicated en, by speech sr"
    assert heard[-1]["stage"] == "done"


async def test_the_job_endpoint_and_the_poll_carry_the_language(client, manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)
    before = job.journal_end
    pipeline.asr_result = answer(JUSKE)

    await pipeline.run(job)

    snapshot = (await client.get(f"/v1/jobs/{VIDEO}")).json()
    stale = (await client.get(f"/v1/jobs/{VIDEO}/events", params={"cursor": "-1"})).json()
    polled = (
        await client.get(f"/v1/jobs/{VIDEO}/events", params={"cursor": str(before)})
    ).json()
    types = [event["type"] for event in polled["events"]]

    assert snapshot["language"] == JUSKE.wire()
    assert stale["snapshot"]["language"] == JUSKE.wire()
    assert types.index("language") < types.index("segmented")


async def test_the_socket_hears_the_language_before_segmented_on_the_author_track(
    manager, connection, pipeline, caplog
):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.info = metadata(audio_language="en", manual_tracks=[author_track("sr")])
    pipeline.track = TRACK
    await connection.say(watch(VIDEO))
    await connection.hear()

    await pipeline.run(manager.get(VIDEO, True))
    heard = await hear_until_done(connection)
    types = [event["type"] for event in heard]
    expected = LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected=None,
        confidence=None, reason="track", source="manual", track="sr", verified=None,
    )

    assert types.index("language") < types.index("segmented")
    assert heard[types.index("language")] == {
        "type": "language", "language": expected.wire(), "videoId": VIDEO,
    }
    assert expected.wire()["note"] == "YouTube indicated en, the author's track sr"
    assert not pipeline.workers
    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"language {VIDEO}: sr (track), declared en (metadata), by speech —, "
            "confidence —, windows: —",
        ),
        (logging.WARNING, f"language {VIDEO}: YouTube indicated en, the author's track sr"),
    ]


@pytest.mark.parametrize(("label", "effective"), [("sr-Latn", "sr"), ("sh", "sr"), ("hr", "hr")])
async def test_the_author_track_label_names_the_language(manager, pipeline, label, effective):
    pipeline.info = metadata(manual_tracks=[author_track(label)])
    pipeline.track = TRACK
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.language == LanguageVerdict(
        effective=effective, declared=None, declared_by=None, detected=None,
        confidence=None, reason="track", source="manual", track=label, verified=None,
    )


async def test_a_pin_is_the_declaration_of_an_author_track(manager, pipeline):
    pipeline.configure(language="hr")
    pipeline.info = metadata(audio_language="en", manual_tracks=[author_track("sr")])
    pipeline.track = TRACK
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.language == LanguageVerdict(
        effective="sr", declared="hr", declared_by="config", detected=None,
        confidence=None, reason="track", source="manual", track="sr", verified=None,
    )
    # hr and sr are one language with three tokens: no mismatch.
    assert job.language.mismatch is False


async def test_a_job_preempted_after_recognition_resumes_with_its_verdict(
    manager, pipeline, monkeypatch
):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = answer(JUSKE)
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED
    assert job.language == JUSKE
    assert [cue.src for cue in job.cues] == ["Dobar dan."]

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert len(pipeline.workers) == 1
    assert job.snapshot()["language"] == JUSKE.wire()
    assert [event["type"] for event in job.events].count("language") == 1


async def test_an_answer_without_a_verdict_falls_back_to_its_language(manager, pipeline):
    pipeline.asr_result = {"language": "sr", "duration": 2.0, "model": "large-v3", "cues": CUES}
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.language == LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected=None,
        confidence=None, reason="detector",
    )


async def test_an_answer_in_an_unknown_language_fails_the_job(manager, pipeline):
    pipeline.asr_result = {"language": "xx", "duration": 2.0, "model": "large-v3", "cues": CUES}
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.ERROR
    # Permanent: a retry would take the card for recognition again for the same answer.
    assert job.retryable is False
    assert job.error == 'the recognizer returned an unknown language "xx"'
    assert job.language is None
    assert job.cues == []


async def test_the_decision_is_one_info_line_with_the_windows(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(ENGLISH, windows=[("en", 0.99, 30.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"language {VIDEO}: en (detector), declared —, by speech en, "
            "confidence 0.99, windows: en 0.99/30s",
        ),
    ]


async def test_a_mismatch_is_a_warning_in_words(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(JUSKE, windows=[("sr", 0.97, 28.0), (None, 0.0, 1.2)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"language {VIDEO}: sr (detector), declared en (metadata), by speech sr, "
            "confidence 0.97, windows: sr 0.97/28s, — 0.00/1s",
        ),
        (logging.WARNING, f"language {VIDEO}: YouTube indicated en, by speech sr"),
    ]
    # The dead line "speech language … the detector gave" is gone: the two above took its place.
    assert not [r for r in caplog.records if "detector gave" in r.getMessage()]


async def test_a_detector_that_never_answered_is_a_warning(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    # There is speech in the window but no detector answer: zero votes, the YouTube label decided —
    # the same path by which Serbian speech under an en label would go to the English decoder.
    pipeline.asr_result = answer(HINTED, windows=[(None, 0.0, 25.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"language {VIDEO}: en (hint), declared en (metadata), by speech —, "
            "confidence —, windows: — 0.00/25s",
        ),
        (logging.WARNING, f"language {VIDEO}: detector did not answer, speech windows without an answer: 1"),
    ]
