"""Translating sr→ru utterances via local ollama.

Alignment of utterances is guaranteed structurally, not by requests in the prompt: a
schema with fixed keys c1..cN is compiled by ollama into a GBNF grammar, and the model
physically cannot merge, split, lose or reorder an utterance. The schema goes outside
the prompt and costs not a single token of context.

The grammar guarantees STRUCTURE and says nothing about CONTENT: an empty string,
untranslated Serbian and duplicated text — all this conforms to the schema.
Therefore the validators below check the output language, not just the form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:27b"

# Letters absent from the Russian alphabet — a reliable sign that a Serbian word
# simply slipped through. Serbian Cyrillic has ђ ј љ њ ћ џ, Russian doesn't.
SERBIAN_ONLY = re.compile(r"[ђЂјЈљЉњЊћЋџЏ]")

# Serbian diacritics: they have nowhere to come from in Russian text, including names.
SERBIAN_DIACRITICS = re.compile(r"[čćžšđČĆŽŠĐ]")

# A Latin word starting with a lowercase letter. Proper names and brands are written with
# a capital, so it's exactly untranslated Serbian lexis that falls under this pattern.
LOWERCASE_LATIN_WORD = re.compile(r"\b[a-z]{4,}\b")

# False friends sr→ru — the main class of errors for this pair. The model copies a Serbian
# word precisely because it looks like a Russian one, and the result is grammatical,
# correctly aligned, of the right length and wrong in meaning.
#
# Starting set. Expand from documented sources: Wikibooks "False Friends
# of the Slavist", the appendix "Ложные друзья переводчика (сербский)" in ru.wiktionary.
FALSE_FRIENDS: dict[str, str] = {
    "pozorište": "театр",
    "slovo": "буква",
    "reč": "слово",
    "ponos": "гордость",
    "stolica": "стул",
    "trudna": "беременная",
    "vredan": "ценный, прилежный",
    "iskustvo": "опыт",
    "umetnost": "искусство",
    "majka": "мать",
    "mladenci": "молодожёны",
    "štuka": "щука",
    "jagoda": "клубника",
    "kosa": "волосы",
    "stan": "квартира",
    "svet": "мир",
    "skoro": "почти",
    "rok": "срок",
    "čast": "честь",
    "sat": "час, часы",
    "voz": "поезд",
    "grad": "город",
    "nedelja": "воскресенье",
    "hitan": "срочный",
    "sramota": "позор, стыд",
    "zavod": "учреждение",
    "dvorac": "замок (здание)",
    "vojska": "армия",
    "glumac": "актёр",
    "pisac": "писатель",
    "proba": "репетиция",
    "plata": "зарплата",
    "prodavnica": "магазин",
    "posao": "работа",
    "odmor": "отдых",
    "pravac": "направление",
    "sprat": "этаж",
    "beba": "младенец",
    "kod": "у (предлог)",
    "ludo": "безумно",
}

SYSTEM_PROMPT = """Ты переводишь субтитры с сербского языка на русский.

ПРАВИЛА:
- Переводи КАЖДУЮ реплику отдельно. Никогда не объединяй, не разбивай, не переставляй \
и не пропускай реплики.
- Верни JSON-объект, ключи которого — ровно c1..cN.
- Длина перевода должна быть примерно как у оригинала: субтитр должны успеть прочитать.
- Источник — распознавание спонтанной сербской речи. В нём бывают ошибки распознавания, \
потерянная диакритика (č ć š ž đ часто выглядят как c s z d), неверные границы слов, \
нет пунктуации и заглавных букв. Восстанавливай смысл по контексту.
- Сербские слова часто похожи на русские, но значат другое. Переводи смысл, никогда не \
копируй сербское слово только потому, что оно выглядит по-русски. В выводе не должно \
остаться ни одного сербского слова.
- Сохраняй разговорный регистр, свойственный видео на YouTube.
- Не пиши пометок о неуверенности, не вставляй многоточия вместо непонятого, не добавляй \
пояснений. Только перевод.
- Неречевые пометки ([Music], [Applause]) переноси без изменений."""


class TranslateError(RuntimeError):
    """ollama didn't return a usable response."""


