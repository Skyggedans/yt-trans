"""Assembling readable subtitles from ASR segments.

Here we own the timeline: Whisper gives word-level timings, so we build the utterances
ourselves rather than inheriting someone else's boundaries.

The constants are the Netflix Russian Timed Text Style Guide: 42 characters per line,
at most two lines, reading speed 17 characters per second, duration from 5/6 second to
7 seconds, a gap between utterances of two frames.

Aligning the number of utterances, which the grammar in translate.py guarantees, has
nothing to do with this module: there the guarantee is "as many returned as given", and
here it's decided how many utterances there will be at all. The order is: first build
the utterances, then translate — otherwise one would have to translate fragments of phrases.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

MAX_CHARS_PER_LINE = 42
MAX_LINES = 2
MAX_CPS = 17.0
MIN_DURATION = 5 / 6
MAX_DURATION = 7.0
GAP = 0.08

MAX_CHARS = MAX_CHARS_PER_LINE * MAX_LINES

# End of a sentence: period, question, exclamation, ellipsis — with a closing quote
# or parenthesis after it and a space following.
SENTENCE_END = re.compile(r'(?<=[.!?…])["»\')\]]*\s+')

# Places where a line break reads best, from best to worst.
BREAK_AFTER = re.compile(r"[,;:—–]\s")
CONJUNCTIONS = frozenset(
    # Serbian (Latin) and Russian conjunctions and prepositions — it's good to break before them
    "i a ali ili pa te da jer kad ako što koji koja koje sa za od do na u o "
    "и а но или да что чтобы если когда как который которая которое с со за от до на в о".split()
)


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def cps(self) -> float:
        return len(self.text) / self.duration if self.duration > 0 else float("inf")


def split_sentences(text: str) -> list[str]:
    """Split into sentences by punctuation.

    large-v3 places punctuation, so punctuation is usually enough.
    If a track with no punctuation at all comes along — wtpsplit SaT
    (`sat-3l-sm`) hooks in here, which holds sentence boundaries in 85 languages and
    doesn't depend on punctuation. Not hooked up yet: on punctuated input it doesn't pay off.

    The joining rule doesn't depend on the language: a part whose first character is
    lowercase doesn't start a sentence and is appended to the previous one with a single
    space. A period before a lowercase letter isn't considered a boundary — this way
    ordinal numerals and years ("5. maja", "1980. godine"), abbreviations
    ("U.S. market", "e.g. tech", "npr.") and an ellipsis before a continuation
    ("ali... ali, nekako") stay intact. Parts with an uppercase letter, a digit, a quote or
    a parenthesis at the start remain separate: a continuation with an uppercase letter
    ("Mr. Smith") is still cut, scripts without case aren't affected — for them
    `islower()` is false. A boundary that coincides with the edge of a VAD chunk lies
    between segments and doesn't reach here.
    """
    parts = [p.strip() for p in SENTENCE_END.split(text) if p.strip()]
    merged: list[str] = []

    for part in parts:
        if merged and part[0].islower():
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)

    return merged or ([text.strip()] if text.strip() else [])


def wrap_lines(text: str) -> list[str]:
    """Lay the text out into at most two lines, breaking by meaning, not by a counter.

    Splitting strictly by character count reads poorly: the break must fall
    on punctuation, and if there's none — on a conjunction or preposition, so as not to
    break a group.
    """
    if len(text) <= MAX_CHARS_PER_LINE:
        return [text]

    words = text.split()
    best_index, best_score = None, None

    # Candidates are all breaks at which both lines fit the limit.
    for i in range(1, len(words)):
        left = " ".join(words[:i])
        right = " ".join(words[i:])

        if len(left) > MAX_CHARS_PER_LINE or len(right) > MAX_CHARS_PER_LINE:
            continue

        # The more even the halves, the better; punctuation and a conjunction give a bonus.
        score = abs(len(left) - len(right))

        if BREAK_AFTER.search(left[-2:] + " "):
            score -= 30
        elif words[i].lower().strip(".,;:!?") in CONJUNCTIONS:
            score -= 15

        if best_score is None or score < best_score:
            best_index, best_score = i, score

    if best_index is None:
        # No break gives two fitting lines — return as is, the caller will cut the
        # length by splitting the sentence into several utterances.
        return [text]

    return [" ".join(words[:best_index]), " ".join(words[best_index:])]


def _split_balanced(text: str, limit: int) -> list[str]:
    """Cut into chunks no longer than limit, trying to make them equal.

    Greedy "fill up to the limit" cutting leaves a stub at the tail: a sentence
    of 77 characters at a limit of 76 falls apart into 69 characters and one word. Such
    an utterance flashes on screen and reads like a glitch, even if it formally fits
    all the constraints. So first we count how many chunks are needed, then divide into
    roughly equal ones.
    """
    if len(text) <= limit:
        return [text]

    words = text.split()
    count = max(2, -(-len(text) // limit))

    while count <= len(words):
        target = len(text) / count
        chunks, current = [], ""

        for word in words:
            candidate = f"{current} {word}".strip()

            if current and len(chunks) < count - 1 and len(candidate) > target:
                chunks.append(current)
                current = word
            else:
                current = candidate

        if current:
            chunks.append(current)

        if all(len(c) <= limit for c in chunks):
            return chunks

        count += 1

    return [text]


def _allocate(chunks: list[str], start: float, end: float) -> list[Cue]:
    """Distribute the interval to the chunks in proportion to the text length.

    In proportion to characters, not equally: a short chunk that got as much
    time as a long one reads as a pause, and the long one doesn't keep up.
    """
    total = sum(len(c) for c in chunks) or 1
    # Gaps eat time, so we count the interval for the text already without them.
    gaps = GAP * (len(chunks) - 1)
    span = max(end - start - gaps, MIN_DURATION * len(chunks))
    cues, cursor = [], start

    for chunk in chunks:
        share = span * len(chunk) / total
        duration = min(max(share, MIN_DURATION), MAX_DURATION)

        cues.append(Cue(start=cursor, end=cursor + duration, lines=wrap_lines(chunk)))
        # The gap goes into the start of the next utterance, and isn't subtracted from
        # the duration of the current one — otherwise the clamping by the minimum is
        # cancelled right after being applied.
        cursor += duration + GAP

    return cues


def build_cues(
    segments: list[dict], drop: Callable[[str, dict], bool] | None = None
) -> list[Cue]:
    """Assemble utterances from ASR segments (start, end, text, words).

    Word timings are used to plant the sentence boundary on a real
    pause between words, rather than on a proportional estimate inside the segment.

    drop is called as drop(sentence, segment); True — the sentence doesn't go into
    utterances. Without drop the behavior is the previous one: spikes/asr_real.py relies
    on this.
    """
    cues: list[Cue] = []

    for segment in segments:
        text = segment["text"].strip()

        if not text:
            continue

        words = segment.get("words") or []
        sentences = split_sentences(text)
        spans = _sentence_spans(sentences, words, segment["start"], segment["end"])

        for sentence, (start, end) in zip(sentences, spans):
            # Intervals are matched to words across all sentences of the segment, before
            # screening: discard a sentence earlier, and each following one would land on
            # someone else's words. Screening goes by the sentence as a whole, before
            # _split_balanced and _enforce_min_duration: otherwise a long echo would be
            # cut into chunks that separately don't look like an echo, and a short one
            # would be glued to a neighbouring real utterance ("Da. Pominju se na srpskom jeziku.").
            if drop is not None and drop(sentence, segment):
                continue

            fits = len(sentence) <= MAX_CHARS
            readable = len(sentence) / max(end - start, 1e-6) <= MAX_CPS

            if fits and readable and end - start <= MAX_DURATION:
                cues.append(Cue(start=start, end=end, lines=wrap_lines(sentence)))
                continue

            # Doesn't fit by characters or by duration — cut into several utterances
            # and distribute the interval proportionally.
            #
            # Reading speed deliberately doesn't enter here. If the speaker uttered
            # 76 characters in 3 seconds, then the split will leave the same 26 characters
            # per second, only the utterances will start changing twice as often — it will
            # get worse, not better. The only lever against high CPS is to shorten the
            # text, and it's found in the translation: the validator flags such utterances,
            # and the model shortens them.
            needed = max(
                len(sentence) / MAX_CHARS,
                (end - start) / MAX_DURATION,
            )
            limit = max(int(len(sentence) / max(needed, 1.0)) + 1, MAX_CHARS_PER_LINE)
            cues += _allocate(_split_balanced(sentence, min(limit, MAX_CHARS)), start, end)

    return _dedupe_overlaps(_enforce_min_duration(cues))


def _enforce_min_duration(cues: list[Cue]) -> list[Cue]:
    """Remove utterances shorter than the minimum: first by merging, then by stretching.

    Short phrases like "Да." and "Нет.", said in a row, come by a direct path
    bypassing the splitting where the minimum is already observed. Showing them for half
    a second is not allowed — they flash and aren't read. Stretching both won't work
    either: there isn't that much time between them. The right solution is the same as
    for live subtitlers — merge into one utterance if the combined text fits two lines.
    """
    merged: list[Cue] = []

    for cue in cues:
        if merged:
            previous = merged[-1]
            combined = f"{previous.text} {cue.text}"
            crowded = previous.duration < MIN_DURATION or cue.duration < MIN_DURATION

            if crowded and cue.start - previous.end <= 1.0 and len(combined) <= MAX_CHARS:
                merged[-1] = Cue(
                    start=previous.start, end=cue.end, lines=wrap_lines(combined)
                )
                continue

        merged.append(cue)

    # What couldn't be merged — stretch into a pause up to the next utterance.
    for index, cue in enumerate(merged):
        if cue.duration >= MIN_DURATION:
            continue

        limit = merged[index + 1].start - GAP if index + 1 < len(merged) else cue.end + MAX_DURATION
        cue.end = min(max(cue.end, cue.start + MIN_DURATION), max(limit, cue.end))

    return merged


def _sentence_spans(
    sentences: list[str], words: list[dict], start: float, end: float
) -> list[tuple[float, float]]:
    """Match sentences to time intervals by word timings."""
    if not words or len(sentences) == 1:
        return _proportional_spans(sentences, start, end)

    spans, cursor = [], 0

    for index, sentence in enumerate(sentences):
        # Words may run out before sentences: the word list is sometimes shorter
        # than the segment text, because counting goes by characters, and punctuation
        # marks and spaces are distributed among words differently than in the source
        # line. We lay out the remainder proportionally inside the remaining interval.
        if cursor >= len(words):
            tail_start = spans[-1][1] if spans else start

            return spans + _proportional_spans(sentences[index:], tail_start, end)

        target = len(sentence.replace(" ", ""))
        taken, consumed = 0, cursor

        while consumed < len(words) and taken < target:
            taken += len(words[consumed]["word"].strip())
            consumed += 1

        consumed = max(consumed, cursor + 1)

        first, last = words[cursor], words[consumed - 1]
        spans.append((first.get("start") or start, last.get("end") or end))
        cursor = consumed

    return spans or _proportional_spans(sentences, start, end)


def _proportional_spans(
    sentences: list[str], start: float, end: float
) -> list[tuple[float, float]]:
    total = sum(len(s) for s in sentences) or 1
    spans, cursor = [], start

    for sentence in sentences:
        stop = cursor + (end - start) * len(sentence) / total
        spans.append((cursor, stop))
        cursor = stop

    return spans


def _dedupe_overlaps(cues: list[Cue]) -> list[Cue]:
    """Don't let neighbouring utterances overlap.

    An overlap means two utterances will be drawn simultaneously, and the search for the
    active utterance by currentTime will return an arbitrary one of them.
    """
    for previous, current in zip(cues, cues[1:]):
        if previous.end > current.start - GAP:
            # The floor is the utterance's own start, not MIN_DURATION/2. The previous
            # variant took the larger of the two options, so with closely spaced
            # utterances the floor won, and the overlap, for whose removal the function
            # exists, remained. At the same time it violated the minimum just set by
            # _enforce_min_duration, by half — and it can't be restored here: pulling an
            # utterance past the start of the next means bringing the overlap back.
            previous.end = max(current.start - GAP, previous.start)

    # Utterances collapsed to zero (the next one starts earlier than after GAP)
    # are useless on the timeline: showing them is impossible anyway.
    return [cue for cue in cues if cue.end > cue.start]
