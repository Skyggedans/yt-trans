"""Коды языков, правило определителя и решение о языке, общие для демона и воркера распознавания.

Только стандартная библиотека. Модуль грузят и демон, и воркер, а faster_whisper
тянет ctranslate2, av и tokenizers, которым в процессе демона делать нечего: список
кодов Whisper поэтому скопирован сюда, а не импортирован. Что сторонних импортов тут
не появится, сторожит test_language_module_imports_only_the_standard_library.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Ровно сто кодов faster-whisper 1.2.1 (faster_whisper.tokenizer._LANGUAGE_CODES).
# Копия, а не импорт: имя закрытое, а импорт втянул бы в демон весь faster_whisper.
# Разойдётся с установленной версией — упадёт тест.
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

# Написания, которых Whisper не знает, но которые значат его код. sh, me и cnr —
# сербский под другими именами; остальные — старые или альтернативные коды ISO,
# которыми пользуются YouTube и таблицы языков.
SUBSTITUTES: dict[str, str] = {
    "sh": "sr", "me": "sr", "cnr": "sr",
    "iw": "he", "jv": "jw", "fil": "tl", "nb": "no", "in": "id",
}

# «Языка нет»: пусто и auto в config.toml, und, zxx, mul и mis из ISO 639.
NO_LANGUAGE: frozenset[str] = frozenset({"", "auto", "und", "zxx", "mul", "mis"})

# Три токена Whisper для одного языка. bs, hr и sr попали в его обучение из VoxLingua107
# по недосмотру, и выбор между ними произволен; family() сводит их к sr.
BCMS: frozenset[str] = frozenset({"sr", "hr", "bs"})


class UnknownLanguage(ValueError):
    """Код, которого Whisper не знает и который не сводится ни к одному его коду."""

    def __init__(self, raw: str) -> None:
        super().__init__(raw)
        self.raw = raw


def normalize(raw: str | None) -> str | None:
    """Код Whisper для написания языка; None — «языка нет» (пусто, auto, und и подобные).

    Регион, письменность и пометки дорожек отбрасываются: «SR», «sr_RS» и «sr-Latn» —
    это sr, «en-orig» — en. yt-dlp склеивает языки дорожек через «+»: такое значение
    годится, только если все части сводятся к одному коду. hr и bs остаются собой —
    это настоящие коды Whisper, а свести их к sr умеет лишь family().

    Незнакомое написание — UnknownLanguage с ИСХОДНОЙ строкой: отказ должен назвать то,
    что написал человек, а не то, во что мы его превратили.
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
    """normalize(), но незнакомое написание — None, а не исключение.

    Для меток YouTube: незнакомое значение там значит просто «не знаем», и задание
    из-за него падать не должно.
    """
    try:
        return normalize(raw)
    except UnknownLanguage:
        return None


def family(code: str | None) -> str | None:
    """hint(), где hr и bs сведены к sr: для сравнения языков, не для декодера.

    Три токена Whisper — один язык, и «hr против sr» расхождением не считается.
    Декодеру же отдают код как есть (normalize), а не семью.
    """
    resolved = hint(code)

    return "sr" if resolved in BCMS else resolved


def is_bcms(code: str | None) -> bool:
    """Это сербский, хорватский или боснийский — под любым из написаний."""
    return family(code) == "sr"


def refusal(raw: str) -> str:
    """Фраза отказа для негодного кода: что не так — первым, где исправить — следом.

    Шаблон закреплён тестом, и резать его не надо. С «serbian» фраза занимает 56 знаков
    и умещается на бейдж; значение длиннее 11 знаков выводит её за 60 («serbo-croatian» —
    63): бейдж обрежет хвост многоточием, полная фраза останется в подсказке, а главное,
    «неизвестный язык «…»», стоит первым.
    """
    return f"неизвестный язык «{raw}»: [asr] language в config.toml"


# ── Определитель: окна, правило, решение ─────────────────────────────────────────

# Константы определителя. Любая из них меняет распознавание уже закэшированных
# роликов, поэтому правка требует поднять helper.cache.PROMPT_VERSION.

# Окно, в котором речи меньше, не голосует: тишина, музыка и заставка дают
# распределение по набивке, и такое окно голосовало бы наравне с речью.
MIN_SPEECH_SECONDS = 3.0

# Доля сербского семейства, с которой определитель решает «сербский», даже если
# сверху другой язык. На сербской речи Whisper тянет к английскому: 3XJLfTSZy6I
# дал en 0.55 против 0.44 у семьи; на английском ролике семьи ноль в каждом окне.
BCMS_FLOOR = 0.2

# Соседи, у которых сербского семейства бывает много по родству: порог их не
# перебивает, иначе македонская или русская речь распознавалась бы сербской.
SLAVIC_NEIGHBOURS = frozenset({"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"})

# Whisper слушает 16 кГц mono, окнами по тридцать секунд.
SAMPLE_RATE = 16000
WINDOW_SAMPLES = 30 * SAMPLE_RATE


