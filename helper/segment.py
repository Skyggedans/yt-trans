"""Сборка читаемых субтитров из сегментов ASR.

Здесь мы владеем таймлайном: Whisper отдаёт пословные тайминги, поэтому реплики
строим сами, а не наследуем чужие границы.

Константы — Netflix Russian Timed Text Style Guide: 42 знака в строке, максимум две
строки, скорость чтения 17 знаков в секунду, длительность от 5/6 секунды до 7 секунд,
зазор между репликами в два кадра.

Выравнивание количества реплик, которое гарантирует грамматика в translate.py, к этому
модулю отношения не имеет: там гарантия «сколько отдали, столько вернулось», а здесь
решается, сколько реплик вообще будет. Порядок такой: сначала собрать реплики, потом
переводить — иначе переводить пришлось бы обрывки фраз.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS_PER_LINE = 42
MAX_LINES = 2
MAX_CPS = 17.0
MIN_DURATION = 5 / 6
MAX_DURATION = 7.0
GAP = 0.08

MAX_CHARS = MAX_CHARS_PER_LINE * MAX_LINES

# Конец предложения: точка, вопрос, восклицание, многоточие — с закрывающей кавычкой
# или скобкой после и пробелом следом.
SENTENCE_END = re.compile(r'(?<=[.!?…])["»\')\]]*\s+')

# Места, где разрыв строки читается лучше всего, от лучшего к худшему.
BREAK_AFTER = re.compile(r"[,;:—–]\s")
CONJUNCTIONS = frozenset(
    # сербские (латиница) и русские союзы и предлоги — перед ними ломать хорошо
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
    """Разбить на предложения по пунктуации.

    large-v3 расставляет знаки препинания, поэтому пунктуации обычно достаточно.
    Если попадётся дорожка совсем без знаков — сюда подключается wtpsplit SaT
    (`sat-3l-sm`), который держит границы предложений на 85 языках и не зависит
    от пунктуации. Пока не подключён: на пунктуированном входе он не окупается.
    """
    parts = [p.strip() for p in SENTENCE_END.split(text) if p.strip()]

    return parts or ([text.strip()] if text.strip() else [])


def wrap_lines(text: str) -> list[str]:
    """Разложить текст максимум в две строки, ломая по смыслу, а не по счётчику.

    Разбиение строго по числу знаков читается плохо: разрыв должен приходиться
    на пунктуацию, а если её нет — на союз или предлог, чтобы не разрывать группу.
    """
    if len(text) <= MAX_CHARS_PER_LINE:
        return [text]

    words = text.split()
    best_index, best_score = None, None

    # Кандидаты — все разрывы, при которых обе строки влезают в лимит.
    for i in range(1, len(words)):
        left = " ".join(words[:i])
        right = " ".join(words[i:])

        if len(left) > MAX_CHARS_PER_LINE or len(right) > MAX_CHARS_PER_LINE:
            continue

        # Чем ровнее половины, тем лучше; пунктуация и союз дают бонус.
        score = abs(len(left) - len(right))

        if BREAK_AFTER.search(left[-2:] + " "):
            score -= 30
        elif words[i].lower().strip(".,;:!?") in CONJUNCTIONS:
            score -= 15

        if best_score is None or score < best_score:
            best_index, best_score = i, score

    if best_index is None:
        # Ни один разрыв не даёт две влезающие строки — вернём как есть,
        # длину порежет вызывающий, разбив предложение на несколько реплик.
        return [text]

    return [" ".join(words[:best_index]), " ".join(words[best_index:])]


def _split_balanced(text: str, limit: int) -> list[str]:
    """Порезать на куски не длиннее limit, стараясь сделать их равными.

    Жадная нарезка «набивай до лимита» оставляет огрызок в хвосте: предложение
    в 77 знаков при лимите 76 разваливается на 69 знаков и одно слово. Такая
    реплика мелькает на экране и читается как сбой, даже если формально влезает
    во все ограничения. Поэтому сначала считаем, сколько кусков нужно, а потом
    делим на примерно равные.
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
    """Раздать интервал кускам пропорционально длине текста.

    Пропорционально знакам, а не поровну: короткий кусок, получивший столько же
    времени, сколько длинный, читается как пауза, а длинный — не успевает.
    """
    total = sum(len(c) for c in chunks) or 1
    # Зазоры съедают время, поэтому интервал под текст считаем уже без них.
    gaps = GAP * (len(chunks) - 1)
    span = max(end - start - gaps, MIN_DURATION * len(chunks))
    cues, cursor = [], start

    for chunk in chunks:
        share = span * len(chunk) / total
        duration = min(max(share, MIN_DURATION), MAX_DURATION)

        cues.append(Cue(start=cursor, end=cursor + duration, lines=wrap_lines(chunk)))
        # Зазор уходит в старт следующей реплики, а не вычитается из длительности
        # текущей — иначе клампинг по минимуму отменяется сразу после применения.
        cursor += duration + GAP

    return cues


