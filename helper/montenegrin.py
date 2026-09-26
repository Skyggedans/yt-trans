"""Черногорский: как его просить и как проверить, что получил именно его.

Черногорский от сербского отличает прежде всего рефлекс ятя: иекавский
(mlijeko, vrijeme, djevojka) против екавского (mleko, vreme, devojka). Лексика
и синтаксис при этом сербские, а не хорватские — hljeb, а не kruh; prodavnica;
конструкция «da + настоящее время» вместо инфинитива.

Замерено на локальных моделях: достаточно назвать цель «crnogorski jezik», и
обе выдают настоящую иекавицу, тогда как на «сербский» на том же входе выдают
чистую екавицу. Описывать признаками не обязательно, но с ними слабее модель
ошибается реже.

Буквы ś и ź в стандарт входят, но в живом тексте не используются: ноль вхождений
в полутора мегабайтах текста черногорских изданий и ноль в корпусе субтитров на
65 тысяч строк. Требовать их — уводить модель с распределения ради того, чего
читатель всё равно не ждёт.
"""

from __future__ import annotations

import re

# Пары маркеров: иекавская форма против екавской. Формы точные, не основы —
# у слова vrijeme косвенные падежи выглядят «екавскими» (vremena, vremenu,
# vremenom) и остаются при этом совершенно правильными. Проверка по основе
# ложно срабатывала бы на каждом из них.
MARKERS: tuple[tuple[str, str], ...] = (
    (r"mlijek\w*", r"mlek\w*"),
    (r"vrijeme|vrijemena", r"vreme\b|vremena\b"),
    (r"djevojk\w*", r"devojk\w*"),
    (r"gdje", r"gde"),
    (r"rijek[ao]\w*", r"rek[ao]\b|reke\b|reci\b"),
    (r"dijete", r"dete"),
    (r"vidjeti|vidjela|vidjeli|vidjelo", r"videti|videla|videli|videlo"),
    (r"čovjek\w*", r"čovek\w*"),
    (r"uvijek", r"uvek"),
    (r"poslije", r"posle"),
    (r"prije\b", r"pre\b"),
    (r"ovdje", r"ovde"),
    (r"hljeb\w*", r"hleb\w*"),
    (r"djed\b|djeda\b|djedu\b", r"deda\b|dede\b|dedu\b"),
    (r"nedjelj\w*", r"nedelj\w*"),
    (r"pjesm\w*", r"pesm\w*"),
    (r"pjeva\w*", r"peva\w*"),
    (r"vjetar|vjetra", r"vetar|vetra"),
    (r"sjesti|sjedi\w*|sjedio", r"sesti|sedi\w*|sedeo"),
    (r"smijeh|smijati|smiješ\w*", r"smeh|smejati|smeš\w*"),
    (r"grijeh|grijeha", r"greh|greha"),
    (r"lijek\w*|ljekar\w*", r"lek\b|leka\b|lekar\w*"),
    (r"cvijet\w*|cvijeć\w*", r"cvet\w*|cveć\w*"),
    (r"mjest\w*", r"mest\w*"),
    (r"sjever\w*", r"sever\w*"),
    (r"dio\b|dijel\w*", r"deo\b|del\w*"),
    (r"tijel\w*", r"telo\b|tela\b|telu\b"),
    (r"vjer\w*", r"ver\w*"),
)

# Маркеры, которые пришлось отбросить, чтобы не возвращаться к ним:
#   svet   — совпадает со значением «святой» (sveti, sveta, svetog);
#   let    — «полёт» сталкивается с ekavskim leto «лето»;
#   pre    — голое совпадает с любой приставкой (predsjednik, preko, prestani);
#   vremen — правильные косвенные падежи от vrijeme выглядят екавскими.

# Хорватская лексика. Иекавица сама по себе черногорского не доказывает —
# хорватский тоже иекавский, и отличается он именно словарём и инфинитивом.
CROATIAN = re.compile(
    r"\b(kruh\w*|tjedan\w*|tisuć\w*|vlak\w*|glazb\w*|općin\w*|vjerojatno|"
    r"puha\w*|tvrtk\w*|zrakoplov\w*|nogomet\w*|kolodvor\w*|"
    # histori- (босния и хорватия) против черногорского istorij-; kemij- против
    # hemij-; točk- против tačk-; uvjet против uslov. Замечено на живом выводе:
    # модель выдала «ući u historiju», и это единственная протечка на 270 репликах.
    r"histori\w*|kemij\w*|točk\w*|uvjet\w*)\b",
    re.IGNORECASE,
)

