"""Затравка под решённый язык и отсев мусора распознавания: воркер и демон."""

import logging
from dataclasses import replace

import numpy as np
import pytest

from helper import jobs
from helper.asr import worker
from helper.asr.prompt import initial_prompt
from helper.jobs import Stage
from helper.language import Detection, LanguageVerdict

VIDEO = "abcdefghijk"
PHRASE = "Ovo je transkript video snimka na srpskom jeziku."
NAMED = initial_prompt("sr", ["Mamini", "Epizoda"])
LOOP = ", ".join(["ne"] * 12)
# jWrDvxtofLk, 201,8–229,6 с: шесть настоящих предложений, потом петля «tak» в том же куске.
DIALOGUE = (
    "Pa nije to dobro. Kako nije? Šta nije dobro? Pa očajno je, pogledaj ti koliko je ovo "
    "krupno. Šta radiš sa ovim? Da ga jedem."
)
TAK = "Ideš, slušaj me, prvo uzduš, ovako, pa poprično, pa što sitnije, " + "tak, " * 70 + "tak"
MINUTE = np.zeros(60 * worker.SAMPLE_RATE, dtype="<f4")

# Восемь окон с речью, решает определитель. Сербская речь — как у канала Juske,
# который YouTube помечает en.
SERBIAN_SPEECH = Detection(
    language="sr", confidence=0.97, bcms_share=0.97, floor=False, votes=8, probed=8,
    windows=(("sr", 0.97, 25.0),) * 8,
)
ENGLISH_SPEECH = Detection(
    language="en", confidence=0.99, bcms_share=0.0, floor=False, votes=8, probed=8,
    windows=(("en", 0.99, 28.0),) * 8,
)

# Аргументы, которые пакетный режим faster-whisper 1.2.1 принимает и не применяет.
DEAD_GUARDS = {
    "temperature",
    "compression_ratio_threshold",
    "log_prob_threshold",
    "no_speech_threshold",
    "condition_on_previous_text",
    "hallucination_silence_threshold",
}


def heard(monkeypatch, detection: Detection) -> None:
    """Определитель «услышал» заданное: окна, VAD и модель здесь ни при чём."""
    monkeypatch.setattr(worker, "_detect", lambda model, audio: detection)


def segment(start, end, text, *, no_speech=0.05, logprob=-0.2, ratio=1.1) -> dict:
    """Сегмент так, как его приносит JSON воркера."""
    return {
        "start": start, "end": end, "text": text, "words": [],
        "no_speech_prob": no_speech, "avg_logprob": logprob, "compression_ratio": ratio,
    }


MIXED = [
    segment(0.0, 5.0, "Dobar dan. Pominju se na srpskom jeziku. Kako ste?"),
    segment(6.0, 9.0, "Hvala na gledanju.", no_speech=0.92, logprob=-1.4),
    segment(10.0, 14.0, LOOP, ratio=3.1),
]


def test_cue_carries_the_three_metrics_of_its_segment(whisper):
    from faster_whisper.transcribe import Word

    whole = whisper.segment(
        1.0, 2.5, " Zdravo svima.", no_speech_prob=0.12, avg_logprob=-0.31, compression_ratio=1.07,
        words=[
            Word(start=1.0, end=1.4, word=" Zdravo", probability=0.9),
            Word(start=1.5, end=2.5, word=" svima.", probability=0.8),
        ],
    )

    assert worker._cue(whole) == {
        "start": 1.0,
        "end": 2.5,
        "text": "Zdravo svima.",
        "words": [
            {"start": 1.0, "end": 1.4, "word": " Zdravo"},
            {"start": 1.5, "end": 2.5, "word": " svima."},
        ],
        "no_speech_prob": 0.12,
        "avg_logprob": -0.31,
        "compression_ratio": 1.07,
    }


def test_prompt_follows_the_decided_language_not_the_metadata(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)
    whisper.segments = [whisper.segment(0.0, 2.0, " Dobar dan.")]

    result = worker.transcribe(
        MINUTE, language="auto", hint="en", prompt_names=["Juske", "Беочин"]
    )

    assert result["language"] == "sr"
    assert result["prompt"] == f"{PHRASE} Pominju se: Juske, Beočin."
    assert whisper.kwargs["initial_prompt"] == result["prompt"]


