"""Normalization of the writing system of Serbian text.

Serbian is digraphic: Cyrillic and Latin are equal. Whisper inherits this and
outputs now one, now the other — sometimes within a single transcript. Measurement
(arXiv 2601.02906): for large-v3 the share of correct Cyrillic is 0.18 versus
0.63–0.93 for Latin, and OpenAI writes directly that predictions "often fluctuate
between the two scripts".

Mixed script within a single batch is the real problem, not a capability of the
models. We reduce to one BEFORE translation.
"""

from __future__ import annotations

import re

import cyrtranslit

from helper.language import is_bcms

CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# Letters that exist in Serbian Cyrillic and are absent from Russian. A direct sign
# that we're dealing with Serbian, not Russian — also useful as a leak detector.
SERBIAN_ONLY = re.compile(r"[ђЂјЈљЉњЊћЋџЏ]")


def to_latin(text: str) -> str:
    """Reduce to Latin. On already Latin text this is a no-op.

    The direction was not chosen by taste. Cyrillic → Latin is an injective 1:1
    mapping, because љ, њ and џ are single glyphs. The reverse direction is
    ambiguous: the digraphs lj/nj/dž sometimes must remain two letters across a
    morpheme boundary (nadživeti → надживети, not наџивети), and rule-based
    transliterators, including cyrtranslit, get that wrong.

    A side benefit: BCMS Latin dominates web corpora (hr, bs and sr-Latn share it),
    whereas Serbian Cyrillic is sparse and visually coincides with Russian — a model
    with a Russian bias risks "reading" it as broken Russian and copying it instead
    of translating. Latin makes the source unambiguously non-Russian.
    """
    return cyrtranslit.to_latin(text, "sr")


def cyrillic_ratio(text: str) -> float:
    """Share of Cyrillic among letters. After normalization it should be near zero.

    This is diagnostics, not cosmetics: a nonzero value at the ASR output means the
    model fluctuated between scripts, and without normalization any WER measurement
    and any comparison with the glossary would be measuring the alphabet choice, not
    the content.
    """
    letters = [c for c in text if c.isalpha()]

    if not letters:
        return 0.0

    return sum(bool(CYRILLIC.match(c)) for c in letters) / len(letters)


def normalize_cues(cues: list[dict], language: str = "sr") -> list[dict]:
    """Reduce both the text and the per-word list to Latin, preserving timings.

    Applied ONLY to languages of the Serbian continuum. The transliteration rules
    here are Serbian, and on foreign Cyrillic they produce nonsense: a Ukrainian or
    Russian utterance turns into its own text in Latin — exactly the symptom this
    check was introduced for. For other languages we return the cues untouched.

    The language is checked by family (is_bcms), not by string: the author's track
    label may be sr-Cyrl or SR, and an exact comparison let such Cyrillic through
    to the translator.

    Words must be normalized together with the text. Segmentation matches sentences
    to words by character count, and the same Serbian letter takes a different number
    of them in the two scripts: Cyrillic "њ" is one character, Latin "nj" is two.
    Leaving the words in the original alphabet, we would be comparing a Latin count
    with a Cyrillic one, and sentence boundaries would drift the more digraphs the
    text contains.
    """
    if not is_bcms(language):
        return cues

    return [
        {
            **cue,
            "text": to_latin(cue["text"]),
            "words": [{**w, "word": to_latin(w["word"])} for w in (cue.get("words") or [])],
        }
        for cue in cues
    ]