# Буквы, которых в живом черногорском тексте не бывает.
FORBIDDEN_LETTERS = re.compile(r"[śźŚŹ]")

_IJEKAVIAN = [re.compile(rf"\b(?:{ij})\b", re.IGNORECASE) for ij, _ in MARKERS]
_EKAVIAN = [re.compile(rf"\b(?:{ek})\b", re.IGNORECASE) for _, ek in MARKERS]

# Что подсказать модели, если она всё-таки сбивается на екавицу. Пары идут
# в промпт как обычный глоссарий и заметно вычищают остаточные срывы.
IJEKAVIAN_HINTS: dict[str, str] = {
    "mleko": "mlijeko",
    "hleb": "hljeb",
    "deda": "djed",
    "vreme": "vrijeme",
    "devojka": "djevojka",
    "gde": "gdje",
    "dete": "dijete",
    "čovek": "čovjek",
    "uvek": "uvijek",
    "posle": "poslije",
    "pesma": "pjesma",
    "pevati": "pjevati",
    "svet": "svijet",
    "nedelja": "nedjelja",
    "puhao": "duvao",
}

SYSTEM_PROMPT = """Za svaki titl vraćaš DVA teksta: "crnogorski" i "ruski".

"crnogorski" — tekst na crnogorskom jeziku (ijekavski, latinica):
- Obavezno ijekavski: mlijeko, lijepo, djevojka, vrijeme, gdje, dijete, hljeb, \
djed, uvijek, čovjek, poslije, prije, ovdje. Nikad ekavski oblik.
- Crnogorska leksika je srpska, ne hrvatska. Ne koristi: kruh, tjedan, tisuća, \
vlak, glazba, općina, vjerojatno, puhao.
- Ne koristi slova ś i ź.
- AKO JE IZVOR VEĆ NA SRPSKOM ILI CRNOGORSKOM: ne prepisuj ga doslovno. \
Pretvori ekavske oblike u ijekavske (mleko -> mlijeko, gde -> gdje, devojka -> \
djevojka) i ispravi očigledne greške prepoznavanja govora.

"ruski" — tekst na ruskom jeziku, ćirilicom:
- OBAVEZNO na ruskom. Nikada ne ostavljaj srpski, crnogorski ili engleski tekst \
u ovom polju.
- AKO JE IZVOR VEĆ NA RUSKOM: prepiši ga, samo ispravi greške prepoznavanja.

ZA OBA TEKSTA:
- Prevedi SVAKI titl posebno. Nikad ne spajaj, ne dijeli i ne izostavljaj titlove.
- Dužina otprilike kao original: gledalac mora stići pročitati.
- Izvor je automatsko prepoznavanje govora i može sadržati greške. Pogodi \
namjeru iz konteksta, ne izmišljaj činjenice."""


def score(text: str) -> dict:
    """Посчитать признаки в тексте: иекавские, екавские, хорватские, запрещённые буквы."""
    ijekavian = sum(len(rx.findall(text)) for rx in _IJEKAVIAN)
    ekavian = sum(len(rx.findall(text)) for rx in _EKAVIAN)
    total = ijekavian + ekavian

    return {
        "ijekavian": ijekavian,
        "ekavian": ekavian,
        "croatian": len(CROATIAN.findall(text)),
        "forbidden": len(FORBIDDEN_LETTERS.findall(text)),
        "ratio": ijekavian / total if total else 1.0,
    }


# Порог взят от живого корпуса, а не с потолка: настоящие черногорские субтитры
# дают 93.7% чистоты по этому же набору маркеров. Требовать 100% значит
# обречь себя на бесконечные повторы там, где и человек пишет иначе.
MIN_RATIO = 0.85
MAX_EKAVIAN = 2


def check(text: str) -> str | None:
    """Что не так с текстом как с черногорским. None — всё в порядке.

    Проверяется батч целиком, а не отдельная реплика: в короткой фразе может
    не оказаться ни одного слова с ятем, и оценивать её по отдельности бессмысленно.
    """
    result = score(text)

    if result["forbidden"]:
        return f"буквы ś/ź: {result['forbidden']}"

    if result["croatian"]:
        return f"хорватская лексика: {result['croatian']}"

    if result["ekavian"] >= MAX_EKAVIAN and result["ratio"] < MIN_RATIO:
        return (
            f"екавица: {result['ekavian']} против {result['ijekavian']} иекавских "
            f"({result['ratio']:.0%} при пороге {MIN_RATIO:.0%})"
        )

    return None