def test_english_speech_is_decoded_without_any_prompt(whisper, monkeypatch):
    heard(monkeypatch, ENGLISH_SPEECH)
    whisper.segments = [whisper.segment(0.0, 2.0, " Hello there.")]

    result = worker.transcribe(MINUTE, language="auto", prompt_names=["Copyright", "Subscribe"])

    assert result["language"] == "en"
    assert result["prompt"] is None
    assert whisper.kwargs["initial_prompt"] is None


def test_a_pinned_croatian_keeps_its_token_and_gets_the_serbian_phrase(whisper, monkeypatch):
    heard(monkeypatch, ENGLISH_SPEECH)

    result = worker.transcribe(MINUTE, language="hr", prompt_names=["Zagreb"])

    assert whisper.kwargs["language"] == "hr"
    assert result["prompt"] == f"{PHRASE} Pominju se: Zagreb."


def test_without_prompt_names_there_is_no_prompt(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)

    result = worker.transcribe(MINUTE, language="sr")

    assert result["prompt"] is None
    assert whisper.kwargs["initial_prompt"] is None


def test_worker_returns_every_segment_with_its_metrics(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)
    whisper.segments = [
        whisper.segment(0.0, 2.0, " Dobar dan.", no_speech_prob=0.05, avg_logprob=-0.2),
        whisper.segment(2.0, 30.0, " Hvala na gledanju.", no_speech_prob=0.92, avg_logprob=-1.4),
    ]

    result = worker.transcribe(MINUTE, language="sr", prompt_names=[])

    assert [(c["text"], c["no_speech_prob"], c["avg_logprob"]) for c in result["cues"]] == [
        ("Dobar dan.", 0.05, -0.2),
        ("Hvala na gledanju.", 0.92, -1.4),
    ]
    assert result["prompt"] == PHRASE


def test_guards_that_batched_mode_ignores_are_not_passed(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)

    worker.transcribe(MINUTE, language="sr", prompt_names=[])

    assert DEAD_GUARDS.isdisjoint(whisper.kwargs)


def test_worker_takes_names_as_a_json_list_and_no_ready_prompt():
    parser = worker.build_parser()

    assert parser.parse_args(["--prompt-names", '["Мамини", "Beograd"]']).prompt_names == [
        "Мамини", "Beograd",
    ]
    assert parser.parse_args([]).prompt_names is None

    refused = (
        ["--initial-prompt", PHRASE],
        ["--prompt-names", '"Beograd"'],
        ["--prompt-names", "Beograd"],
    )

    for argv in refused:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_silence_repetition_and_echo_are_dropped_with_a_reason():
    cues, dropped = jobs.asr_cues(MIXED, "sr", NAMED)

    assert [c.text for c in cues] == ["Dobar dan.", "Kako ste?"]
    assert sorted(dropped) == sorted([
        ("тишина", "Hvala na gledanju."),
        ("повтор", LOOP),
        ("эхо затравки", "Pominju se na srpskom jeziku."),
    ])


def test_echo_is_judged_after_cyrillic_becomes_latin():
    cues, dropped = jobs.asr_cues([segment(0.0, 3.0, "Помињу се на српском језику.")], "sr", NAMED)

    assert cues == []
    assert dropped == [("эхо затравки", "Pominju se na srpskom jeziku.")]


def test_without_a_prompt_the_phrase_is_just_speech():
    cues, dropped = jobs.asr_cues([segment(0.0, 4.0, PHRASE)], "en", None)

    assert [c.text for c in cues] == [PHRASE]
    assert dropped == []


def test_a_loop_takes_only_its_own_sentence_not_the_dialogue_before_it():
    # Кусок целиком сжимается в 3.45 раза, а каждое настоящее предложение в нём — не
    # больше чем в 0.89: выброс куска унёс бы диалог вместе с петлёй.
    cues, dropped = jobs.asr_cues(
        [segment(201.8, 229.6, f"{DIALOGUE} {TAK}", ratio=3.45)], "sr", NAMED
    )

    assert [c.text for c in cues] == [
        "Pa nije to dobro.", "Kako nije?", "Šta nije dobro?",
        "Pa očajno je, pogledaj ti koliko je ovo krupno.", "Šta radiš sa ovim?", "Da ga jedem.",
    ]
    assert dropped == [("повтор", TAK)]