def build_cues(segments: list[dict]) -> list[Cue]:
    """Собрать реплики из сегментов ASR (start, end, text, words).

    Пословные тайминги используются, чтобы посадить границу предложения на реальную
    паузу между словами, а не на пропорциональную оценку внутри сегмента.
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
            fits = len(sentence) <= MAX_CHARS
            readable = len(sentence) / max(end - start, 1e-6) <= MAX_CPS

            if fits and readable and end - start <= MAX_DURATION:
                cues.append(Cue(start=start, end=end, lines=wrap_lines(sentence)))
                continue

            # Не влезает по знакам или по длительности — режем на несколько реплик
            # и раздаём интервал пропорционально.
            #
            # Скорость чтения сюда намеренно не входит. Если говорящий произнёс
            # 76 знаков за 3 секунды, то разбиение оставит те же 26 знаков в секунду,
            # только реплики начнут сменяться вдвое чаще — станет хуже, а не лучше.
            # Единственный рычаг против высокого CPS — укоротить текст, и он находится
            # в переводе: валидатор помечает такие реплики, и модель их сокращает.
            needed = max(
                len(sentence) / MAX_CHARS,
                (end - start) / MAX_DURATION,
            )
            limit = max(int(len(sentence) / max(needed, 1.0)) + 1, MAX_CHARS_PER_LINE)
            cues += _allocate(_split_balanced(sentence, min(limit, MAX_CHARS)), start, end)

    return _dedupe_overlaps(_enforce_min_duration(cues))


def _enforce_min_duration(cues: list[Cue]) -> list[Cue]:
    """Убрать реплики короче минимума: сначала объединением, потом растягиванием.

    Короткие фразы вроде «Да.» и «Нет.», сказанные подряд, приходят прямым путём
    в обход разбиения, где минимум уже соблюдается. Показывать их по полсекунды
    нельзя — они мелькают и не читаются. Растянуть обе тоже не выйдет: между ними
    нет столько времени. Правильное решение то же, что у живых субтитровщиков, —
    объединить в одну реплику, если объединённый текст влезает в две строки.
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

    # Что не удалось объединить — растягиваем в паузу до следующей реплики.
    for index, cue in enumerate(merged):
        if cue.duration >= MIN_DURATION:
            continue

        limit = merged[index + 1].start - GAP if index + 1 < len(merged) else cue.end + MAX_DURATION
        cue.end = min(max(cue.end, cue.start + MIN_DURATION), max(limit, cue.end))

    return merged


def _sentence_spans(
    sentences: list[str], words: list[dict], start: float, end: float
) -> list[tuple[float, float]]:
    """Сопоставить предложениям интервалы времени по пословным таймингам."""
    if not words or len(sentences) == 1:
        return _proportional_spans(sentences, start, end)

    spans, cursor = [], 0

    for index, sentence in enumerate(sentences):
        # Слова могут кончиться раньше предложений: пословный список бывает короче
        # текста сегмента, потому что счёт идёт по знакам, а знаки препинания и
        # пробелы распределены между словами не так, как в исходной строке.
        # Остаток раскладываем пропорционально внутри оставшегося интервала.
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
    """Не дать соседним репликам наехать друг на друга.

    Перекрытие означает, что две реплики нарисуются одновременно, а поиск активной
    реплики по currentTime вернёт произвольную из них.
    """
    for previous, current in zip(cues, cues[1:]):
        if previous.end > current.start - GAP:
            # Пол — собственное начало реплики, а не MIN_DURATION/2. Прежний вариант
            # брал больший из двух вариантов, поэтому при тесно идущих репликах
            # выигрывал пол, и перекрытие, ради устранения которого функция и нужна,
            # оставалось на месте. Заодно он нарушал минимум, только что выставленный
            # _enforce_min_duration, вдвое — и восстановить его здесь нельзя: тянуть
            # реплику за начало следующей значит вернуть перекрытие.
            previous.end = max(current.start - GAP, previous.start)

    # Реплики, схлопнувшиеся в ноль (следующая начинается раньше, чем через GAP),
    # на таймлайне бесполезны: показать их всё равно невозможно.
    return [cue for cue in cues if cue.end > cue.start]
