"""Коды языков, общие для демона и воркера распознавания.

Только стандартная библиотека. Модуль грузят и демон, и воркер, а faster_whisper
тянет ctranslate2, av и tokenizers, которым в процессе демона делать нечего: список
кодов Whisper поэтому скопирован сюда, а не импортирован. Что сторонних импортов тут
не появится, сторожит test_language_module_imports_only_the_standard_library.
"""

from __future__ import annotations

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
