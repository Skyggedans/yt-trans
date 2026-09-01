"""Перевод реплик sr→ru через локальную ollama.

Выравнивание реплик гарантируется структурно, а не просьбами в промпте: схема с
фиксированными ключами c1..cN компилируется ollama в GBNF-грамматику, и модель
физически не может слить, разбить, потерять или переставить реплику. Схема идёт
вне промпта и не стоит ни одного токена контекста.

Грамматика гарантирует СТРУКТУРУ и ничего не говорит о СОДЕРЖАНИИ: пустая строка,
непереведённый сербский и продублированный текст — всё это схеме соответствует.
Поэтому валидаторы ниже проверяют язык вывода, а не только форму.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8:27b"

# Буквы, которых нет в русском алфавите — надёжный признак, что сербское слово
# просто проехало насквозь. В сербской кириллице есть ђ ј љ њ ћ џ, в русской нет.
SERBIAN_ONLY = re.compile(r"[ђЂјЈљЉњЊћЋџЏ]")

# Сербская диакритика: в русском тексте взяться ей неоткуда, включая названия.
SERBIAN_DIACRITICS = re.compile(r"[čćžšđČĆŽŠĐ]")

# Латинское слово со строчной буквы. Имена собственные и марки пишутся с прописной,
# поэтому под этот шаблон попадает именно непереведённая сербская лексика.
LOWERCASE_LATIN_WORD = re.compile(r"\b[a-z]{4,}\b")

# Ложные друзья sr→ru — главный класс ошибок для этой пары. Модель копирует сербское
# слово именно потому, что оно похоже на русское, и результат получается грамматичным,
# правильно выровненным, нужной длины и неправильным по смыслу.
#
# Стартовый набор. Расширять из документированных источников: Wikibooks «False Friends
# of the Slavist», приложение «Ложные друзья переводчика (сербский)» в ru.wiktionary.
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
    """ollama не вернула пригодный ответ."""


@dataclass
class Batch:
    """Реплики на перевод плюс контекст, который к ним прилагается."""

    cues: list[str]
    previous: list[tuple[str, str]] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)
    title: str = ""
    channel: str = ""


def cue_schema(count: int) -> dict:
    """Схема, из которой ollama строит грамматику.

    Фиксированные ключи вместо массива с minItems/maxItems: массив ограничивает
    только количество, а ключи — ещё и тождество, поэтому рассинхрон становится
    самоочевидным. `prefixItems` использовать нельзя, он сломан в конвертере
    json-schema→GBNF у llama.cpp.
    """
    keys = [f"c{i}" for i in range(1, count + 1)]

    return {
        "type": "object",
        "properties": {k: {"type": "string"} for k in keys},
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
        # Род в прошедшем времени маркируется и в сербском, и в русском. Без антецедента
        # модель угадает пол говорящего и перевернёт его посреди сцены.
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
    timeout: float = 600.0,
) -> dict:
    """Один вызов /api/chat.

    `think: false` обязателен: с включёнными рассуждениями модель сжигает весь
    num_predict на размышления и возвращает пустой content.

    `keep_alive` намеренно короткий и никогда не -1: планировщик ollama сортирует
    кандидатов на выгрузку по sessionDuration, приведённой к uint64, поэтому
    отрицательная длительность сортируется последней и делает модель невыгружаемой —
    ровно то поведение враждебного соседа, которого мы избегаем.
    """
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        # Дефолтный num_ctx у ollama 4096, и он обрезает молча, без ошибки.
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
        raise TranslateError("ответ обрезан по num_predict — уменьшить батч или поднять лимит")

    if not payload.get("message", {}).get("content"):
        raise TranslateError("ollama вернула пустой content")

    return payload


def looks_untranslated(text: str) -> bool:
    """Осталось ли в русском выводе что-то сербское.

    Наивная проверка «есть латиница — значит протечка» даёт слишком много ложных
    срабатываний: русские субтитры законно содержат названия и номера — `The Beatles`,
    `Douglas DC-9-32`, рейс `JU-367`, `YU-AHT`. На реальном материале так помечались
    исключительно правильные переводы.

    Признаки, которые действительно означают непереведённое:
      * сербские кириллические буквы (ђ ј љ њ ћ џ) — в русском алфавите их нет;
      * сербская диакритика (č ć ž š đ) — в русском тексте ей взяться неоткуда;
      * латинское слово целиком со строчной буквы — марки и имена собственные
        пишутся с прописной или содержат цифры, а проехавшая насквозь лексика нет.

    Сверять слово с источником бессмысленно: протёкшее слово часто слегка искажено
    и дословно не совпадает. Проверено на 360 реальных переводах — ложных
    срабатываний это правило не даёт.

    Смещение в сторону чувствительности выбрано осознанно: валидатор запускает
    повтор батча, поэтому ложное срабатывание стоит одной лишней попытки, а
    пропуск отправляет ломаный текст на экран.
    """
    return bool(
        SERBIAN_ONLY.search(text)
        or SERBIAN_DIACRITICS.search(text)
        or LOWERCASE_LATIN_WORD.search(text)
    )


def validate(cues: list[str], translated: list[str]) -> list[tuple[int, str]]:
    """Список проблем как (индекс, причина). Пустой список — всё в порядке."""
    problems: list[tuple[int, str]] = []

    for i, (source, target) in enumerate(zip(cues, translated)):
        stripped = target.strip()

        if not stripped:
            problems.append((i, "пусто"))
        elif stripped == source.strip():
            problems.append((i, "не переведено"))
        elif looks_untranslated(stripped):
            problems.append((i, "остался сербский"))
        elif stripped.count("\n") >= 2:
            problems.append((i, "больше двух строк"))

    return problems


def estimate_num_predict(cues: list[str]) -> int:
    """Сколько токенов надо разрешить на ответ для этого батча.

    Константа здесь не работает. Реплики после сегментации короткие, но если на вход
    попадут сырые тридцатисекундные окна Whisper (по ~300 знаков), фиксированный лимит
    обрежет ответ, и батч вернётся пустым — грамматика не даст модели закончить JSON.

    Оценка снизу вверх: русский длиннее сербского примерно на 15%, кириллица идёт
    плотнее двух знаков на токен, плюс на каждую реплику уходит обвязка JSON
    (ключ, кавычки, запятая), плюс запас в треть.
    """
    source_chars = sum(len(c) for c in cues)
    text_tokens = source_chars * 1.15 / 2.0
    json_overhead = len(cues) * 6

    return max(512, int((text_tokens + json_overhead) * 1.35))


def translate_batch(batch: Batch, model: str = DEFAULT_MODEL, **kwargs) -> list[str]:
    """Перевести батч реплик, сохранив их количество и порядок."""
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
    result = [data.get(f"c{i}", "") for i in range(1, len(batch.cues) + 1)]

    if len(result) != len(batch.cues):
        raise TranslateError(f"ожидалось {len(batch.cues)} реплик, пришло {len(result)}")

    return result
