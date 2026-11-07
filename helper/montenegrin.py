"""Montenegrin: how to ask for it and how to verify that it's exactly what you got.

What distinguishes Montenegrin from Serbian is above all the reflex of yat: ijekavian
(mlijeko, vrijeme, djevojka) versus ekavian (mleko, vreme, devojka). The lexis and
syntax are meanwhile Serbian, not Croatian — hljeb, not kruh; prodavnica; the
construction "da + present" instead of the infinitive.

Measured on local models: it's enough to name the goal "crnogorski jezik", and both
produce genuine ijekavica, whereas for "Serbian" on the same input they produce pure
ekavica. Describing it with features isn't mandatory, but with them a weaker model errs
less often.

The letters ś and ź are part of the standard but aren't used in living text: zero
occurrences in a megabyte and a half of text from Montenegrin publications and zero in
a subtitle corpus of 65 thousand lines. Requiring them is steering the model away from
the distribution for the sake of something the reader doesn't expect anyway.
"""

from __future__ import annotations

import re

# Marker pairs: ijekavian form versus ekavian. The forms are exact, not stems —
# for the word vrijeme the oblique cases look "ekavian" (vremena, vremenu,
# vremenom) and remain perfectly correct. Checking by stem would falsely trigger on
# every one of them.
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

# Markers that had to be discarded so as not to come back to them:
#   svet   — coincides with the meaning "holy" (sveti, sveta, svetog);
#   let    — "flight" collides with ekavian leto "summer";
#   pre    — bare coincides with any prefix (predsjednik, preko, prestani);
#   vremen — the correct oblique cases of vrijeme look ekavian.

# Croatian lexis. Ijekavica by itself doesn't prove Montenegrin —
# Croatian is ijekavian too, and it differs precisely in vocabulary and the infinitive.
CROATIAN = re.compile(
    r"\b(kruh\w*|tjedan\w*|tisuć\w*|vlak\w*|glazb\w*|općin\w*|vjerojatno|"
    r"puha\w*|tvrtk\w*|zrakoplov\w*|nogomet\w*|kolodvor\w*|"
    # histori- (Bosnia and Croatia) versus Montenegrin istorij-; kemij- versus
    # hemij-; točk- versus tačk-; uvjet versus uslov. Noticed on live output:
    # the model produced "ući u historiju", and it was the only leak in 270 utterances.
    r"histori\w*|kemij\w*|točk\w*|uvjet\w*)\b",
    re.IGNORECASE,
)

# Letters that don't occur in living Montenegrin text.
FORBIDDEN_LETTERS = re.compile(r"[śźŚŹ]")

_IJEKAVIAN = [re.compile(rf"\b(?:{ij})\b", re.IGNORECASE) for ij, _ in MARKERS]
_EKAVIAN = [re.compile(rf"\b(?:{ek})\b", re.IGNORECASE) for _, ek in MARKERS]

# What to hint to the model if it does slip into ekavian. The pairs go
# into the prompt as an ordinary glossary and noticeably clean up residual slips.
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
    """Count features in the text: ijekavian, ekavian, Croatian, forbidden letters."""
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


# The threshold is taken from a live corpus, not from thin air: real Montenegrin
# subtitles give 93.7% purity by this same set of markers. Demanding 100% means dooming
# yourself to endless retries where even a human writes differently.
MIN_RATIO = 0.85
MAX_EKAVIAN = 2


def check(text: str) -> str | None:
    """What's wrong with the text as Montenegrin. None — everything is fine.

    The batch is checked as a whole, not an individual utterance: a short phrase may
    contain no word with yat, and evaluating it separately is meaningless.
    """
    result = score(text)

    if result["forbidden"]:
        return f"letters ś/ź: {result['forbidden']}"

    if result["croatian"]:
        return f"Croatian lexis: {result['croatian']}"

    if result["ekavian"] >= MAX_EKAVIAN and result["ratio"] < MIN_RATIO:
        return (
            f"ekavica: {result['ekavian']} versus {result['ijekavian']} ijekavian "
            f"({result['ratio']:.0%} at a threshold of {MIN_RATIO:.0%})"
        )

    return None