def test_a_two_sentence_loop_keeps_its_first_turn():
    looped = "Dobro jutro. Kako si? " + "Idemo dalje. Vidimo se. " * 6

    cues, dropped = jobs.asr_cues([segment(0.0, 30.0, looped, ratio=3.0)], "sr", NAMED)

    # Каждое предложение петли сжимается плохо, ловит её только повтор уже сказанного.
    assert [c.text for c in cues] == ["Dobro jutro.", "Kako si?", "Idemo dalje.", "Vidimo se."]
    assert dropped == [("повтор", "Idemo dalje."), ("повтор", "Vidimo se.")] * 5


def test_repeats_in_an_unmarked_segment_are_speech():
    cues, dropped = jobs.asr_cues([segment(0.0, 4.0, "Da. Ne. Da. Ne znam. Da.")], "sr", NAMED)

    assert " ".join(c.text for c in cues) == "Da. Ne. Da. Ne znam. Da."
    assert dropped == []


def test_dropped_lines_never_become_job_cues_and_each_is_logged(manager, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    job = manager.get_or_create(VIDEO, "ru", True)
    verdict = LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.97,
        reason="detector",
    )
    result = {
        "language": "sr",
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.97, "votes": 8, "probed": 8, "windows": [["sr", 0.97, 25.0]] * 8,
        },
        "duration": 14.0,
        "model": "large-v3",
        "prompt": NAMED,
        "cues": MIXED,
    }

    manager.runner._accept_asr(job, result)

    assert [(c.index, c.src) for c in job.cues] == [(0, "Dobar dan."), (1, "Kako ste?")]

    logged = [r.getMessage() for r in caplog.records if r.name == "helper.jobs"]

    assert f"затравка {VIDEO}: {NAMED}" in logged
    assert f"выброшено {VIDEO} (тишина): Hvala na gledanju." in logged
    assert f"выброшено {VIDEO} (повтор): {LOOP}" in logged
    assert f"выброшено {VIDEO} (эхо затравки): Pominju se na srpskom jeziku." in logged


async def test_daemon_sends_title_and_tag_names_not_a_ready_prompt(pipeline, manager):
    pipeline.info = replace(
        pipeline.info,
        title="Мамини синови - Епизода 5",
        tags=["Beograd"],
        description="Copyright Label Instagram",
    )
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    argv = pipeline.workers[-1].args
    parsed = worker.build_parser().parse_args(argv[3:])

    assert job.stage is Stage.DONE
    assert "--initial-prompt" not in argv
    assert argv[argv.index("--prompt-names") + 1] == '["Мамини", "Епизода", "Beograd"]'
    assert parsed.prompt_names == ["Мамини", "Епизода", "Beograd"]


async def test_an_empty_name_list_means_no_prompt_and_the_phrase_is_speech(pipeline, manager, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    # Ни заголовок, ни теги имён не дают: список пуст, воркер пришлёт prompt=None,
    # и фраза затравки после этого — обычная речь, а не эхо.
    pipeline.info = replace(pipeline.info, title="copyright 2026", tags=["subscribe", "instagram"])
    verdict = LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.97,
        reason="detector",
    )
    pipeline.asr_result = {
        "language": "sr",
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.97, "votes": 8, "probed": 8, "windows": [["sr", 0.97, 25.0]] * 8,
        },
        "duration": 2.0,
        "model": "large-v3",
        "prompt": None,
        "cues": [segment(0.0, 4.0, PHRASE)],
    }
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    argv = pipeline.workers[-1].args

    assert job.stage is Stage.DONE
    assert argv[argv.index("--prompt-names") + 1] == "[]"
    assert [c.src for c in job.cues] == [PHRASE]
    logged = [r.getMessage() for r in caplog.records if r.name == "helper.jobs"]
    assert f"затравка {VIDEO}: нет" in logged