@dataclass
class Batch:
    """Utterances to translate plus the context attached to them."""

    cues: list[str]
    previous: list[tuple[str, str]] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)
    title: str = ""
    channel: str = ""


def cue_schema(count: int) -> dict:
    """The schema from which ollama builds the grammar.

    Fixed keys instead of an array with minItems/maxItems: an array limits
    only the count, while keys — also identity, so desynchronization becomes
    self-evident. `prefixItems` can't be used, it's broken in llama.cpp's
    json-schema→GBNF converter.
    """
    keys = [f"c{i}" for i in range(1, count + 1)]

    return {
        "type": "object",
        "properties": {k: {"type": "string"} for k in keys},
        "required": keys,
        "additionalProperties": False,
    }


def dual_cue_schema(count: int) -> dict:
    """A schema for two languages at once: {c1: {crnogorski, ruski}, …}.

    Nested objects are assembled by hand, not via Pydantic. This isn't a matter of taste,
    but a safety property: model_json_schema() always moves nested models
    into $ref/$defs, and on them the schema-to-grammar converter in llama.cpp expands
    everything inline, hits the rule budget and **refuses silently** — returns 200
    and generates without any constraints at all. The guarantee of the utterance count then
    disappears without a single error in the log.

    The names of the inner keys were chosen not for beauty. Under constrained decoding
    the grammar writes them itself, the model can neither skip them nor paraphrase them,
    and each one ends up in the context right before its field — i.e. it works
    as an irremovable language label on every utterance.

    The order of the keys is preserved all the way to the grammar, so Montenegrin goes
    first deliberately: that way it's translated from the original. If it stood second,
    Russian text would end up between the source and it, and you'd get a calque from
    Russian — exactly what we avoid.
    """
    cue = {
        "type": "object",
        "properties": {
            "crnogorski": {"type": "string"},
            "ruski": {"type": "string"},
        },
        "required": ["crnogorski", "ruski"],
        "additionalProperties": False,
    }
    keys = [f"c{i}" for i in range(1, count + 1)]

    return {
        "type": "object",
        "properties": {k: cue for k in keys},
        "required": keys,
        "additionalProperties": False,
    }


def build_user_message(batch: Batch) -> str:
    parts: list[str] = []

    if batch.title:
        parts.append(f"НАЗВАНИЕ ВИДЕО: {batch.title}")

    if batch.channel:
        parts.append(f"КАНАЛ: {batch.channel}")

    glossary = {**FALSE_FRIENDS, **batch.glossary}

    if glossary:
        pairs = "\n".join(f"{src} :: {dst}" for src, dst in glossary.items())
        parts.append(f"СЛОВАРЬ (соблюдать неукоснительно):\n{pairs}")

    if batch.previous:
        # Gender in the past tense is marked in both Serbian and Russian. Without an
        # antecedent the model will guess the speaker's gender and flip it in the middle
        # of the scene.
        lines = "\n".join(f"{src} → {dst}" for src, dst in batch.previous)
        parts.append(f"ПРЕДЫДУЩИЕ РЕПЛИКИ (контекст, НЕ переводить):\n{lines}")

    cues = "\n".join(f"c{i}: {text}" for i, text in enumerate(batch.cues, 1))
    parts.append(f"РЕПЛИКИ НА ПЕРЕВОД:\n{cues}")

    return "\n\n".join(parts)


