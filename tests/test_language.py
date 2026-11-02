"""helper/language.py: коды языков, правило определителя и решение о языке."""

import ast
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
