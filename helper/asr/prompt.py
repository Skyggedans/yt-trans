"""Затравка распознавания и отсев его мусора — общее у воркера и демона.

Затравка нужна ровно для одного — прибить латиницу у сербского семейства
(`helper/script.py`), поэтому фраза есть только у `sr`. Где фразы нет, нет и затравки:
имена тоже не идут.

Пакетный режим faster-whisper 1.2.1 подаёт одну и ту же затравку каждому куску до 30
секунд (`generate_segment_batched` копирует её на каждый кусок пачки), и на кусках без
речи она всплывает эхом. Из затравки он берёт последние 223 токена (`get_prompt`:
`max_length // 2 - 1`): переполнение съело бы фразу, а не имена.

Пороги тишины и повторов пакетный режим принимает и не применяет («Unused Arguments»
в докстроке `BatchedInferencePipeline.transcribe`), поэтому отсев — на стороне демона:
сегмент судят по метрикам, которые он и так несёт (`segment_verdict`), а предложения
сегмента с «повтором» — по своей `compression_ratio` и по повтору уже сказанного
(`helper.jobs.asr_cues`).
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections.abc import Mapping, Sequence

from helper.language import family
from helper.script import CYRILLIC, to_latin

# Пороги — те же, что Whisper применяет в последовательном режиме, а пакетный режим
# только хранит в опциях. Тишина выбрасывает сегмент целиком, повтор его лишь помечает.
NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4

# Дольше воркер всё равно не возьмёт: переполнение затравки съело бы фразу.
MAX_NAMES = 12

PHRASES = {"sr": "Ovo je transkript video snimka na srpskom jeziku."}
NAMES_LEAD = "Pominju se:"

# Обиходные слова фразы и вводки: сами по себе эхо они не делают.
ECHO_COMMON = frozenset({"ovo", "je", "video", "na", "se", "srpskom", "jeziku"})


def harvest_names(title: str, tags: Sequence[str]) -> list[str]:
    """Имена из заголовка и тегов в их собственном написании, без транслитерации.

    Эхо в поле шло из шаблонного текста описаний (Label, Copyright, Patreon, Instagram,
    спонсоры), поэтому описания нет даже в параметрах. Слово капсом длиннее четырёх букв
    — выделение в заголовке (UDARIO, OFARBANOG, NAJGORIH), а короткие BMW и NATO — названия.
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
    """Затравка для решённого языка; None — язык без фразы, имена тогда не идут.

    Имена транслитерируются сербскими правилами и выкидываются, если после них осталась
    кириллица («Для» → «Dlя»): в затравке от них один мусор. Тест BCMS здесь
    структурный — фраза есть только у семейства `sr`.
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
    """Слова строки без регистра, диакритики и знаков: «Jürgen» и «jurgen» — одно."""
    # у đ нет разложения в NFKD, поэтому диграф подставляется до него.
    folded = text.casefold().replace("đ", "dj")
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(char)
    )

    return set(re.findall(r"\w+", folded))


def is_echo(sentence: str, prompt: str | None) -> bool:
    """Предложение состоит только из слов затравки, и это её хвост, а не обиходная речь.

    Отступление от буквы замысла: буквального совпадения с затравкой нет ни в одной из
    66 620 закэшированных реплик, эхо — обрывки и перестановки её хвоста («Pominju se na
    srpskom jeziku.») и списки имён. Обиходные слова фразы эхо не делают («Ovo je video.»,
    «Na srpskom jeziku.»), одно слово тоже («Andrija, Andrija.»), и одно имя из заголовка
    в обиходной фразе тоже: заголовок «ŠTA SE KRILO U TITOVOM PLAVOM VOZU?» даёт имя ŠTA,
    и без этого условия «Šta je ovo?» уходило бы эхом.
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
    """Степень сжатия по формуле faster-whisper (`get_compression_ratio`).

    Нужна демону для предложений внутри сегмента: метрику сегмента считает сама
    библиотека, а предложения — только мы.
    """
    data = text.encode("utf-8")

    return len(data) / len(zlib.compress(data))


def segment_verdict(segment: Mapping[str, object]) -> str | None:
    """«тишина», «повтор» или None по метрикам сегмента распознавания.

    «тишина» — правило, которым Whisper сам пропускает окно в последовательном режиме,
    и такой сегмент выбрасывается целиком. «повтор» — замена отката по температуре,
    которого в пакетном режиме нет (Whisper перерасшифровал бы кусок), но сегмент
    пакетного режима — целый кусок речи до 30 секунд, и петля часто начинается посреди
    него, после настоящего диалога. Поэтому «повтор» сегмент только помечает: внутри
    выбрасываются одни зациклившиеся предложения (`asr_cues`), а речь перед петлёй
    остаётся. Каждый выброс уходит в журнал.
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
