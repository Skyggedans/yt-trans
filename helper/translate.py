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


def dual_cue_schema(count: int) -> dict:
    """Схема на два языка сразу: {c1: {crnogorski, ruski}, …}.

    Вложенные объекты собираются руками, а не через Pydantic. Это не вкусовщина,
    а свойство безопасности: model_json_schema() всегда выносит вложенные модели
    в $ref/$defs, а на них конвертер схемы в грамматику у llama.cpp разворачивает
    всё inline, упирается в бюджет правил и **отказывает молча** — возвращает 200
    и генерирует без ограничений вообще. Гарантия числа реплик при этом исчезает
    без единой ошибки в логе.

    Имена внутренних ключей выбраны не для красоты. Под ограниченным декодированием
    грамматика выписывает их сама, модель не может ни пропустить их, ни перефразировать,
    и каждый оказывается в контексте прямо перед своим полем — то есть работает
    как неснимаемая пометка языка на каждой реплике.

    Порядок ключей сохраняется до самой грамматики, поэтому черногорский идёт
    первым намеренно: так он переводится с оригинала. Стой он вторым, между
    источником и ним оказался бы русский текст, и получилась бы калька с русского —
    ровно то, чего мы избегаем.
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
    # Десять минут были не запасом, а ловушкой. Замерено: батч из сорока реплик
    # с обеими строками занимает 24-43 секунды, так что до таймаута дело доходит
    # только когда что-то пошло не так, — и тогда конвейер стоял ДЕСЯТЬ МИНУТ,
    # молча, не прислав ни одного события. Зритель видел замерший счётчик и решал,
    # что связь оборвалась. Четыре минуты — всё ещё шестикратный запас к худшему
    # замеру, но срыв теперь уходит в дробление батча за разумное время.
    timeout: float = 240.0,
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


def translate_dual(batch: Batch, model: str = DEFAULT_MODEL, **kwargs) -> list[tuple[str, str]]:
    """Перевести батч сразу на черногорский и русский за один проход.

    Один проход, а не два: вдвое дешевле по времени, и русская строка получается
    глоссой именно к этой черногорской, а не независимым переводом. Для учебного
    режима это и нужно — зритель сопоставляет строки слово к слову.

    Глоссарий ложных друзей sr→ru сюда не попадает намеренно: его ключи екавские
    (svet, nedelja, devojka), и в промпте с черногорской целью они тянут модель
    обратно к екавице. Вместо него идут иекавские подсказки.
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
        # В контекст уходит только черногорская сторона. Екавские примеры
        # в контексте разворачивают модель обратно к екавице — замерено:
        # у qwen срываются все десять реплик из десяти.
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


# Окно поиска: на сколько реплик вперёд и назад мог уехать текст. Замерено —
# на живом материале дрейф доходил до пяти позиций, но за четыре берём с запасом:
# шире окно, больше шансов на случайное совпадение у коротких реплик.
DRIFT_WINDOW = 4

# Реплики короче трёх знаменательных слов не оцениваем: «Da.», «Evo sad.»,
# «Izazovan za istraživanje.» совпадают с чем угодно, и голосовать им нельзя.
MIN_STEMS = 3

# Насколько увереннее чужая позиция должна быть своей, чтобы считать это дрейфом.
DRIFT_MARGIN = 0.34

# Доля съехавших реплик, после которой батч бракуется. Замерено на 160 настоящих
# репликах: у qwen3.8:27b — 0 из 122 оценённых на четырёх батчах, у translategemma:12b
# на тех же батчах 11%, 32%, 50% и 81%. Между нулём и одиннадцатью процентами
# места хватает, и порог тут — не подгонка под один прогон, а разделение двух
# качественно разных поведений: модель либо держит соответствие один к одному,
# либо не держит вовсе.
DRIFT_RATIO = 0.10


def _stems(text: str) -> set[str]:
    """Знаменательные слова, огрублённые до основы.

    Четыре знака, потому что сербский флективен: pogledom и pogleda это одно
    слово, и различать их здесь значит не находить очевидных соответствий.
    """
    return {w[:4] for w in re.findall(r"\w+", text.lower()) if len(w) >= 4}


