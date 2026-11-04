"""Решение о языке на проводе: поле language в снимке и событие language раньше реплик."""

import logging

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import jobs
from helper.jobs import Stage, refusal_snapshot
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack

# Одиннадцать знаков, как у настоящего ролика.
VIDEO = "langwire001"

# Сербская речь на канале, который YouTube пометил английским (Juske, Q2QcYOBk0Jo):
# определитель уверен в сербском, объявление с речью расходится.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# Английский ролик без метаданных: расходиться не с чем, сказать нечего.
ENGLISH = LanguageVerdict(
    effective="en", declared=None, declared_by=None,
    detected="en", confidence=0.99, reason="detector",
)
# Ни одно окно не проголосовало, и язык взят у YouTube.
HINTED = LanguageVerdict(
    effective="en", declared="en", declared_by="metadata",
    detected=None, confidence=None, reason="hint",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
TRACK = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
# Ключи снимка до этого плана. Новое поле одно, и его имя расширение не читает.
OLD_SNAPSHOT_KEYS = {
    "type", "videoId", "authorTrack", "targetLang", "generation", "stage", "error",
    "retryable", "waiting", "title", "total", "translated", "cues",
}
# Типы событий, на которые расширение отвечает действием (content.js, background.js).
EXTENSION_TYPES = {"state", "snapshot", "cues", "helper", "restored", "gone"}


def author_track(lang):
    return CaptionTrack(lang=lang, url="", ext="json3", name="")


def answer(verdict, windows=(), cues=CUES):
    """Ответ воркера по протоколу задачи 5: решение, окна определителя, реплики."""
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
    """Строки журнала о решении по языку этого ролика: уровень и текст."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "helper.jobs" and record.getMessage().startswith(f"язык {VIDEO}:")
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
    assert job.events[-1] == {"type": "language", "language": JUSKE.wire()}
    assert job.snapshot()["language"] == JUSKE.wire()
    # Не через _set_stage: стадия и ожидание карты остаются, надпись бейджа та же.
    assert job.stage is Stage.QUEUED
    assert job.waiting == "gpu"


def test_the_language_reuses_no_name_the_extension_reads(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_language(JUSKE)
    event = job.events[-1]
    snapshot = job.snapshot()

    assert event.keys() == {"type", "language"}
    assert event["type"] not in EXTENSION_TYPES
    # Ключи снимка до плана о языке. pending и translatedThrough добавил план страницы
    # для планшета; расширение не читает ни одного из трёх, поэтому любой четвёртый
    # новый ключ — ошибка.
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
    # Решение приходит сразу за распознаванием: его выносит воркер.
    assert heard[at - 1]["stage"] == "asr"
    assert heard[at] == {"type": "language", "language": JUSKE.wire()}
    assert heard[at]["language"]["mismatch"] is True
    assert heard[at]["language"]["note"] == "YouTube указал en, по речи sr"
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
    assert heard[types.index("language")] == {"type": "language", "language": expected.wire()}
    assert expected.wire()["note"] == "YouTube указал en, дорожка автора sr"
    assert not pipeline.workers
    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: sr (track), объявлен en (metadata), по речи —, "
            "уверенность —, окна: —",
        ),
        (logging.WARNING, f"язык {VIDEO}: YouTube указал en, дорожка автора sr"),
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
    # hr и sr — один язык с тремя токенами: расхождения нет.
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
    # Окончательно: повтор снова занял бы карту распознаванием ради того же ответа.
    assert job.retryable is False
    assert job.error == "распознаватель вернул неизвестный язык «xx»"
    assert job.language is None
    assert job.cues == []


async def test_the_decision_is_one_info_line_with_the_windows(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(ENGLISH, windows=[("en", 0.99, 30.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: en (detector), объявлен —, по речи en, "
            "уверенность 0.99, окна: en 0.99/30с",
        ),
    ]


async def test_a_mismatch_is_a_warning_in_words(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(JUSKE, windows=[("sr", 0.97, 28.0), (None, 0.0, 1.2)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: sr (detector), объявлен en (metadata), по речи sr, "
            "уверенность 0.97, окна: sr 0.97/28с, — 0.00/1с",
        ),
        (logging.WARNING, f"язык {VIDEO}: YouTube указал en, по речи sr"),
    ]
    # Мёртвая строка «язык речи … определитель дал» ушла: её место заняли две выше.
    assert not [r for r in caplog.records if "определитель дал" in r.getMessage()]


async def test_a_detector_that_never_answered_is_a_warning(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    # Речь в окне есть, а ответа определителя нет: голосов ноль, решила метка YouTube —
    # тот же путь, которым сербская речь под меткой en ушла бы в английский декодер.
    pipeline.asr_result = answer(HINTED, windows=[(None, 0.0, 25.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: en (hint), объявлен en (metadata), по речи —, "
            "уверенность —, окна: — 0.00/25с",
        ),
        (logging.WARNING, f"язык {VIDEO}: определитель не ответил, окон с речью без ответа: 1"),
    ]