def chat(
    messages: list[dict],
    schema: dict | None = None,
    model: str = DEFAULT_MODEL,
    *,
    temperature: float = 0.0,
    num_ctx: int = 8192,
    num_predict: int = 4000,
    keep_alive: str = "60s",
    # Ten minutes weren't headroom but a trap. Measured: a batch of forty utterances
    # with both lines takes 24-43 seconds, so the timeout is reached
    # only when something has gone wrong — and then the pipeline stood TEN MINUTES,
    # silently, without sending a single event. The viewer saw a frozen counter and
    # decided that the connection had dropped. Four minutes is still a sixfold headroom
    # to the worst measurement, but a failure now goes into batch splitting in reasonable time.
    timeout: float = 240.0,
) -> dict:
    """One /api/chat call.

    `think: false` is mandatory: with reasoning enabled the model burns all
    num_predict on thinking and returns empty content.

    `keep_alive` is deliberately short and never -1: ollama's scheduler sorts
    unloading candidates by sessionDuration reduced to uint64, so
    a negative duration sorts last and makes the model unloadable —
    exactly the hostile-neighbour behavior we avoid.
    """
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        # ollama's default num_ctx is 4096, and it truncates silently, without an error.
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }

    if schema is not None:
        body["format"] = schema

    response = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=timeout)
    response.raise_for_status()
    payload = response.json()

    if payload.get("done_reason") == "length":
        raise TranslateError("the response was truncated by num_predict — reduce the batch or raise the limit")

    if not payload.get("message", {}).get("content"):
        raise TranslateError("ollama returned empty content")

    return payload


