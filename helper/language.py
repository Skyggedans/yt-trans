"""Language codes, the detector rule and the language decision, shared by the daemon and the recognition worker.

Standard library only. The module is loaded by both the daemon and the worker, and
faster_whisper pulls in ctranslate2, av and tokenizers, which have no business in the
daemon process: the list of Whisper codes is therefore copied here rather than imported.
test_language_module_imports_only_the_standard_library watches that no third-party
imports appear here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Exactly one hundred codes of faster-whisper 1.2.1 (faster_whisper.tokenizer._LANGUAGE_CODES).
# A copy, not an import: the name is private, and an import would drag all of
# faster_whisper into the daemon. If it diverges from the installed version — the test will fail.
WHISPER_CODES: frozenset[str] = frozenset({
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs",
    "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr", "gl", "gu",
    "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "jw", "ka",
    "kk", "km", "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml",
    "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt",
    "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi", "yo",
    "yue", "zh",
})

# Spellings Whisper doesn't know but that mean its code. sh, me and cnr —
# Serbian under other names; the rest are old or alternative ISO codes
# used by YouTube and language tables.
SUBSTITUTES: dict[str, str] = {
    "sh": "sr", "me": "sr", "cnr": "sr",
    "iw": "he", "jv": "jw", "fil": "tl", "nb": "no", "in": "id",
}

# "No language": empty and auto in config.toml, und, zxx, mul and mis from ISO 639.
NO_LANGUAGE: frozenset[str] = frozenset({"", "auto", "und", "zxx", "mul", "mis"})

# Three Whisper tokens for one language. bs, hr and sr got into its training from VoxLingua107
# by oversight, and the choice between them is arbitrary; family() folds them to sr.
BCMS: frozenset[str] = frozenset({"sr", "hr", "bs"})


class UnknownLanguage(ValueError):
    """A code that Whisper doesn't know and that doesn't reduce to any of its codes."""

    def __init__(self, raw: str) -> None:
        super().__init__(raw)
        self.raw = raw


def normalize(raw: str | None) -> str | None:
    """The Whisper code for a language spelling; None — "no language" (empty, auto, und and the like).

    Region, script and track annotations are discarded: "SR", "sr_RS" and "sr-Latn" are
    sr, "en-orig" — en. yt-dlp glues track languages with "+": such a value is valid
    only if all parts reduce to one code. hr and bs remain themselves — they're real
    Whisper codes, and only family() can fold them to sr.

    An unfamiliar spelling — UnknownLanguage with the ORIGINAL string: the refusal must
    name what the person wrote, not what we turned it into.
    """
    if raw is None:
        return None

    code = raw.strip().lower().replace("_", "-")

    if code in NO_LANGUAGE:
        return None

    codes: set[str] = set()

    for part in code.split("+"):
        primary = part.split("-")[0]
        primary = SUBSTITUTES.get(primary, primary)

        if primary not in WHISPER_CODES:
            raise UnknownLanguage(raw)

        codes.add(primary)

    if len(codes) != 1:
        raise UnknownLanguage(raw)

    return codes.pop()


def hint(raw: str | None) -> str | None:
    """normalize(), but an unfamiliar spelling — None, not an exception.

    For YouTube labels: an unfamiliar value there simply means "we don't know", and a job
    mustn't fail because of it.
    """
    try:
        return normalize(raw)
    except UnknownLanguage:
        return None


def family(code: str | None) -> str | None:
    """hint(), where hr and bs are folded to sr: for comparing languages, not for the decoder.

    Three Whisper tokens — one language, and "hr versus sr" isn't considered a discrepancy.
    The decoder, however, is given the code as is (normalize), not the family.
    """
    resolved = hint(code)

    return "sr" if resolved in BCMS else resolved


def is_bcms(code: str | None) -> bool:
    """Whether this is Serbian, Croatian or Bosnian — under any of the spellings."""
    return family(code) == "sr"


def refusal(raw: str) -> str:
    """The refusal phrase for an invalid code: what's wrong — first, where to fix — after.

    The template is pinned by a test, and it mustn't be cut. With "serbian" the phrase
    takes 56 characters and fits on a badge; a value longer than 11 characters pushes it
    past 60 ("serbo-croatian" — 63): the badge will cut the tail with an ellipsis, the full
    phrase will remain in the tooltip, and most importantly, "unknown language "…"" comes first.
    """
    return f'unknown language "{raw}": [asr] language in config.toml'


# ── Detector: windows, rule, decision ─────────────────────────────────────────

# Detector constants. Any of them changes the recognition of already cached
# videos, so an edit requires bumping helper.cache.PROMPT_VERSION.

# A window with less speech doesn't vote: silence, music and an intro produce
# a distribution on padding, and such a window would vote on par with speech.
MIN_SPEECH_SECONDS = 3.0

# The share of the Serbian family at which the detector decides "Serbian", even if
# another language is on top. On Serbian speech Whisper pulls toward English: 3XJLfTSZy6I
# gave en 0.55 versus 0.44 for the family; on an English video the family is zero in every window.
BCMS_FLOOR = 0.2

# Neighbours whose Serbian family is often large by kinship: the threshold doesn't
# override them, otherwise Macedonian or Russian speech would be recognized as Serbian.
SLAVIC_NEIGHBOURS = frozenset({"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"})

# Whisper listens to 16 kHz mono, in windows of thirty seconds.
SAMPLE_RATE = 16000
WINDOW_SAMPLES = 30 * SAMPLE_RATE


def probe_offsets(
    n_samples: int,
    *,
    windows: int = 8,
    span: tuple[float, float] = (0.10, 0.90),
    window: int = WINDOW_SAMPLES,
) -> list[int]:
    """Starts of the detector windows in a recording of n_samples samples, ascending.

    The windows lie within a span of length: the first starts at 10%, the last ends at 90%,
    the rest — evenly between them. Taking from the start isn't allowed — there's the intro,
    music or a sponsor's ad, and the language would be determined from them. Measured on this
    corpus: for KCR-dkZIhxY the first utterance starts at the 21st second, i.e. the first
    thirty-second window would be two-thirds intro.

    The starts are clamped to [0, n_samples - window]: on a recording of 30 to 37.5 s the
    windows come out whole and overlap rather than go negative, and coinciding starts merge
    into one. A recording no longer than a window — one window from the start.
    """
    if n_samples <= window:
        return [0]

    low, high = span
    first = n_samples * low
    last = n_samples * high - window
    latest = n_samples - window
    starts = {
        min(max(round(first + (last - first) * i / max(windows - 1, 1)), 0), latest)
        for i in range(windows)
    }

    return sorted(starts)


@dataclass(frozen=True)
class Detection:
    """What the detector heard in the recording's windows and which language follows from that.

    language — the winning family (hr and bs folded to sr) or None if no window voted;
    confidence — its average share over the voting windows; bcms_share — the average share
    of the Serbian family; floor — Serbian was taken by the BCMS_FLOOR threshold rather than
    first place; votes — how many windows voted; probed — how many windows were listened to in all.
    """

    language: str | None
    confidence: float | None
    bcms_share: float
    floor: bool
    votes: int
    probed: int
    # Per window: the top family or None (the window didn't vote), its share, seconds of speech.
    windows: tuple[tuple[str | None, float, float], ...]


def summarize(windows: Sequence[tuple[float, Sequence[tuple[str, float]]]]) -> Detection:
    """The detector rule: windows (seconds of speech, Whisper distribution) → Detection.

    A window's distribution is reduced by family: the shares of bs, hr and sr are summed
    under sr, a code Whisper doesn't know is skipped. A window votes where there's at least
    MIN_SPEECH_SECONDS of speech and the detector gave an answer. Family shares are averaged
    over the voting windows — distributions, not top-1 votes: hard voting would split Serbian
    into three labels. The largest mean wins; the Serbian family with an average share from
    BCMS_FLOOR takes the top even without being first, except over neighbours from SLAVIC_NEIGHBOURS.
    """
    records: list[tuple[str | None, float, float]] = []
    voting: list[dict[str, float]] = []

    for speech, distribution in windows:
        shares: dict[str, float] = {}

        for code, share in distribution:
            folded = family(code)

            if folded is not None:
                shares[folded] = shares.get(folded, 0.0) + share

        # An empty distribution — the detector crashed on the window or wasn't called.
        if speech < MIN_SPEECH_SECONDS or not shares:
            records.append((None, 0.0, speech))

            continue

        top = max(shares, key=shares.__getitem__)
        records.append((top, shares[top], speech))
        voting.append(shares)

    if not voting:
        return Detection(None, None, 0.0, False, 0, len(windows), tuple(records))

    totals: dict[str, float] = {}

    for shares in voting:
        for code, share in shares.items():
            totals[code] = totals.get(code, 0.0) + share

    means = {code: total / len(voting) for code, total in totals.items()}
    bcms_share = means.get("sr", 0.0)
    winner = max(means, key=means.__getitem__)
    floor = winner != "sr" and bcms_share >= BCMS_FLOOR and winner not in SLAVIC_NEIGHBOURS

    if floor:
        winner = "sr"

    return Detection(
        language=winner,
        # At the threshold this is bcms_share.
        confidence=means[winner],
        bcms_share=bcms_share,
        floor=floor,
        votes=len(voting),
        probed=len(windows),
        windows=tuple(records),
    )


@dataclass(frozen=True)
class LanguageVerdict:
    """The source language decision: which language the utterances are in and where that's known from.

    effective — the Whisper code with which recognition was done (for the author's track —
    the language of its label); declared — what was declared before the audio, declared_by —
    by whom: "config" (the owner's pin), "metadata" (the YouTube label) or None; detected and
    confidence — what the detector heard (None — no speech was found or it wasn't listened to);
    reason — where effective comes from: "pin", "detector", "floor", "hint", "fallback", for the
    author's track — "track"; source — "asr" or "manual" (the author's track), track — its label;
    verified — whether the track was checked against the speech (None — not checked).
    """

    effective: str
    declared: str | None
    declared_by: str | None
    detected: str | None
    confidence: float | None
    reason: str
    source: str = "asr"
    track: str | None = None
    verified: bool | None = None

    @property
    def mismatch(self) -> bool:
        """The declared diverged from the heard, and for the author's track — from its language.

        Families are compared: hr versus sr — not a discrepancy. Unknown on either side
        (not declared, no speech found) — also not.
        """
        declared = family(self.declared)
        heard = family(self.effective if self.source == "manual" else self.detected)

        return declared is not None and heard is not None and declared != heard

    def note(self) -> str | None:
        """A phrase for the viewer and the log, up to 60 characters; None — nothing to say."""
        if self.mismatch and self.declared_by == "metadata":
            if self.source == "manual":
                return f"YouTube indicated {self.declared}, the author's track {self.effective}"

            return f"YouTube indicated {self.declared}, by speech {self.detected}"

        if self.mismatch and self.declared_by == "config":
            if self.source == "manual":
                return f"in the settings {self.declared}, the author's track {self.effective}"

            return f"in the settings {self.declared}, by speech {self.detected}"

        if self.reason == "hint":
            return f"almost no speech, the language came from YouTube: {self.effective}"

        if self.reason == "fallback":
            return f"almost no speech, the language was assumed to be {self.effective}"

        return None

    def wire(self) -> dict:
        """The decision for the worker's response, the snapshot and the cache: mismatch and note — computed."""
        return {
            "effective": self.effective,
            "declared": self.declared,
            "declaredBy": self.declared_by,
            "detected": self.detected,
            "confidence": None if self.confidence is None else round(self.confidence, 2),
            "reason": self.reason,
            "source": self.source,
            "track": self.track,
            "verified": self.verified,
            "mismatch": self.mismatch,
            "note": self.note(),
        }

    @classmethod
    def from_wire(cls, data: object) -> LanguageVerdict | None:
        """A decision from wire(); None — this is not a decision. Never raises.

        Reads both the worker's response and a cache record made, perhaps, by a previous
        version. Without a valid effective there's no decision: the code is checked as is,
        without normalization ("SR" and "sh" are not a decision). The rest is tolerated — an
        invalid field becomes the default. mismatch and note aren't read: they're always
        recomputed.
        """
        if not isinstance(data, dict):
            return None

        effective = data.get("effective")

        if not isinstance(effective, str) or effective not in WHISPER_CODES:
            return None

        def text(key: str) -> str | None:
            value = data.get(key)

            return value if isinstance(value, str) and value else None

        raw = data.get("confidence")
        confidence = None

        # bool is a subclass of int, but not a share.
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            try:
                value = float(raw)
            except OverflowError:
                pass  # an integer larger than any float: no such share exists
            else:
                # NaN and infinity aren't valid in JSON: JSONResponse on the wire would
                # fail on every response of this video, and the cache record carries them.
                confidence = value if math.isfinite(value) else None

        reason = data.get("reason")
        verified = data.get("verified")

        return cls(
            effective=effective,
            declared=text("declared"),
            declared_by=text("declaredBy"),
            detected=text("detected"),
            confidence=confidence,
            reason=reason if isinstance(reason, str) else "detector",
            source="manual" if data.get("source") == "manual" else "asr",
            track=text("track"),
            verified=verified if isinstance(verified, bool) else None,
        )


def decide(
    pin: str | None, hint: str | None, detection: Detection, *, fallback: str = "sr"
) -> LanguageVerdict:
    """The recognition language: the owner's pin, else the speech, else the YouTube label, else fallback.

    pin and hint — already Whisper codes: the callers bring them (normalize and hint). The pin
    always decides, and the detector only checks it. Without a pin the speech decides, if at
    least one window voted: the hint doesn't outweigh the voting windows (Juske — label en,
    speech Serbian). The label decides only when there's almost no speech. The language is
    never empty or None: with that Whisper would determine the language itself, by the first
    thirty seconds.
    """
    if pin:
        effective, reason = pin, "pin"
    elif detection.votes:
        effective, reason = detection.language, ("floor" if detection.floor else "detector")
    elif hint:
        effective, reason = hint, "hint"
    else:
        effective, reason = fallback, "fallback"

    return LanguageVerdict(
        effective=effective,
        declared=pin or hint,
        declared_by="config" if pin else ("metadata" if hint else None),
        detected=detection.language,
        confidence=detection.confidence,
        reason=reason,
    )
