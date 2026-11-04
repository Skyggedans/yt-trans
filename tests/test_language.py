"""helper/language.py: коды языков, правило определителя и решение о языке."""

import ast
import json
import sys
from pathlib import Path

import pytest

from helper import language


def test_whisper_codes_are_the_installed_faster_whisper_list():
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    assert language.WHISPER_CODES == frozenset(_LANGUAGE_CODES)
    assert len(language.WHISPER_CODES) == 100


SUBSTITUTE_ROWS = [
    ("sh", "sr"),
    ("me", "sr"),
    ("cnr", "sr"),
    ("iw", "he"),
    ("jv", "jw"),
    ("fil", "tl"),
    ("nb", "no"),
    ("in", "id"),
]


def test_every_substitute_has_its_own_row():
    assert dict(SUBSTITUTE_ROWS) == language.SUBSTITUTES


@pytest.mark.parametrize(("raw", "code"), SUBSTITUTE_ROWS)
def test_substitute_lands_on_a_whisper_code(raw, code):
    assert raw not in language.WHISPER_CODES
    assert code in language.WHISPER_CODES
    assert language.normalize(raw) == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sr", "sr"),
        ("SR", "sr"),
        (" sr ", "sr"),
        ("sr-Latn", "sr"),
        ("sr-Cyrl", "sr"),
        ("sr_RS", "sr"),
        ("cnr-ME", "sr"),
        ("hr", "hr"),
        ("bs", "bs"),
        ("en-US", "en"),
        ("EN-us", "en"),
        ("en-orig", "en"),
        ("en-desc", "en"),
        ("zh-Hans", "zh"),
        ("yue", "yue"),
        ("en+en-US", "en"),
    ],
)
def test_normalize_keeps_the_primary_subtag(raw, code):
    assert language.normalize(raw) == code


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "auto", "AUTO", " Auto ", "und", "zxx", "mul", "mis"]
)
def test_normalize_reads_no_language_as_none(raw):
    assert language.normalize(raw) is None


@pytest.mark.parametrize("raw", ["serbian", "xx", "srb", "en+sr", "live_chat"])
def test_normalize_refuses_an_unknown_code(raw):
    with pytest.raises(language.UnknownLanguage) as caught:
        language.normalize(raw)

    assert caught.value.raw == raw
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("en-orig", "en"),
        ("SR", "sr"),
        ("sh", "sr"),
        ("serbian", None),
        ("live_chat", None),
        ("en+sr", None),
        ("und", None),
        (None, None),
    ],
)
def test_hint_never_raises(raw, code):
    assert language.hint(raw) == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sr", "sr"),
        ("hr", "sr"),
        ("bs", "sr"),
        ("sh", "sr"),
        ("cnr", "sr"),
        ("sr-Latn", "sr"),
        ("mk", "mk"),
        ("en-US", "en"),
        ("serbian", None),
        (None, None),
    ],
)
def test_family_folds_the_three_bcms_tokens(raw, code):
    assert language.family(raw) == code


@pytest.mark.parametrize("raw", ["sr", "hr", "bs", "sh", "me", "cnr", "sr-Latn", "sr-Cyrl", "SR"])
def test_bcms_spellings_are_bcms(raw):
    assert language.is_bcms(raw) is True


@pytest.mark.parametrize("raw", ["mk", "sl", "ru", "en", None, "", "live_chat", "serbian"])
def test_other_codes_are_not_bcms(raw):
    assert language.is_bcms(raw) is False


def test_bcms_is_three_whisper_tokens():
    assert language.BCMS == {"sr", "hr", "bs"}
    assert language.BCMS <= language.WHISPER_CODES


def test_refusal_names_the_value_and_where_to_fix_it():
    phrase = language.refusal("serbian")

    assert phrase == "неизвестный язык «serbian»: [asr] language в config.toml"
    assert len(phrase) <= 60


def test_language_module_imports_only_the_standard_library():
    tree = ast.parse(Path(language.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])

    assert roots <= sys.stdlib_module_names


# ── Определитель: окна, правило, решение ─────────────────────────────────────────


def test_detector_constants_are_pinned():
    # Смена любой из них меняет распознавание закэшированных роликов: правишь здесь —
    # поднимай helper.cache.PROMPT_VERSION.
    assert language.MIN_SPEECH_SECONDS == 3.0
    assert language.BCMS_FLOOR == 0.2
    assert language.SLAVIC_NEIGHBOURS == {"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"}
    assert language.SAMPLE_RATE == 16000
    assert language.WINDOW_SAMPLES == 30 * 16000


