"""Recognition seed and screening of its garbage — shared by the worker and the daemon.

The seed is needed for exactly one thing — to pin Latin script in the Serbian family
(`helper/script.py`), so the phrase exists only for `sr`. Where there's no phrase,
there's no seed either: names don't go either.

faster-whisper 1.2.1's batched mode feeds the same seed to every chunk up to 30
seconds (`generate_segment_batched` copies it to every chunk of the batch), and on
chunks without speech it surfaces as an echo. From the seed it takes the last 223
tokens (`get_prompt`: `max_length // 2 - 1`): an overflow would eat the phrase, not
the names.

The batched mode accepts and doesn't apply the silence and repetition thresholds
("Unused Arguments" in the docstring of `BatchedInferencePipeline.transcribe`), so
screening is on the daemon side: a segment is judged by the metrics it carries anyway
(`segment_verdict`), and the segment's sentences with a "repeat" — by their own
`compression_ratio` and by repetition of what was already said (`helper.jobs.asr_cues`).
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections.abc import Mapping, Sequence

from helper.language import family
from helper.script import CYRILLIC, to_latin

# Thresholds are the same ones Whisper applies in sequential mode, and the batched mode
# only stores them in options. Silence discards the segment whole, a repeat only marks it.
NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4

# The worker won't take longer anyway: an overflow of the seed would eat the phrase.
MAX_NAMES = 12

PHRASES = {"sr": "Ovo je transkript video snimka na srpskom jeziku."}
NAMES_LEAD = "Pominju se:"

# Common words of the phrase and the lead-in: by themselves they don't make an echo.
ECHO_COMMON = frozenset({"ovo", "je", "video", "na", "se", "srpskom", "jeziku"})


def harvest_names(title: str, tags: Sequence[str]) -> list[str]:
    """Names from the title and tags in their own spelling, without transliteration.

    The echo in the field came from the template text of descriptions (Label, Copyright,
    Patreon, Instagram, sponsors), so the description isn't even in the parameters. A word
    in caps longer than four letters is an emphasis in the title (UDARIO, OFARBANOG,
    NAJGORIH), and short BMW and NATO are names.
    """
    names: dict[str, None] = {}

    for text in (title, *tags):
        for word in re.findall(r"\b\w{3,}", text):
            first = word[0]

            if not (first.isalpha() and first.isupper()):
                continue

            if word.isupper() and len(word) > 4:
                continue

            names.setdefault(word, None)

            if len(names) >= MAX_NAMES:
                return list(names)

    return list(names)


def initial_prompt(language: str | None, names: Sequence[str]) -> str | None:
    """The seed for a decided language; None — a language without a phrase, then names don't go either.

    Names are transliterated by Serbian rules and dropped if Cyrillic remains after them
    ("Для" → "Dlя"): in the seed they're just garbage. The BCMS test here is structural —
    the phrase exists only for the `sr` family.
    """
    phrase = PHRASES.get(family(language))

    if phrase is None:
        return None

    kept: dict[str, None] = {}

    for name in names:
        latin = to_latin(name)

        if CYRILLIC.search(latin):
            continue

        kept.setdefault(latin, None)

        if len(kept) >= MAX_NAMES:
            break

    if not kept:
        return phrase

    return f"{phrase} {NAMES_LEAD} {', '.join(kept)}."


def _words(text: str) -> set[str]:
    """Words of a line without case, diacritics and punctuation: "Jürgen" and "jurgen" are one."""
    # đ has no decomposition in NFKD, so the digraph is substituted before it.
    folded = text.casefold().replace("đ", "dj")
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(char)
    )

    return set(re.findall(r"\w+", folded))


def is_echo(sentence: str, prompt: str | None) -> bool:
    """The sentence consists only of the seed's words, and it's its tail, not everyday speech.

    A deviation from the letter of the intent: there's no literal match with the seed in
    any of the 66,620 cached utterances, the echo is fragments and permutations of its
    tail ("Pominju se na srpskom jeziku.") and lists of names. The phrase's common words
    don't make an echo ("Ovo je video.", "Na srpskom jeziku."), neither does a single word
    ("Andrija, Andrija."), nor one name from the title in an everyday phrase: the title
    "ŠTA SE KRILO U TITOVOM PLAVOM VOZU?" yields the name ŠTA, and without this condition
    "Šta je ovo?" would go off as an echo.
    """
    if prompt is None:
        return False

    phrase, _, names = prompt.partition(NAMES_LEAD)
    words = _words(sentence)
    common = ECHO_COMMON

    if len(words) < 2 or not words <= _words(prompt) or not words - common:
        return False

    frame = _words(phrase) | _words(NAMES_LEAD)

    return bool(words & (frame - common)) or len((words & _words(names)) - common) >= 2


def compression_ratio(text: str) -> float:
    """Compression degree by faster-whisper's formula (`get_compression_ratio`).

    Needed by the daemon for sentences inside a segment: the segment metric is computed
    by the library itself, but sentences — only by us.
    """
    data = text.encode("utf-8")

    return len(data) / len(zlib.compress(data))


def segment_verdict(segment: Mapping[str, object]) -> str | None:
    """ "тишина" (silence), "повтор" (repeat) or None by the recognition segment's metrics.

    "тишина" — the rule by which Whisper itself skips a window in sequential mode, and
    such a segment is discarded whole. "повтор" — a replacement for the temperature
    fallback, which doesn't exist in batched mode (Whisper would re-decode the chunk),
    but a batched-mode segment is a whole piece of speech up to 30 seconds, and the loop
    often starts in the middle of it, after real dialogue. Therefore "повтор" only marks
    the segment: inside it only the looping sentences are discarded (`asr_cues`), while
    the speech before the loop remains. Every discard goes to the log.
    """
    no_speech = segment.get("no_speech_prob")
    logprob = segment.get("avg_logprob")
    ratio = segment.get("compression_ratio")

    if (
        no_speech is not None
        and logprob is not None
        and no_speech > NO_SPEECH_THRESHOLD
        and logprob < LOG_PROB_THRESHOLD
    ):
        return "тишина"

    if ratio is not None and ratio > COMPRESSION_RATIO_THRESHOLD:
        return "повтор"

    return None
