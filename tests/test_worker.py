"""Воркер распознавания без карты: окна определителя, решение о языке, разбор аргументов."""

import json
import sys

import numpy as np
import pytest

from helper import language
from helper.asr import worker

SR = language.SAMPLE_RATE
WINDOW = language.WINDOW_SAMPLES


class Model:
    """WhisperModel для _detect: отвечает распределениями по очереди и помнит, что слушал."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        # Длины отрывков, отданных detect_language, по порядку.
        self.heard: list[int] = []

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        assert vad_filter is True
        self.heard.append(audio.size)
        answer = self.answers.pop(0)

        if isinstance(answer, Exception):
            raise answer

        return answer[0][0], answer[0][1], answer


@pytest.fixture
def vad(monkeypatch):
    """VAD без silero: речь — это ненулевые отсчёты отрывка."""
    import faster_whisper.vad

    def speech(audio, vad_options=None, sampling_rate=16000, **kwargs):
        voiced = int(np.count_nonzero(audio))

        return [{"start": 0, "end": voiced}] if voiced else []

    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", speech)


def recording(seconds: float, speech: list[float]) -> np.ndarray:
    """Тишина длиной seconds; в начале каждого окна — столько секунд «речи», сколько в speech."""
    audio = np.zeros(int(seconds * SR), dtype="<f4")

    for offset, voiced in zip(language.probe_offsets(audio.size), speech):
        audio[offset : offset + int(voiced * SR)] = 0.1

    return audio


def test_detect_hears_only_windows_with_speech(vad):
    model = Model(*[[("sr", 0.9), ("en", 0.1)]] * 6)

    detection = worker._detect(model, recording(20 * 60, [30, 30, 30, 2, 0, 30, 30, 30]))

    assert model.heard == [WINDOW] * 6
    assert (detection.votes, detection.probed) == (6, 8)
    assert detection.language == "sr"
    assert [w[2] for w in detection.windows] == [30.0, 30.0, 30.0, 2.0, 0.0, 30.0, 30.0, 30.0]


def test_window_where_the_detector_fails_does_not_vote(vad):
    # Без доли сербской семьи: исход не зависит от BCMS_FLOOR и его калибровки (задача 12).
    model = Model(RuntimeError("CUDA"), *[[("en", 0.95), ("de", 0.05)]] * 7)

    detection = worker._detect(model, recording(20 * 60, [30] * 8))

    assert (detection.votes, detection.probed) == (7, 8)
    assert detection.windows[0] == (None, 0.0, 30.0)
    assert detection.language == "en"


def test_short_audio_is_one_window_on_the_same_path(vad):
    audio = np.full(20 * SR, 0.1, dtype="<f4")
    model = Model([("hr", 0.45), ("sr", 0.40), ("en", 0.15)])

    detection = worker._detect(model, audio)

    assert model.heard == [audio.size]
    assert detection.probed == 1
    # Семья, а не верхняя метка: сегодня этот путь отдавал hr 0.45.
    assert detection.language == "sr"
    assert detection.confidence == pytest.approx(0.85)


def test_audio_between_30_and_37_5_seconds_gives_whole_windows(vad):
    model = Model(*[[("sr", 0.9), ("en", 0.1)]] * 4)

    detection = worker._detect(model, np.full(int(31.25 * SR), 0.1, dtype="<f4"))

    assert model.heard == [WINDOW] * 4
    assert detection.probed == 4


def test_no_votes_means_one_pass_over_the_whole_file(vad):
    audio = recording(20 * 60, [2] * 8)
    # Десять секунд речи в заставке, вне окон: окна начинаются со 120-й секунды.
    audio[: 10 * SR] = 0.1
    model = Model([("sr", 0.8), ("en", 0.2)])

    detection = worker._detect(model, audio)

    assert model.heard == [audio.size]
    assert (detection.votes, detection.probed) == (1, 9)
    assert detection.windows[-1] == ("sr", pytest.approx(0.8), 26.0)
    assert detection.language == "sr"


def test_silent_file_gets_no_vote_and_no_model_call(vad):
    model = Model()

    detection = worker._detect(model, np.zeros(20 * 60 * SR, dtype="<f4"))

    assert model.heard == []
    assert (detection.language, detection.votes, detection.probed) == (None, 0, 9)
    assert detection.windows[-1] == (None, 0.0, 0.0)


def test_short_silent_audio_makes_no_extra_pass(vad):
    model = Model()

    detection = worker._detect(model, np.zeros(10 * SR, dtype="<f4"))

    assert model.heard == []
    assert (detection.votes, detection.probed) == (0, 1)


SERBIAN = language.Detection(
    language="sr", confidence=0.97, bcms_share=0.97, floor=False, votes=8, probed=8,
    windows=(("sr", 0.97, 25.0),) * 8,
)
ENGLISH = language.Detection(
    language="en", confidence=0.95, bcms_share=0.05, floor=False, votes=8, probed=8,
    windows=(("en", 0.95, 25.0),) * 8,
)
SILENCE = language.Detection(
    language=None, confidence=None, bcms_share=0.0, floor=False, votes=0, probed=9,
    windows=((None, 0.0, 0.0),) * 9,
)
AUDIO = np.zeros(5 * SR, dtype="<f4")


def detector_says(monkeypatch, detection) -> list[int]:
    """_detect отвечает готовым Detection; список — длины записей, на которых его звали."""
    heard: list[int] = []

    def detect(model, audio):
        heard.append(audio.size)

        return detection

    monkeypatch.setattr(worker, "_detect", detect)

    return heard


def test_detector_runs_even_with_a_pin(whisper, monkeypatch):
    heard = detector_says(monkeypatch, ENGLISH)

    result = worker.transcribe(AUDIO, language="sr")

    assert heard == [AUDIO.size]
    assert whisper.kwargs["language"] == "sr"
    assert result["language"] == "sr"
    assert result["verdict"]["reason"] == "pin"
    assert result["verdict"]["detected"] == "en"
    assert result["verdict"]["note"] == "в настройках sr, по речи en"


def test_speech_outweighs_the_youtube_label(whisper, monkeypatch):
    detector_says(monkeypatch, SERBIAN)

    result = worker.transcribe(AUDIO, language="auto", hint="en-US")

    assert whisper.kwargs["language"] == "sr"
    assert (result["verdict"]["declared"], result["verdict"]["declaredBy"]) == ("en", "metadata")
    assert result["verdict"]["mismatch"] is True


@pytest.mark.parametrize(
    ("hint", "decoded", "reason"),
    [(None, "sr", "fallback"), ("en-US", "en", "hint"), ("xx-YY", "sr", "fallback")],
)
def test_without_speech_whisper_never_gets_an_empty_language(whisper, monkeypatch, hint, decoded, reason):
    detector_says(monkeypatch, SILENCE)

    result = worker.transcribe(AUDIO, language="auto", hint=hint)

    assert whisper.kwargs["language"] == decoded
    assert result["language"] == decoded
    assert result["verdict"]["reason"] == reason
    assert result["verdict"]["detected"] is None


def test_result_carries_the_verdict_and_the_windows(whisper, monkeypatch):
    detector_says(monkeypatch, SERBIAN)

    result = worker.transcribe(AUDIO, language="auto")

    assert set(result) == {"language", "verdict", "detection", "duration", "model", "cues"}
    assert result["detection"] == {
        "bcmsShare": 0.97,
        "votes": 8,
        "probed": 8,
        "windows": [["sr", 0.97, 25.0]] * 8,
    }
    json.dumps(result)


def test_unknown_pin_fails_before_the_model_loads(monkeypatch):
    import faster_whisper

    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda *a, **k: pytest.fail("модель грузить рано"))

    with pytest.raises(language.UnknownLanguage):
        worker.transcribe(AUDIO, language="serbian")


def test_parser_keeps_the_cli_contract():
    args = worker.build_parser().parse_args([])

    # helper/measure.py не передаёт --language и рассчитывает на sr.
    assert args.language == "sr"
    assert args.hint is None
    assert (args.model, args.compute_type, args.batch_size) == (worker.DEFAULT_MODEL, "float16", 16)
    assert (args.no_vad, args.initial_prompt, args.hotwords, args.pcm) == (False, None, None, None)


def test_main_refuses_an_unknown_pin_before_reading_audio(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["helper.asr.worker", "--language", "serbian"])
    monkeypatch.setattr(worker, "transcribe", lambda *a, **k: pytest.fail("до распознавания доходить нельзя"))

    assert worker.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "неизвестный язык «serbian»: [asr] language в config.toml",
        "permanent": True,
    }