@pytest.mark.parametrize(
    ("seconds", "offsets"),
    [
        (5, [0]),
        (30, [0]),
        (31.25, [0, 4286, 15714, 20000]),
        (37.5, [60000]),
        (20 * 60, [1920000, 4045714, 6171429, 8297143, 10422857, 12548571, 14674286, 16800000]),
    ],
)
def test_probe_offsets(seconds, offsets):
    n_samples = int(seconds * language.SAMPLE_RATE)

    assert language.probe_offsets(n_samples) == offsets
    assert all(0 <= start <= max(n_samples - language.WINDOW_SAMPLES, 0) for start in offsets)


def window(*shares: tuple[str, float], speech: float = 25.0):
    return (speech, list(shares))


# 3XJLfTSZy6I: сербская речь без метаданных. en сверху в пяти окнах из восьми,
# в среднем en 0.55 против 0.44 у сербского семейства.
IZA_MRAKA = [window(("en", 0.80), ("sr", 0.10), ("hr", 0.06), ("bs", 0.03), ("de", 0.01))] * 5 + [
    window(("sr", 0.45), ("hr", 0.27), ("bs", 0.15), ("en", 0.10), ("de", 0.03)),
    window(("hr", 0.40), ("sr", 0.35), ("en", 0.15), ("bs", 0.10)),
    window(("sr", 0.50), ("bs", 0.20), ("hr", 0.15), ("en", 0.15)),
]
# Q2QcYOBk0Jo (Juske): метка YouTube en, речь сербская.
JUSKE = [window(("sr", 0.60), ("hr", 0.25), ("bs", 0.12), ("en", 0.03))] * 8
# 3UbtKc9MlXg: английский, сербского семейства ноль во всех восьми окнах.
ENGLISH = [window(("en", 0.95), ("de", 0.03), ("nl", 0.02))] * 8
# Русская речь, у которой сербского семейства 0.3: выше порога, но язык свой.
RUSSIAN = [window(("ru", 0.60), ("sr", 0.20), ("hr", 0.10), ("uk", 0.10))] * 8
# Клип почти без речи: ни одно окно не голосует.
SILENCE = [window(speech=0.0)] * 8


def test_floor_turns_the_english_bias_into_serbian():
    detection = language.summarize(IZA_MRAKA)

    assert [w[0] for w in detection.windows].count("en") == 5
    assert detection.language == "sr"
    assert detection.floor is True
    assert detection.bcms_share == pytest.approx(0.44)
    assert detection.confidence == pytest.approx(0.44)
    assert (detection.votes, detection.probed) == (8, 8)


def test_serbian_speech_under_an_english_label():
    detection = language.summarize(JUSKE)

    assert (detection.language, detection.floor) == ("sr", False)
    assert detection.confidence == pytest.approx(0.97)

    verdict = language.decide(None, "en", detection)

    assert verdict.effective == "sr"
    assert (verdict.declared, verdict.declared_by) == ("en", "metadata")
    assert verdict.reason == "detector"
    assert verdict.mismatch is True
    assert verdict.note() == "YouTube указал en, по речи sr"


def test_english_without_bcms_stays_english():
    detection = language.summarize(ENGLISH)

    assert (detection.language, detection.floor) == ("en", False)
    assert detection.bcms_share == 0.0
    assert detection.confidence == pytest.approx(0.95)


def test_slavic_neighbour_keeps_its_language_over_the_floor():
    detection = language.summarize(RUSSIAN)

    assert (detection.language, detection.floor) == ("ru", False)
    assert detection.bcms_share == pytest.approx(0.30)
    assert detection.confidence == pytest.approx(0.60)


@pytest.mark.parametrize(
    ("shares", "winner", "floor"),
    [
        ([("en", 0.80), ("sr", 0.20)], "sr", True),
        ([("en", 0.81), ("sr", 0.19)], "en", False),
        ([("mk", 0.50), ("sr", 0.30), ("bg", 0.20)], "mk", False),
    ],
)
def test_floor_boundary(shares, winner, floor):
    detection = language.summarize([window(*shares)])

    assert (detection.language, detection.floor) == (winner, floor)


def test_windows_without_speech_or_answer_do_not_vote():
    detection = language.summarize(
        [
            window(("en", 0.99), speech=2.9),
            window(speech=0.0),
            # Речь есть, но определитель на этом окне упал: ответа нет.
            window(speech=25.0),
        ]
    )

    assert detection == language.Detection(
        language=None,
        confidence=None,
        bcms_share=0.0,
        floor=False,
        votes=0,
        probed=3,
        windows=((None, 0.0, 2.9), (None, 0.0, 0.0), (None, 0.0, 25.0)),
    )


def test_three_seconds_of_speech_vote_and_a_window_reports_its_family():
    detection = language.summarize([window(("hr", 0.40), ("sr", 0.35), ("en", 0.25), speech=3.0)])

    assert detection.votes == 1
    assert detection.windows == (("sr", pytest.approx(0.75), 3.0),)


