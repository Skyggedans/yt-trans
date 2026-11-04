"""Сборка реплик из сегментов распознавания: helper.segment."""

from helper.segment import build_cues

FILM_ECHO = (
    "Pominju se, Terminus, Full, Movie, Johnny, Hallyday, Karen, Allen, Jürgen, "
    "Prochnow, However, Genres, Action."
)


def word(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "word": text}


GREETING = {
    "start": 0.0,
    "end": 5.0,
    "text": "Dobar dan. Pominju se na srpskom jeziku. Kako ste?",
    "words": [
        word(0.0, 0.4, " Dobar"), word(0.5, 0.9, " dan."),
        word(1.0, 1.4, " Pominju"), word(1.5, 1.6, " se"), word(1.7, 1.8, " na"),
        word(1.9, 2.2, " srpskom"), word(2.3, 2.6, " jeziku."),
        word(4.0, 4.4, " Kako"), word(4.5, 5.0, " ste?"),
    ],
}


def timeline(cues) -> list[tuple[str, float, float]]:
    return [(c.text, round(c.start, 2), round(c.end, 2)) for c in cues]


def test_a_dropped_sentence_leaves_no_cue_and_its_neighbours_keep_their_words():
    cues = build_cues([GREETING], drop=lambda sentence, segment: sentence.startswith("Pominju"))

    assert timeline(cues) == [("Dobar dan.", 0.0, 0.9), ("Kako ste?", 4.0, 5.0)]


def test_a_long_sentence_is_dropped_whole_not_piece_by_piece():
    segment = {"start": 0.0, "end": 6.0, "text": FILM_ECHO, "words": []}

    assert len(build_cues([segment])) == 2
    assert build_cues([segment], drop=lambda sentence, segment: sentence == FILM_ECHO) == []


def test_a_predicate_that_drops_nothing_changes_nothing():
    assert build_cues([GREETING], drop=lambda sentence, segment: False) == build_cues([GREETING])