def probe_offsets(
    n_samples: int,
    *,
    windows: int = 8,
    span: tuple[float, float] = (0.10, 0.90),
    window: int = WINDOW_SAMPLES,
) -> list[int]:
    """Начала окон определителя в записи из n_samples отсчётов, по возрастанию.

    Окна лежат внутри span длины: первое начинается на 10%, последнее кончается на 90%,
    остальные — поровну между ними. С начала брать нельзя — там заставка, музыка или
    реклама спонсора, и язык определился бы по ним. Замерено на этом корпусе: у
    KCR-dkZIhxY первая реплика начинается на 21-й секунде, то есть первое
    тридцатисекундное окно на две трети состояло бы из заставки.

    Начала прижаты к [0, n_samples - window]: на записи от 30 до 37,5 с окна выходят
    целыми и перекрываются, а не уходят в минус, и совпавшие начала сливаются в одно.
    Запись не длиннее окна — одно окно с начала.
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
    """Что определитель услышал в окнах записи и какой язык из этого следует.

    language — победившая семья (hr и bs сведены к sr) или None, если не проголосовало
    ни одно окно; confidence — её средняя доля по голосующим окнам; bcms_share — средняя
    доля сербского семейства; floor — сербский взят порогом BCMS_FLOOR, а не первым
    местом; votes — сколько окон голосовало; probed — сколько окон слушали всего.
    """

    language: str | None
    confidence: float | None
    bcms_share: float
    floor: bool
    votes: int
    probed: int
    # По окну: верхняя семья или None (окно не голосовало), её доля, секунды речи.
    windows: tuple[tuple[str | None, float, float], ...]


def summarize(windows: Sequence[tuple[float, Sequence[tuple[str, float]]]]) -> Detection:
    """Правило определителя: окна (секунды речи, распределение Whisper) → Detection.

    Распределение окна сводится по семье: доли bs, hr и sr складываются под sr, код,
    которого Whisper не знает, пропускается. Голосует окно, где речи не меньше
    MIN_SPEECH_SECONDS и есть ответ определителя. Доли семей усредняются по голосующим
    окнам — распределения, а не голоса top-1: жёсткое голосование разрывало бы сербский
    на три метки. Побеждает наибольшее среднее; сербское семейство со средней долей от
    BCMS_FLOOR берёт верх и не будучи первым, кроме как над соседями из SLAVIC_NEIGHBOURS.
    """
    records: list[tuple[str | None, float, float]] = []
    voting: list[dict[str, float]] = []

    for speech, distribution in windows:
        shares: dict[str, float] = {}

        for code, share in distribution:
            folded = family(code)

            if folded is not None:
                shares[folded] = shares.get(folded, 0.0) + share

        # Пустое распределение — определитель на окне упал или его не звали.
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
        # При пороге это и есть bcms_share.
        confidence=means[winner],
        bcms_share=bcms_share,
        floor=floor,
        votes=len(voting),
        probed=len(windows),
        windows=tuple(records),
    )


@dataclass(frozen=True)
class LanguageVerdict:
    """Решение о языке источника: на каком языке реплики и откуда это известно.

    effective — код Whisper, с которым распознавали (у дорожки автора — язык её метки);
    declared — что объявлено до звука, declared_by — кем: "config" (закрепление хозяина),
    "metadata" (метка YouTube) или None; detected и confidence — что услышал определитель
    (None — речи не нашлось или её не слушали); reason — откуда effective: "pin",
    "detector", "floor", "hint", "fallback", у дорожки автора — "track"; source — "asr"
    или "manual" (дорожка автора), track — её метка; verified — сверена ли дорожка
    с речью (None — не сверяли).
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
        """Объявленное разошлось с услышанным, а у дорожки автора — с её языком.

        Сравниваются семьи: hr против sr — не расхождение. Неизвестное с любой стороны
        (не объявлено, речи не нашлось) — тоже.
        """
        declared = family(self.declared)
        heard = family(self.effective if self.source == "manual" else self.detected)

        return declared is not None and heard is not None and declared != heard

    def note(self) -> str | None:
        """Фраза для зрителя и журнала, до 60 знаков; None — сказать нечего."""
        if self.mismatch and self.declared_by == "metadata":
            if self.source == "manual":
                return f"YouTube указал {self.declared}, дорожка автора {self.effective}"

            return f"YouTube указал {self.declared}, по речи {self.detected}"

        if self.mismatch and self.declared_by == "config":
            if self.source == "manual":
                return f"в настройках {self.declared}, дорожка автора {self.effective}"

            return f"в настройках {self.declared}, по речи {self.detected}"

        if self.reason == "hint":
            return f"речи почти нет, язык взят у YouTube: {self.effective}"

        if self.reason == "fallback":
            return f"речи почти нет, язык принят за {self.effective}"

        return None

    def wire(self) -> dict:
        """Решение для ответа воркера, снимка и кэша: mismatch и note — вычисленные."""
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
        """Решение из wire(); None — это не решение. Никогда не бросает.

        Читает и ответ воркера, и запись кэша, сделанную, может быть, прежней версией.
        Без годного effective решения нет: код сверяется как есть, без приведения
        («SR» и «sh» — не решение). Остальное терпимо — негодное поле становится
        умолчанием. mismatch и note не читаются: их всегда вычисляют заново.
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

        # bool — подкласс int, но не доля.
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            try:
                value = float(raw)
            except OverflowError:
                pass  # целое больше любого float: доли такой не бывает
            else:
                # NaN и бесконечность в JSON не годятся: JSONResponse на проводе падал
                # бы на каждом ответе этого ролика, а запись кэша их несёт.
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
    """Язык распознавания: закрепление хозяина, иначе речь, иначе метка YouTube, иначе fallback.

    pin и hint — уже коды Whisper: их приводят вызывающие (normalize и hint). Закрепление
    решает всегда, и определитель его только проверяет. Без закрепления решает речь, если
    проголосовало хоть одно окно: подсказка голосующие окна не перевешивает (Juske — метка
    en, речь сербская). Метка решает, только когда речи почти нет. Пустым и None язык не
    бывает никогда: с ним Whisper определял бы язык сам, по первым тридцати секундам.
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