@pytest.mark.parametrize(
    ("pin", "hint", "windows", "effective", "reason", "declared_by"),
    [
        ("sr", None, ENGLISH, "sr", "pin", "config"),
        ("hr", None, JUSKE, "hr", "pin", "config"),
        ("en", "sr", SILENCE, "en", "pin", "config"),
        (None, "en", JUSKE, "sr", "detector", "metadata"),
        (None, None, IZA_MRAKA, "sr", "floor", None),
        (None, "en", SILENCE, "en", "hint", "metadata"),
        (None, None, SILENCE, "sr", "fallback", None),
    ],
)
def test_decide_precedence(pin, hint, windows, effective, reason, declared_by):
    detection = language.summarize(windows)
    verdict = language.decide(pin, hint, detection)

    assert (verdict.effective, verdict.reason, verdict.declared_by) == (effective, reason, declared_by)
    assert verdict.declared == (pin or hint)
    assert (verdict.detected, verdict.confidence) == (detection.language, detection.confidence)
    assert (verdict.source, verdict.track, verdict.verified) == ("asr", None, None)


def manual(effective: str, declared: str | None, declared_by: str | None) -> language.LanguageVerdict:
    return language.LanguageVerdict(
        effective=effective,
        declared=declared,
        declared_by=declared_by,
        detected=None,
        confidence=None,
        reason="track",
        source="manual",
        track="sr-Latn",
    )


@pytest.mark.parametrize(
    ("pin", "hint", "windows", "mismatch", "note"),
    [
        ("sr", None, ENGLISH, True, "в настройках sr, по речи en"),
        ("hr", None, JUSKE, False, None),
        (None, "en", JUSKE, True, "YouTube указал en, по речи sr"),
        (None, None, IZA_MRAKA, False, None),
        (None, "en", SILENCE, False, "речи почти нет, язык взят у YouTube: en"),
        (None, None, SILENCE, False, "речи почти нет, язык принят за sr"),
    ],
)
def test_mismatch_and_note_from_speech(pin, hint, windows, mismatch, note):
    verdict = language.decide(pin, hint, language.summarize(windows))

    assert verdict.mismatch is mismatch
    assert verdict.note() == note
    assert note is None or len(note) <= 60


@pytest.mark.parametrize(
    ("declared", "declared_by", "mismatch", "note"),
    [
        ("en", "metadata", True, "YouTube указал en, дорожка автора sr"),
        ("ru", "config", True, "в настройках ru, дорожка автора sr"),
        ("hr", "config", False, None),
        (None, None, False, None),
    ],
)
def test_mismatch_and_note_for_an_author_track(declared, declared_by, mismatch, note):
    verdict = manual("sr", declared, declared_by)

    assert verdict.mismatch is mismatch
    assert verdict.note() == note
    assert note is None or len(note) <= 60


def test_wire_carries_exactly_the_agreed_keys():
    verdict = language.decide(None, "en", language.summarize(JUSKE))

    assert verdict.wire() == {
        "effective": "sr",
        "declared": "en",
        "declaredBy": "metadata",
        "detected": "sr",
        "confidence": 0.97,
        "reason": "detector",
        "source": "asr",
        "track": None,
        "verified": None,
        "mismatch": True,
        "note": "YouTube указал en, по речи sr",
    }


def test_wire_rounds_confidence_to_two_places():
    verdict = language.LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.4361, reason="floor"
    )

    assert verdict.wire()["confidence"] == 0.44


def test_from_wire_round_trips():
    asr = language.LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected="sr", confidence=0.97, reason="detector"
    )
    track = manual("sr", None, None)

    assert language.LanguageVerdict.from_wire(asr.wire()) == asr
    assert language.LanguageVerdict.from_wire(track.wire()) == track


@pytest.mark.parametrize(
    "data", [None, "sr", [], {}, {"effective": ""}, {"effective": 5}, {"effective": "xx"}, {"effective": "SR"}]
)
def test_from_wire_refuses_what_is_not_a_verdict(data):
    assert language.LanguageVerdict.from_wire(data) is None


def test_from_wire_ignores_derived_and_broken_fields():
    verdict = language.LanguageVerdict.from_wire(
        {"effective": "en", "mismatch": True, "note": "чужое", "confidence": "high", "declaredBy": 7, "verified": "yes"}
    )

    assert verdict == language.LanguageVerdict(
        effective="en", declared=None, declared_by=None, detected=None, confidence=None, reason="detector"
    )
    assert verdict.mismatch is False


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_from_wire_drops_a_confidence_that_is_not_a_finite_share(confidence):
    # json.dumps пишет NaN и Infinity, json.loads их читает: правленная руками запись
    # кэша донесла бы их до JSONResponse, а тот на них падает (allow_nan=False).
    verdict = language.LanguageVerdict.from_wire({"effective": "sr", "confidence": confidence})

    assert verdict.confidence is None
    json.dumps(verdict.wire(), allow_nan=False)