def _coverage(source: str, output: str) -> float:
    """Какая доля слов исходной реплики нашлась в этом выводе."""
    stems = _stems(source)

    return len(stems & _stems(output)) / len(stems) if stems else 0.0


def alignment_shifted(sources: list[str], outputs: list[str]) -> bool:
    """Разъехался ли вывод с источником: реплики слиты, сдвинуты или потеряны.

    Грамматика гарантирует, что ключей вернётся ровно N. Она НЕ гарантирует,
    что ключ cN относится к исходной реплике N: модель может слить две соседние
    в одну и сдвинуть весь хвост, оставив последние огрызками. Субтитр при этом
    показывает текст соседней реплики и выглядит совершенно правдоподобно.

    Ищем по содержанию, а не по длинам. Прежняя версия сравнивала корреляцию
    длин при прямом сопоставлении и при сдвиге ровно на одну позицию — и на
    настоящем материале не срабатывала НИ РАЗУ, включая батчи, где съехало
    четыре пятых реплик. Две причины. Сдвиг там не на одну позицию, а
    накопительный: слияния идут одно за другим, и хвост уезжает всё дальше.
    И длина — слишком слабый признак: у соседних реплик она похожа, так что
    корреляция почти не различает верное сопоставление и смещённое.

    Содержание таким недостатком не страдает: сербский источник и черногорский
    вывод — это почти один и тот же текст, поэтому слова исходной реплики видно
    в её переводе прямо. Спрашиваем не «похожи ли длины», а «где оказались
    слова этой реплики»; если у большинства они лежат не на своём месте,
    соответствие один к одному сломано.
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
    """Проверить оба поля разными мерками. Возвращает список замечаний к батчу.

    Мерки обязаны быть разными. Детектор протечки сербского в русский для поля
    `ruski` подходит идеально — он ровно для этого и написан. На черногорское
    поле его наводить нельзя: он ругается на диакритику č ć ž š đ, которая там
    обязательна, и на латинские слова со строчной буквы, то есть на каждое
    черногорское слово. Направленный не туда, он пометил бы как брак сто
    процентов правильного вывода и загнал бы лестницу починки в пустой цикл.
    """
    from helper.montenegrin import check as check_montenegrin

    problems: list[str] = []

    # Черногорский проверяется по батчу целиком: в короткой реплике может
    # не оказаться ни одного слова с ятем, и судить по ней бессмысленно.
    montenegrin = check_montenegrin(" ".join(me for me, _ in pairs))

    if montenegrin:
        problems.append(f"черногорский: {montenegrin}")

    leaked = sum(1 for _, ru in pairs if ru.strip() and looks_untranslated(ru))

    if leaked:
        problems.append(f"в поле ruski не русский текст: {leaked} из {len(pairs)}")

    empty = sum(1 for me, ru in pairs if not me.strip() or not ru.strip())

    if empty:
        problems.append(f"пустых полей: {empty}")

    if sources and alignment_shifted(sources, [me for me, _ in pairs]):
        problems.append(
            "вывод съехал относительно источника: реплики слиты, "
            "и субтитр покажет текст соседней"
        )

    return problems


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
    _check_keys(data, len(batch.cues))

    return [data[f"c{i}"] for i in range(1, len(batch.cues) + 1)]


def _check_keys(data: dict, count: int) -> None:
    """Убедиться, что модель вернула ровно ожидаемые ключи.

    Проверять надо разобранный ответ, а не собранный из него список: список
    строится по диапазону 1..N и всегда получается нужной длины, поэтому такая
    проверка не сработала бы никогда.

    А проверять необходимо, потому что грамматика может и не примениться:
    при сбое разбора схемы llama.cpp пишет ошибку в свой лог, возвращает 200
    и генерирует без ограничений. Отказ помечен как «не планируется к исправлению»,
    так что единственная защита — сверить ключи на нашей стороне.
    """
    if not isinstance(data, dict):
        raise TranslateError(f"ожидался объект, пришло {type(data).__name__}")

    expected = {f"c{i}" for i in range(1, count + 1)}
    missing = expected - data.keys()

    if missing:
        raise TranslateError(
            f"грамматика не применилась: нет ключей {sorted(missing)[:5]} "
            f"из {count} ожидаемых"
        )