def translate_dual(batch: Batch, model: str = DEFAULT_MODEL, **kwargs) -> list[tuple[str, str]]:
    """Translate a batch into Montenegrin and Russian at once in one pass.

    One pass, not two: twice as cheap in time, and the Russian line comes out
    as a gloss to this very Montenegrin one, rather than an independent translation.
    For the study mode that's exactly what's needed — the viewer matches the lines
    word to word.

    The false-friends glossary sr→ru deliberately doesn't go here: its keys are ekavian
    (svet, nedelja, devojka), and in a prompt with a Montenegrin goal they pull the model
    back to ekavica. Instead, ijekavian hints go in.
    """
    if not batch.cues:
        return []

    import json

    from helper.montenegrin import IJEKAVIAN_HINTS, SYSTEM_PROMPT as ME_PROMPT

    kwargs.setdefault("num_predict", estimate_num_predict(batch.cues) * 2)

    hints = "\n".join(f"{bad} -> {good}" for bad, good in IJEKAVIAN_HINTS.items())
    parts = [f"IJEKAVSKI OBLICI (obavezno):\n{hints}"]

    if batch.title:
        parts.append(f"NASLOV VIDEA: {batch.title}")

    if batch.previous:
        # Only the Montenegrin side goes into the context. Ekavian examples
        # in the context turn the model back to ekavica — measured:
        # qwen fails all ten utterances out of ten.
        lines = "\n".join(f"{src} -> {dst}" for src, dst in batch.previous)
        parts.append(f"PRETHODNI TITLOVI (samo kontekst, ne prevoditi):\n{lines}")

    cues = "\n".join(f"c{i}: {text}" for i, text in enumerate(batch.cues, 1))
    parts.append(f"TITLOVI ZA PRIJEVOD:\n{cues}")

    payload = chat(
        [
            {"role": "system", "content": ME_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        schema=dual_cue_schema(len(batch.cues)),
        model=model,
        **kwargs,
    )

    data = json.loads(payload["message"]["content"])
    _check_keys(data, len(batch.cues))

    return [
        (data[f"c{i}"].get("crnogorski", ""), data[f"c{i}"].get("ruski", ""))
        for i in range(1, len(batch.cues) + 1)
    ]


# Search window: how many utterances forward and back the text could have moved. Measured —
# on live material the drift reached five positions, but we take four with headroom:
# the wider the window, the more chances of a random match for short utterances.
DRIFT_WINDOW = 4

# Utterances shorter than three content words aren't scored: "Da.", "Evo sad.",
# "Izazovan za istraživanje." match anything, and they can't vote.
MIN_STEMS = 3

# How much more confident someone else's position must be than its own to count as drift.
DRIFT_MARGIN = 0.34

# The share of drifted utterances after which the batch is rejected. Measured on 160 real
# utterances: for qwen3.8:27b — 0 out of 122 evaluated across four batches, for
# translategemma:12b on the same batches 11%, 32%, 50% and 81%. Between zero and eleven
# percent there's enough room, and the threshold here isn't tuning to one run, but a
# separation of two qualitatively different behaviors: the model either keeps one-to-one
# correspondence, or doesn't keep it at all.
DRIFT_RATIO = 0.10


def _stems(text: str) -> set[str]:
    """Content words, coarsened to a stem.

    Four characters, because Serbian is inflective: pogledom and pogleda are one
    word, and distinguishing them here means not finding obvious correspondences.
    """
    return {w[:4] for w in re.findall(r"\w+", text.lower()) if len(w) >= 4}


def _coverage(source: str, output: str) -> float:
    """What share of the source utterance's words was found in this output."""
    stems = _stems(source)

    return len(stems & _stems(output)) / len(stems) if stems else 0.0


def alignment_shifted(sources: list[str], outputs: list[str]) -> bool:
    """Whether the output has drifted from the source: utterances merged, shifted or lost.

    The grammar guarantees that exactly N keys come back. It does NOT guarantee
    that key cN relates to source utterance N: the model may merge two neighbouring ones
    into one and shift the whole tail, leaving the last ones as stubs. The subtitle
    meanwhile shows the text of the neighbouring utterance and looks completely plausible.

    We search by content, not by lengths. The previous version compared the correlation
    of lengths under direct matching and under a shift of exactly one position — and on
    real material it didn't trigger ONCE, including batches where four fifths of the
    utterances had drifted. Two reasons. The shift there isn't by one position but
    cumulative: merges go one after another, and the tail moves further and further.
    And length is too weak a feature: neighbouring utterances have a similar one, so
    correlation hardly distinguishes a correct match from a shifted one.

    Content doesn't suffer from this drawback: the Serbian source and the Montenegrin
    output are almost the same text, so the words of the source utterance are visible
    in its translation directly. We ask not "are the lengths similar", but "where did
    this utterance's words end up"; if for most of them they lie in the wrong place,
    the one-to-one correspondence is broken.
    """
    if len(sources) != len(outputs) or len(sources) < 6:
        return False

    scored = drifted = 0

    for i, source in enumerate(sources):
        if len(_stems(source)) < MIN_STEMS:
            continue

        scored += 1
        here = _coverage(source, outputs[i])
        best, best_at = here, i

        for j in range(max(0, i - DRIFT_WINDOW), min(len(outputs), i + DRIFT_WINDOW + 1)):
            if j == i:
                continue

            elsewhere = _coverage(source, outputs[j])

            if elsewhere > best:
                best, best_at = elsewhere, j

        if best_at != i and best > here + DRIFT_MARGIN:
            drifted += 1

    return scored >= 6 and drifted / scored > DRIFT_RATIO


def validate_dual(pairs: list[tuple[str, str]], sources: list[str] | None = None) -> list[str]:
    """Check both fields by different measures. Returns a list of remarks on the batch.

    The measures must be different. The Serbian-leak detector into Russian is perfectly
    suited to the `ruski` field — it was written exactly for that. It mustn't be pointed
    at the Montenegrin field: it complains about the diacritics č ć ž š đ, which are
    mandatory there, and about lowercase Latin words, i.e. about every Montenegrin word.
    Pointed in the wrong direction, it would mark a hundred percent of correct output as
    defective and drive the repair ladder into an empty loop.
    """
    from helper.montenegrin import check as check_montenegrin

    problems: list[str] = []

    # Montenegrin is checked over the whole batch: a short utterance may
    # contain no word with yat, and judging by it is meaningless.
    montenegrin = check_montenegrin(" ".join(me for me, _ in pairs))

    if montenegrin:
        problems.append(f"Montenegrin: {montenegrin}")

    leaked = sum(1 for _, ru in pairs if ru.strip() and looks_untranslated(ru))

    if leaked:
        problems.append(f"the ruski field is not Russian text: {leaked} of {len(pairs)}")

    empty = sum(1 for me, ru in pairs if not me.strip() or not ru.strip())

    if empty:
        problems.append(f"empty fields: {empty}")

    if sources and alignment_shifted(sources, [me for me, _ in pairs]):
        problems.append(
            "the output drifted relative to the source: utterances merged, "
            "and the subtitle will show the neighbouring one"
        )

    return problems


def looks_untranslated(text: str) -> bool:
    """Whether something Serbian remains in the Russian output.

    The naive check "there's Latin — so it's a leak" gives too many false
    positives: Russian subtitles legitimately contain names and numbers — `The Beatles`,
    `Douglas DC-9-32`, flight `JU-367`, `YU-AHT`. On real material, only correct
    translations were marked this way.

    The signs that really mean untranslated:
      * Serbian Cyrillic letters (ђ ј љ њ ћ џ) — they're absent from the Russian alphabet;
      * Serbian diacritics (č ć ž š đ) — they have nowhere to come from in Russian text;
      * a Latin word entirely in lowercase — brands and proper names are written with
        a capital or contain digits, but lexis that slipped through isn't.

    Comparing a word with the source is pointless: a leaked word is often slightly distorted
    and doesn't match literally. Verified on 360 real translations — this rule gives no
    false positives.

    The tilt toward sensitivity is deliberate: the validator triggers a repeat of the batch,
    so a false positive costs one extra attempt, while a miss sends broken text to the screen.
    """
    return bool(
        SERBIAN_ONLY.search(text)
        or SERBIAN_DIACRITICS.search(text)
        or LOWERCASE_LATIN_WORD.search(text)
    )


def validate(cues: list[str], translated: list[str]) -> list[tuple[int, str]]:
    """List of problems as (index, reason). An empty list — everything is fine."""
    problems: list[tuple[int, str]] = []

    for i, (source, target) in enumerate(zip(cues, translated)):
        stripped = target.strip()

        if not stripped:
            problems.append((i, "empty"))
        elif stripped == source.strip():
            problems.append((i, "not translated"))
        elif looks_untranslated(stripped):
            problems.append((i, "Serbian remained"))
        elif stripped.count("\n") >= 2:
            problems.append((i, "more than two lines"))

    return problems


def estimate_num_predict(cues: list[str]) -> int:
    """How many tokens must be allowed for the response for this batch.

    A constant doesn't work here. Utterances after segmentation are short, but if raw
    thirty-second Whisper windows get into the input (~300 characters each), a fixed limit
    will truncate the response, and the batch will come back empty — the grammar won't
    let the model finish the JSON.

    Estimate from the bottom up: Russian is about 15% longer than Serbian, Cyrillic goes
    denser than two characters per token, plus each utterance takes JSON wrapping
    (key, quotes, comma), plus a third of headroom.
    """
    source_chars = sum(len(c) for c in cues)
    text_tokens = source_chars * 1.15 / 2.0
    json_overhead = len(cues) * 6

    return max(512, int((text_tokens + json_overhead) * 1.35))


def translate_batch(batch: Batch, model: str = DEFAULT_MODEL, **kwargs) -> list[str]:
    """Translate a batch of utterances, preserving their count and order."""
    if not batch.cues:
        return []

    import json

    kwargs.setdefault("num_predict", estimate_num_predict(batch.cues))

    payload = chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(batch)},
        ],
        schema=cue_schema(len(batch.cues)),
        model=model,
        **kwargs,
    )

    data = json.loads(payload["message"]["content"])
    _check_keys(data, len(batch.cues))

    return [data[f"c{i}"] for i in range(1, len(batch.cues) + 1)]


def _check_keys(data: dict, count: int) -> None:
    """Make sure the model returned exactly the expected keys.

    One must check the parsed response, not a list assembled from it: the list
    is built from the range 1..N and always comes out the right length, so such a
    check would never fire.

    And checking is necessary because the grammar may not apply:
    on a schema parse failure llama.cpp writes an error to its log, returns 200
    and generates without constraints. The refusal is marked "won't fix",
    so the only defense is to verify the keys on our side.
    """
    if not isinstance(data, dict):
        raise TranslateError(f"expected an object, got {type(data).__name__}")

    expected = {f"c{i}" for i in range(1, count + 1)}
    missing = expected - data.keys()

    if missing:
        raise TranslateError(
            f"the grammar didn't apply: missing keys {sorted(missing)[:5]} "
            f"of {count} expected"
        )
