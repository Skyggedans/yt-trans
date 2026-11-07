"""Recognition priming and screening of its garbage: the pure functions of helper.asr.prompt."""

import pytest

from helper.asr.prompt import (
    MAX_NAMES,
    compression_ratio,
    harvest_names,
    initial_prompt,
    is_echo,
    segment_verdict,
)

PHRASE = "Ovo je transkript video snimka na srpskom jeziku."


def test_names_come_from_title_and_tags_in_their_own_script():
    names = harvest_names(
        "Мамини синови - Епизода 5 (ОФИЦИЈЕЛНИ спот)", ["Andrija i Anđelka", "Beograd"]
    )

    assert names == ["Мамини", "Епизода", "Andrija", "Anđelka", "Beograd"]


def test_shouted_title_words_are_not_names_but_short_acronyms_are():
    assert harvest_names("UDARIO SAM TEK OFARBANOG BMW", ["NATO", "MOTOR"]) == [
        "SAM", "TEK", "BMW", "NATO",
    ]


def test_names_are_deduplicated_in_order_and_capped():
    title = "Ana Bora Cana Dara Ema Fila Goca Hana Ivana Jana Kaja Lana Maja Nada"

    assert harvest_names("Beograd i Beograd", ["Beograd", "Novi Sad"]) == [
        "Beograd", "Novi", "Sad",
    ]
    assert harvest_names(title, []) == title.split()[:MAX_NAMES]


def test_serbian_gets_the_latin_phrase_and_transliterated_names():
    assert initial_prompt("sr", ["Мамини", "Beograd"]) == f"{PHRASE} Pominju se: Mamini, Beograd."


@pytest.mark.parametrize("language", ["hr", "bs", "sh", "me", "cnr", "sr-Latn", "SR"])
def test_every_label_of_the_serbian_family_gets_the_serbian_phrase(language):
    assert initial_prompt(language, ["Zagreb"]) == f"{PHRASE} Pominju se: Zagreb."


@pytest.mark.parametrize("language", ["en", "ru", "uk", "mk"])
def test_a_language_without_a_phrase_gets_no_prompt_not_even_names(language):
    assert initial_prompt(language, ["Clarkson"]) is None


@pytest.mark.parametrize("language", [None, "", "auto", "xx"])
def test_an_undecided_or_unknown_language_gets_no_prompt(language):
    assert initial_prompt(language, ["Clarkson"]) is None


def test_without_names_the_phrase_stands_alone():
    assert initial_prompt("sr", []) == PHRASE


def test_names_still_cyrillic_after_serbian_rules_are_dropped():
    assert initial_prompt("sr", ["Для", "Patreon"]) == f"{PHRASE} Pominju se: Patreon."
    assert initial_prompt("sr", ["Для"]) == PHRASE


def test_a_name_written_in_both_scripts_appears_once():
    assert initial_prompt("sr", ["Београд", "Beograd"]) == f"{PHRASE} Pominju se: Beograd."


def test_the_prompt_never_carries_more_than_twelve_names():
    names = [f"Ime{letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]

    assert initial_prompt("sr", names) == f"{PHRASE} Pominju se: {', '.join(names[:MAX_NAMES])}."


NAMED = initial_prompt("sr", ["Mamini", "Epizoda"])
FILM = initial_prompt("sr", [
    "Terminus", "Full", "Movie", "Johnny", "Hallyday", "Karen",
    "Allen", "Jürgen", "Prochnow", "However", "Genres", "Action",
])
SERIES = initial_prompt("sr", ["AndrijaiAnđelka", "Label", "Zabranjeno", "Andrija"])


@pytest.mark.parametrize("prompt, sentence", [
    (PHRASE, "Ovo je transkript video snimka na srpskom jeziku."),
    (NAMED, "Pominju se na srpskom jeziku."),
    (NAMED, "Pominju se."),
    (NAMED, "Pominju se na srpskom"),
    (NAMED, "Mamini, Epizoda."),
    (FILM, "Pominju se, Terminus, Full, Movie, Johnny, Hallyday, Karen, Allen, Jürgen, "
           "Prochnow, However, Genres, Action."),
    (FILM, "Karen, Allen, Jürgen, Prochnow, However, Genres, Action."),
    (SERIES, "AndrijaiAndjelka, Label, Zabranjeno,"),
])
def test_field_echoes_of_the_prompt_are_recognised(prompt, sentence):
    assert is_echo(sentence, prompt)


@pytest.mark.parametrize("prompt, sentence", [
    (NAMED, "Međutim, zadacima pominju se i ubistva."),
    (NAMED, "Evo, gospodine, uputstvo na srpskom jeziku."),
    (NAMED, "Ovo je video."),
    (NAMED, "Na srpskom jeziku."),
    (NAMED, "Pominju se, da je Rusija mogla organizirati diversiju"),
    (NAMED, "Pominju se 30 sekundi."),
    (PHRASE, "Pominju se."),
    (SERIES, "Ćao, Andrija."),
    (SERIES, "Andrija, Andrija."),
    (None, "Ovo je transkript video snimka na srpskom jeziku."),
    # One name from the title in an everyday phrase is speech: in the cache "Šta je ovo?" appears in 17
    # of 160 videos, and the title "ŠTA SE KRILO U TITOVOM PLAVOM VOZU?" yields the name ŠTA.
    (initial_prompt("sr", ["ŠTA", "VOZU", "Prava", "Istina"]), "Šta je ovo?"),
    (initial_prompt("sr", ["Beograd"]), "Ovo je Beograd."),
    # An everyday word of the phrase that became a name does not yield a second name: the title
    # "OVO JE NAJGORI AUTO NA SVETU" yields the names OVO and AUTO.
    (initial_prompt("sr", ["OVO", "AUTO"]), "Ovo je auto."),
])
def test_speech_that_shares_words_with_the_prompt_is_kept(prompt, sentence):
    assert not is_echo(sentence, prompt)


@pytest.mark.parametrize("metrics, verdict", [
    ({"no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 1.0}, "тишина"),
    ({"no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 3.0}, "тишина"),
    ({"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 2.5}, "повтор"),
    ({"no_speech_prob": 0.9, "avg_logprob": -0.3, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.1, "avg_logprob": -1.5, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.6, "avg_logprob": -1.5, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.9, "avg_logprob": -1.0, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 2.4}, None),
    ({"no_speech_prob": 0.9, "compression_ratio": 1.0}, None),
    ({}, None),
])
def test_segment_verdict_drops_only_silence_and_repetition(metrics, verdict):
    assert segment_verdict({"text": "Dobar dan.", **metrics}) == verdict


def test_compression_ratio_is_the_one_faster_whisper_uses():
    import helper.env  # noqa: F401 — HF_HOME to a temporary cache before faster_whisper, as in the worker

    from faster_whisper.transcribe import get_compression_ratio

    loop = "Ideš, slušaj me, prvo uzduš, ovako, pa poprično, pa što sitnije, " + "tak, " * 70 + "tak"

    for text in (PHRASE, "Dobar dan.", ", ".join(["ne"] * 12), loop, "Šta je ovo?"):
        assert compression_ratio(text) == get_compression_ratio(text)
