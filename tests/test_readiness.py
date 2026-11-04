"""Готовность на проводе: где перевод кончается и какие куски ещё пусты."""

from conftest import preempt_after_asr
from helper import jobs
from helper.jobs import JobCue, Stage

VIDEO = "ready000001"
CUES = [
    {"start": 0.0, "end": 2.0, "text": "Dobar dan.", "words": []},
    {"start": 2.0, "end": 4.0, "text": "Kako si?", "words": []},
]


def job_with(manager, lines):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.cues = [
        JobCue(index=i, start=float(i * 2), end=float(i * 2 + 2), src=f"c{i}", lines=lines[i])
        for i in range(len(lines))
    ]

    return job


def test_the_snapshot_says_where_the_translation_stops(manager):
    job = job_with(manager, [["А"], [], ["В"], []])

    snapshot = job.snapshot()

    assert snapshot["pending"] == [[2.0, 4.0], [6.0, 8.0]]
    assert snapshot["translatedThrough"] == 2.0


def test_a_fully_translated_job_has_nothing_pending(manager):
    job = job_with(manager, [["А"], ["Б"]])

    assert job.snapshot()["pending"] == []
    assert job.snapshot()["translatedThrough"] is None


def test_a_job_without_cues_has_nothing_pending(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert job.snapshot()["pending"] == []
    assert job.snapshot()["translatedThrough"] is None


def test_neighbouring_gaps_are_one_stretch(manager):
    job = job_with(manager, [["А"], [], [], ["Г"]])

    assert job.snapshot()["pending"] == [[2.0, 6.0]]


async def test_the_segmented_event_says_the_whole_transcript_is_still_pending(
    manager, pipeline, monkeypatch
):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = {
        "language": "hr",
        "duration": 4.0,
        "model": "large-v3",
        "cues": CUES,
    }
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED
    segmented = next(e for e in job.events if e["type"] == "segmented")
    assert segmented["total"] == 2
    # Нарезка обрезает конец реплики по тишине, так что между репликами остаётся
    # настоящий просвет, и сливать его не с чем. Проверяем то, что обещает поле:
    # непереведённое покрывает ролик с начала до конца, и перевод не начат.
    assert segmented["pending"][0][0] == 0.0
    assert segmented["pending"][-1][1] == 4.0
    assert segmented["translatedThrough"] == 0.0


async def test_the_cues_event_says_nothing_is_left_when_the_batch_finished(
    manager, pipeline
):
    job = job_with(manager, [[], [], []])

    async with pipeline.lease.acquire(0) as held:
        await pipeline.runner._translate(job, held)

    event = job.events[-1]
    assert event["type"] == "cues"
    assert event["pending"] == []
    assert event["translatedThrough"] is None
