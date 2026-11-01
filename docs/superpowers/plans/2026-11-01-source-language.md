# Язык источника: определитель всегда, коды проверены, кэш помнит решение — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Задачи 1–11 — исполнителям по одной; задача 12 — ручная, для хозяина: исполнителю её не отдавать, после задачи 11 остановиться и передать её хозяину.

**Goal:** демон верно определяет язык речи любого ролика и не прячет решение за строкой `auto` в ключе кэша: определитель работает всегда, метаданные YouTube — только подсказка, коды языков проверяются, а решение видно клиентам и лежит в кэше вместе с дорожкой.

**Architecture:** новый чистый модуль `helper/language.py` (только стандартная библиотека) держит коды, правило определителя и решение о языке; его импортируют и воркер распознавания, и демон. Воркер всегда меряет язык по окнам с речью, решает по правилу, сам составляет затравку под решённый язык и возвращает решение вместе с метриками сегментов. Демон отсекает тишину, повторы и эхо затравки, кладёт решение на задание, в снимок и в запись кэша. Ключ кэша несёт язык, объявленный человеком, а не строку `"auto"`; старые дорожки перестают находиться уже с новым ключом (задача 7: без `target_lang`, `SCHEMA_VERSION` 2), а `PROMPT_VERSION` 2 (задача 11) метит смену правил языка.

**Tech Stack:** Python 3.14, FastAPI, faster-whisper 1.2.1 (пакетный режим), yt-dlp 2026.08.19, pytest + pytest-asyncio, httpx ASGITransport.

**Spec:** `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` — разделы «4. Языки: вход любой, выход всегда один», «Исследование языков: что оказалось неверно и что реально ломается» (поломки 3–9 и «Решения»), «Что надо поправить прямо сейчас», «Вторая строка: решение хозяина», «Язык источника: внутренняя величина, а не настройка». Поломки 1, 2 и 10 — план «Проверка перевода».

**Основа:** ветка `source-language` от вершины PR #1 (`ba274ea`) с влитым `main` (`98a3ad2`: ролики 18+ и README). Номера строк — на `98a3ad2`; каждая ссылка называет и функцию, искать по имени: задачи меняют одни и те же файлы, и номера после первых задач поедут. PR #1 ещё не влит; если его поправят по отзывам, ветку надо догнать слиянием. План закоммичен в ветку отдельным коммитом до задачи 1. Если в нём отмечают шаги, строка ` M docs/superpowers/plans/2026-11-01-source-language.md` в `git status` — не дефект; в коммиты задач план не добавлять.

## Global Constraints

- `extension/` не меняется ни строкой. Протокол меняется только добавлением. Новых значений `Stage` нет. Новых ключей верхнего уровня в снимке и в событиях `state` с именами `source`, `message`, `waiting`, `retry` нет: `extension/content.js` читает их (строки 250, 364–370) и переделает надпись бейджа. Решение о языке живёт только под ключом снимка `language` и в новом событии `{"type": "language", ...}`; незнакомые типы событий и ключи снимка расширение пропускает.
- На выходе всегда те же две строки, русская и черногорская; `targetLang` выход не меняет.
- Определение языка идёт внутри того же запуска воркера под тем же пропуском на карту: без второй загрузки модели, второго пропуска, скачивания моделей и второй модели в памяти карты.
- Семантика аккаунта 18+ в `JobRunner._fetch` не меняется.
- Тесты не касаются сети, карты и настоящих путей кэша; изоляция `YT_TRANS_CACHE`/`YT_TRANS_CONFIG` в `tests/conftest.py` остаётся первой: `JobManager.__init__` стирает каталог отложенного звука.
- `helper/language.py` импортирует только стандартную библиотеку: его грузят и демон, и воркер.
- У воркера `--language` по умолчанию остаётся `"sr"`: на это опирается `helper/measure.py`.
- Русский язык в комментариях, строках журнала и отказах. Фразы, которые доходят до бейджа, — не длиннее 60 знаков, главное в начале.
- Коммиты: сообщение по-русски, без строк `Co-Authored-By` и других подписей. `--no-verify` не использовать никогда. Хук перед коммитом гоняет обзор диффа локальной моделью до 300 с — таймаут команды не меньше 360000 мс. Git — через `/usr/bin/git`.
- Тесты: `.venv/bin/python -m pytest -q` (на `98a3ad2` — 143 passed).

## Правила языка

- **Закреплённый язык решает.** Значение `[asr] language`, отличное от `auto`, идёт в декодер как есть (`hr` и `bs` не сворачиваются в `sr`); определитель всё равно работает, а расхождение помечается сравнением по семье языков. Так велит замысел: объявление человека — «защита от неверного угадывания» («Язык источника: внутренняя величина»), «канал на английском помечается один раз, и дальше распознавание не угадывает» (раздел 4), а определитель при нём работает «как сверка, а не как запасной путь» («Решения»). «Подсказка» из «Решений» здесь — метка YouTube (`--hint`): будь подсказкой и закреп, его перевешивали бы те же голоса окон, от ошибки которых он защищает.
- **Негодный закреплённый язык не роняет демон.** При загрузке настроек — `log.error` с человеческой фразой; каждое новое задание получает `PermanentJobError` до первого обращения к YouTube и без пропуска на карту, а ролики, закэшированные без закрепа, по-прежнему открываются: восстановление читает негодный код как «не закреплено»; сделанные при прежнем годном закрепе не находятся, пока код не исправлен.
- **Решение с низкой уверенностью тоже кэшируется.** Повторное распознавание стоило бы карты при каждом просмотре; лекарство — видимая пометка на проводе и закрепление языка человеком (действует на все ролики и на ключ кэша).

## Решения, расходящиеся с буквой замысла

1. **Ключ кэша несёт язык, объявленный человеком, а не действующий** (замысел, «Решения»: «В ключ кэша идёт действующий язык… Поиск по объявленному, запись под действующим»). Кэш обязан ответить до любой работы на карте (`helper/cache.py:4-5`), а действующий язык появляется только после воркера. Когда язык закрепил человек, действующий с ним совпадает — закрепление решает токен декодера, — так что «поиск по объявленному, запись под действующим» выполняется буквально. Когда не объявил никто, в ключе пустая строка, а решение определителя лежит в записи и видно на проводе. Ошибку определителя исправляет закрепление языка человеком (другой ключ — другая запись) или смена правила с поднятием `PROMPT_VERSION`. Пока объявления на ролик нет («Отложено»), а закреп действует на все ролики, ошибка определителя на незакреплённом ролике в библиотеке на нескольких языках живёт в кэше до следующего `PROMPT_VERSION` (снятый закреп снова находит запись с `pin=""`); без объявления `mismatch` не загорается — видны только `effective`, `reason` и `confidence`.
2. **Метаданные YouTube в ключ не входят и голосующие окна не перевешивают:** канал Juske помечен `en` при сербской речи. Подсказка решает, только когда речи не нашлось ни в одном окне.
3. **«Реплика, совпадающая с затравкой»** понимается как «состоит только из слов затравки» (`is_echo`): буквальное равенство не встретилось ни разу на 66 620 закэшированных репликах, а записанное эхо — обрывки фразы затравки и списка имён. Одних слов затравки мало: среди них должно быть необиходное слово фразы или вводки имён (для `sr` — transkript, snimka, pominju) либо два разных имени из списка, не считая обиходных слов фразы (заголовок «OVO JE …» делает именем и OVO). Иначе одно имя из заголовка в обиходной фразе («Šta je ovo?» под заголовком «ŠTA SE KRILO…», «Ovo je Beograd.») уходило бы как эхо. «Сегменты фильтруются по полям» тоже понимается не буквально: «тишина» выбрасывает сегмент целиком, а «повтор» его только помечает, и внутри выбрасываются одни зациклившиеся предложения — со своей степенью сжатия (формула библиотеки) выше порога или повторяющие уже сказанное в этом сегменте. Сегмент пакетного режима — кусок речи до 30 с, и петля посреди него иначе уносила бы диалог перед ней (в кэше хозяина — у 20 роликов из 163).
4. **Дорожка автора на любом языке и тридцатисекундный зонд отложены** (замысел, «Решения»: «Авторская дорожка открывается для любого языка… на одном тридцатисекундном зонде»). Дешёвого определителя без карты на машине нет (среди скачанных моделей только large-v3 и turbo), зонд на карте противоречит «снимает и карту», а брать не сербскую дорожку без зонда — значит вернуть случай Juske, где английский перевод автора стал бы источником, то есть перевод перевода. В этом плане метки дорожек нормализуются, сербская дорожка берётся при любой записи метки, а закреплённый не сербский язык запрещает сербскую дорожку. Условия следующего шага — в «Отложено». Обратный случай остаётся, как и на 98a3ad2: английский ролик с сербским переводом от автора без закрепа берёт этот перевод источником. Видно это, только когда YouTube дал метку языка: `mismatch` («YouTube указал en, дорожка автора sr») и предупреждение в журнале; без метки — никак. Закрыть его может только зонд («Отложено»).
5. **Промпт переводчика не меняется.** Рабочий путь — `translate_dual` с `montenegrin.SYSTEM_PROMPT`, который язык источника не называет; строка «с сербского языка на русский» (`translate.py:83`) живёт в спящем одностроковом пути.

## Отложено

- **Дорожка автора на любом языке с зондом.** Условия: не сербская дорожка не выбирается без вердикта зонда; до зонда в кэше ищутся все кандидаты-отпечатки и `asr`, в порядке восстановления; у зонда есть ручка отмены, как `_asr_proc` у распознавания; окно без речи даёт `verified: false`, а не вердикт; выбор детектора (модель на процессоре, её скачивание и память) — по замеру времени на этой машине.
- **Объявление языка на ролик и на ссылку.** Читать только через `JobRunner._declaration(job)`; место в ключе уже есть (`CacheKey.pin`). Следствие для реестра: ключ задания `(video_id, use_author_track)` выдаст готовое задание, сделанное при другом объявлении, — при вводе поля это надо учесть. Исключения — проверка закрепления в начале `_pipeline` и `JobManager._key_for`: у восстановления нет задания, оно читает `[asr] language` через `hint` и меняется вместе с `_declaration`.
- **Видимые пометки «выравнивание не проверено» и «вторая строка не удостоверена»** — условие готовности страницы планшета. Языконезависимая защита от съезда реплик, соблюдение приговора проверяльщиков, типизированная вторая строка, `montenegrin.check` только для sr/cnr — план «Проверка перевода».
- **Сторона страницы планшета** (замысел, «Вторая строка: решение хозяина», «Язык источника: внутренняя величина»): решение о языке на экране; оригинальная звуковая дорожка в склейке (та же сортировка `-S lang,+size`, готовность — язык дорожки равен `language.effective`); скрытие второй строки только при совпадении её языка с первой, в том числе на источнике по-русски или по-черногорски. Демон отдаёт для них `language.effective`, `mismatch` и `note`; `lang` второй строки — «Проверка перевода».
- **Перенос строк по союзам других языков** (`CONJUNCTIONS` в `helper/segment.py`) — косметика внутри реплики, на кэш не влияет.
- **Удаление старых записей кэша** (около 18 МБ после смены версии) — решение хозяина.
- **`has_automatic_captions`** намеренно остаётся непрочитанным: это та же дорожка распознавания YouTube, что и `audio_language`, то есть второй сигнал из того же источника.
- **Другие места удаления отложенного звука** (`helper/jobs.py`, около строк 922, 545, 770) и **осиротевший yt-dlp при отмене** — без изменений.
- **Лишние проходы VAD в определителе**: на файле без речи — один лишний проход по всему файлу (около 13 с на трёхчасовом). Речь окна можно считать один раз и звать `detect_language` с `vad_filter=False`.
- **«Task exception was never retrieved … Preempted» в журнале** при каждой отмене во время распознавания: `_run_asr_watched` не дожидается `work` после `CancelledError`. Было и до этого плана; шум, а не поломка.

## Review Focus

- Ролик почти без речи (клип, тихое вступление): ни одно окно не голосует → один дополнительный проход → подсказка → `"sr"` с причиной `fallback`; в Whisper никогда не уходит пустая строка, воркер не падает. Тест в задаче 5.
- Звук длиной от 30 до 37,5 с: смещения окон не уходят в минус; звук до 30 с — одно окно тем же путём. Тест в задаче 5.
- Закреплённый язык с регионом, письменностью или старым кодом (`sr-Latn`, `SR`, `sh`, `cnr`) нормализуется; мусор (`serbian`) отказывает задание один раз, без петли пересоздания, а ролики, закэшированные без закрепа, по-прежнему открываются: восстановление читает негодный код как «не закреплено»; сделанные при прежнем годном закрепе не находятся, пока код не исправлен. Тесты в задаче 4 (отказ до YouTube и без пересоздания) и в задаче 7 (негодный закреп при восстановлении читается как «не закреплено», закэшированный ролик открывается).
- Запись кэша без поля `language` (повреждённая или правленная руками) или с `language: null` отдаётся с `language: null` без падения; запись без реплик не отдаётся. Тест в задаче 7.
- Русская, украинская или македонская речь, у которой доля сербского семейства перешла порог: язык остаётся своим (страж славянских соседей). Тест в задаче 5.

---
### Task 1: Отмена не оставляет зомби

Сегодня `Job.cancel()` сначала стирает отложенный звук паузы и только потом убивает
распознавание и отменяет задачу. `discard_audio` снимает ручку и зовёт
`path.unlink(missing_ok=True)`, а `missing_ok` глотает только ENOENT: ошибка диска
(EACCES, EPERM, EROFS, EIO) вылетает из `cancel()` раньше `kill_asr()`. `JobManager._remove`
к этому моменту уже вынул задание из реестра. Возобновлённое задание посреди распознавания
так и держит карту, но уже без хозяина: Whisper доходит до конца, задание снова берёт пропуск
под переводчика, пишет дорожку в кэш и заканчивается DONE. Цикл в `drop` обрывается на нём и
не доходит до соседнего варианта ролика. `/drop` отвечает 500. Та же ошибка в сбросе звука
сразу после распознавания (`_pipeline`, строка 928) выбрасывает готовый транскрипт: задание
падает повторяемым отказом раньше, чем присвоены `job.cues`, и через минуту распознаёт
ролик заново.

**Files:**
- Modify: `helper/jobs.py:555-565` (`Job.cancel`), `helper/jobs.py:926-928` (`JobRunner._pipeline`: сброс звука после распознавания), `helper/jobs.py:1781-1784` (комментарий в `JobManager._evict`)
- Modify: `tests/conftest.py:3-12` (импорты), после фикстуры `clock` (`:81-100`) — фикстуры `audio_dir` и `refuse_in_audio_dir`
- Modify: `tests/test_limits.py:301-312` (фикстура `audio_dir` уходит в conftest), `tests/test_limits.py:472-488` (функция `refuse_in_audio_dir` уходит в conftest), `tests/test_limits.py:491-494` и `:510` (`test_disk_errors_in_audio_housekeeping_do_not_fail_a_request` берёт фикстуру)
- Test: `tests/test_drop.py` (импорты `:4-7`; три теста в конец файла)

**Interfaces:**
- Consumes: фикстуры `manager` и `client` из `tests/conftest.py`.
- Produces:
  - фикстура `audio_dir(manager) -> Path`: это `helper.jobs.PAUSED_AUDIO_DIR`, созданный к началу теста и убранный после (`shutil.rmtree(..., ignore_errors=True)`). Перенесена из `tests/test_limits.py`, поведение то же.
  - фикстура `refuse_in_audio_dir(monkeypatch) -> Callable[[str], None]`. Вызов `refuse_in_audio_dir("unlink")` (или `"stat"`, `"iterdir"`) подменяет этот метод `pathlib.Path` до конца теста. Для пути, который сам `PAUSED_AUDIO_DIR` или лежит прямо в нём, метод бросает `PermissionError(errno.EACCES, "Permission denied", str(path))`, для остальных путей работает как обычно. Было функцией `refuse_in_audio_dir(monkeypatch, method)` в `tests/test_limits.py`. Фикстура-фабрика, потому что подмене нужен `monkeypatch` теста; простые помощники conftest (как `metadata` из задачи 3) тесты берут `from conftest import …` — это работает в режиме импорта по умолчанию, который `pytest.ini` не меняет.
  - `Job.cancel(self) -> None` больше не бросает `OSError`. Порядок такой: `self._audio_dead = True`, потом `self.kill_asr()`, потом `self._task.cancel()` (если задача есть), и только потом `self.discard_audio()` под `except OSError as exc: log.warning("звук %s не убран при сбросе: %s", self.video_id, exc)`.
  - `JobRunner._pipeline` после успешного распознавания сбрасывает звук под `except OSError as exc: log.warning("звук %s не убран после распознавания: %s", job.video_id, exc)` и идёт дальше: реплики присваиваются, задание переходит к переводу.

- [ ] **Step 1: Перенести `audio_dir` и `refuse_in_audio_dir` в `tests/conftest.py`**

В импорты `tests/conftest.py` добавить, сохраняя алфавитный порядок, `import errno` и
`from pathlib import Path` (стандартная библиотека, импорт наверху безопасен). `helper.*`
по-прежнему импортируется только внутри фикстур. Сразу после фикстуры `clock` вставить:

```python
@pytest.fixture
def audio_dir(manager):
    """Каталог отложенного звука: к началу теста чистый, по окончании убранный.

    После manager, а не раньше: JobManager() при создании сносит этот каталог.
    Убирать надо и ради разреженных файлов на гигабайты: места они не занимают, но
    остались бы в каталоге кэша до конца прогона.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    yield PAUSED_AUDIO_DIR

    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)


@pytest.fixture
def refuse_in_audio_dir(monkeypatch):
    """Фабрика refuse(method): метод Path внутри каталога звука пауз отказывает правами.

    Отказ такой же, как у настоящего каталога без прав: PermissionError с именем пути.
    Права на сам каталог тесту не годятся: под root их не проверяет никто, и тест
    проходил бы там, где ничего не проверил. Пути вне каталога работают как обычно, чтобы
    подмена не задела ни остальной демон, ни pytest. Подмена держится до конца теста.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    def refuse(method: str) -> None:
        original = getattr(Path, method)

        def refusing(self, *args, **kwargs):
            if PAUSED_AUDIO_DIR in (self, self.parent):
                raise PermissionError(errno.EACCES, "Permission denied", str(self))

            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, refusing)

    return refuse
```

В `tests/test_limits.py`:
- удалить фикстуру `audio_dir` (301-312) и функцию `refuse_in_audio_dir` (472-488);
- в `test_disk_errors_in_audio_housekeeping_do_not_fail_a_request` (491-494) добавить параметр `refuse_in_audio_dir` после `caplog`: `(manager, audio_dir, clock, monkeypatch, caplog, refuse_in_audio_dir, method)`. Вызов на строке 510 `refuse_in_audio_dir(monkeypatch, method)` заменить на `refuse_in_audio_dir(method)`;
- импорты `errno`, `shutil`, `Path` и `PAUSED_AUDIO_DIR` оставить: ими пользуются `test_evict_without_a_paused_audio_directory_is_quiet` и `test_audio_that_cannot_be_removed_still_counts_against_the_budget`.

- [ ] **Step 2: Убедиться, что перенос ничего не сломал**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 143 passed

- [ ] **Step 3: Написать падающие тесты**

Импорты `tests/test_drop.py` (4-7) становятся такими:

```python
import logging

import numpy as np
import pytest

from helper import jobs
from helper.jobs import PLAYHEAD_TTL, Stage
```

В конец файла:

```python
# ── сброс, когда звук паузы не стирается ─────────────────────────────────


class LiveTask:
    """Задача задания, что ещё идёт: отмену запоминает, done() отвечает после неё."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> bool:
        self.cancelled = True

        return True

    def done(self) -> bool:
        return self.cancelled


class RunningWhisper:
    """Процесс распознавания, что ещё работает: poll() — None, пока его не убили."""

    def __init__(self) -> None:
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


def resumed_in_asr(job):
    """Возобновлённое задание посреди распознавания: звук паузы на диске, задача и Whisper живы.

    Только такое держит и карту, и звук: при первом проходе звук в памяти, а у паузы нет
    ни задачи, ни процесса.
    """
    job.stage = Stage.ASR
    job.stash_audio(np.zeros(1000, dtype="<f4"))
    job._task = LiveTask()
    job._asr_proc = RunningWhisper()

    return job._audio_path


def test_cancel_frees_the_card_even_if_the_paused_audio_stays(
    manager, audio_dir, refuse_in_audio_dir, caplog, monkeypatch
):
    job = manager.get_or_create(VIDEO, "ru", True)
    path = resumed_in_asr(job)
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")
    # Что держало карту в тот миг, когда сброс дошёл до звука.
    seen = []
    discard = job.discard_audio

    def watched():
        seen.append((job._asr_proc.killed, job._task.cancelled))
        discard()

    monkeypatch.setattr(job, "discard_audio", watched)

    job.cancel()

    # Карту держат процесс и задача, и сброс снимает обоих раньше звука, что бы с ним
    # ни случилось: иначе задание, которое реестр уже забыл, распознавало бы и переводило дальше.
    assert seen == [(True, True)]
    assert job._asr_proc.killed
    assert job._task.cancelled
    # Звук не стёрся и остался без хозяина; отказ диска — строка в журнале, а не исключение.
    assert job._audio_path is None
    assert path.exists()
    assert any(
        str(path) in record.getMessage() and "Permission denied" in record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    )


def test_drop_with_undeletable_audio_still_stops_every_variant(
    manager, audio_dir, refuse_in_audio_dir
):
    steady = manager.get_or_create(VIDEO, "ru", False)
    steady._task = LiveTask()
    stuck = manager.get_or_create(VIDEO, "ru", True)
    resumed_in_asr(stuck)
    # Тронуто последним: jobs_for отдаёт его сбросу первым, и отказ на нём не должен
    # оставить соседний вариант неотменённым. Явно, а не по часам: два вызова подряд
    # могли бы получить одно показание, и порядок решила бы вставка в реестр.
    stuck.touched_at = steady.touched_at + 1.0
    refuse_in_audio_dir("unlink")

    assert manager.jobs_for(VIDEO)[0] is stuck
    assert manager.drop(VIDEO) is True
    assert manager.jobs_for(VIDEO) == []
    assert stuck._asr_proc.killed
    assert stuck._task.cancelled
    assert steady._task.cancelled


async def test_drop_endpoint_answers_even_if_the_paused_audio_cannot_be_removed(
    client, manager, audio_dir, refuse_in_audio_dir
):
    job = manager.get_or_create(VIDEO, "ru", True)
    resumed_in_asr(job)
    refuse_in_audio_dir("unlink")

    response = await client.post(f"/v1/jobs/{VIDEO}/drop")

    # Настоящий сервер на исключение отсюда отвечал 500; ASGITransport тестов
    # отдаёт его самому тесту.
    assert response.status_code == 200
    assert response.json() == {"dropped": VIDEO, "cancelled": True}
    assert job._asr_proc.killed
    assert job._task.cancelled
```

- [ ] **Step 4: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: FAIL, 3 failed, 22 passed. Все три новых теста падают на
`PermissionError: [Errno 13] Permission denied: '/tmp/yt-trans-cache-…/paused-audio/abcdefghijk-….f32'`,
поднятом в `Job.discard_audio` (`helper/jobs.py:553`) из `Job.cancel`. В первом тесте его
бросает `job.cancel()`, во втором `manager.drop`, в третьем `client.post`:
`httpx.ASGITransport` по умолчанию отдаёт исключение приложения тесту, а настоящий сервер
ответил бы 500 (проверено с `raise_app_exceptions=False`).

- [ ] **Step 5: Переставить сброс в `Job.cancel` и охранить оба сброса звука**

`helper/jobs.py:555-565`, `Job.cancel` целиком:

```python
    def cancel(self) -> None:
        # Приостановленное задание своей задачи не имеет — она уже завершилась
        # паузой, — и убрать его звук, кроме как здесь, некому. Флаг ставится
        # до сброса ручки, а запись читает его после того, как ручку выставила:
        # так файл, дописанный уже после сброса, тоже не останется ничьим.
        self._audio_dead = True

        # Сначала то, что держит карту, — процесс распознавания и задача, — и только
        # потом звук. Сбрасываемое задание реестр уже забыл (JobManager._remove), и
        # ошибка диска, вылетавшая отсюда раньше, оставляла его работать без хозяина:
        # Whisper на карте, за ним перевод и запись в кэш, а /drop отвечал 500.
        self.kill_asr()

        if self._task:
            self._task.cancel()

        # Не стёршийся звук остаётся на диске без хозяина: discard_audio снимает ручку
        # до удаления. Его уберёт бюджет (_trim_paused_audio), когда звука станет
        # больше PAUSED_AUDIO_MAX_BYTES, или перезапуск демона. Сброс от этого не зависит.
        try:
            self.discard_audio()
        except OSError as exc:
            log.warning("звук %s не убран при сбросе: %s", self.video_id, exc)
```

`helper/jobs.py:926-928`, `JobRunner._pipeline`: строку `job.discard_audio()` после
комментария «Распознано — отложенный звук больше не нужен…» заменить на:

```python
            # Ошибка диска здесь не повод выбросить готовый транскрипт: файл
            # остаётся без хозяина, как в cancel, а задание идёт дальше.
            try:
                job.discard_audio()
            except OSError as exc:
                log.warning("звук %s не убран после распознавания: %s", job.video_id, exc)

```

Двухстрочный комментарий над ней остаётся. Тестом эта охрана здесь не покрыта: строку 928
проходит только настоящий конвейер, а оснастка для него появится в задаче 3. Там её проверяет
`test_a_resumed_asr_keeps_its_transcript_when_the_paused_audio_stays`. На коде 98a3ad2 этот
тест падает: задание умирает на этой строке, и до перевода дело не доходит.

`helper/jobs.py:1781-1784`, `JobManager._evict`: последняя фраза комментария, «Звук, что
не убрался, останется до следующего обхода.», неверна. `discard_audio` снимает ручку до
удаления, и следующий обход к такому файлу не вернётся. Весь абзац заменить на:

```python
        # Отказ диска здесь запрос не роняет. _evict идёт в конце get_or_create (опрос
        # с create=true, watch, POST /v1/jobs) и restore, когда задание уже заведено, а
        # новое и запущено, — исключение отсюда превращало бы в 500 ответ о работающем
        # задании. Звук, что не убрался, остаётся на диске без хозяина: discard_audio
        # снимает ручку до удаления, и следующий обход к нему не вернётся. Такой файл
        # уберёт бюджет (_trim_paused_audio), когда звука станет больше
        # PAUSED_AUDIO_MAX_BYTES, или перезапуск демона.
```

Не трогать: `JobManager._remove` (сначала вынуть из реестра, потом `cancel`), `drop`,
`drop_job` в `helper/app.py:311-324`, `discard_audio` (ручка по-прежнему снимается до
удаления). `kill_asr()` не бросает (`Popen.kill` сам глотает `ProcessLookupError`),
`Task.cancel()` тоже. Поздний сброс из `finally` в `JobRunner.run` (770) после этого ничего
не делает: ручки уже нет.

- [ ] **Step 6: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: PASS, 25 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 146 passed

- [ ] **Step 7: Коммит**

```bash
/usr/bin/git add helper/jobs.py tests/conftest.py tests/test_limits.py tests/test_drop.py
/usr/bin/git commit -m "Сброс задания сначала снимает распознавание и задачу, а звук паузы, что не стёрся, — строка в журнале, а не 500 и не зомби на карте"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 2: Путь к отложенному звуку читается один раз

`stash_audio` и `take_audio` выполняются в рабочем потоке (`asyncio.to_thread`, `_pipeline`
894 и 919). Уборка бюджета звука (`JobManager._trim_paused_audio` → `owner.discard_audio()`
на каждом `get_or_create` и `restore`) идёт в цикле событий и может сбросить `_audio_path`
между двумя чтениями. Второе чтение тогда даёт `None.exists()`. Это `AttributeError`, а не
`OSError`, поэтому задание падает в `JobRunner.run` повторяемым отказом (`except Exception`),
вместо того чтобы взять звук или скачать его заново. `stash_audio` читает ручку дважды
(строка 507), `take_audio` трижды (538 и 542).

**Files:**
- Modify: `helper/jobs.py:497-528` (`Job.stash_audio`, чтения на 507), `helper/jobs.py:530-547` (`Job.take_audio`, чтения на 538 и 542). Задача 1 эти строки не сдвигает: её правки начинаются с 555.
- Test: `tests/test_limits.py` (в конец файла, после `test_a_handle_cleared_between_two_reads_does_not_fail_a_request`, 564-586). `tests/test_limits.py` после задачи 1 короче: искать по имени.

**Interfaces:**
- Consumes: фикстура `audio_dir` (задача 1); `stash(job, mtime)` и `CHUNK` из `tests/test_limits.py` (315-321 и 297).
- Produces: `Job.stash_audio(self, audio) -> None` и `Job.take_audio(self) -> np.ndarray | None` читают `self._audio_path` ровно один раз, в локальную `path`, и дальше работают только с ней. Ветка `except OSError` в `take_audio` по-прежнему зовёт `self.discard_audio()`.

- [ ] **Step 1: Написать падающие тесты**

В конец `tests/test_limits.py`:

```python
def handle_gone_after_first_read(monkeypatch, path) -> None:
    """Ручка задания видит файл только при первом чтении, дальше она None.

    Так выглядит уборка бюджета (_trim_paused_audio → discard_audio в цикле событий),
    сбросившая ручку, пока take_audio или stash_audio шли в рабочем потоке. Свойство на
    классе заслоняет поле экземпляра, запись в него глотается.
    """
    reads = []

    def handle(self):
        reads.append(self)

        return path if len(reads) == 1 else None

    monkeypatch.setattr(
        jobs.Job, "_audio_path", property(handle, lambda self, value: None), raising=False
    )


def test_take_audio_survives_a_handle_cleared_between_two_reads(manager, audio_dir, monkeypatch):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    handle_gone_after_first_read(monkeypatch, path)

    # Ручка прочитана один раз: файл, что был на месте при проверке, и прочитан. Второе
    # чтение после сброса упало бы AttributeError, и возобновление провалилось бы
    # повторяемым отказом вместо того, чтобы взять звук или скачать его заново.
    assert job.take_audio().size == CHUNK


def test_stash_audio_survives_a_handle_cleared_between_two_reads(
    manager, audio_dir, monkeypatch
):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    handle_gone_after_first_read(monkeypatch, path)

    job.stash_audio(np.zeros(CHUNK, dtype="<f4"))

    # Файл застан при первом и единственном чтении ручки, и звук не пишется второй раз.
    assert list(audio_dir.iterdir()) == [path]
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: FAIL, 2 failed, 34 passed:
`test_take_audio_survives_a_handle_cleared_between_two_reads` падает на
`AttributeError: 'NoneType' object has no attribute 'exists'` в `helper/jobs.py:538`,
`test_stash_audio_survives_a_handle_cleared_between_two_reads` — на том же в
`helper/jobs.py:507`.

- [ ] **Step 3: Читать ручку один раз**

`Job.stash_audio`: проверку на 507 заменить на

```python
        # Ручку читаем один раз: запись идёт в рабочем потоке, а уборка бюджета в цикле
        # событий может сбросить её между проверкой на None и проверкой файла.
        path = self._audio_path

        if path is not None and path.exists():
            return
```

Остальное тело (`mkstemp`, `self._audio_path = Path(name)`, запись, проверка `_audio_dead`)
не меняется.

`Job.take_audio`: сразу после `self.touched_at = time.monotonic()` прочитать
`path = self._audio_path` с комментарием «Один раз, по той же причине, что в stash_audio:
чтение идёт в рабочем потоке.». Проверку на 538 сделать `if path is None or not path.exists():`,
чтение на 542 — `np.fromfile(path, dtype="<f4")`. Ветка `except OSError` остаётся как есть.

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: PASS, 36 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 148 passed

- [ ] **Step 5: Коммит**

```bash
/usr/bin/git add helper/jobs.py tests/test_limits.py
/usr/bin/git commit -m "Ручка отложенного звука читается один раз: уборка бюджета посреди take_audio и stash_audio больше не роняет задание"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 3: Оснастка конвейера

Сегодня ни один тест не гоняет `JobRunner.run` / `_pipeline` и `helper.asr.worker.transcribe`,
а задачи 4–10 меняют именно их. Здесь появляется оснастка: настоящий конвейер на подделках
сети, карты и ollama, со своим кэшем у каждого теста, и настоящий воркер на подделке
faster_whisper. Тесты оснастки доказывают, что работает настоящий код. Цепочку стадий
`state` выдаёт только сам `_pipeline`. argv воркеру собирает настоящий `_run_asr`, его ответ
разбирается настоящим кодом. Пропуск берётся в настоящем порядке. Дорожка ложится в
настоящий `TrackCache` и читается с диска. На стороне воркера `transcribe` строит модель с
`device="cuda"`, передаёт язык пакетному распознавателю и чистит текст сегментов; окна
определителя проверяет задача 5 своими тестами. Сегодняшнее поведение языка тесты не
закрепляют: ни флагов воркера, ни ключа кэша, ни затравки, которые меняют следующие задачи.
Все их проверки остаются верны после задач 4–10, поэтому переворачивать их позже не придётся.

**Files:**
- Modify: `tests/conftest.py` (в импорты — `contextlib`, `json`, `subprocess`, `threading`, `types`, `numpy as np`; всё остальное в конец файла, после фикстуры `connection`)
- Test: `tests/test_harness.py` (импорты в начало, пять тестов в конец)

**Interfaces:**
- Consumes: фикстура `manager`; `audio_dir` и `refuse_in_audio_dir` (задача 1); охрана сброса звука после распознавания (задача 1), её проверяет третий тест.
- Produces. Всё в `tests/conftest.py`, `helper.*` там по-прежнему импортируется только внутри функций.
  - **Функция `metadata(**overrides) -> VideoInfo`** — обычная функция модуля, не фикстура: тесты импортируют её `from conftest import metadata`. Импорт работает, потому что `pytest.ini` не задаёт `--import-mode`, а у `tests/` нет `__init__.py`: в режиме по умолчанию (`prepend`) pytest кладёт `tests/` в начало `sys.path` и грузит conftest под именем `conftest`, и `from conftest import …` получает тот же уже загруженный модуль из `sys.modules['conftest']`, а не второй экземпляр. Любое поле `VideoInfo` переопределяется по имени; `manual_tracks` принимает и строки, и готовые `CaptionTrack`.
  - **Фикстура `pipeline(manager, monkeypatch) -> Pipeline`.** В `helper.jobs` подменены `probe`, `audio_pcm`, `fetch_manual_track`, `translate_dual`, `translate_batch`, `FETCH_BACKOFF = (0.0, 0.0)` и `subprocess`: пространство имён с `Popen`, которое строит `FakeWorker`, и настоящими `PIPE` и `TimeoutExpired`. yt-dlp и asyncio видят настоящий `subprocess`. Настоящими остаются `usable_manual_track`, `normalize_cues`, `build_cues`, `validate_dual`, весь `JobRunner`.
    - Кэш у каждого теста свой. Каталог `<tmp> = tempfile.mkdtemp(prefix="pipeline-", dir=helper.env.CACHE_DIR)`, `helper.cache.CACHE_DIR` подменён на него до конструктора, поэтому `index.sqlite` тоже свой. Хранилище — настоящий `TrackCache(root=<tmp>/tracks)`. На время теста `manager.cache` указывает на тот же объект, так что `manager.restore` читает этот кэш. После теста прежний кэш возвращается раньше, чем фикстура `manager` закроет его индекс; индекс теста закрывается, `<tmp>` удаляется.
    - Сценарий (`info`, `audio`, `track`, `asr_result`, `asr_exit`, `asr_stderr`, `gate`) тест ставит до `run`, записи (`calls`, `names()`, `workers`, `worker_started`, `lease`, `cache`, `runner`, `config`) читает после; имена, умолчания и записи — как в коде шага 3 (докстроки `Pipeline`, `FakeWorker`, `FakeLease`). С задачи 5 argv воркера разбирается его собственным парсером: `worker.build_parser().parse_args(pipeline.workers[-1].args[3:])`.
    - Методы:
      - `configure(**asr) -> None` собирает `Config(asr=AsrConfig(**asr), translator=TranslatorConfig())` и новый `JobRunner(lease, cache, config)`. Тот же `Config` и бегун получают `manager.config` и `manager.runner`, поэтому restore считает ключ по той же настройке. Запись пропуска начинается заново: сразу после вызова `lease.calls == [("claim", "qwen3.8:27b")]`. Конструктор `Pipeline` уже позвал `configure()` без аргументов. Значение `language` идёт в `AsrConfig` как есть, мимо `config.load()`.
      - `async run(job) -> None` — это `await runner.run(job)`, настоящий `JobRunner.run` → `_pipeline`.
    - Как пользоваться. Задание заводится через `job = manager.get_or_create("abcdefghijk", "ru", True)`: оно в реестре, но не запущено, потому что `Job.start` фикстуры `manager` только записывает. Можно и `jobs.Job("abcdefghijk")` вне реестра, тогда `manager.restore` идёт в кэш. Затем `await pipeline.run(job)`. Возобновление после паузы: `job.info = metadata()`, `job.stage = Stage.PAUSED`, `job.stash_audio(pipeline.audio)`, с фикстурой `audio_dir`. Вытеснение посреди распознавания: `pipeline.gate = threading.Event()`, запуск через `asyncio.create_task(pipeline.run(job))`, потом `await asyncio.to_thread(pipeline.worker_started.wait, 5)` и `pipeline.lease.holders[-1].preempt()`.
  - **Фикстура `whisper(monkeypatch) -> FakeWhisper`.** Сначала импортирует `helper.env` (HF_HOME до faster_whisper, как в воркере), затем подменяет ровно два имени: `faster_whisper.WhisperModel` на `whisper.build_model` и `faster_whisper.BatchedInferencePipeline` на `whisper.build_pipeline`. `helper.asr.worker.transcribe` и `_detect` остаются настоящими, VAD тоже: `faster_whisper.vad.get_speech_timestamps` фикстура не трогает, и настоящий Silero идёт на процессоре, без сети и карты (около 0,1 с на пять секунд тишины; в нулях речи он не находит). До подмены доходит код, который берёт эти имена из модуля в момент вызова, как `transcribe` берёт классы.
    - Сценарий (`segments`), записи (`model`, `kwargs`) и строитель `segment(start, end, text, *, ...)` с метриками сегмента — имена, умолчания и записи как в коде шага 3 (докстроки `FakeWhisper` и `segment`).

- [ ] **Step 1: Написать падающие тесты**

Начало `tests/test_harness.py` (сейчас там только `from pathlib import Path`):

```python
import asyncio
import logging
import sys
import threading
from pathlib import Path

import numpy as np

from conftest import metadata
from helper.jobs import Stage

VIDEO = "abcdefghijk"
```

Импорт `Stage` внутри `test_manager_records_a_resume_without_launching_it` можно не трогать.
В конец файла:

```python
# ── конвейер на подделках ────────────────────────────────────────────────


async def test_pipeline_runs_an_asr_video_to_done(manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    # Настоящий _pipeline: те же стадии, что видит зритель, и ни одного выхода в сеть.
    assert job.stage is Stage.DONE
    assert [event["stage"] for event in job.events if event["type"] == "state"] == [
        "probing", "queued", "fetching", "asr", "translating", "done",
    ]
    assert pipeline.names() == ["probe", "audio", "asr", "translate"]
    # Воркер запущен настоящим _run_asr со звуком задания, его ответ разобран настоящим кодом.
    worker = pipeline.workers[-1]
    assert worker.args[:3] == [sys.executable, "-m", "helper.asr.worker"]
    assert worker.input == pipeline.audio.tobytes()
    assert [(cue.src, cue.lines, cue.study) for cue in job.cues] == [
        ("Dobar dan.", ["ru Dobar dan."], "cnr Dobar dan."),
    ]
    # Дисциплина карты: переводчик выгружен до пропуска под Whisper, фазы берут его по очереди.
    assert [call[0] for call in pipeline.lease.calls] == [
        "claim", "unload", "acquire", "release", "acquire", "release",
    ]
    # Дорожка легла в настоящий кэш теста и читается с диска.
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["asr"]
    assert [cue["src"] for cue in pipeline.cache.variants(VIDEO)[0]["cues"]] == ["Dobar dan."]


async def test_pipeline_takes_a_serbian_author_track_without_asr(manager, pipeline):
    pipeline.info = metadata(manual_tracks=["sr"])
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.source == "manual:sr"
    assert pipeline.calls[:2] == [("probe", VIDEO), ("track", VIDEO, "sr")]
    assert pipeline.names() == ["probe", "track", "translate"]
    assert pipeline.workers == []
    # Без распознавания карта нужна только переводчику.
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "acquire", "release"]
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["manual:sr"]
    assert [cue.src for cue in job.cues] == ["Zdravo svima."]


async def test_a_resumed_asr_keeps_its_transcript_when_the_paused_audio_stays(
    manager, pipeline, audio_dir, refuse_in_audio_dir, caplog
):
    job = manager.get_or_create(VIDEO, "ru", True)
    # Пауза после загрузки звука: метаданные есть, звук отложен на диск, реплик нет.
    job.info = metadata()
    job.stage = Stage.PAUSED
    job.stash_audio(pipeline.audio)
    path = job._audio_path
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")

    await pipeline.run(job)

    # Возобновление взяло отложенный звук, а не скачало его заново.
    assert pipeline.names() == ["asr", "translate"]
    assert pipeline.workers[-1].input == pipeline.audio.tobytes()
    # Звук не стёрся, но распознанное не выброшено: задание дошло до конца и до кэша.
    assert job.stage is Stage.DONE
    assert [cue.src for cue in job.cues] == ["Dobar dan."]
    assert len(pipeline.cache.puts) == 1
    assert job._audio_path is None
    assert path.exists()
    assert any(
        str(path) in record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    )


async def test_preemption_during_asr_kills_the_worker_and_keeps_the_audio(
    manager, pipeline, audio_dir
):
    pipeline.gate = threading.Event()
    job = manager.get_or_create(VIDEO, "ru", True)
    running = asyncio.create_task(pipeline.run(job))

    # Воркер запущен и держит карту, пока его не отпустят; сторож видит пользователя.
    assert await asyncio.to_thread(pipeline.worker_started.wait, 5)
    pipeline.lease.holders[-1].preempt()
    await asyncio.wait_for(running, 5)

    assert pipeline.workers[-1].killed
    assert job.stage is Stage.PAUSED
    assert job.cues == []
    assert job._audio_path.exists()
    assert pipeline.cache.puts == []
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "unload", "acquire", "release"]


# ── воркер на подделке faster_whisper ────────────────────────────────────


def test_whisper_fixture_runs_the_real_worker_transcribe(whisper):
    from helper.asr import worker

    whisper.segments = [whisper.segment(0.0, 1.5, " Dobar dan. ")]

    result = worker.transcribe(np.zeros(5 * worker.SAMPLE_RATE, dtype="<f4"), language="sr")

    # Подменены только модель и пакетный распознаватель; всё вокруг них — настоящий transcribe.
    assert (whisper.model.name, whisper.model.device, whisper.model.compute_type) == (
        "large-v3", "cuda", "float16",
    )
    assert (whisper.kwargs["language"], whisper.kwargs["task"]) == ("sr", "transcribe")
    assert (result["language"], result["model"], result["duration"]) == ("sr", "large-v3", 5.0)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in result["cues"]] == [
        (0.0, 1.5, "Dobar dan."),
    ]
```

Почему эти проверки не перевернутся в задачах 4–10. Цепочку стадий, порядок пропуска
(`claim, unload, acquire, release, acquire, release`) и `names()` не меняет ни одна задача:
новых стадий план не вводит, а определитель работает внутри того же запуска воркера под тем
же пропуском. Событие `language` из задачи 6 имеет тип не `state`. Ответ по умолчанию без
`verdict` проходит запасную ветку `_accept_asr`: решение строится из `result["language"]`.
После задачи 5 `transcribe(..., language="sr")` определяет язык всегда, но решает
закреплённый код. В пяти секундах нулей настоящий VAD речи не находит, и `detect_language`
модели не зовётся вовсе. Тест воркера не читает `result["detected"]` и
`result["confidence"]`: задача 5 уносит их под `result["verdict"]`. Новые поля реплик и
флаги задачи 8 тесты не читают.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: FAIL — ошибка сбора `tests/test_harness.py`, ни один тест не запущен:
`ImportError: cannot import name 'metadata' from 'conftest'` и
`Interrupted: 1 error during collection`.

- [ ] **Step 3: Завести оснастку**

В импорты `tests/conftest.py` добавить, сохраняя порядок: `import contextlib`, `import json`,
`import subprocess`, `import threading`, `import types` (к стандартным) и `import numpy as np`
(после `import httpx`). В конец файла, после фикстуры `connection`, дописать код ниже. Он и
есть контракт, на котором задачи 4–10 пишут свои тесты, поэтому имена, умолчания и записи
переносить точно.

```python
# ── конвейер на подделках ────────────────────────────────────────────────


def metadata(**overrides):
    """VideoInfo ролика abcdefghijk: все поля по умолчанию, overrides — поля по имени.

    Обычная функция, а не фикстура: тесты берут её `from conftest import metadata`.
    manual_tracks принимает и строки: "sr" — это CaptionTrack(lang="sr", url="",
    ext="json3", name=""); готовый CaptionTrack идёт как есть.
    """
    from helper.sources.ytdlp import CaptionTrack, VideoInfo

    tracks = [
        track
        if isinstance(track, CaptionTrack)
        else CaptionTrack(lang=track, url="", ext="json3", name="")
        for track in overrides.pop("manual_tracks", [])
    ]
    fields = {
        "video_id": "abcdefghijk",
        "title": "Naslov",
        "description": "",
        "channel": "Kanal",
        "tags": [],
        "chapters": [],
        "duration": 60.0,
        "audio_language": None,
        "live_status": None,
        "has_automatic_captions": False,
    }

    return VideoInfo(**{**fields, **overrides}, manual_tracks=tracks)


class FakeLease:
    """Пропуск на карту без NVML и ollama: записывает обращения и выдаёт настоящий _Holder.

    calls по порядку: ("claim", модель), ("unload", модель), ("acquire", МиБ, модель),
    ("release", МиБ). holders — выданные ручки; holders[-1].preempt() — то, что сделал бы
    сторож, увидев пользователя на карте.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.holders: list = []

    def claim_model(self, name: str) -> None:
        self.calls.append(("claim", name))

    def unload(self, model: str) -> None:
        self.calls.append(("unload", model))

    @contextlib.asynccontextmanager
    async def acquire(self, need_mib: int, *, model: str | None = None, wait: bool = True):
        from helper.gpulease import _Holder

        holder = _Holder()
        self.calls.append(("acquire", need_mib, model))
        self.holders.append(holder)

        try:
            yield holder
        finally:
            self.calls.append(("release", need_mib))


class RecordingCache:
    """Настоящий TrackCache, который помнит ключи: под какими искали (gets) и писали (puts).

    Пишет по-настоящему, в каталог теста, и variants() читает записанное с диска. Всё,
    кроме get и put, уходит к нему как есть.
    """

    def __init__(self, store) -> None:
        self.store = store
        self.gets: list = []
        self.puts: list = []

    def get(self, key, length_seconds=None):
        self.gets.append(key)

        return self.store.get(key, length_seconds)

    def put(self, key, cues, length_seconds, **kwargs) -> None:
        self.puts.append(key)
        self.store.put(key, cues, length_seconds, **kwargs)

    def __getattr__(self, name):
        return getattr(self.store, name)


class FakeWorker:
    """subprocess.Popen для `python -m helper.asr.worker`: запоминает вызов, отвечает сценарием.

    args — argv целиком, как его собрал JobRunner._run_asr; options — флаги после
    `-m helper.asr.worker` парами; input — байты звука из communicate; killed — звали kill().
    Ответ берётся у конвейера в момент communicate: stdout — asr_result (dict уходит
    JSON-ом, bytes — как есть), код выхода — asr_exit, stderr — asr_stderr. Убитый
    отвечает кодом -9 и пустым выводом, как после SIGKILL.

    gate держит communicate, как долгий Whisper, а kill() его отпускает. communicate
    идёт в рабочем потоке (asyncio.to_thread), поэтому gate — threading.Event, не asyncio.
    """

    def __init__(self, rig, args, **kwargs) -> None:
        self.rig = rig
        self.args = list(args)
        self.input: bytes | None = None
        self.returncode: int | None = None
        self.killed = False
        rig.workers.append(self)
        rig.calls.append(("asr", self.options))
        rig.worker_started.set()

    @property
    def options(self) -> dict[str, str]:
        flags = self.args[3:]

        return dict(zip(flags[::2], flags[1::2]))

    def communicate(self, input=None, timeout=None):
        self.input = input

        if self.rig.gate is not None:
            self.rig.gate.wait(5)

        if self.killed:
            self.returncode = -9

            return b"", b""

        self.returncode = self.rig.asr_exit
        out = self.rig.asr_result

        if isinstance(out, dict):
            out = json.dumps(out, ensure_ascii=False).encode()

        return out, self.rig.asr_stderr

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True

        if self.rig.gate is not None:
            self.rig.gate.set()


class Pipeline:
    """Настоящий JobRunner на подделках: сети, карты и ollama нет, конвейер свой.

    Сценарий (тест задаёт до run):
      info        — VideoInfo, которую отдаёт probe; по умолчанию metadata();
      audio       — звук от audio_pcm: две секунды нулей, float32, 16 кГц;
      track       — реплики дорожки автора от fetch_manual_track или исключение, которое
                    она бросит (YtDlpError уводит конвейер в распознавание);
      asr_result  — stdout воркера: dict уходит JSON-ом, bytes — как есть;
      asr_exit    — код выхода воркера (0); asr_stderr — его stderr (b"");
      gate        — threading.Event или None: держит воркер до set() или kill().
    Записи (тест читает после):
      calls          — обращения наружу по порядку: ("probe", id), ("track", id, язык),
                       ("audio", id), ("asr", флаги воркера), ("translate", реплики);
      workers        — запущенные FakeWorker; worker_started — Popen уже позван;
      lease          — FakeLease: calls и holders;
      cache          — RecordingCache: gets и puts (CacheKey), variants(id) с диска;
      runner, config — бегун и его настройка; их пересобирает configure().
    """

    def __init__(self, manager, cache) -> None:
        self.manager = manager
        self.cache = cache
        self.lease = FakeLease()
        self.calls: list[tuple] = []
        self.workers: list[FakeWorker] = []
        self.worker_started = threading.Event()
        self.gate: threading.Event | None = None
        self.info = metadata()
        self.audio = np.zeros(2 * 16000, dtype="<f4")
        self.track = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima.", "words": []}]
        self.asr_result: dict | bytes = {
            "language": "sr",
            "duration": 2.0,
            "model": "large-v3",
            "cues": [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}],
        }
        self.asr_exit = 0
        self.asr_stderr = b""
        self.configure()

    def configure(self, **asr) -> None:
        """Бегун на Config(asr=AsrConfig(**asr)); тот же Config и бегун получает реестр.

        Запись пропуска начинается заново: сразу после вызова в lease.calls только
        ("claim", модель переводчика) нового бегуна.
        """
        from helper import jobs
        from helper.config import AsrConfig, Config, TranslatorConfig

        self.config = Config(asr=AsrConfig(**asr), translator=TranslatorConfig())
        self.lease.calls.clear()
        self.runner = jobs.JobRunner(self.lease, self.cache, self.config)
        # restore считает ключ по manager.config: та же настройка, что у бегуна.
        self.manager.config = self.config
        self.manager.runner = self.runner

    def names(self) -> list[str]:
        """Имена обращений наружу по порядку: ["probe", "audio", "asr", "translate"]."""
        return [call[0] for call in self.calls]

    async def run(self, job) -> None:
        """Настоящий JobRunner.run(job): _pipeline со всеми его ветками и отказами."""
        await self.runner.run(job)


@pytest.fixture
def pipeline(manager, monkeypatch):
    """JobRunner на подделках: настоящий конвейер без сети, карты и ollama. См. Pipeline."""
    from helper import cache as track_cache
    from helper import env, jobs

    # Свой кэш у каждого теста. Общий кэш прогона наполнился бы готовыми дорожками, и
    # restore в чужих тестах их находил бы. CACHE_DIR подменён до TrackCache(): индекс
    # index.sqlite открывается в конструкторе.
    base = Path(tempfile.mkdtemp(prefix="pipeline-", dir=env.CACHE_DIR))
    monkeypatch.setattr(track_cache, "CACHE_DIR", base)
    rig = Pipeline(manager, RecordingCache(track_cache.TrackCache(root=base / "tracks")))
    # Простым присваиванием, не monkeypatch: его откат пришёл бы уже после того, как
    # manager закроет индекс своего кэша, и закрыл бы он тогда этот.
    shared = manager.cache
    manager.cache = rig.cache

    def probe(video_id, *, cookies=None):
        rig.calls.append(("probe", video_id))

        return rig.info

    def audio_pcm(video_id, *, cookies=None):
        rig.calls.append(("audio", video_id))

        return rig.audio

    def fetch_manual_track(video_id, lang, *, cookies=None):
        rig.calls.append(("track", video_id, lang))

        if isinstance(rig.track, BaseException):
            raise rig.track

        return rig.track

    def translate_dual(batch, model, **kwargs):
        rig.calls.append(("translate", list(batch.cues)))

        return [(f"cnr {cue}", f"ru {cue}") for cue in batch.cues]

    def translate_batch(batch, model, **kwargs):
        rig.calls.append(("translate", list(batch.cues)))

        return [f"ru {cue}" for cue in batch.cues]

    monkeypatch.setattr(jobs, "probe", probe)
    monkeypatch.setattr(jobs, "audio_pcm", audio_pcm)
    monkeypatch.setattr(jobs, "fetch_manual_track", fetch_manual_track)
    monkeypatch.setattr(jobs, "translate_dual", translate_dual)
    monkeypatch.setattr(jobs, "translate_batch", translate_batch)
    monkeypatch.setattr(jobs, "FETCH_BACKOFF", (0.0, 0.0))
    # Подменён только subprocess модуля helper.jobs: yt-dlp и asyncio видят настоящий.
    monkeypatch.setattr(
        jobs,
        "subprocess",
        types.SimpleNamespace(
            Popen=lambda args, **kwargs: FakeWorker(rig, args, **kwargs),
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    yield rig

    manager.cache = shared
    rig.cache.store._db.close()
    shutil.rmtree(base, ignore_errors=True)


# ── воркер на подделке faster_whisper ────────────────────────────────────


class FakeWhisperModel:
    """WhisperModel без карты: помнит, с чем её построили, и всегда слышит сербский."""

    def __init__(self, name, device, compute_type) -> None:
        self.name = name
        self.device = device
        self.compute_type = compute_type

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        # С задачи 5 определитель работает при каждом transcribe. Ответ — как у настоящей:
        # лучший язык, его доля и всё распределение. Окна и голоса проверяет задача 5
        # своими тестами, на своей заглушке модели.
        return "sr", 1.0, [("sr", 1.0)]


class FakeBatchedPipeline:
    """BatchedInferencePipeline без карты: запоминает аргументы, отдаёт сегменты сценария."""

    def __init__(self, whisper, model) -> None:
        self.whisper = whisper
        self.model = model

    def transcribe(self, audio, **kwargs):
        from faster_whisper.transcribe import TranscriptionInfo

        self.whisper.kwargs = kwargs
        seconds = len(audio) / 16000
        info = TranscriptionInfo(
            language=kwargs.get("language"),
            language_probability=1.0,
            duration=seconds,
            duration_after_vad=seconds,
            all_language_probs=None,
            transcription_options=None,
            vad_options=None,
        )

        return iter(self.whisper.segments), info


class FakeWhisper:
    """faster_whisper без карты для тестов воркера: transcribe и _detect настоящие.

    Сценарий (задаёт тест):
      segments — что вернёт BatchedInferencePipeline.transcribe; строить через segment().
    Записи (читает тест):
      model  — последняя модель, что построил воркер: name, device, compute_type;
      kwargs — именованные аргументы последнего BatchedInferencePipeline.transcribe.
    VAD не подменён: faster_whisper.vad.get_speech_timestamps — настоящий Silero, на
    процессоре и без сети; в нулях речи он не находит.
    """

    def __init__(self) -> None:
        self.segments: list = []
        self.model: FakeWhisperModel | None = None
        self.kwargs: dict | None = None

    def build_model(self, model_size_or_path, device="auto", compute_type="default", **kwargs):
        self.model = FakeWhisperModel(model_size_or_path, device, compute_type)

        return self.model

    def build_pipeline(self, model):
        return FakeBatchedPipeline(self, model)

    @staticmethod
    def segment(
        start,
        end,
        text,
        *,
        no_speech_prob=0.1,
        avg_logprob=-0.3,
        compression_ratio=1.5,
        words=(),
    ):
        """Настоящий faster_whisper.transcribe.Segment: имена полей проверяет сама библиотека.

        words — faster_whisper.transcribe.Word, как их отдаёт word_timestamps=True.
        """
        from faster_whisper.transcribe import Segment

        return Segment(
            id=0,
            seek=0,
            start=start,
            end=end,
            text=text,
            tokens=[],
            avg_logprob=avg_logprob,
            compression_ratio=compression_ratio,
            no_speech_prob=no_speech_prob,
            words=list(words),
            temperature=0.0,
        )


@pytest.fixture
def whisper(monkeypatch):
    """Подделка faster_whisper для тестов воркера, см. FakeWhisper. Карта не нужна."""
    # Сначала helper.env, как в самом воркере: он ставит HF_HOME во временный кэш до того,
    # как faster_whisper потянет huggingface_hub.
    import helper.env  # noqa: F401

    import faster_whisper

    fake = FakeWhisper()
    # transcribe импортирует оба класса внутри себя, при каждом вызове: подмена в модуле
    # faster_whisper до него доходит. VAD остаётся настоящим.
    monkeypatch.setattr(faster_whisper, "WhisperModel", fake.build_model)
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", fake.build_pipeline)

    return fake
```

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: PASS, 10 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 153 passed

`python -X dev -m pytest` после зелёного прогона падает при выходе интерпретатора: это
несовпадение аллокаторов в C-расширении faster_whisper. Обычный прогон это не задевает;
не разбираться.

- [ ] **Step 5: Коммит**

```bash
/usr/bin/git add tests/conftest.py tests/test_harness.py
/usr/bin/git commit -m "Оснастка конвейера: настоящие JobRunner и воркер в тестах, на подделках сети, карты, ollama и faster_whisper"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---
### Task 4: Коды языков: проверка, приведение и отказ до YouTube

**Files:** номера строк — на `98a3ad2`, предыдущие задачи их сдвинули: искать по имени функции.
- Create: `helper/language.py` — часть кодов (часть определителя допишет задача 5)
- Modify: `helper/config.py:23-26` (импорты), `helper/config.py:66-72` (комментарий к `AsrConfig.language`), `helper/config.py:179-206` (`load`: сразу после сборки `AsrConfig` на строке 189)
- Modify: `config.example.toml:21-24`
- Modify: `helper/jobs.py:30-33` (импорты); `helper/jobs.py:772-786` (`JobRunner._pipeline`: между возобновлением, 775-778, и запросом метаданных, 784-786); `helper/jobs.py:1195-1209` (`JobRunner._run_asr`: ненулевой код выхода, между строками 1207 и 1209); `helper/jobs.py:1218-1242` (`JobRunner._source_language`: ветка конфига 1229-1232; новый метод `JobRunner._pin` — прямо перед ним)
- Test: `tests/test_language.py`, `tests/test_config.py`, `tests/test_job_language.py` — все три новые

**Interfaces:**
- Consumes: фикстура `manager` (`tests/conftest.py`). Из задачи 3: построитель `metadata(**overrides) -> VideoInfo` (тесты импортируют его `from conftest import metadata`; без аргументов — ролик без дорожек автора и без `audio_language`) и фикстура `pipeline`: `pipeline.configure(**asr)` собирает бегун на `AsrConfig(**asr)`, `await pipeline.run(job)` гонит настоящий `JobRunner.run`, `pipeline.workers` — поддельные процессы воркера по порядку, у каждого `.args` — полный argv, как у `subprocess.Popen.args`: `[sys.executable, "-m", "helper.asr.worker", ...]`; ответ поддельного воркера задают `pipeline.asr_result` (stdout: dict уходит JSON-ом, bytes — как есть), `pipeline.asr_exit` (код выхода) и `pipeline.asr_stderr`.
- Produces:
  - `helper/language.py`, только стандартная библиотека: `WHISPER_CODES: frozenset[str]`, `SUBSTITUTES: dict[str, str]`, `NO_LANGUAGE: frozenset[str]`, `BCMS: frozenset[str]`, `class UnknownLanguage(ValueError)` с атрибутом `raw: str`, `normalize(raw: str | None) -> str | None`, `hint(raw: str | None) -> str | None`, `family(code: str | None) -> str | None`, `is_bcms(code: str | None) -> bool`, `refusal(raw: str) -> str`.
  - `JobRunner._pin(self) -> str | None` — код Whisper из `[asr] language` или `None` для auto; негодный код → `PermanentJobError(language.refusal(raw))`.
  - `helper.config.load()`: `asr.language` — код Whisper, `"auto"` или, если код негодный, исходная строка (плюс `log.error`). `Config.describe()["asr"]["language"]` остаётся строкой: её печатает `extension/options.js:125`.
  - `JobRunner._run_asr`, ненулевой выход воркера не по сигналу: stdout разбирается раньше хвоста stderr. Словарь с `"error"` и `"permanent": true` → `PermanentJobError(error)`; словарь с `"error"` без этого флага → `RuntimeError(f"ASR не справился: {error}")`; всё остальное — прежний `RuntimeError` с хвостом stderr.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_language.py
"""helper/language.py: коды языков, правило определителя и решение о языке."""

import ast
import sys
from pathlib import Path

import pytest

from helper import language


def test_whisper_codes_are_the_installed_faster_whisper_list():
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    assert language.WHISPER_CODES == frozenset(_LANGUAGE_CODES)
    assert len(language.WHISPER_CODES) == 100


SUBSTITUTE_ROWS = [
    ("sh", "sr"),
    ("me", "sr"),
    ("cnr", "sr"),
    ("iw", "he"),
    ("jv", "jw"),
    ("fil", "tl"),
    ("nb", "no"),
    ("in", "id"),
]


def test_every_substitute_has_its_own_row():
    assert dict(SUBSTITUTE_ROWS) == language.SUBSTITUTES


@pytest.mark.parametrize(("raw", "code"), SUBSTITUTE_ROWS)
def test_substitute_lands_on_a_whisper_code(raw, code):
    assert raw not in language.WHISPER_CODES
    assert code in language.WHISPER_CODES
    assert language.normalize(raw) == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sr", "sr"),
        ("SR", "sr"),
        (" sr ", "sr"),
        ("sr-Latn", "sr"),
        ("sr-Cyrl", "sr"),
        ("sr_RS", "sr"),
        ("cnr-ME", "sr"),
        ("hr", "hr"),
        ("bs", "bs"),
        ("en-US", "en"),
        ("EN-us", "en"),
        ("en-orig", "en"),
        ("en-desc", "en"),
        ("zh-Hans", "zh"),
        ("yue", "yue"),
        ("en+en-US", "en"),
    ],
)
def test_normalize_keeps_the_primary_subtag(raw, code):
    assert language.normalize(raw) == code


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "auto", "AUTO", " Auto ", "und", "zxx", "mul", "mis"]
)
def test_normalize_reads_no_language_as_none(raw):
    assert language.normalize(raw) is None


@pytest.mark.parametrize("raw", ["serbian", "xx", "srb", "en+sr", "live_chat"])
def test_normalize_refuses_an_unknown_code(raw):
    with pytest.raises(language.UnknownLanguage) as caught:
        language.normalize(raw)

    assert caught.value.raw == raw
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("en-orig", "en"),
        ("SR", "sr"),
        ("sh", "sr"),
        ("serbian", None),
        ("live_chat", None),
        ("en+sr", None),
        ("und", None),
        (None, None),
    ],
)
def test_hint_never_raises(raw, code):
    assert language.hint(raw) == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sr", "sr"),
        ("hr", "sr"),
        ("bs", "sr"),
        ("sh", "sr"),
        ("cnr", "sr"),
        ("sr-Latn", "sr"),
        ("mk", "mk"),
        ("en-US", "en"),
        ("serbian", None),
        (None, None),
    ],
)
def test_family_folds_the_three_bcms_tokens(raw, code):
    assert language.family(raw) == code


@pytest.mark.parametrize("raw", ["sr", "hr", "bs", "sh", "me", "cnr", "sr-Latn", "sr-Cyrl", "SR"])
def test_bcms_spellings_are_bcms(raw):
    assert language.is_bcms(raw) is True


@pytest.mark.parametrize("raw", ["mk", "sl", "ru", "en", None, "", "live_chat", "serbian"])
def test_other_codes_are_not_bcms(raw):
    assert language.is_bcms(raw) is False


def test_bcms_is_three_whisper_tokens():
    assert language.BCMS == {"sr", "hr", "bs"}
    assert language.BCMS <= language.WHISPER_CODES


def test_refusal_names_the_value_and_where_to_fix_it():
    phrase = language.refusal("serbian")

    assert phrase == "неизвестный язык «serbian»: [asr] language в config.toml"
    assert len(phrase) <= 60


def test_language_module_imports_only_the_standard_library():
    tree = ast.parse(Path(language.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])

    assert roots <= sys.stdlib_module_names
```

```python
# tests/test_config.py
"""[asr] language в config.toml: что написал хозяин и что получает демон."""

import logging
import tomllib
from pathlib import Path

import pytest

from helper import language
from helper.config import load

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    return path


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ('"cnr"', "sr"),
        ('"sh"', "sr"),
        ('"SR"', "sr"),
        ('"sr-Latn"', "sr"),
        ('"hr"', "hr"),
        ('"en-US"', "en"),
        ('"auto"', "auto"),
        ('"AUTO"', "auto"),
        ('""', "auto"),
    ],
)
def test_language_is_normalised_at_load(tmp_path, raw, stored):
    config = load(written(tmp_path, f"[asr]\nlanguage = {raw}\n"))

    assert config.asr.language == stored
    assert config.describe()["asr"]["language"] == stored


def test_missing_file_means_auto(tmp_path):
    assert load(tmp_path / "absent.toml").asr.language == "auto"


@pytest.mark.parametrize(("raw", "kept"), [('"serbian"', "serbian"), ("5", "5")])
def test_unknown_language_is_logged_not_fatal(tmp_path, caplog, raw, kept):
    with caplog.at_level(logging.ERROR, logger="helper.config"):
        config = load(written(tmp_path, f"[asr]\nlanguage = {raw}\n"))

    assert config.asr.language == kept
    assert config.describe()["asr"]["language"] == kept
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        language.refusal(kept)
    ]


def test_example_config_ships_auto():
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["asr"]["language"] == "auto"
    assert load(EXAMPLE).asr.language == "auto"
```

```python
# tests/test_job_language.py
"""Язык на стороне демона: закрепление хозяина, отказ и разговор с воркером."""

import pytest
from conftest import metadata

from helper import jobs, language
from helper.config import AsrConfig, Config, TranslatorConfig

VIDEO = "abcdefghijk"


def runner_with(manager, raw: str) -> jobs.JobRunner:
    config = Config(asr=AsrConfig(language=raw), translator=TranslatorConfig())

    return jobs.JobRunner(manager.lease, manager.cache, config)


@pytest.mark.parametrize(
    ("raw", "pin"),
    [
        ("sr", "sr"),
        ("SR", "sr"),
        ("sr-Latn", "sr"),
        ("sh", "sr"),
        ("cnr", "sr"),
        ("hr", "hr"),
        ("auto", None),
        ("AUTO", None),
        ("", None),
    ],
)
def test_pin_is_the_normalised_config_code(manager, raw, pin):
    assert runner_with(manager, raw)._pin() == pin


def test_unknown_pin_is_a_permanent_refusal(manager):
    with pytest.raises(jobs.PermanentJobError) as caught:
        runner_with(manager, "serbian")._pin()

    assert str(caught.value) == "неизвестный язык «serbian»: [asr] language в config.toml"


async def test_unknown_pin_refuses_the_job_before_youtube(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("serbian")
    assert job.retryable is False
    # Метаданных не спрашивали (job.info пуст), звука и воркера не было: отказ
    # не стоит ничего ни YouTube, ни карте.
    assert job.info is None
    assert pipeline.workers == []


async def test_language_refusal_is_not_recreated_after_cooldown(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)
    await pipeline.run(job)
    # Остывание повторяемого отказа прошло бы: окончательный отказ оно не трогает.
    job.failed_at -= jobs.RETRY_COOLDOWN

    assert manager.get_or_create(VIDEO, "ru", True) is job
    assert manager.started == [VIDEO]


@pytest.mark.parametrize(
    ("raw", "sent"),
    [("sh", "sr"), ("SR", "sr"), ("sr-Latn", "sr"), ("cnr", "sr"), ("AUTO", "auto")],
)
async def test_pin_reaches_the_worker_normalised(manager, pipeline, raw, sent):
    pipeline.configure(language=raw)
    job = manager.get_or_create(VIDEO, "ru", True)
    # Ролик без дорожек автора и без метки языка: путь распознавания.
    job.info = metadata()

    await pipeline.run(job)

    args = pipeline.workers[-1].args
    assert args[args.index("--language") + 1] == sent


async def test_worker_refusal_on_stdout_is_permanent(manager, pipeline):
    pipeline.asr_result = {"error": language.refusal("xx"), "permanent": True}
    pipeline.asr_exit = 2
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("xx")
    assert job.retryable is False


async def test_worker_crash_keeps_the_stderr_tail(manager, pipeline):
    pipeline.asr_result = b""
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b"Traceback (most recent call last):\nValueError: boom"
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR не справился: Traceback (most recent call last):\nValueError: boom"
    assert job.retryable is True


async def test_worker_error_reply_reaches_the_viewer(manager, pipeline):
    # Воркер отказал сам, фразой в stdout, а stderr пуст: сегодня зритель видел
    # «ASR не справился: » с пустым хвостом.
    pipeline.asr_result = {"error": "аудио короче половины секунды"}
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b""
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR не справился: аудио короче половины секунды"
    assert job.retryable is True
```

Все три теста ответа воркера идут через настоящий конвейер, а не зовут `_run_asr` напрямую: `job.info` им ставит поддельный `probe`, а с задачи 8 `_run_asr` читает из него заголовок и теги. `test_worker_crash_keeps_the_stderr_tail` проходит и до правки: он сторожит, что падение воркера без JSON-ответа остаётся прежним повторяемым `RuntimeError` с хвостом stderr, а не становится окончательным.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_language.py tests/test_config.py tests/test_job_language.py -q`
Expected: FAIL — `3 errors during collection`, у всех трёх `ImportError: cannot import name 'language' from 'helper'`

- [ ] **Step 3: Модуль кодов `helper/language.py`**

Новый файл. Докстрока модуля: коды языков, общие для демона и воркера распознавания; **только стандартная библиотека** — модуль грузят и демон, и воркер, а `faster_whisper` тянет ctranslate2, av и tokenizers, которым в процессе демона делать нечего (сторожит `test_language_module_imports_only_the_standard_library`). Первой строкой кода — `from __future__ import annotations`.

Таблицы — ровно такие (комментарии по-русски, как ниже):

```python
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
```

Функции (поведение закреплено тестами шага 1):

- `normalize(raw: str | None) -> str | None`, по порядку: `None` → `None`; `code = raw.strip().lower().replace("_", "-")`; `code in NO_LANGUAGE` → `None`; значение режется по `+` (так yt-dlp склеивает языки дорожек), и каждая часть приводится отдельно: первичный подтег до первого `-`, затем `SUBSTITUTES.get(...)`, затем проверка `in WHISPER_CODES`, иначе `raise UnknownLanguage(raw)` — с **исходной** строкой; части сошлись в один код — он и возвращается, разошлись (`en+sr`) — `UnknownLanguage(raw)`. `hr` и `bs` остаются собой: это настоящие коды Whisper, сводит их только `family`.
- `hint(raw)` — `normalize`, но `UnknownLanguage` → `None`: для меток YouTube, где незнакомое — просто «не знаем».
- `family(code)` — `hint(code)`, затем любой код из `BCMS` → `"sr"`. Для сравнения языков, не для декодера.
- `is_bcms(code)` — `family(code) == "sr"`.
- `refusal(raw)` — ровно `f"неизвестный язык «{raw}»: [asr] language в config.toml"` (56 знаков для `serbian`, умещается на бейдж); значение длиннее 11 знаков выводит фразу за 60 (`serbo-croatian` — 63): бейдж обрежет хвост многоточием, полная фраза останется в подсказке, а главное, «неизвестный язык «…»», стоит первым; шаблон закреплён — не резать.

Три прежних определения BCMS (`helper/asr/worker.py:48`, `helper/script.py:25`, `helper/sources/ytdlp.py:35`) в этой задаче не трогать: их заменяют задачи, что меняют эти файлы (воркер — задача 5).

- [ ] **Step 4: Запустить**

Run: `.venv/bin/python -m pytest tests/test_language.py -q`
Expected: PASS, 79 passed

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_job_language.py -q`
Expected: FAIL — 29 failed, 4 passed. Проходят строки `"hr"` и `"auto"` в `test_language_is_normalised_at_load`, `test_missing_file_means_auto` и сторож `test_worker_crash_keeps_the_stderr_tail`. Падают: конфиг хранит значение как есть (`assert 'cnr' == 'sr'`; число из TOML остаётся числом — `assert 5 == '5'`), в журнале нет ошибки, пример отдаёт `'sr'`, `AttributeError: 'JobRunner' object has no attribute '_pin'`, задание с `serbian` доходит до воркера и до `done`, в воркер уходит `--language sh`, а отказ воркера и его фраза об ошибке доходят до задания пустым хвостом stderr (`assert 'ASR не справился: ' == …`).

- [ ] **Step 5: Конфиг и пример конфига**

`helper/config.py`, импорты (строки 23-26): `from dataclasses import dataclass, replace` и `from helper import language` перед `from helper.env import CONFIG_DIR`.

`helper/config.py`, `load` (179-206): сразу после `asr = AsrConfig(**_section(raw, "asr", AsrConfig))` (строка 189):

```python
    # Код языка приводится к виду Whisper здесь, один раз: "sh", "SR" и "sr-Latn" — это
    # sr, и дальше по демону ходит уже код. Незнакомое значение — не ошибка запуска, как
    # и cookies: демон поднимается, а каждое новое задание получает отказ той же фразой
    # (JobRunner._pin). str() — на случай числа вместо строки.
    try:
        asr = replace(asr, language=language.normalize(str(asr.language)) or "auto")
    except language.UnknownLanguage as exc:
        asr = replace(asr, language=exc.raw)
        log.error(language.refusal(exc.raw))
```

`describe()` не меняется: `language` в нём и так строка.

`helper/config.py:66-71` — комментарий над `language: str = "auto"` заменить целиком (прежний говорит «язык берётся из метаданных», а метаданные теперь только подсказка):

```python
    # Язык речи. "auto" — решает определитель: он слушает окна по всей длине ролика
    # и работает всегда, а метка языка от YouTube для него лишь подсказка. Код языка
    # закрепляет язык для КАЖДОГО ролика: Whisper декодирует с ним, даже если звучит
    # другое, а определитель только отмечает расхождение. Закреплять стоит, лишь
    # когда все ролики на одном языке: на чужом звуке поведение декодера не
    # определено, и он начинает выбирать между переводом, транслитерацией и записью
    # услышанного чужим алфавитом — заново в каждом тридцатисекундном окне.
    # load() приводит значение к коду Whisper (helper/language.py), auto остаётся auto.
```

`config.example.toml:21-24` — заменить комментарий «Язык фиксируется жёстко, автодетект не используется…» и строку `language = "sr"` на:

```toml
# Язык речи. "auto" — язык определяет сам демон: определитель слушает окна
# по всей длине ролика и работает всегда, а метка языка от YouTube для него
# лишь подсказка (сербские каналы YouTube бывает помечает английскими).
#
# Код языка ("sr", "en", "ru"…) закрепляет язык для КАЖДОГО ролика: Whisper
# распознаёт с ним, даже если звучит другой язык, а определитель только
# отмечает расхождение. Ставить, лишь если все ролики на одном языке.
# sh, me и cnr годятся и означают sr; регион и письменность отбрасываются
# ("sr-Latn" — это sr). Код, которого Whisper не знает, — ошибка в журнале
# при старте и отказ каждого нового ролика: «неизвестный язык …».
language = "auto"
```

Фразы «определитель работает всегда» и «отмечает расхождение» станут правдой с задачами 5 и 6 этой же ветки. Оба комментария пишутся сразу окончательными, чтобы не править их в каждой задаче: ветка вливается целиком.

- [ ] **Step 6: Демон: закрепление, ранний отказ, ответ воркера**

`helper/jobs.py`, импорты: `from helper import language` перед `from helper.asr.worker import SAMPLE_RATE` (строка 33). Других имён `language` в модуле нет — коллизий нет до задачи 8 (параметр `asr_cues`).

Новый метод `JobRunner._pin(self) -> str | None` — прямо перед `_source_language` (1218):

```python
    def _pin(self) -> str | None:
        """Язык, закреплённый хозяином в [asr] language: код Whisper или None («auto»).

        load() приводит значение к коду, а негодное лишь пишет в журнал: демон должен
        подняться. Задание же с негодным кодом получает отказ человеческой фразой,
        окончательный: повтор с тем же конфигом дал бы то же самое.
        """
        try:
            return language.normalize(self._config.asr.language)
        except language.UnknownLanguage as exc:
            raise PermanentJobError(language.refusal(exc.raw)) from None
```

`JobRunner._pipeline`: вызов `self._pin()` со своим комментарием — сразу после `return` ветки возобновления (`if job.cues: … return`, 778), перед комментарием «Метаданные уже есть…» (780), который остаётся над `if job.info is None:`. Комментарий вызова: негодный код в `[asr] language` — отказ до первого обращения к YouTube и к карте, ни метаданных, ни звука, ни пропуска; отказ окончательный, иначе остывание в `get_or_create` пересоздавало бы задание раз в минуту, как было с трассировкой Whisper; возобновление выше языка не спрашивает — распознавание уже позади. `JobRunner.run` уже превращает `PermanentJobError` в `retryable=False` (752-754), менять его не нужно.

`JobRunner._source_language`, ветка конфига (1229-1232): вместо `configured = self._config.asr.language` / `if configured and configured != "auto": return configured` — `pin = self._pin()` / `if pin: return pin`. Это переходная правка: в воркер уходит уже приведённый код (`sh` → `sr`, `AUTO` → ветка метаданных и `auto`), а сам метод удалит задача 5. Ветку метаданных (1234-1242) не трогать.

`JobRunner._run_asr`, ненулевой код выхода: обе ветки сигналов (1198-1207) остаются первыми; между ними и `raise RuntimeError(f"ASR не справился: …")` (1209) вставить:

```python
            # Воркер отказал сам — фраза в stdout, а не трассировка в stderr. Отказ с
            # "permanent" не пройдёт и при повторе (негодный код языка), поэтому
            # окончательный: остывание не пересоздаёт такое задание. Фраза без флага
            # («аудио короче половины секунды») — отказ повторяемый, но зритель видит её,
            # а не пустой хвост stderr.
            try:
                reply = json.loads(out)
            except ValueError:
                reply = None

            if isinstance(reply, dict) and reply.get("error"):
                if reply.get("permanent") is True:
                    raise PermanentJobError(str(reply["error"]))

                raise RuntimeError(f"ASR не справился: {reply['error']}")
```

`ValueError` ловит и `json.JSONDecodeError` (пустой stdout), и `UnicodeDecodeError`. Не JSON и JSON без `"error"` идут прежним путём, к `RuntimeError` с хвостом stderr. Ветку `"error" in result` после разбора на нулевом коде (1211-1214) не трогать.

`restore()`, `_key_for` и `_cached_variant` `_pin()` не зовут и звать не должны: восстановление после F5 не выполняет конвейер и не обязано отказывать.

- [ ] **Step 7: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_language.py tests/test_config.py tests/test_job_language.py -q`
Expected: PASS, 112 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 265 passed

- [ ] **Step 8: Коммит**

```bash
/usr/bin/git add helper/language.py helper/config.py helper/jobs.py config.example.toml tests/test_language.py tests/test_config.py tests/test_job_language.py
/usr/bin/git commit -m "Коды языков проверяются: sh, me, cnr и sr-Latn — это sr, негодный код — окончательный отказ до обращения к YouTube"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 5: Определитель работает всегда, метка YouTube — только подсказка

**Files:** номера строк — на `98a3ad2`, предыдущие задачи их сдвинули: искать по имени функции.
- Modify: `helper/language.py` — дописать часть определителя в конец файла; импорты `from collections.abc import Sequence` и `from dataclasses import dataclass`
- Modify: `helper/asr/worker.py:1-15` (докстрока модуля, пример вызова на строке 14), `:17-27` (импорты, `SAMPLE_RATE`), `:36-122` (`PROBE_WINDOWS`, `PROBE_SPAN`, `WINDOW_SECONDS`, `BCMS`, `_detect`, `_merge_bcms` — уходят; вместо них `_speech_seconds`, `_detect`, `_window`), `:125-215` (`transcribe`), `:218-259` (`main` → `build_parser` + `main`)
- Modify: `helper/jobs.py:1151-1167` (`JobRunner._run_asr`, argv); `JobRunner._source_language` удаляется целиком, метод `JobRunner._pin` прямо над ним (задача 4) остаётся, а `JobRunner._declaration` встаёт на место `_source_language`, под `_pin`
- Test: `tests/test_language.py` (дописать), `tests/test_worker.py` (новый), `tests/test_job_language.py` (дописать)

**Interfaces:**
- Consumes: задача 4 — коды `helper.language`, `JobRunner._pin`. Задача 3 — фикстура `whisper` (подменяет `faster_whisper.WhisperModel` и `BatchedInferencePipeline`; `whisper.kwargs` — именованные аргументы последнего `BatchedInferencePipeline.transcribe`), фикстура `pipeline` и `metadata`, как в задаче 4.
- Produces:
  - `helper/language.py`: `MIN_SPEECH_SECONDS = 3.0`, `BCMS_FLOOR = 0.2`, `SLAVIC_NEIGHBOURS = frozenset({"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"})`, `SAMPLE_RATE = 16000`, `WINDOW_SAMPLES = 30 * SAMPLE_RATE`; `probe_offsets(n_samples: int, *, windows: int = 8, span: tuple[float, float] = (0.10, 0.90), window: int = WINDOW_SAMPLES) -> list[int]`; `@dataclass(frozen=True) class Detection(language: str | None, confidence: float | None, bcms_share: float, floor: bool, votes: int, probed: int, windows: tuple[tuple[str | None, float, float], ...])`; `summarize(windows: Sequence[tuple[float, Sequence[tuple[str, float]]]]) -> Detection`; `@dataclass(frozen=True) class LanguageVerdict(effective: str, declared: str | None, declared_by: str | None, detected: str | None, confidence: float | None, reason: str, source: str = "asr", track: str | None = None, verified: bool | None = None)` с `mismatch` (свойство), `note() -> str | None`, `wire() -> dict`, `from_wire(cls, data: object) -> LanguageVerdict | None`; `decide(pin: str | None, hint: str | None, detection: Detection, *, fallback: str = "sr") -> LanguageVerdict`.
  - `helper/asr/worker.py` (модуль языков импортирован как `lang`, см. шаг 7): `_detect(model, audio: np.ndarray) -> lang.Detection`; `transcribe(audio, *, model_name=DEFAULT_MODEL, compute_type="float16", language: str = "sr", hint: str | None = None, initial_prompt=None, hotwords=None, batch_size=16, vad_filter=True) -> dict` (`initial_prompt` до задачи 8); `build_parser() -> argparse.ArgumentParser` с новым `--hint CODE`; ответ `{"language": verdict.effective, "verdict": verdict.wire(), "detection": {"bcmsShare", "votes", "probed", "windows"}, "duration", "model", "cues"}`; негодный `--language` → stdout `{"error": refusal, "permanent": true}`, код 2.
  - `JobRunner._declaration(self, job: Job) -> tuple[str | None, str | None]` — `(self._pin(), language.hint(job.info.audio_language) if job.info else None)`; воркер получает `--language <pin или auto>` и `--hint <код>`, когда метка есть.

- [ ] **Step 1: Написать падающие тесты правила**

Дописать в конец `tests/test_language.py` (импорты те же):

```python
# ── Определитель: окна, правило, решение ─────────────────────────────────────────


def test_detector_constants_are_pinned():
    # Смена любой из них меняет распознавание закэшированных роликов: правишь здесь —
    # поднимай helper.cache.PROMPT_VERSION.
    assert language.MIN_SPEECH_SECONDS == 3.0
    assert language.BCMS_FLOOR == 0.2
    assert language.SLAVIC_NEIGHBOURS == {"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"}
    assert language.SAMPLE_RATE == 16000
    assert language.WINDOW_SAMPLES == 30 * 16000


@pytest.mark.parametrize(
    ("seconds", "offsets"),
    [
        (5, [0]),
        (30, [0]),
        (31.25, [0, 4286, 15714, 20000]),
        (37.5, [60000]),
        (20 * 60, [1920000, 4045714, 6171429, 8297143, 10422857, 12548571, 14674286, 16800000]),
    ],
)
def test_probe_offsets(seconds, offsets):
    n_samples = int(seconds * language.SAMPLE_RATE)

    assert language.probe_offsets(n_samples) == offsets
    assert all(0 <= start <= max(n_samples - language.WINDOW_SAMPLES, 0) for start in offsets)


def window(*shares: tuple[str, float], speech: float = 25.0):
    return (speech, list(shares))


# 3XJLfTSZy6I: сербская речь без метаданных. en сверху в пяти окнах из восьми,
# в среднем en 0.55 против 0.44 у сербского семейства.
IZA_MRAKA = [window(("en", 0.80), ("sr", 0.10), ("hr", 0.06), ("bs", 0.03), ("de", 0.01))] * 5 + [
    window(("sr", 0.45), ("hr", 0.27), ("bs", 0.15), ("en", 0.10), ("de", 0.03)),
    window(("hr", 0.40), ("sr", 0.35), ("en", 0.15), ("bs", 0.10)),
    window(("sr", 0.50), ("bs", 0.20), ("hr", 0.15), ("en", 0.15)),
]
# Q2QcYOBk0Jo (Juske): метка YouTube en, речь сербская.
JUSKE = [window(("sr", 0.60), ("hr", 0.25), ("bs", 0.12), ("en", 0.03))] * 8
# 3UbtKc9MlXg: английский, сербского семейства ноль во всех восьми окнах.
ENGLISH = [window(("en", 0.95), ("de", 0.03), ("nl", 0.02))] * 8
# Русская речь, у которой сербского семейства 0.3: выше порога, но язык свой.
RUSSIAN = [window(("ru", 0.60), ("sr", 0.20), ("hr", 0.10), ("uk", 0.10))] * 8
# Клип почти без речи: ни одно окно не голосует.
SILENCE = [window(speech=0.0)] * 8


def test_floor_turns_the_english_bias_into_serbian():
    detection = language.summarize(IZA_MRAKA)

    assert [w[0] for w in detection.windows].count("en") == 5
    assert detection.language == "sr"
    assert detection.floor is True
    assert detection.bcms_share == pytest.approx(0.44)
    assert detection.confidence == pytest.approx(0.44)
    assert (detection.votes, detection.probed) == (8, 8)


def test_serbian_speech_under_an_english_label():
    detection = language.summarize(JUSKE)

    assert (detection.language, detection.floor) == ("sr", False)
    assert detection.confidence == pytest.approx(0.97)

    verdict = language.decide(None, "en", detection)

    assert verdict.effective == "sr"
    assert (verdict.declared, verdict.declared_by) == ("en", "metadata")
    assert verdict.reason == "detector"
    assert verdict.mismatch is True
    assert verdict.note() == "YouTube указал en, по речи sr"


def test_english_without_bcms_stays_english():
    detection = language.summarize(ENGLISH)

    assert (detection.language, detection.floor) == ("en", False)
    assert detection.bcms_share == 0.0
    assert detection.confidence == pytest.approx(0.95)


def test_slavic_neighbour_keeps_its_language_over_the_floor():
    detection = language.summarize(RUSSIAN)

    assert (detection.language, detection.floor) == ("ru", False)
    assert detection.bcms_share == pytest.approx(0.30)
    assert detection.confidence == pytest.approx(0.60)


@pytest.mark.parametrize(
    ("shares", "winner", "floor"),
    [
        ([("en", 0.80), ("sr", 0.20)], "sr", True),
        ([("en", 0.81), ("sr", 0.19)], "en", False),
        ([("mk", 0.50), ("sr", 0.30), ("bg", 0.20)], "mk", False),
    ],
)
def test_floor_boundary(shares, winner, floor):
    detection = language.summarize([window(*shares)])

    assert (detection.language, detection.floor) == (winner, floor)


def test_windows_without_speech_or_answer_do_not_vote():
    detection = language.summarize(
        [
            window(("en", 0.99), speech=2.9),
            window(speech=0.0),
            # Речь есть, но определитель на этом окне упал: ответа нет.
            window(speech=25.0),
        ]
    )

    assert detection == language.Detection(
        language=None,
        confidence=None,
        bcms_share=0.0,
        floor=False,
        votes=0,
        probed=3,
        windows=((None, 0.0, 2.9), (None, 0.0, 0.0), (None, 0.0, 25.0)),
    )


def test_three_seconds_of_speech_vote_and_a_window_reports_its_family():
    detection = language.summarize([window(("hr", 0.40), ("sr", 0.35), ("en", 0.25), speech=3.0)])

    assert detection.votes == 1
    assert detection.windows == (("sr", pytest.approx(0.75), 3.0),)


@pytest.mark.parametrize(
    ("pin", "hint", "windows", "effective", "reason", "declared_by"),
    [
        ("sr", None, ENGLISH, "sr", "pin", "config"),
        ("hr", None, JUSKE, "hr", "pin", "config"),
        ("en", "sr", SILENCE, "en", "pin", "config"),
        (None, "en", JUSKE, "sr", "detector", "metadata"),
        (None, None, IZA_MRAKA, "sr", "floor", None),
        (None, "en", SILENCE, "en", "hint", "metadata"),
        (None, None, SILENCE, "sr", "fallback", None),
    ],
)
def test_decide_precedence(pin, hint, windows, effective, reason, declared_by):
    detection = language.summarize(windows)
    verdict = language.decide(pin, hint, detection)

    assert (verdict.effective, verdict.reason, verdict.declared_by) == (effective, reason, declared_by)
    assert verdict.declared == (pin or hint)
    assert (verdict.detected, verdict.confidence) == (detection.language, detection.confidence)
    assert (verdict.source, verdict.track, verdict.verified) == ("asr", None, None)


def manual(effective: str, declared: str | None, declared_by: str | None) -> language.LanguageVerdict:
    return language.LanguageVerdict(
        effective=effective,
        declared=declared,
        declared_by=declared_by,
        detected=None,
        confidence=None,
        reason="track",
        source="manual",
        track="sr-Latn",
    )


@pytest.mark.parametrize(
    ("pin", "hint", "windows", "mismatch", "note"),
    [
        ("sr", None, ENGLISH, True, "в настройках sr, по речи en"),
        ("hr", None, JUSKE, False, None),
        (None, "en", JUSKE, True, "YouTube указал en, по речи sr"),
        (None, None, IZA_MRAKA, False, None),
        (None, "en", SILENCE, False, "речи почти нет, язык взят у YouTube: en"),
        (None, None, SILENCE, False, "речи почти нет, язык принят за sr"),
    ],
)
def test_mismatch_and_note_from_speech(pin, hint, windows, mismatch, note):
    verdict = language.decide(pin, hint, language.summarize(windows))

    assert verdict.mismatch is mismatch
    assert verdict.note() == note
    assert note is None or len(note) <= 60


@pytest.mark.parametrize(
    ("declared", "declared_by", "mismatch", "note"),
    [
        ("en", "metadata", True, "YouTube указал en, дорожка автора sr"),
        ("ru", "config", True, "в настройках ru, дорожка автора sr"),
        ("hr", "config", False, None),
        (None, None, False, None),
    ],
)
def test_mismatch_and_note_for_an_author_track(declared, declared_by, mismatch, note):
    verdict = manual("sr", declared, declared_by)

    assert verdict.mismatch is mismatch
    assert verdict.note() == note
    assert note is None or len(note) <= 60


def test_wire_carries_exactly_the_agreed_keys():
    verdict = language.decide(None, "en", language.summarize(JUSKE))

    assert verdict.wire() == {
        "effective": "sr",
        "declared": "en",
        "declaredBy": "metadata",
        "detected": "sr",
        "confidence": 0.97,
        "reason": "detector",
        "source": "asr",
        "track": None,
        "verified": None,
        "mismatch": True,
        "note": "YouTube указал en, по речи sr",
    }


def test_wire_rounds_confidence_to_two_places():
    verdict = language.LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.4361, reason="floor"
    )

    assert verdict.wire()["confidence"] == 0.44


def test_from_wire_round_trips():
    asr = language.LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected="sr", confidence=0.97, reason="detector"
    )
    track = manual("sr", None, None)

    assert language.LanguageVerdict.from_wire(asr.wire()) == asr
    assert language.LanguageVerdict.from_wire(track.wire()) == track


@pytest.mark.parametrize(
    "data", [None, "sr", [], {}, {"effective": ""}, {"effective": 5}, {"effective": "xx"}, {"effective": "SR"}]
)
def test_from_wire_refuses_what_is_not_a_verdict(data):
    assert language.LanguageVerdict.from_wire(data) is None


def test_from_wire_ignores_derived_and_broken_fields():
    verdict = language.LanguageVerdict.from_wire(
        {"effective": "en", "mismatch": True, "note": "чужое", "confidence": "high", "declaredBy": 7, "verified": "yes"}
    )

    assert verdict == language.LanguageVerdict(
        effective="en", declared=None, declared_by=None, detected=None, confidence=None, reason="detector"
    )
    assert verdict.mismatch is False
```

Числа окон взяты так, чтобы средние совпали с полевыми записями: у `IZA_MRAKA` en в сумме 4.40 из 8 (0.55), семья 3.52 из 8 (0.44); у `JUSKE` семья 0.97 в каждом окне.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_language.py -q`
Expected: FAIL — 44 failed, 79 passed: новые тесты падают на `AttributeError: module 'helper.language' has no attribute …` (`MIN_SPEECH_SECONDS`, `SAMPLE_RATE`, `summarize`, `decide`, `LanguageVerdict`), тесты кодов из задачи 4 проходят

- [ ] **Step 3: Правило определителя в `helper/language.py`**

Дописать в конец файла. Константы — с комментарием над блоком: любая из них меняет распознавание уже закэшированных роликов, поэтому правка требует поднять `helper.cache.PROMPT_VERSION`:

```python
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

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 30 * SAMPLE_RATE
```

`probe_offsets` — окна лежат внутри `span` длины, как сегодня у воркера (первое начинается на 10%, последнее кончается на 90%); начала округляются `round` (не `int`: значения теста, например 4286 и 6171429, зависят от этого) и прижимаются к `[0, n_samples - window]`, поэтому на записи от 30 до 37,5 с окна целые и перекрываются, а не уходят в минус:

```python
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
```

В докстроку `probe_offsets` перенести обоснование из `helper/asr/worker.py:36-40`: с начала брать нельзя — там заставка, музыка или реклама спонсора; у KCR-dkZIhxY первая реплика на 21-й секунде.

`Detection` — `@dataclass(frozen=True)` с полями в порядке из Interfaces; комментарий у `windows`: по окну — верхняя семья или `None` (окно не голосовало), её доля, секунды речи.

`summarize(windows)` — чистая функция, по порядку:
1. Для каждого окна `(speech, distribution)` распределение сводится по семье: `family(code)` для каждого кода, доли bs/hr/sr складываются под `"sr"`; код, у которого `family` — `None`, пропускается.
2. Окно голосует, если `speech >= MIN_SPEECH_SECONDS` **и** сведённое распределение не пусто (пустое — определитель на окне упал или его не звали). Не голосует — запись `(None, 0.0, speech)`; голосует — `(верхняя семья окна, её доля, speech)`.
3. Ни одного голоса — `Detection(None, None, 0.0, False, 0, len(windows), записи)`.
4. Иначе доли каждой семьи усредняются по голосующим окнам; `bcms_share` — среднее `"sr"` (0.0, если его нет); победитель — наибольшее среднее.
5. Победитель не `"sr"`, `bcms_share >= BCMS_FLOOR` и победитель не в `SLAVIC_NEIGHBOURS` → победитель `"sr"`, `floor=True`. `confidence` — среднее победителя (при пороге это и есть `bcms_share`).

`LanguageVerdict` — `@dataclass(frozen=True)`, поля в порядке из Interfaces.
- `mismatch` (свойство): сравниваемый язык — `detected` при `source == "asr"`, `effective` при `source == "manual"`; `True`, только когда известны и `declared`, и сравниваемый, и их `family()` различны (`hr` против `sr` — не расхождение).
- `note()` — ровно эти шаблоны, по порядку: при `mismatch` и `declared_by == "metadata"`: для asr `f"YouTube указал {declared}, по речи {detected}"`, для manual `f"YouTube указал {declared}, дорожка автора {effective}"`; при `mismatch` и `declared_by == "config"`: `f"в настройках {declared}, по речи {detected}"` / `f"в настройках {declared}, дорожка автора {effective}"`; иначе `reason == "hint"` → `f"речи почти нет, язык взят у YouTube: {effective}"`; `reason == "fallback"` → `f"речи почти нет, язык принят за {effective}"`; иначе `None`.
- `wire()` — ровно ключи `effective, declared, declaredBy, detected, confidence, reason, source, track, verified, mismatch, note`; `confidence` — `round(…, 2)` или `None`; `mismatch` и `note` — вычисленные.
- `from_wire(data)` (`@classmethod`) — никогда не бросает: не словарь → `None`; `effective` не строка или не из `WHISPER_CODES` (без приведения: `"SR"` и `"sh"` — `None`) → `None`. Остальное терпимо: `declared`, `declaredBy`, `detected`, `track` — непустая строка или `None`; `confidence` — `int`/`float`, но не `bool`, иначе `None`, хранится как `float`; `reason` — строка или `"detector"`; `source` — `"manual"`, только если так и записано, иначе `"asr"`; `verified` — `bool` или `None`; `mismatch` и `note` не читаются.

`decide(pin, hint, detection, *, fallback="sr")` — `pin` и `hint` уже коды Whisper (их приводят вызывающие: `normalize` и `hint`). `effective`: `pin`, если есть; иначе `detection.language`, если `detection.votes`; иначе `hint`; иначе `fallback` — никогда не `""` и не `None`. `reason`: `"pin"` | (`"floor"` если `detection.floor` иначе `"detector"`) | `"hint"` | `"fallback"`. `declared = pin or hint`; `declared_by = "config" if pin else ("metadata" if hint else None)`; `detected`, `confidence` — из `detection`; `source` и прочее — умолчания. Подсказка голосующие окна не перевешивает (случай Juske).

- [ ] **Step 4: Запустить**

Run: `.venv/bin/python -m pytest tests/test_language.py -q`
Expected: PASS, 123 passed

- [ ] **Step 5: Написать падающие тесты воркера и демона**

```python
# tests/test_worker.py
"""Воркер распознавания без карты: окна определителя, решение о языке, разбор аргументов."""

import json
import sys

import numpy as np
import pytest

from helper import language
from helper.asr import worker

SR = language.SAMPLE_RATE
WINDOW = language.WINDOW_SAMPLES


class Model:
    """WhisperModel для _detect: отвечает распределениями по очереди и помнит, что слушал."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        # Длины отрывков, отданных detect_language, по порядку.
        self.heard: list[int] = []

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        assert vad_filter is True
        self.heard.append(audio.size)
        answer = self.answers.pop(0)

        if isinstance(answer, Exception):
            raise answer

        return answer[0][0], answer[0][1], answer


@pytest.fixture
def vad(monkeypatch):
    """VAD без silero: речь — это ненулевые отсчёты отрывка."""
    import faster_whisper.vad

    def speech(audio, vad_options=None, sampling_rate=16000, **kwargs):
        voiced = int(np.count_nonzero(audio))

        return [{"start": 0, "end": voiced}] if voiced else []

    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", speech)


def recording(seconds: float, speech: list[float]) -> np.ndarray:
    """Тишина длиной seconds; в начале каждого окна — столько секунд «речи», сколько в speech."""
    audio = np.zeros(int(seconds * SR), dtype="<f4")

    for offset, voiced in zip(language.probe_offsets(audio.size), speech):
        audio[offset : offset + int(voiced * SR)] = 0.1

    return audio


def test_detect_hears_only_windows_with_speech(vad):
    model = Model(*[[("sr", 0.9), ("en", 0.1)]] * 6)

    detection = worker._detect(model, recording(20 * 60, [30, 30, 30, 2, 0, 30, 30, 30]))

    assert model.heard == [WINDOW] * 6
    assert (detection.votes, detection.probed) == (6, 8)
    assert detection.language == "sr"
    assert [w[2] for w in detection.windows] == [30.0, 30.0, 30.0, 2.0, 0.0, 30.0, 30.0, 30.0]


def test_window_where_the_detector_fails_does_not_vote(vad):
    # Без доли сербской семьи: исход не зависит от BCMS_FLOOR и его калибровки (задача 12).
    model = Model(RuntimeError("CUDA"), *[[("en", 0.95), ("de", 0.05)]] * 7)

    detection = worker._detect(model, recording(20 * 60, [30] * 8))

    assert (detection.votes, detection.probed) == (7, 8)
    assert detection.windows[0] == (None, 0.0, 30.0)
    assert detection.language == "en"


def test_short_audio_is_one_window_on_the_same_path(vad):
    audio = np.full(20 * SR, 0.1, dtype="<f4")
    model = Model([("hr", 0.45), ("sr", 0.40), ("en", 0.15)])

    detection = worker._detect(model, audio)

    assert model.heard == [audio.size]
    assert detection.probed == 1
    # Семья, а не верхняя метка: сегодня этот путь отдавал hr 0.45.
    assert detection.language == "sr"
    assert detection.confidence == pytest.approx(0.85)


def test_audio_between_30_and_37_5_seconds_gives_whole_windows(vad):
    model = Model(*[[("sr", 0.9), ("en", 0.1)]] * 4)

    detection = worker._detect(model, np.full(int(31.25 * SR), 0.1, dtype="<f4"))

    assert model.heard == [WINDOW] * 4
    assert detection.probed == 4


def test_no_votes_means_one_pass_over_the_whole_file(vad):
    audio = recording(20 * 60, [2] * 8)
    # Десять секунд речи в заставке, вне окон: окна начинаются со 120-й секунды.
    audio[: 10 * SR] = 0.1
    model = Model([("sr", 0.8), ("en", 0.2)])

    detection = worker._detect(model, audio)

    assert model.heard == [audio.size]
    assert (detection.votes, detection.probed) == (1, 9)
    assert detection.windows[-1] == ("sr", pytest.approx(0.8), 26.0)
    assert detection.language == "sr"


def test_silent_file_gets_no_vote_and_no_model_call(vad):
    model = Model()

    detection = worker._detect(model, np.zeros(20 * 60 * SR, dtype="<f4"))

    assert model.heard == []
    assert (detection.language, detection.votes, detection.probed) == (None, 0, 9)
    assert detection.windows[-1] == (None, 0.0, 0.0)


def test_short_silent_audio_makes_no_extra_pass(vad):
    model = Model()

    detection = worker._detect(model, np.zeros(10 * SR, dtype="<f4"))

    assert model.heard == []
    assert (detection.votes, detection.probed) == (0, 1)


SERBIAN = language.Detection(
    language="sr", confidence=0.97, bcms_share=0.97, floor=False, votes=8, probed=8,
    windows=(("sr", 0.97, 25.0),) * 8,
)
ENGLISH = language.Detection(
    language="en", confidence=0.95, bcms_share=0.05, floor=False, votes=8, probed=8,
    windows=(("en", 0.95, 25.0),) * 8,
)
SILENCE = language.Detection(
    language=None, confidence=None, bcms_share=0.0, floor=False, votes=0, probed=9,
    windows=((None, 0.0, 0.0),) * 9,
)
AUDIO = np.zeros(5 * SR, dtype="<f4")


def detector_says(monkeypatch, detection) -> list[int]:
    """_detect отвечает готовым Detection; список — длины записей, на которых его звали."""
    heard: list[int] = []

    def detect(model, audio):
        heard.append(audio.size)

        return detection

    monkeypatch.setattr(worker, "_detect", detect)

    return heard


def test_detector_runs_even_with_a_pin(whisper, monkeypatch):
    heard = detector_says(monkeypatch, ENGLISH)

    result = worker.transcribe(AUDIO, language="sr")

    assert heard == [AUDIO.size]
    assert whisper.kwargs["language"] == "sr"
    assert result["language"] == "sr"
    assert result["verdict"]["reason"] == "pin"
    assert result["verdict"]["detected"] == "en"
    assert result["verdict"]["note"] == "в настройках sr, по речи en"


def test_speech_outweighs_the_youtube_label(whisper, monkeypatch):
    detector_says(monkeypatch, SERBIAN)

    result = worker.transcribe(AUDIO, language="auto", hint="en-US")

    assert whisper.kwargs["language"] == "sr"
    assert (result["verdict"]["declared"], result["verdict"]["declaredBy"]) == ("en", "metadata")
    assert result["verdict"]["mismatch"] is True


@pytest.mark.parametrize(
    ("hint", "decoded", "reason"),
    [(None, "sr", "fallback"), ("en-US", "en", "hint"), ("xx-YY", "sr", "fallback")],
)
def test_without_speech_whisper_never_gets_an_empty_language(whisper, monkeypatch, hint, decoded, reason):
    detector_says(monkeypatch, SILENCE)

    result = worker.transcribe(AUDIO, language="auto", hint=hint)

    assert whisper.kwargs["language"] == decoded
    assert result["language"] == decoded
    assert result["verdict"]["reason"] == reason
    assert result["verdict"]["detected"] is None


def test_result_carries_the_verdict_and_the_windows(whisper, monkeypatch):
    detector_says(monkeypatch, SERBIAN)

    result = worker.transcribe(AUDIO, language="auto")

    assert set(result) == {"language", "verdict", "detection", "duration", "model", "cues"}
    assert result["detection"] == {
        "bcmsShare": 0.97,
        "votes": 8,
        "probed": 8,
        "windows": [["sr", 0.97, 25.0]] * 8,
    }
    json.dumps(result)


def test_unknown_pin_fails_before_the_model_loads(monkeypatch):
    import faster_whisper

    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda *a, **k: pytest.fail("модель грузить рано"))

    with pytest.raises(language.UnknownLanguage):
        worker.transcribe(AUDIO, language="serbian")


def test_parser_keeps_the_cli_contract():
    args = worker.build_parser().parse_args([])

    # helper/measure.py не передаёт --language и рассчитывает на sr.
    assert args.language == "sr"
    assert args.hint is None
    assert (args.model, args.compute_type, args.batch_size) == (worker.DEFAULT_MODEL, "float16", 16)
    assert (args.no_vad, args.initial_prompt, args.hotwords, args.pcm) == (False, None, None, None)


def test_main_refuses_an_unknown_pin_before_reading_audio(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["helper.asr.worker", "--language", "serbian"])
    monkeypatch.setattr(worker, "transcribe", lambda *a, **k: pytest.fail("до распознавания доходить нельзя"))

    assert worker.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "неизвестный язык «serbian»: [asr] language в config.toml",
        "permanent": True,
    }
```

Последний тест заодно проверяет порядок: под захватом вывода pytest чтение stdin бросает `OSError`, так что отказ обязан случиться раньше чтения звука.

В `tests/test_job_language.py` добавить импорт `from helper.asr import worker` после `from helper import jobs, language` и дописать в конец:

```python
# ── Объявленное уходит в воркер подсказкой ───────────────────────────────────────


def test_declaration_is_the_pin_and_the_normalised_label(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert runner_with(manager, "SR")._declaration(job) == ("sr", None)

    job.info = metadata(audio_language="en-US")

    assert runner_with(manager, "SR")._declaration(job) == ("sr", "en")
    assert runner_with(manager, "auto")._declaration(job) == (None, "en")

    job.info = metadata(audio_language="xx-YY")

    assert runner_with(manager, "auto")._declaration(job) == (None, None)


@pytest.mark.parametrize(
    ("pin", "audio_language", "sent", "hint"),
    [
        ("auto", "en-US", "auto", "en"),
        ("auto", None, "auto", None),
        ("auto", "xx-YY", "auto", None),
        ("sr", "en", "sr", "en"),
        ("sh", None, "sr", None),
    ],
)
async def test_worker_argv_parses_with_the_worker_parser(manager, pipeline, pin, audio_language, sent, hint):
    pipeline.configure(language=pin)
    job = manager.get_or_create(VIDEO, "ru", True)
    job.info = metadata(audio_language=audio_language)

    await pipeline.run(job)

    args = pipeline.workers[-1].args
    parsed = worker.build_parser().parse_args(args[3:])

    assert args[1:3] == ["-m", "helper.asr.worker"]
    assert (parsed.language, parsed.hint) == (sent, hint)
```

Разбор argv настоящим парсером воркера — контракт демона и воркера: любой флаг, который демон шлёт, а воркер не знает, уронит тест на `SystemExit` argparse.

- [ ] **Step 6: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_worker.py tests/test_job_language.py -q`
Expected: FAIL — 22 failed, 20 passed (проходят тесты задачи 4). В `test_worker.py` все 16: старый `_detect` зовёт модель на каждом окне и отдаёт кортеж (`assert [480000, …] == […]`, `AttributeError: 'tuple' object has no attribute 'votes'` (на 20 с — `… no attribute 'probed'`), на 31,25 с — укороченные окна `[450000, …]`, на тишине — `IndexError: pop from empty list`: заглушке нечего ответить), при закрепе старый `transcribe` определитель не зовёт (`assert [] == [80000]`), а при `auto` распаковывает его ответ (`TypeError: cannot unpack non-iterable Detection object`), `TypeError: transcribe() got an unexpected keyword argument 'hint'`, `Failed: модель грузить рано`, `AttributeError: module 'helper.asr.worker' has no attribute 'build_parser'`, `OSError: pytest: reading from stdin while output is captured!`. В `test_job_language.py` 6: `AttributeError: 'JobRunner' object has no attribute '_declaration'` и тот же `build_parser`

- [ ] **Step 7: Воркер**

`helper/asr/worker.py`:

- Импорты: `import math` — к стандартным, между `json` и `sys` (секунды окон в ответе, см. ниже); после `import helper.env` (строка 25) — `from helper import language as lang` с комментарием, почему не `language`: параметры `transcribe` называются `language` и `hint` и затенили бы модуль и его функцию. `SAMPLE_RATE = lang.SAMPLE_RATE` вместо `16000` (строка 27): одно определение; `jobs.py:33` и `spikes/asr_real.py:31` импортируют его отсюда и продолжают работать.
- Удалить строки 36-122: `PROBE_WINDOWS`, `PROBE_SPAN`, `WINDOW_SECONDS` (раскладка теперь `lang.probe_offsets`), `BCMS` (теперь `lang.family`), старые `_detect` и `_merge_bcms`. Вместо них три функции (поведение закреплено семью тестами `_detect` шага 5):
  - `_speech_seconds(audio: np.ndarray) -> float` — секунды речи в отрывке: сумма `end - start` по `get_speech_timestamps(audio)` из `faster_whisper.vad`, делённая на `SAMPLE_RATE`. Докстрока: тот же VAD с теми же умолчаниями, что у `detect_language(vad_filter=True)`, — ровно столько определитель и услышит.
  - `_window(model, audio: np.ndarray, speech: float) -> tuple[float, list[tuple[str, float]]]` — одна запись для `summarize`. Речи меньше `lang.MIN_SPEECH_SECONDS` — `(speech, [])`, модель на таком отрывке не зовётся. Иначе распределение — третий элемент ответа `model.detect_language(audio=audio, vad_filter=True)`, запись `(speech, list(probs))`. Исключение из `detect_language` — тоже `(speech, [])`: `except Exception` с пометкой `# noqa: BLE001 — одно окно не должно ронять распознавание`.
  - `_detect(model, audio: np.ndarray) -> lang.Detection` — окна по `lang.probe_offsets(audio.size)`, каждое `audio[offset : offset + lang.WINDOW_SAMPLES]` со своей речью по `_speech_seconds`, и `lang.summarize` по всем записям. Если не проголосовало ни одно окно и запись длиннее окна (`audio.size > lang.WINDOW_SAMPLES`), последней записью идёт проход по всему файлу, `_window(model, audio, min(30.0, _speech_seconds(audio)))`, и `summarize` считается заново.

  Импорт `get_speech_timestamps` из `faster_whisper.vad` — внутри `_speech_seconds`, не на уровне модуля: `jobs.py` импортирует этот модуль ради `SAMPLE_RATE`, и faster_whisper в процессе демона не нужен; тест подменяет `faster_whisper.vad.get_speech_timestamps`, и подмена видна только при чтении атрибута во время вызова. Докстрока `_detect`: модель приходит уже загруженной (одна на определение и распознавание); окна раскладываем сами, потому что штатный `language_detection_segments` — это «первые N×30 секунд речи подряд», а порог 0.5 обрывает перебор на первом окне; окно, где речи меньше `MIN_SPEECH_SECONDS`, модели не отдаётся — на набивке она всё равно ответила бы, и тишина голосовала бы наравне с речью; упавшее окно не голосует; не проголосовало ни одно окно, а запись длиннее окна — один проход по первым тридцати секундам речи всего файла, последней записью в `windows` (на ролике длиннее 37,5 с — девятой; речь могла оказаться между окнами), и модель на нём зовут, только если речи в файле не меньше `MIN_SPEECH_SECONDS`. Прежняя докстрока («Отрывок берётся с 40% длительности») и комментарий «Окно могло целиком оказаться тишиной — VAD вернёт пусто» неверны и уходят вместе с функцией.

- `transcribe` (125-215): новый параметр `hint: str | None = None` сразу после `language`; остальная подпись прежняя (`initial_prompt` остаётся до задачи 8). Порядок внутри: импорт faster_whisper как был; `pin = lang.normalize(language)` **до** `WhisperModel(...)` — негодный код бросает `UnknownLanguage`, не загрузив весов (`"auto"` входит в `NO_LANGUAGE`, так что отдельная проверка `language == "auto"` не нужна); `WhisperModel(...)`; `detection = _detect(model, audio)` — **всегда**, и при закреплённом языке; `verdict = lang.decide(pin, lang.hint(hint), detection)`; в `pipeline.transcribe` — `language=verdict.effective`. Комментарий над этим аргументом (156-158, «Язык фиксируется жёстко… смотрит только первые 30 секунд») заменить: язык решён выше и никогда не пустой и не `None`; собственное определение библиотеки слушает только первые тридцать секунд речи, а там интро; bs/hr/sr для неё — три токена одного языка с произвольным выбором. Блок `detected, confidence = "", 0.0` / `if language == "auto": …` (142-150) удалить. Ответ (205-215):

```python
    return {
        # Язык, с которым распознавали: демон латинизирует по нему реплики.
        "language": verdict.effective,
        "verdict": verdict.wire(),
        # Окна определителя — для журнала демона и для подбора порогов по нему.
        "detection": {
            "bcmsShare": round(detection.bcms_share, 2),
            "votes": detection.votes,
            "probed": detection.probed,
            # Секунды речи — вниз: окно, показанное с MIN_SPEECH_SECONDS и больше, ушло в модель.
            "windows": [
                [code, round(share, 2), math.floor(speech * 10) / 10]
                for code, share, speech in detection.windows
            ],
        },
        "duration": info.duration,
        "model": model_name,
        "cues": [c for c in cues if c["text"]],
    }
```

  Ключей `detected` и `confidence` верхнего уровня больше нет: они живут в `verdict`. Демон читает их только в мёртвой строке журнала `jobs.py:932-934`, которую заменяет `_accept_asr` в задаче 6; до неё строка остаётся мёртвой, как и была.

- `build_parser() -> argparse.ArgumentParser` — вынести из `main` (219-231) все флаги как есть, плюс `parser.add_argument("--hint", default=None, help="метка языка от YouTube, только подсказка")` сразу после `--language`. У `--language` умолчание остаётся `"sr"`, комментарий: код, закреплённый хозяином, или auto; на умолчание `sr` рассчитывает `helper/measure.py`, который язык не передаёт.
- `main()`: `args = build_parser().parse_args()`; **до** чтения звука (234) — проверка закрепления:

```python
    try:
        lang.normalize(args.language)
    except lang.UnknownLanguage as exc:
        print(json.dumps({"error": lang.refusal(exc.raw), "permanent": True}, ensure_ascii=False))
        return 2
```

  с комментарием: отказ стоит ровно разбора аргументов; код 2 и `"permanent"` — демон делает из этого окончательный отказ (`_run_asr`, задача 4). В вызов `transcribe` добавить `hint=args.hint`. Прочие JSON-ошибки (`на вход не пришло аудио`, `аудио короче половины секунды`) и их код 1 не менять.
- Докстрока модуля, пример на строке 14: `python -m helper.asr.worker --language auto --hint en` и строка под ним: `--language` — код, закреплённый хозяином, или auto; `--hint` — метка языка от YouTube; определитель слушает запись в любом случае, решение — `helper.language.decide`.

`helper/measure.py` не меняется: без `--language` воркер берёт закрепление `sr`, и определитель теперь пройдёт до восьми окон по одному энкодеру перед замеряемым распознаванием; пик памяти по-прежнему даёт пакетное распознавание. Тест оснастки из задачи 3, что зовёт настоящий `transcribe` (`test_whisper_fixture_runs_the_real_worker_transcribe`), после этой задачи гоняет настоящий Silero VAD — фикстура `whisper` его не подменяет: на процессоре, без сети и карты, около 0,1 с на 5 с тишины. Речи в нулях нет, и `detect_language` подделки даже не зовётся. Тесты `transcribe` этой задачи подменяют `worker._detect` (`detector_says`), а прямые тесты `_detect` идут на своей заглушке `Model` и фикстуре `vad`.

- [ ] **Step 8: Демон: объявленное — подсказка**

`helper/jobs.py`: удалить метод `_source_language` целиком (после задачи 4 его ветка конфига зовёт `self._pin()`); метод `_pin`, стоящий прямо над ним, остаётся; `_declaration` — на месте `_source_language`, под `_pin`:

```python
    def _declaration(self, job: Job) -> tuple[str | None, str | None]:
        return self._pin(), (language.hint(job.info.audio_language) if job.info else None)
```

Докстрока: что сказано о языке до звука — (закрепление хозяина, метка YouTube); решает не это, а воркер (`helper.language.decide`), определитель слушает всегда; метка YouTube — только подсказка: yt-dlp выводит её из ASR-дорожки YouTube, а та помечает сербские каналы английскими (Juske), у сербских роликов её обычно нет вовсе (60 из 60 в корпусе — замер из прежней докстроки); незнакомая метка — просто «не знаем». Сюда потом придёт объявление на ролик и на ссылку («Отложено»): объявление для решения, ключа кэша и выбора дорожки читается только здесь; исключения — проверка закрепления в начале `_pipeline` (`_pin`) и восстановление (`JobManager._key_for`: задания там нет, оно читает настройку через `hint` и меняется вместе с этой функцией). Строка журнала «язык %s взят из метаданных, распознаватель определять не будет» уходит вместе с ранним возвратом.

`JobRunner._run_asr` (1153-1167): `pin, hint = self._declaration(job)` рядом с `prompt = _initial_prompt(job.info)`; в argv вместо `self._source_language(job)` — `pin or "auto"`; после списка, до `--initial-prompt`:

```python
        if hint:
            args += ["--hint", hint]
```

Блок результата в `_pipeline` (929-941) не трогать: его заменяет `_accept_asr` в задаче 6. `--initial-prompt` и `_initial_prompt` не трогать: это задача 8.

- [ ] **Step 9: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_worker.py tests/test_job_language.py -q`
Expected: PASS, 42 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 331 passed

- [ ] **Step 10: Коммит**

```bash
/usr/bin/git add helper/language.py helper/asr/worker.py helper/jobs.py tests/test_language.py tests/test_worker.py tests/test_job_language.py
/usr/bin/git commit -m "Определитель языка слушает всегда: голосуют только окна с речью, порог сербского семейства, метка YouTube — лишь подсказка"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---
### Task 6: Решение о языке видно клиентам

**Files:** номера строк — на `98a3ad2`, предыдущие задачи их сдвинули: искать по имени функции.
- Modify: `helper/jobs.py` — импорты (строка 37, рядом с `from helper.gpulease import GpuLease, Preempted`); `Job.__init__` (239-337, рядом с `self.source`, 269-271); `Job.snapshot` (427-451, после `"generation"`, 440); новый `Job._set_language` между `Job._set_stage` (453-457) и `Job._fail` (459-463); новая модульная `_log_language` между `refusal_snapshot` (688-701) и `class JobRunner` (704); `JobRunner._pipeline` (772-943) — ветка дорожки автора `else:` (853-860) и блок после `_run_asr_watched` (926-941); новый `JobRunner._accept_asr` сразу после `JobRunner._run_asr_watched` (1070-1096)
- Modify: `tests/test_limits.py:243-276` (`test_events_with_create_answers_200_with_an_error_snapshot`)
- Modify: `tests/conftest.py` — рядом с `metadata` (задача 3) три обычные функции: `watch`, `hear_until_done`, `preempt_after_asr`
- Test: `tests/test_language_wire.py` (новый)

**Interfaces:**
- Consumes:
  - `helper.language.LanguageVerdict` (задача 5): поля по порядку `effective, declared, declared_by, detected, confidence, reason, source="asr", track=None, verified=None`; свойство `mismatch`; `note() -> str | None`; `wire() -> dict` с ключами `effective, declared, declaredBy, detected, confidence, reason, source, track, verified, mismatch, note`; `LanguageVerdict.from_wire(data: object) -> LanguageVerdict | None` (None, не словарь, нет или негодный `effective` → `None`).
  - `language.normalize`, `language.UnknownLanguage`, `language.hint` (задача 4); модуль уже импортирован в `helper/jobs.py` именем `language` (задача 4).
  - `language.MIN_SPEECH_SECONDS` (задача 5, 3.0) — с этих секунд речи окно уходит в определитель; секунды окон в ответе воркера округлены вниз до десятых (задача 5), так что окно, показанное с этим числом и больше, в модель ушло. Порог предупреждения в `_log_language`.
  - `JobRunner._declaration(self, job) -> tuple[str | None, str | None]` — `(закрепление, подсказка метаданных)` (задача 5).
  - Ответ воркера (задача 5): `{"language": <действующий код>, "verdict": LanguageVerdict.wire(), "detection": {"bcmsShare", "votes", "probed", "windows": [[код или null, доля, секунды речи], ...]}, "duration", "model", "cues"}`.
  - Оснастка (задача 3): построитель `metadata(**overrides) -> VideoInfo` из `tests/conftest.py`; фикстура `pipeline` — `run(job)`, `workers`, `cache`, `configure(**asr)` и входы `pipeline.asr_result` (JSON, которым отвечает поддельный воркер), `pipeline.info` (что отдаёт `probe`; по умолчанию ролик без дорожек автора и без `audio_language`), `pipeline.track` (что отдаёт `fetch_manual_track`); её пропуск на карту выдаёт настоящий `helper.gpulease._Holder`. Фикстуры `manager`, `client`, `connection` уже есть.
- Produces:
  - `Job.language: LanguageVerdict | None` — `None`, пока решения нет; в ключ реестра `(video_id, use_author_track)` не входит.
  - `Job._set_language(self, verdict: LanguageVerdict) -> None` — ставит `Job.language` и пишет в журнал `{"type": "language", "language": verdict.wire()}`.
  - `Job.snapshot()["language"]` — `self.language.wire()` или `None`; у `refusal_snapshot` — `None`.
  - `JobRunner._accept_asr(self, job: Job, result: dict) -> None` — решение о языке, реплики и `segmented` из ответа воркера; задача 8 заменит в нём сборку реплик на `asr_cues(...)`. Ответ без годного `verdict`, чей `language` не приводится к коду Whisper, — `PermanentJobError(f"распознаватель вернул неизвестный язык «{code}»")`: окончательный, без повтора на карте.
  - `_log_language(job: Job, verdict: LanguageVerdict, windows: list) -> None` (модульная, закрытая): INFO `язык <ролик>: <effective> (<reason>), объявлен <declared> (<declared_by>), по речи <detected>, уверенность <0.00>, окна: <код> <0.00>/<N>с, ...`, где каждое отсутствующее значение (объявление целиком, определённый язык, уверенность, код окна без голоса, список окон) пишется знаком `—`; WARNING `язык <ролик>: <note()>` при `mismatch`; WARNING `язык <ролик>: определитель не ответил, окон с речью без ответа: <N>`, когда хотя бы у одного окна код `None` при секундах речи не меньше `language.MIN_SPEECH_SECONDS`.
  - Порядок событий: `language` идёт раньше `segmented` на путях распознавания и дорожки автора (кэш и восстановление — задача 7).
  - В `tests/conftest.py`, рядом с `metadata`, — обычные функции, тесты берут их `from conftest import …`: `watch(video_id, **fields) -> dict` — сообщение `watch` вкладки; `async hear_until_done(connection) -> list[dict]` — всё, что сокет услышал до события `state` со стадией `done` или `error`; `preempt_after_asr(pipeline, monkeypatch) -> None` — карту отбирают на первой проверке вытеснения после воркера (подмена `gpulease._Holder.check`). Задача 7 берёт все три.

- [ ] **Step 1: Написать падающие тесты**

В `tests/conftest.py`, сразу после функции `metadata` (задача 3), — три обычные функции. Ими пользуются эта задача и задача 7; `helper.*` по-прежнему импортируется внутри функции:

```python
def watch(video_id, **fields):
    """Сообщение watch от вкладки: подписка на задание ролика."""
    return {"op": "watch", "videoId": video_id, **fields}


async def hear_until_done(connection):
    """Всё, что сокет услышал до конца задания: до события state со стадией done или error."""
    heard = []

    while True:
        event = await connection.hear()
        heard.append(event)

        if event["type"] == "state" and event["stage"] in ("done", "error"):
            return heard


def preempt_after_asr(pipeline, monkeypatch):
    """Карту отбирают сразу после распознавания: первая проверка вытеснения после воркера."""
    from helper import gpulease

    real_check = gpulease._Holder.check
    taken = []

    def check(holder):
        if pipeline.workers and not taken:
            taken.append(holder)

            raise gpulease.Preempted("пользователь начал свою работу на GPU")

        real_check(holder)

    monkeypatch.setattr(gpulease._Holder, "check", check)
```

```python
# tests/test_language_wire.py
"""Решение о языке на проводе: поле language в снимке и событие language раньше реплик."""

import logging

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import jobs
from helper.jobs import Stage, refusal_snapshot
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack

# Одиннадцать знаков, как у настоящего ролика.
VIDEO = "langwire001"

# Сербская речь на канале, который YouTube пометил английским (Juske, Q2QcYOBk0Jo):
# определитель уверен в сербском, объявление с речью расходится.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# Английский ролик без метаданных: расходиться не с чем, сказать нечего.
ENGLISH = LanguageVerdict(
    effective="en", declared=None, declared_by=None,
    detected="en", confidence=0.99, reason="detector",
)
# Ни одно окно не проголосовало, и язык взят у YouTube.
HINTED = LanguageVerdict(
    effective="en", declared="en", declared_by="metadata",
    detected=None, confidence=None, reason="hint",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
TRACK = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
# Ключи снимка до этого плана. Новое поле одно, и его имя расширение не читает.
OLD_SNAPSHOT_KEYS = {
    "type", "videoId", "authorTrack", "targetLang", "generation", "stage", "error",
    "retryable", "waiting", "title", "total", "translated", "cues",
}
# Типы событий, на которые расширение отвечает действием (content.js, background.js).
EXTENSION_TYPES = {"state", "snapshot", "cues", "helper", "restored", "gone"}


def author_track(lang):
    return CaptionTrack(lang=lang, url="", ext="json3", name="")


def answer(verdict, windows=(), cues=CUES):
    """Ответ воркера по протоколу задачи 5: решение, окна определителя, реплики."""
    return {
        "language": verdict.effective,
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.0,
            "votes": sum(code is not None for code, _, _ in windows),
            "probed": len(windows),
            "windows": [list(window) for window in windows],
        },
        "duration": 2.0,
        "model": "large-v3",
        "cues": cues,
    }


def language_lines(caplog):
    """Строки журнала о решении по языку этого ролика: уровень и текст."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "helper.jobs" and record.getMessage().startswith(f"язык {VIDEO}:")
    ]


def test_snapshot_has_no_language_until_the_daemon_decides(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert job.language is None
    assert job.snapshot()["language"] is None
    assert refusal_snapshot(VIDEO, "ru", True, "отказ", True)["language"] is None


def test_the_language_event_lands_in_the_journal_and_the_snapshot(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_stage(Stage.QUEUED, waiting="gpu")

    job._set_language(JUSKE)

    assert job.language is JUSKE
    assert job.events[-1] == {"type": "language", "language": JUSKE.wire()}
    assert job.snapshot()["language"] == JUSKE.wire()
    # Не через _set_stage: стадия и ожидание карты остаются, надпись бейджа та же.
    assert job.stage is Stage.QUEUED
    assert job.waiting == "gpu"


def test_the_language_reuses_no_name_the_extension_reads(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_language(JUSKE)
    event = job.events[-1]
    snapshot = job.snapshot()

    assert event.keys() == {"type", "language"}
    assert event["type"] not in EXTENSION_TYPES
    assert snapshot.keys() - OLD_SNAPSHOT_KEYS == {"language"}
    assert snapshot.keys() >= OLD_SNAPSHOT_KEYS


async def test_the_socket_hears_the_language_before_segmented_after_asr(
    manager, connection, pipeline
):
    pipeline.asr_result = answer(JUSKE)
    await connection.say(watch(VIDEO))
    caught_up = await connection.hear()

    assert (caught_up["type"], caught_up["language"]) == ("snapshot", None)

    await pipeline.run(manager.get(VIDEO, True))
    heard = await hear_until_done(connection)
    types = [event["type"] for event in heard]
    at = types.index("language")

    assert at < types.index("segmented")
    # Решение приходит сразу за распознаванием: его выносит воркер.
    assert heard[at - 1]["stage"] == "asr"
    assert heard[at] == {"type": "language", "language": JUSKE.wire()}
    assert heard[at]["language"]["mismatch"] is True
    assert heard[at]["language"]["note"] == "YouTube указал en, по речи sr"
    assert heard[-1]["stage"] == "done"


async def test_the_job_endpoint_and_the_poll_carry_the_language(client, manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)
    before = job.journal_end
    pipeline.asr_result = answer(JUSKE)

    await pipeline.run(job)

    snapshot = (await client.get(f"/v1/jobs/{VIDEO}")).json()
    stale = (await client.get(f"/v1/jobs/{VIDEO}/events", params={"cursor": "-1"})).json()
    polled = (
        await client.get(f"/v1/jobs/{VIDEO}/events", params={"cursor": str(before)})
    ).json()
    types = [event["type"] for event in polled["events"]]

    assert snapshot["language"] == JUSKE.wire()
    assert stale["snapshot"]["language"] == JUSKE.wire()
    assert types.index("language") < types.index("segmented")


async def test_the_socket_hears_the_language_before_segmented_on_the_author_track(
    manager, connection, pipeline, caplog
):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.info = metadata(audio_language="en", manual_tracks=[author_track("sr")])
    pipeline.track = TRACK
    await connection.say(watch(VIDEO))
    await connection.hear()

    await pipeline.run(manager.get(VIDEO, True))
    heard = await hear_until_done(connection)
    types = [event["type"] for event in heard]
    expected = LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected=None,
        confidence=None, reason="track", source="manual", track="sr", verified=None,
    )

    assert types.index("language") < types.index("segmented")
    assert heard[types.index("language")] == {"type": "language", "language": expected.wire()}
    assert expected.wire()["note"] == "YouTube указал en, дорожка автора sr"
    assert not pipeline.workers
    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: sr (track), объявлен en (metadata), по речи —, "
            "уверенность —, окна: —",
        ),
        (logging.WARNING, f"язык {VIDEO}: YouTube указал en, дорожка автора sr"),
    ]


@pytest.mark.parametrize(("label", "effective"), [("sr-Latn", "sr"), ("sh", "sr"), ("hr", "hr")])
async def test_the_author_track_label_names_the_language(manager, pipeline, label, effective):
    pipeline.info = metadata(manual_tracks=[author_track(label)])
    pipeline.track = TRACK
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.language == LanguageVerdict(
        effective=effective, declared=None, declared_by=None, detected=None,
        confidence=None, reason="track", source="manual", track=label, verified=None,
    )


async def test_a_pin_is_the_declaration_of_an_author_track(manager, pipeline):
    pipeline.configure(language="hr")
    pipeline.info = metadata(audio_language="en", manual_tracks=[author_track("sr")])
    pipeline.track = TRACK
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.language == LanguageVerdict(
        effective="sr", declared="hr", declared_by="config", detected=None,
        confidence=None, reason="track", source="manual", track="sr", verified=None,
    )
    # hr и sr — один язык с тремя токенами: расхождения нет.
    assert job.language.mismatch is False


async def test_a_job_preempted_after_recognition_resumes_with_its_verdict(
    manager, pipeline, monkeypatch
):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = answer(JUSKE)
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED
    assert job.language == JUSKE
    assert [cue.src for cue in job.cues] == ["Dobar dan."]

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert len(pipeline.workers) == 1
    assert job.snapshot()["language"] == JUSKE.wire()
    assert [event["type"] for event in job.events].count("language") == 1


async def test_an_answer_without_a_verdict_falls_back_to_its_language(manager, pipeline):
    pipeline.asr_result = {"language": "sr", "duration": 2.0, "model": "large-v3", "cues": CUES}
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.language == LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected=None,
        confidence=None, reason="detector",
    )


async def test_an_answer_in_an_unknown_language_fails_the_job(manager, pipeline):
    pipeline.asr_result = {"language": "xx", "duration": 2.0, "model": "large-v3", "cues": CUES}
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.ERROR
    # Окончательно: повтор снова занял бы карту распознаванием ради того же ответа.
    assert job.retryable is False
    assert job.error == "распознаватель вернул неизвестный язык «xx»"
    assert job.language is None
    assert job.cues == []


async def test_the_decision_is_one_info_line_with_the_windows(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(ENGLISH, windows=[("en", 0.99, 30.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: en (detector), объявлен —, по речи en, "
            "уверенность 0.99, окна: en 0.99/30с",
        ),
    ]


async def test_a_mismatch_is_a_warning_in_words(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(JUSKE, windows=[("sr", 0.97, 28.0), (None, 0.0, 1.2)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: sr (detector), объявлен en (metadata), по речи sr, "
            "уверенность 0.97, окна: sr 0.97/28с, — 0.00/1с",
        ),
        (logging.WARNING, f"язык {VIDEO}: YouTube указал en, по речи sr"),
    ]
    # Мёртвая строка «язык речи … определитель дал» ушла: её место заняли две выше.
    assert not [r for r in caplog.records if "определитель дал" in r.getMessage()]


async def test_a_detector_that_never_answered_is_a_warning(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    # Речь в окне есть, а ответа определителя нет: голосов ноль, решила метка YouTube —
    # тот же путь, которым сербская речь под меткой en ушла бы в английский декодер.
    pipeline.asr_result = answer(HINTED, windows=[(None, 0.0, 25.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: en (hint), объявлен en (metadata), по речи —, "
            "уверенность —, окна: — 0.00/25с",
        ),
        (logging.WARNING, f"язык {VIDEO}: определитель не ответил, окон с речью без ответа: 1"),
    ]
```

И одна строка в существующий тест, который перечисляет поля снимка отказа по одному:

```python
# tests/test_limits.py, test_events_with_create_answers_200_with_an_error_snapshot,
# сразу после assert snapshot["waiting"] is None:
    assert snapshot["language"] is None
```

Сравнение `snapshot.keys() == full[0].snapshot().keys()` в этом же тесте остаётся верным без правок: `refusal_snapshot` собирает снимок из настоящего `Job`. `refusal_event` (`helper/app.py:67-79`) не трогается — тесты, что сверяют его на точное равенство (`tests/test_limits.py:221-227`, `tests/test_create_paths.py`), остаются зелёными.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_language_wire.py tests/test_limits.py -q`
Expected: FAIL, 17 failed (16 новых и `test_events_with_create_answers_200_with_an_error_snapshot`): `AttributeError: 'Job' object has no attribute 'language'` и `... '_set_language'`; `KeyError: 'language'` в снимках; `ValueError: list.index(x): x not in list` — события `language` в потоке нет; тест неизвестного языка — `assert <Stage.DONE: 'done'> is <Stage.ERROR: 'error'>` (сегодня любой код из ответа воркера уходит в `normalize_cues` без проверки); тесты журнала — `assert [] == [(20, 'язык langwire001: ...')]`.

- [ ] **Step 3: Поле, событие и снимок**

В импорты `helper/jobs.py`: `from helper.language import LanguageVerdict`.

`Job.__init__`, рядом с `self.source`: `self.language: LanguageVerdict | None = None`. Комментарий: какой язык решил демон и почему; `None`, пока решения нет; пишется только через `_set_language`; в ключ реестра не входит — вариант ролика это `(video_id, use_author_track)`, а язык — свойство материала.

`Job._set_language(self, verdict: LanguageVerdict) -> None` между `_set_stage` и `_fail`: `self.language = verdict`, затем `self._emit({"type": "language", "language": verdict.wire()})`. Комментарий — почему не через `_set_stage`: тот снимает `waiting` и шлёт `state`, по которому `extension/content.js` (`updateChip`) перерисовывает бейдж, а `background.js` (`lastStage`) решает, продолжать ли опрос. Событие нового типа расширение пропускает: `trackStage`, `mergeCues` и `updateChip` действуют только на `state`, `snapshot` и `cues`. Проверено прогоном неизменённых `content.js` и `background.js` в Node: снимок с вложенным `language` и событие `language` оставляют бейдж «перевожу… 2/290» как был, а `lastStage` по одному событию `language` даёт `undefined` — опрос идёт дальше.

`Job.snapshot()`: после `"generation"` — `"language": self.language.wire() if self.language else None`. Комментарий: решение целиком, одним вложенным объектом; ключи верхнего уровня `source`, `message`, `waiting`, `retry` читает `content.js` (строки 250, 364-370), и `source: "asr"` наверху превратил бы бейдж в «качаю asr…». `refusal_snapshot` не меняется: он собран из настоящего `Job` и получает `"language": null` сам.

- [ ] **Step 4: `_accept_asr` — ответ воркера на задание**

В `_pipeline` (ветка распознавания, 929-940 на `98a3ad2`) строки от `segments = result["cues"]` до `job._emit({"type": "segmented", ...})` включительно заменить двумя элементами: переписанный комментарий (бывшие 936–938, «Присваиваем реплики ДО проверки вытеснения…») — «Решение о языке и реплики — до проверки вытеснения: …» тем же доводом: приостановленное после распознавания задание возобновляется с перевода, и решение о языке должно лежать на нём вместе с репликами, — и под ним вызов `self._accept_asr(job, result)`; в саму `_accept_asr` комментарий не переносить. Удаление отложенного звука перед ними (охранённое задачей 1) и `held.check()` после вызова остаются на месте. Мёртвая строка `log.info("язык речи: %s (определитель дал %s, уверенность %.2f)", ...)` (932-934) удаляется: в пакетном режиме `detected` всегда равен переданному, и она не срабатывала ни разу; её место занимает `_log_language`.

`JobRunner._accept_asr` — метод сразу после `_run_asr_watched`. Порядок важен — событие `language` раньше `segmented`, а неизвестный язык обрывает задание до того, как на нём что-то появилось:

```python
    def _accept_asr(self, job: Job, result: dict) -> None:
        """Ответ воркера — на задание: сначала решение о языке, потом реплики и segmented."""
        verdict = LanguageVerdict.from_wire(result.get("verdict"))

        if verdict is None:
            # Решения в ответе нет или оно негодное: язык берётся из поля language тем же
            # правилом, что и закрепление. Неизвестный код — отказ окончательный: повтор
            # через минуту снова гонял бы распознавание на карте и получал тот же ответ.
            raw = result.get("language")

            try:
                effective = language.normalize(raw)
            except language.UnknownLanguage:
                effective = None

            if effective is None:
                raise PermanentJobError(f"распознаватель вернул неизвестный язык «{raw}»")

            verdict = LanguageVerdict(
                effective=effective, declared=None, declared_by=None,
                detected=None, confidence=None, reason="detector",
            )

        job._set_language(verdict)
        _log_language(job, verdict, (result.get("detection") or {}).get("windows") or [])
        job.cues = self._to_job_cues(
            build_cues(normalize_cues(result["cues"], verdict.effective))
        )
        job._emit({"type": "segmented", "total": len(job.cues)})
```

`_log_language(job: Job, verdict: LanguageVerdict, windows: list) -> None` — модульная функция между `refusal_snapshot` и `class JobRunner`. Формат строк — в Interfaces и в тестах `test_the_decision_is_one_info_line_with_the_windows`, `test_a_mismatch_is_a_warning_in_words`, `test_a_detector_that_never_answered_is_a_warning` и в тесте сокета на дорожке автора. По порядку:
- одна строка INFO с решением и окнами; окно — `<код или —> <доля:.2f>/<int(секунды)>с`, окна через запятую, пустой список — `—`; секунды целые и вниз, как на проводе: окно, показанное с тремя секундами и больше, ушло в модель (`:.0f` показал бы окно в 2,6–2,9 с, не дошедшее до модели, как «3с» — без предупреждения ниже);
- WARNING `note()` при `mismatch`;
- WARNING «определитель не ответил, окон с речью без ответа: N», когда у N ≥ 1 окон код `None` при секундах речи не меньше `language.MIN_SPEECH_SECONDS`. Комментарий: без голосов решение молча уходит к метке YouTube или к `sr`, и сломавшийся определитель (падает на каждом окне, например после обновления faster-whisper) выглядел бы как ролик без речи; окно короче порога в модель не шло, и молчать о нём верно.

Докстрока: решение о языке — в журнал, строка INFO и предупреждения словами; расширение решения не покажет, а страницы планшета ещё нет: до неё журнал и снимок (`GET /v1/jobs/{id}`) — единственные места, где решение видно.

- [ ] **Step 5: Решение на пути дорожки автора**

В `_pipeline`, ветка `else:` после удачного `fetch_manual_track` (853-860), до `job.cues = ...` и `segmented`:

```python
                pin, hint = self._declaration(job)
                verdict = LanguageVerdict(
                    effective=language.hint(track.lang) or "sr",
                    declared=pin or hint,
                    declared_by="config" if pin else ("metadata" if hint else None),
                    detected=None,
                    confidence=None,
                    reason="track",
                    source="manual",
                    track=track.lang,
                    verified=None,
                )
                job._set_language(verdict)
                _log_language(job, verdict, [])
```

Объявление то же, что у `decide`: закрепление человека перекрывает метаданные. `verified=None` — тридцатисекундный зонд отложен (см. «Отложено»). `normalize_cues(segments, track.lang)` пока остаётся как есть: на `verdict.effective` его переводит задача 10. В ветке отказа дорожки (`except YtDlpError`, 841-852) ничего не объявляется: решение уходит только после удачной загрузки, и отзывать нечего.

Возобновлению ничего делать не нужно: приостановленное задание держит `Job.language` так же, как `job.cues`, а `_pipeline` с готовыми репликами сразу идёт в `_finish_translation`, без второго события. Пересозданное после остывшего отказа — новый `Job` с `language=None` и своим объявлением.

- [ ] **Step 6: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_language_wire.py tests/test_limits.py -q`
Expected: PASS — 16 новых тестов зелёные, в `tests/test_limits.py` ни одного падения.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 347 passed

- [ ] **Step 7: Коммит**

```bash
/usr/bin/git add helper/jobs.py tests/conftest.py tests/test_language_wire.py tests/test_limits.py
/usr/bin/git commit -m "Решение о языке видно клиентам: поле language в снимке, событие раньше реплик, расхождение — предупреждением в журнале"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 7: Ключ кэша и записанное решение

**Files:** номера строк — на `98a3ad2`, предыдущие задачи их сдвинули: искать по имени функции.
- Modify: `helper/cache.py` — докстрока модуля (7-9); общий комментарий версий (24-25) и `SCHEMA_VERSION` (27); `CacheKey` (32-61, поля 34-44 и `digest` 46-61); `TrackCache.put` (137-185, запись 146-162)
- Modify: `helper/jobs.py` — новая модульная `track_key` рядом с `_log_language` (задача 6), перед `class JobRunner` (704); `JobRunner._cache_key` (720-731); `JobRunner._finish_translation` (1098-1149, запись в кэш 1143-1148); `JobRunner._serve_cached` (1244-1274); `JobManager._key_for` (1652-1661); `JobManager.restore` (1663-1726: вызов на 1696, новое задание на 1704-1705); `JobManager._cached_variant` (1728-1765)
- Modify: `tests/test_job_identity.py:335` (`test_restore_from_cache_registers_the_variant_under_its_own_key`)
- Test: `tests/test_cache_flow.py` (новый; `watch`, `hear_until_done` и `preempt_after_asr` берёт из `tests/conftest.py`, задача 6 — своих копий не заводит)

**Interfaces:**
- Consumes:
  - Задача 6: `Job.language`, `Job._set_language(verdict)`, событие `language`.
  - Задача 5: `LanguageVerdict.wire()`, `LanguageVerdict.from_wire(data) -> LanguageVerdict | None`; `JobRunner._declaration(job) -> tuple[str | None, str | None]` — `(закрепление, подсказка метаданных)`, закрепление в нём — `JobRunner._pin()` (задача 4), которое `_pipeline` уже проверил до `probe`.
  - Задача 4: `language.hint(raw) -> str | None` (никогда не бросает).
  - Оснастка (задача 3): `metadata`, `pipeline` — `run`, `workers`, `cache` (настоящий `TrackCache`, свой у каждого теста), `configure(**asr)` (бегун пересобирается, кэш тот же), входы `asr_result`, `info`, `track`; пропуск выдаёт настоящий `helper.gpulease._Holder`. Фикстуры `manager`, `client`, `connection`. Из задачи 6 — функции `tests/conftest.py`: `watch`, `hear_until_done`, `preempt_after_asr(pipeline, monkeypatch)`; тесты берут их `from conftest import …`.
- Produces:
  - `helper.cache.CacheKey(video_id: str, pin: str, asr_model: str, translator_model: str, study_lang: str = "", track_fingerprint: str = "asr")` — `target_lang` нет, `source_lang` переименован в `pin` (`""` — человек язык не закреплял). `digest()` — sha256 от `video_id|track_fingerprint|pin|asr_model|translator_model|study_lang|PROMPT_VERSION|SCHEMA_VERSION`. `SCHEMA_VERSION = 2`; `PROMPT_VERSION` остаётся 1 (поднимает задача 11).
  - `TrackCache.put(self, key: CacheKey, cues: list[dict], length_seconds: float | None, *, language: dict | None = None) -> None`; запись: `video_id, pin, asr_model, translator_model, study_lang, track_fingerprint, prompt_version, schema_version, length_seconds, language, created_at, cues` — без `source_lang` и `target_lang`. Читатели берут решение через `.get("language")`.
  - `helper.jobs.track_key(config: Config, video_id: str, fingerprint: str, pin: str | None) -> CacheKey` — единственный рецепт ключа.
  - `JobRunner._cache_key(job)` = `track_key(self._config, job.video_id, job.source, self._declaration(job)[0])`.
  - `JobManager._key_for(self, video_id: str, fingerprint: str) -> CacheKey` = `track_key(self.config, video_id, fingerprint, language.hint(self.config.asr.language))`.
  - `JobManager._cached_variant(self, video_id: str, use_author_track: bool, duration: float | None = None) -> dict | None` — без `target_lang`.
  - Поведение: результат без реплик — `{"type": "state", "stage": "done", "total": 0, "cached": false}` и в кэш не идёт; запись без реплик — промах и для `_serve_cached`; попадание в кэш и восстановление шлют `language` раньше `cues`; `Job.target_lang` и `targetLang` в снимке остаются.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_cache_flow.py
"""Ключ кэша несёт закрепление человека, запись — решение о языке, пустая дорожка не кэшируется."""

import hashlib
import json
from dataclasses import fields

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import cache as cache_module
from helper import jobs
from helper.cache import CacheKey, TrackCache
from helper.config import AsrConfig, Config, TranslatorConfig
from helper.jobs import Stage
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack

# Ролик тестов конвейера: кэш фикстуры pipeline свой у каждого теста. Тесты
# восстановления пишут в общий на прогон manager.cache, и у каждого свой ролик.
VIDEO = "cacheflow01"

# 3XJLfTSZy6I: сербская речь без метаданных, английский сверху в пяти окнах из восьми,
# доля сербского семейства 0.44 — решил порог BCMS_FLOOR.
FLOOR = LanguageVerdict(
    effective="sr", declared=None, declared_by=None,
    detected="sr", confidence=0.44, reason="floor",
)
# Juske, Q2QcYOBk0Jo: YouTube пометил en, речь сербская.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# Человек закрепил sr в [asr] language.
PINNED = LanguageVerdict(
    effective="sr", declared="sr", declared_by="config",
    detected="sr", confidence=0.97, reason="pin",
)
# Ни одно окно не голосовало, подсказки нет.
SILENT = LanguageVerdict(
    effective="sr", declared=None, declared_by=None,
    detected=None, confidence=None, reason="fallback",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
# Готовые реплики в записи — так их кладёт _finish_translation (JobCue.payload()).
STORED = [
    {
        "i": 0, "start": 0.0, "end": 1.5, "src": "Dobar dan.", "lines": ["Добрый день."],
        "study": "Dobar dan.", "failed": False,
    },
]
# Значение для rewrite: поля в записи нет вовсе.
ABSENT = object()


def answer(verdict, cues=CUES):
    """Ответ воркера по протоколу задачи 5."""
    return {
        "language": verdict.effective,
        "verdict": verdict.wire(),
        "detection": {"bcmsShare": 0.0, "votes": 0, "probed": 0, "windows": []},
        "duration": 2.0,
        "model": "large-v3",
        "cues": cues,
    }


def key_of(stored):
    """Ключ записи по её же полям: тест попадает ровно туда, куда писал конвейер."""
    return CacheKey(
        video_id=stored["video_id"], pin=stored["pin"], asr_model=stored["asr_model"],
        translator_model=stored["translator_model"], study_lang=stored["study_lang"],
        track_fingerprint=stored["track_fingerprint"],
    )


def rewrite(store, key, **changes):
    """Поправить запись на диске; поле со значением ABSENT удаляется."""
    path = store._path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))

    for name, value in changes.items():
        if value is ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── ключ ─────────────────────────────────────────────────────────────────


def test_the_key_is_the_pin_recipe_without_the_target_language():
    key = CacheKey(
        video_id=VIDEO, pin="sr", asr_model="large-v3/float16",
        translator_model="qwen3.8:27b", study_lang="cnr", track_fingerprint="manual:sr",
    )
    raw = "|".join([
        VIDEO, "manual:sr", "sr", "large-v3/float16", "qwen3.8:27b", "cnr",
        str(cache_module.PROMPT_VERSION), str(cache_module.SCHEMA_VERSION),
    ])

    assert [field.name for field in fields(CacheKey)] == [
        "video_id", "pin", "asr_model", "translator_model", "study_lang", "track_fingerprint",
    ]
    assert key.digest() == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert cache_module.SCHEMA_VERSION == 2


def test_the_pipeline_and_restore_build_one_key(manager):
    job = jobs.Job(VIDEO)
    job.source = "manual:sr-Latn"
    expected = CacheKey(
        video_id=VIDEO, pin="", asr_model="large-v3/float16",
        translator_model="qwen3.8:27b", study_lang="cnr", track_fingerprint="manual:sr-Latn",
    )

    assert jobs.track_key(manager.config, VIDEO, "manual:sr-Latn", None) == expected
    assert manager.runner._cache_key(job) == expected
    assert manager._key_for(VIDEO, "manual:sr-Latn") == expected


def test_a_pin_goes_into_both_keys(manager, monkeypatch):
    pinned = Config(asr=AsrConfig(language="sr"), translator=TranslatorConfig())
    monkeypatch.setattr(manager, "config", pinned)
    monkeypatch.setattr(manager.runner, "_config", pinned)

    assert manager.runner._cache_key(jobs.Job(VIDEO)).pin == "sr"
    assert manager._key_for(VIDEO, "asr").pin == "sr"


def test_restore_reads_a_broken_pin_as_no_pin(manager, monkeypatch):
    # Отказ по негодному закреплению — дело конвейера (задача 4). Восстановление только
    # читает: ролики, закэшированные без закрепа, по-прежнему открываются — негодный код
    # читается как «не закреплено»; сделанные при прежнем годном закрепе не находятся,
    # пока код не исправлен.
    manager.cache.put(manager._key_for("brokenpin01", "asr"), STORED, 10.0)
    broken = Config(asr=AsrConfig(language="serbian"), translator=TranslatorConfig())
    # Настройка у демона одна на реестр и бегун: подменяются обе.
    monkeypatch.setattr(manager, "config", broken)
    monkeypatch.setattr(manager.runner, "_config", broken)

    assert manager._key_for(VIDEO, "asr").pin == ""
    assert manager.restore("brokenpin01", "ru", True, 10.0).stage is Stage.DONE


# ── запись ───────────────────────────────────────────────────────────────


def test_put_records_the_verdict_and_the_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    store = TrackCache(root=tmp_path / "tracks")
    config = Config(asr=AsrConfig(), translator=TranslatorConfig())
    asr = jobs.track_key(config, VIDEO, "asr", None)
    track = jobs.track_key(config, VIDEO, "manual:sr", None)

    try:
        store.put(asr, STORED, 10.0, language=FLOOR.wire())
        store.put(track, STORED, 10.0)
        payload = store.get(asr, 10.0)

        assert payload["language"] == FLOOR.wire()
        assert payload["pin"] == ""
        assert payload["schema_version"] == 2
        assert "source_lang" not in payload
        assert "target_lang" not in payload
        assert store.get(track, 10.0)["language"] is None
    finally:
        store._db.close()


async def test_a_second_view_hears_the_verdict_before_the_cues(manager, connection, pipeline):
    pipeline.asr_result = answer(JUSKE)
    await pipeline.run(jobs.Job(VIDEO))
    await connection.say(watch(VIDEO))

    assert (await connection.hear())["language"] is None

    job = manager.get(VIDEO, True)
    await pipeline.run(job)
    heard = await hear_until_done(connection)
    (stored,) = pipeline.cache.variants(VIDEO)

    assert [event["type"] for event in heard] == ["state", "language", "cues", "state"]
    assert heard[1] == {"type": "language", "language": JUSKE.wire()}
    assert heard[-1] == {"type": "state", "stage": "done", "cached": True, "total": 1}
    assert job.snapshot()["language"]["note"] == "YouTube указал en, по речи sr"
    assert len(pipeline.workers) == 1
    assert (stored["pin"], stored["language"]) == ("", JUSKE.wire())


async def test_metadata_does_not_split_the_cache(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    pipeline.info = metadata(audio_language="en")
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 1
    assert again.events[-1] == {"type": "state", "stage": "done", "cached": True, "total": 1}
    assert again.language == FLOOR


async def test_another_target_language_shares_the_cache(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO, "ru"))
    other = jobs.Job(VIDEO, "sr")

    await pipeline.run(other)

    assert len(pipeline.workers) == 1
    assert other.events[-1]["cached"] is True
    assert other.snapshot()["targetLang"] == "sr"


async def test_a_human_pin_is_another_entry(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    pipeline.configure(language="sr")
    pipeline.asr_result = answer(PINNED)
    pinned = jobs.Job(VIDEO)

    await pipeline.run(pinned)

    assert len(pipeline.workers) == 2
    assert pinned.language == PINNED
    assert sorted(stored["pin"] for stored in pipeline.cache.variants(VIDEO)) == ["", "sr"]


async def test_a_result_without_cues_is_not_cached(manager, pipeline):
    pipeline.asr_result = answer(SILENT, cues=[])
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.events[-1] == {"type": "state", "stage": "done", "total": 0, "cached": False}
    assert pipeline.cache.variants(VIDEO) == []
    assert job.snapshot()["language"]["note"] == "речи почти нет, язык принят за sr"


async def test_an_entry_without_cues_is_a_miss_for_the_pipeline_too(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    (stored,) = pipeline.cache.variants(VIDEO)
    key = key_of(stored)
    pipeline.cache.put(key, [], None, language=FLOOR.wire())
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 2
    assert again.events[-1] == {"type": "state", "stage": "done", "total": 1}
    assert len(pipeline.cache.get(key)["cues"]) == 1


async def test_a_cached_entry_without_a_verdict_is_served_with_language_null(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    (stored,) = pipeline.cache.variants(VIDEO)
    rewrite(pipeline.cache, key_of(stored), language=ABSENT)
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 1
    assert again.language is None
    assert again.snapshot()["language"] is None
    assert [event["type"] for event in again.events] == ["state", "cues", "state"]


async def test_a_resumed_job_writes_its_verdict(manager, pipeline, monkeypatch):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = answer(JUSKE)
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED

    await pipeline.run(job)
    (stored,) = pipeline.cache.variants(VIDEO)

    assert job.stage is Stage.DONE
    assert stored["language"] == JUSKE.wire()


async def test_an_author_track_entry_keeps_its_verdict(manager, pipeline):
    pipeline.info = metadata(
        audio_language="en",
        manual_tracks=[CaptionTrack(lang="sr-Latn", url="", ext="json3", name="")],
    )
    pipeline.track = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
    await pipeline.run(jobs.Job(VIDEO))
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert [event["type"] for event in again.events] == ["state", "language", "cues", "state"]
    assert again.source == "manual:sr-Latn"
    assert again.language == LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected=None,
        confidence=None, reason="track", source="manual", track="sr-Latn", verified=None,
    )


# ── восстановление ───────────────────────────────────────────────────────


async def test_restore_brings_the_verdict_back_before_the_cues(client, manager):
    video = "restverdict"
    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=JUSKE.wire())

    body = (await client.get(f"/v1/jobs/{video}/restore", params={"duration": "10"})).json()
    restored = manager.get(video, True)

    assert body["found"] is True
    assert body["snapshot"]["language"] == JUSKE.wire()
    assert [event["type"] for event in restored.events] == ["language", "cues", "state"]
    assert restored.events[0] == {"type": "language", "language": JUSKE.wire()}


@pytest.mark.parametrize(
    ("video", "verdict"),
    [("noverdict01", ABSENT), ("noverdict02", None), ("noverdict03", "sr")],
    ids=["absent", "null", "not-an-object"],
)
def test_an_entry_without_a_verdict_restores_with_language_null(manager, video, verdict):
    key = manager._key_for(video, "asr")
    manager.cache.put(key, STORED, 10.0)
    rewrite(manager.cache, key, language=verdict)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored.stage is Stage.DONE
    assert restored.language is None
    assert restored.snapshot()["language"] is None
    assert [event["type"] for event in restored.events] == ["cues", "state"]


def test_restore_follows_the_current_pin(manager, monkeypatch):
    video = "restpinned1"
    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=FLOOR.wire())
    pinned = Config(asr=AsrConfig(language="sr"), translator=TranslatorConfig())
    monkeypatch.setattr(manager, "config", pinned)

    assert manager.restore(video, "ru", True, 10.0) is None

    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=PINNED.wire())

    assert manager.restore(video, "ru", True, 10.0).language == PINNED


def test_restore_never_takes_versions_or_models_from_the_entry(manager, monkeypatch):
    video = "restorphan1"
    other_model = Config(asr=AsrConfig(), translator=TranslatorConfig(model="translategemma:12b"))

    with monkeypatch.context() as patch:
        patch.setattr(cache_module, "PROMPT_VERSION", cache_module.PROMPT_VERSION - 1)
        manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=FLOOR.wire())

    manager.cache.put(
        jobs.track_key(other_model, video, "asr", None), STORED, 10.0, language=FLOOR.wire()
    )

    assert manager.restore(video, "ru", True, 10.0) is None
    # Обе записи на месте: восстановление их не видит, но и не трогает.
    assert len(manager.cache.variants(video)) == 2


def test_restore_prefers_the_newest_author_track(manager):
    video = "restnewest1"
    older = manager._key_for(video, "manual:sr")
    newer = manager._key_for(video, "manual:sr-Latn")
    manager.cache.put(older, [{**STORED[0], "src": "Stara"}], 10.0)
    manager.cache.put(newer, [{**STORED[0], "src": "Nova"}], 10.0)
    rewrite(manager.cache, older, created_at=1000.0)
    rewrite(manager.cache, newer, created_at=2000.0)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored.source == "manual:sr-Latn"
    assert [cue.src for cue in restored.cues] == ["Nova"]
```

Ролики тестов восстановления (`brokenpin01`, `restverdict`, `noverdict01`–`noverdict03`, `restpinned1`, `restorphan1`, `restnewest1`) пишутся в общий на прогон `manager.cache`, поэтому у каждого теста свой; `abcdefghijk` туда не попадает — на его пустоте держится `test_restore_looks_up_the_variant_the_viewer_asked_for`. В последнем тесте старшая по `created_at` запись лежит в индексе первой: порядок строк индекса и порядок по свежести расходятся, и тест отличает одно от другого.

И новая подпись `_key_for` в существующем тесте:

```python
# tests/test_job_identity.py:335, test_restore_from_cache_registers_the_variant_under_its_own_key
    manager.cache.put(manager._key_for(video, "asr"), cues, 10.0)
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_cache_flow.py tests/test_job_identity.py -q`
Expected: FAIL, 22 failed (21 новый и `test_restore_from_cache_registers_the_variant_under_its_own_key`): `TypeError: CacheKey.__init__() got an unexpected keyword argument 'pin'`; `AttributeError: 'CacheKey' object has no attribute 'pin'`; `AttributeError: module 'helper.jobs' has no attribute 'track_key'`; `TypeError: JobManager._key_for() missing 1 required positional argument: 'fingerprint'` во всех тестах восстановления; в тестах конвейера попадание в кэш даёт `['state', 'cues', 'state']` без `language`, `assert None == LanguageVerdict(...)`, другой `targetLang` — `assert 2 == 1` (сегодня он в ключе, и распознавание идёт второй раз), `KeyError: 'pin'` и `KeyError: 'language'` в записи, у `done` пустого результата нет `cached: False`.

- [ ] **Step 3: Ключ и запись в `helper/cache.py`**

`CacheKey`: вместо `source_lang` и `target_lang` — одно поле `pin: str` на месте `source_lang`. Комментарий: язык, закреплённый человеком (`[asr] language`), нормализованный; `""` — не закреплён; ни `"auto"`, ни метаданных YouTube тут не бывает; действующий язык и решение определителя — в записи, поле `language`. В `digest()` из списка частей уходит `target_lang`, `source_lang` заменяется на `pin`; остальные части и порядок те же: `video_id, track_fingerprint, pin, asr_model, translator_model, study_lang, PROMPT_VERSION, SCHEMA_VERSION`.

Версии (24-27). Общий комментарий над обеими («Поднимать при любом изменении промпта или формата записи…») сузить до `PROMPT_VERSION`, а у `SCHEMA_VERSION = 2` — свой полный комментарий прямо над ним:

```python
# Поднимать при любом изменении промпта — это часть ключа, поэтому смена версии
# автоматически обесценивает старые записи.
PROMPT_VERSION = 1

# Формат записи на диске: поднимать, когда меняется то, что пишет put.
# 2 — запись несёт language и pin вместо source_lang и target_lang.
SCHEMA_VERSION = 2
```

Значение `PROMPT_VERSION` не трогать: его один раз поднимает задача 11 и тогда же переписывает комментарий над ним целиком.

`TrackCache.put(self, key, cues, length_seconds, *, language: dict | None = None)`. В записи `"source_lang"` и `"target_lang"` заменить на `"pin": key.pin` — поле ключа пишется в файл по той же причине, что `study_lang` (комментарий рядом: без него запись при разборе глазами не объясняет, почему ключ не сходится); после `"length_seconds"` добавить `"language": language`. В докстроку `put`: `language` — `LanguageVerdict.wire()` или `None`, читатели берут его через `.get("language")`, и запись без поля отдаётся с языком `null`.

Докстрока модуля, строки 7-9: заменить «…старая запись просто перестанет находиться, а потом уедет по LRU» на: «…старая запись просто перестанет находиться. С диска она сама не уходит: вытеснение по давности чтения идёт только внутри put и только когда записи переросли бюджет, так что осиротевшие лежат, пока его хватает. Удалять их — решение хозяина.»

- [ ] **Step 4: Один рецепт ключа в `helper/jobs.py`**

Модульная `track_key(config: Config, video_id: str, fingerprint: str, pin: str | None) -> CacheKey` перед `class JobRunner`: `CacheKey(video_id=video_id, pin=pin or "", asr_model=f"{config.asr.model}/{config.asr.compute_type}", translator_model=config.translator.model, study_lang=config.translator.study_language, track_fingerprint=fingerprint)`. Докстрока — решение и отступление от замысла (замысел: «в ключ кэша идёт действующий язык… поиск по объявленному, запись под действующим»): язык в ключе — закрепление человека, а не действующий язык, потому что кэш обязан ответить до любой работы на карте (`helper/cache.py:4-5`), а действующий язык появляется только после воркера; когда человек закрепил язык, действующий с ним совпадает, а решение по незакреплённому ролику лежит в записи и видно на проводе. Метаданные YouTube в ключ не входят: канал Juske помечен `en` при сербской речи, и язык такого ролика решает определитель. `targetLang` не входит: на выходе всегда русская и черногорская строки, и клиент с другим `targetLang` получает ту же дорожку. Сюда же перенести комментарий из `_cache_key` о том, что обе модели входят в ключ.

`JobRunner._cache_key(job)` = `track_key(self._config, job.video_id, job.source, self._declaration(job)[0])`. Объявление — через `_declaration` (сейчас это `_pin()`), а не `hint`; негодное закрепление `_pipeline` уже отказал через `_pin()`; сюда же придёт объявление ссылки, см. «Отложено». Возобновление после паузы пишет под тем же ключом, под которым задание искало: настройка не меняется до перезапуска демона.

`JobManager._key_for(self, video_id: str, fingerprint: str) -> CacheKey` = `track_key(self.config, video_id, fingerprint, language.hint(self.config.asr.language))`. Комментарий: тот же рецепт, что у конвейера, иначе восстановление молча не находило бы сделанного; закрепление через `hint`, потому что восстановление только читает и не отказывает — негодное закрепление отказывает задание в конвейере (задача 4), а ролики, закэшированные без закрепа, по-прежнему открываются: восстановление читает негодный код как «не закреплено»; сделанные при прежнем годном закрепе не находятся, пока код не исправлен.

- [ ] **Step 5: Запись и чтение решения**

`_finish_translation`: перед записью в кэш, после веток `pending` и `failed`, — `if not job.cues: job._set_stage(Stage.DONE, total=0, cached=False); return`. Комментарий: пустая дорожка в кэш не идёт — записанная, она отдавалась бы готовой навсегда (в кэше демона таких три, одна на 41-минутный ролик), а восстановление считает её испорченной записью; цена — ролик без единой реплики не кэшируется: новое задание по нему — после 12 других готовых или перезапуска — снова качает звук и распознаёт под пропуском. В `self._cache.put` добавить `language=job.language.wire() if job.language else None` (`asyncio.to_thread` передаёт именованный аргумент как есть).

`_serve_cached`: `if not cached or not cached.get("cues"): return False` — запись без реплик это промах, как у `restore`. Затем, ДО `job.cues = [...]`: `verdict = LanguageVerdict.from_wire(cached.get("language"))`, и если не `None` — `job._set_language(verdict)`. Комментарий: решение раньше реплик, как на свежем распознавании, иначе флаг расхождения пропадал бы на каждом повторном просмотре; запись без поля или с негодным — язык `null` и события `language` нет, без падения.

`restore`: вызов `self._cached_variant(video_id, use_author_track, duration)`; после `job.source = ...` и до `job.cues = [...]` — то же копирование решения через `from_wire`. Проверку «пустая дорожка — испорченная запись» (1698-1702) не трогать. `target_lang` остаётся в подписи `restore`: им создаётся `Job` и он уходит в снимок как `targetLang`.

- [ ] **Step 6: Восстановление: свежие первыми, ключ по текущей настройке**

`_cached_variant(self, video_id, use_author_track, duration=None)` — без `target_lang`. Из записи берётся только отпечаток (`track_fingerprint`, по умолчанию `asr`), закрепление — текущее, модели и версии — нынешнего кода; на каждый отпечаток один `get` по `self._key_for(video_id, fingerprint)` с `duration` (ключ у него один, а `get` пишет `used_at`); из найденного свежая по `created_at` идёт первой — из двух дорожек автора (`manual:sr` и `manual:sr-Latn`) отдаётся последняя сделанная, а не первая по порядку строк индекса. С `use_author_track` отдаётся самая свежая дорожка автора (`manual:…`), а нет её — запись распознавания (`asr`); без него — только `asr`; не нашлось ничего — `None`. Комментарии о длительности от плеера (1746-1749) и о приоритете дорожки автора (1758-1760) сохранить на своих местах.

В докстроку `_cached_variant` дописать: закрепление в пересчитанном ключе — нынешнее, из настройки, а не записанное в файле; поэтому запись, сделанная при другом закреплении, при другой модели или версии, не находится, но и не удаляется.

- [ ] **Step 7: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_cache_flow.py tests/test_job_identity.py -q`
Expected: PASS — 21 новый тест зелёный, в `tests/test_job_identity.py` ни одного падения.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 368 passed

- [ ] **Step 8: Коммит**

```bash
/usr/bin/git add helper/cache.py helper/jobs.py tests/test_cache_flow.py tests/test_job_identity.py
/usr/bin/git commit -m "Ключ кэша несёт закрепление человека вместо строки auto и без targetLang; запись хранит решение о языке, пустая дорожка не кэшируется"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---
### Task 8: Затравка зависит от языка, мусор распознавания отсекается

Поломка 3 замысла. Затравку сегодня собирает демон до воркера и без языка (`_initial_prompt`, `helper/jobs.py:1443-1458`): сербская фраза уходит и в английский ролик, а имена из заголовка, описания и тегов любого языка проходят сербскую транслитерацию (`_proper_nouns`, `helper/jobs.py:1461-1478`). Пакетный режим faster-whisper 1.2.1 подаёт затравку каждому куску заново и не применяет ни одного из заградителей, которые ставит воркер, так что эхо затравки, тишина и петли повторов доходят до переводчика и до экрана.

Что решено. После задачи 5 язык решается в воркере, поэтому и затравку составляет воркер — по решённому языку (`verdict.effective`), а не по объявленному; демон шлёт ему только имена. Воркер отдаёт все сегменты вместе с их метриками, а отсекает демон: stderr воркера при успехе выбрасывается (`_run_asr`), и выброс, решённый в воркере, в журнале не был бы виден; отдельный прогон воркера при этом показывает сырой вывод. Эхо отсекается по предложению внутри `build_cues`, и петля повторов тоже: сегмент пакетного режима — кусок речи до 30 с, и «повтор» его только помечает — уходят зациклившиеся предложения, а диалог перед петлёй остаётся; целиком выбрасывается только «тишина». «Реплика, совпадающая с затравкой» понимается как «состоит только из слов затравки, и среди них есть необиходное слово фразы или вводки имён (для `sr` — transkript, snimka, pominju) либо два разных имени не из её обиходных слов». Оба толкования — запись 3 в «Решениях, расходящихся с буквой замысла». Дорожку автора фильтры не трогают: у неё нет ни затравки, ни метрик. `PROMPT_VERSION` здесь не поднимать — задача 11 поднимает его один раз за всё, что меняет закэшированную дорожку.

Номера строк — на `98a3ad2`. К этой задаче `helper/jobs.py` и `helper/asr/worker.py` уже изменены задачами 4–7: искать по имени функции.

**Files:**
- Create: `helper/asr/prompt.py`
- Modify: `helper/segment.py:16-19` (импорты), `helper/segment.py:177-218` (`build_cues`, цикл по предложениям — строка 195)
- Modify: `helper/asr/worker.py` — импорты (17-25); `transcribe` (125-215: параметр `initial_prompt` — 131, пустые заградители с комментариями — 164-174, `initial_prompt=initial_prompt` — 187, сборка реплик — 192-203, ответ — 205-215); `build_parser` из задачи 5 (на `98a3ad2` разбор аргументов внутри `main`, 219-232, флаг `--initial-prompt` — 223); вызов `transcribe` в `main` (246-255, `initial_prompt=args.initial_prompt` — 251)
- Modify: `helper/jobs.py` — импорты (16-28: `import re`; 33-39); `JobRunner._accept_asr` из задачи 6 (на `98a3ad2` её строка с репликами — 939 в `_pipeline`); `JobRunner._run_asr` (1151-1167: затравка — 1153, флаг — 1166-1167); удалить `_initial_prompt` (1443-1458) и `_proper_nouns` (1461-1478), на их месте — модульная `asr_cues`
- Modify: `tests/test_worker.py` (задача 5) — `test_result_carries_the_verdict_and_the_windows` и `test_parser_keeps_the_cli_contract` (шаг 11)
- Test: `tests/test_prompt.py`, `tests/test_segment.py`, `tests/test_asr_filters.py` (новые)

**Interfaces:**
- Consumes:
  - `helper.language.family(code: str | None) -> str | None` (задача 4).
  - `helper.language.Detection`, `LanguageVerdict` (`wire()`), `decide(pin, hint, detection, *, fallback="sr")`; в воркере `_detect(model, audio) -> Detection`, `transcribe(..., language: str = "sr", hint: str | None = None, ...)`, `build_parser() -> argparse.ArgumentParser`, флаг `--hint` (задача 5).
  - `JobRunner._accept_asr(self, job: Job, result: dict) -> None`, `held.check()` после неё в `_pipeline` (задача 6).
  - Фикстуры `manager`; `pipeline` — `pipeline.info`, `pipeline.run(job)`, `pipeline.workers[-1].args`; `whisper` — `whisper.segments`, `whisper.kwargs` и строитель `whisper.segment(start, end, text, *, no_speech_prob=0.1, avg_logprob=-0.3, compression_ratio=1.5, words=())` (задача 3).
  - `tests/test_worker.py` (задача 5): `test_result_carries_the_verdict_and_the_windows` сверяет набор ключей ответа, `test_parser_keeps_the_cli_contract` — умолчания парсера, в том числе `args.initial_prompt`.
- Produces:
  - `helper/asr/prompt.py`: `NO_SPEECH_THRESHOLD = 0.6`, `LOG_PROB_THRESHOLD = -1.0`, `COMPRESSION_RATIO_THRESHOLD = 2.4`, `MAX_NAMES = 12`, `PHRASES = {"sr": "Ovo je transkript video snimka na srpskom jeziku."}`, `NAMES_LEAD = "Pominju se:"`, `ECHO_COMMON = frozenset({"ovo", "je", "video", "na", "se", "srpskom", "jeziku"})`;
    `def harvest_names(title: str, tags: Sequence[str]) -> list[str]`;
    `def initial_prompt(language: str | None, names: Sequence[str]) -> str | None`;
    `def is_echo(sentence: str, prompt: str | None) -> bool`;
    `def compression_ratio(text: str) -> float` — формула faster-whisper (`get_compression_ratio`): байты UTF-8 на байты после `zlib`;
    `def segment_verdict(segment: Mapping[str, object]) -> str | None` — `"тишина"`, `"повтор"` или `None`.
  - `helper.segment.build_cues(segments: list[dict], drop: Callable[[str, dict], bool] | None = None) -> list[Cue]` — `drop(sentence, segment)`, сегмент — тот словарь, по которому идёт сборка, с метриками.
  - Воркер: `def _cue(segment) -> dict`; `transcribe(audio, *, model_name=DEFAULT_MODEL, compute_type="float16", language: str = "sr", hint: str | None = None, prompt_names: Sequence[str] | None = None, hotwords: str | None = None, batch_size: int = 16, vad_filter: bool = True) -> dict`. Ответ дополнительно несёт `"prompt": str | None`, каждая реплика — `"no_speech_prob"`, `"avg_logprob"`, `"compression_ratio"`. Флаг `--prompt-names JSON` вместо `--initial-prompt`.
  - `helper.jobs.asr_cues(segments: list[dict], language: str, prompt: str | None) -> tuple[list[Cue], list[tuple[str, str]]]`; причины выброса: `"тишина"` (сегмент целиком), `"повтор"` (зациклившееся предложение помеченного сегмента), `"эхо затравки"` (предложение).
  - Протокол демон → воркер: `--prompt-names '<JSON-список>'` в каждом запуске, даже `[]`; `--initial-prompt` больше нет. Ответ воркера меняется только добавлением ключей.

- [ ] **Step 1: Написать падающие тесты чистых функций**

Строки эха и речи взяты из локального кэша дорожек (66 620 реплик) и скопированы литералами: тесты кэш пользователя не читают.

```python
# tests/test_prompt.py
"""Затравка распознавания и отсев его мусора: чистые функции helper.asr.prompt."""

import pytest

from helper.asr.prompt import (
    MAX_NAMES,
    compression_ratio,
    harvest_names,
    initial_prompt,
    is_echo,
    segment_verdict,
)

PHRASE = "Ovo je transkript video snimka na srpskom jeziku."


def test_names_come_from_title_and_tags_in_their_own_script():
    names = harvest_names(
        "Мамини синови - Епизода 5 (ОФИЦИЈЕЛНИ спот)", ["Andrija i Anđelka", "Beograd"]
    )

    assert names == ["Мамини", "Епизода", "Andrija", "Anđelka", "Beograd"]


def test_shouted_title_words_are_not_names_but_short_acronyms_are():
    assert harvest_names("UDARIO SAM TEK OFARBANOG BMW", ["NATO", "MOTOR"]) == [
        "SAM", "TEK", "BMW", "NATO",
    ]


def test_names_are_deduplicated_in_order_and_capped():
    title = "Ana Bora Cana Dara Ema Fila Goca Hana Ivana Jana Kaja Lana Maja Nada"

    assert harvest_names("Beograd i Beograd", ["Beograd", "Novi Sad"]) == [
        "Beograd", "Novi", "Sad",
    ]
    assert harvest_names(title, []) == title.split()[:MAX_NAMES]


def test_serbian_gets_the_latin_phrase_and_transliterated_names():
    assert initial_prompt("sr", ["Мамини", "Beograd"]) == f"{PHRASE} Pominju se: Mamini, Beograd."


@pytest.mark.parametrize("language", ["hr", "bs", "sh", "me", "cnr", "sr-Latn", "SR"])
def test_every_label_of_the_serbian_family_gets_the_serbian_phrase(language):
    assert initial_prompt(language, ["Zagreb"]) == f"{PHRASE} Pominju se: Zagreb."


@pytest.mark.parametrize("language", ["en", "ru", "uk", "mk"])
def test_a_language_without_a_phrase_gets_no_prompt_not_even_names(language):
    assert initial_prompt(language, ["Clarkson"]) is None


@pytest.mark.parametrize("language", [None, "", "auto", "xx"])
def test_an_undecided_or_unknown_language_gets_no_prompt(language):
    assert initial_prompt(language, ["Clarkson"]) is None


def test_without_names_the_phrase_stands_alone():
    assert initial_prompt("sr", []) == PHRASE


def test_names_still_cyrillic_after_serbian_rules_are_dropped():
    assert initial_prompt("sr", ["Для", "Patreon"]) == f"{PHRASE} Pominju se: Patreon."
    assert initial_prompt("sr", ["Для"]) == PHRASE


def test_a_name_written_in_both_scripts_appears_once():
    assert initial_prompt("sr", ["Београд", "Beograd"]) == f"{PHRASE} Pominju se: Beograd."


def test_the_prompt_never_carries_more_than_twelve_names():
    names = [f"Ime{letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]

    assert initial_prompt("sr", names) == f"{PHRASE} Pominju se: {', '.join(names[:MAX_NAMES])}."


NAMED = initial_prompt("sr", ["Mamini", "Epizoda"])
FILM = initial_prompt("sr", [
    "Terminus", "Full", "Movie", "Johnny", "Hallyday", "Karen",
    "Allen", "Jürgen", "Prochnow", "However", "Genres", "Action",
])
SERIES = initial_prompt("sr", ["AndrijaiAnđelka", "Label", "Zabranjeno", "Andrija"])


@pytest.mark.parametrize("prompt, sentence", [
    (PHRASE, "Ovo je transkript video snimka na srpskom jeziku."),
    (NAMED, "Pominju se na srpskom jeziku."),
    (NAMED, "Pominju se."),
    (NAMED, "Pominju se na srpskom"),
    (NAMED, "Mamini, Epizoda."),
    (FILM, "Pominju se, Terminus, Full, Movie, Johnny, Hallyday, Karen, Allen, Jürgen, "
           "Prochnow, However, Genres, Action."),
    (FILM, "Karen, Allen, Jürgen, Prochnow, However, Genres, Action."),
    (SERIES, "AndrijaiAndjelka, Label, Zabranjeno,"),
])
def test_field_echoes_of_the_prompt_are_recognised(prompt, sentence):
    assert is_echo(sentence, prompt)


@pytest.mark.parametrize("prompt, sentence", [
    (NAMED, "Međutim, zadacima pominju se i ubistva."),
    (NAMED, "Evo, gospodine, uputstvo na srpskom jeziku."),
    (NAMED, "Ovo je video."),
    (NAMED, "Na srpskom jeziku."),
    (NAMED, "Pominju se, da je Rusija mogla organizirati diversiju"),
    (NAMED, "Pominju se 30 sekundi."),
    (PHRASE, "Pominju se."),
    (SERIES, "Ćao, Andrija."),
    (SERIES, "Andrija, Andrija."),
    (None, "Ovo je transkript video snimka na srpskom jeziku."),
    # Одно имя из заголовка в обиходной фразе — речь: в кэше «Šta je ovo?» есть в 17
    # роликах из 160, а заголовок «ŠTA SE KRILO U TITOVOM PLAVOM VOZU?» даёт имя ŠTA.
    (initial_prompt("sr", ["ŠTA", "VOZU", "Prava", "Istina"]), "Šta je ovo?"),
    (initial_prompt("sr", ["Beograd"]), "Ovo je Beograd."),
    # Обиходное слово фразы, ставшее именем, второго имени не даёт: заголовок
    # «OVO JE NAJGORI AUTO NA SVETU» даёт имена OVO и AUTO.
    (initial_prompt("sr", ["OVO", "AUTO"]), "Ovo je auto."),
])
def test_speech_that_shares_words_with_the_prompt_is_kept(prompt, sentence):
    assert not is_echo(sentence, prompt)


@pytest.mark.parametrize("metrics, verdict", [
    ({"no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 1.0}, "тишина"),
    ({"no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 3.0}, "тишина"),
    ({"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 2.5}, "повтор"),
    ({"no_speech_prob": 0.9, "avg_logprob": -0.3, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.1, "avg_logprob": -1.5, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.6, "avg_logprob": -1.5, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.9, "avg_logprob": -1.0, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 2.4}, None),
    ({"no_speech_prob": 0.9, "compression_ratio": 1.0}, None),
    ({}, None),
])
def test_segment_verdict_drops_only_silence_and_repetition(metrics, verdict):
    assert segment_verdict({"text": "Dobar dan.", **metrics}) == verdict


def test_compression_ratio_is_the_one_faster_whisper_uses():
    import helper.env  # noqa: F401 — HF_HOME во временный кэш до faster_whisper, как в воркере

    from faster_whisper.transcribe import get_compression_ratio

    loop = "Ideš, slušaj me, prvo uzduš, ovako, pa poprično, pa što sitnije, " + "tak, " * 70 + "tak"

    for text in (PHRASE, "Dobar dan.", ", ".join(["ne"] * 12), loop, "Šta je ovo?"):
        assert compression_ratio(text) == get_compression_ratio(text)
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_prompt.py -v`
Expected: FAIL — ошибка сбора `ModuleNotFoundError: No module named 'helper.asr.prompt'`

- [ ] **Step 3: Создать `helper/asr/prompt.py`**

Импорты: `re`, `unicodedata`, `zlib`, `from collections.abc import Mapping, Sequence`, `from helper.language import family`, `from helper.script import CYRILLIC, to_latin`. Именно `family`, а не модуль `helper.language` целиком: параметр `language` у `initial_prompt` затенил бы модуль. `CYRILLIC` — уже существующий образец `[Ѐ-ӿ]` (`helper/script.py:18`), второго не заводить. Ни `faster_whisper`, ни `helper.jobs`: модуль грузят и воркер, и демон.

Константы — как в Interfaces. `PHRASES["sr"]` и `NAMES_LEAD` дословно те, что собирал `_initial_prompt` (`helper/jobs.py:1456`, `1458`): при тех же именах затравка для сербского остаётся байт в байт прежней, меняется только набор имён и то, кому она достаётся.

Докстрока модуля, коротко и по-русски, — что проверено в faster-whisper 1.2.1 и почему устроено так:
- затравка нужна ровно для одного — прибить латиницу у сербского семейства (`helper/script.py:4-6`), поэтому фраза есть только у `sr`; где фразы нет, нет и затравки, имена тоже не идут;
- пакетный режим подаёт одну и ту же затравку каждому куску до 30 с (`generate_segment_batched` копирует её на каждый кусок пачки), и на кусках без речи она всплывает эхом;
- из затравки он берёт последние 223 токена (`get_prompt`: `max_length // 2 - 1`): переполнение съело бы фразу, а не имена;
- пороги тишины и повторов пакетный режим принимает и не применяет («Unused Arguments» в докстроке `BatchedInferencePipeline.transcribe`), поэтому отсев — на стороне демона: сегмент судят по метрикам, которые он и так несёт (`segment_verdict`), а предложения сегмента с «повтором» — по своей `compression_ratio` и по повтору уже сказанного (`helper.jobs.asr_cues`).

`harvest_names(title, tags)`: слова `\b\w{3,}` из заголовка, затем из тегов, по порядку. Берётся слово с заглавной первой буквой в любой письменности (`word[0].isalpha() and word[0].isupper()`); слово капсом длиннее четырёх букв (`word.isupper() and len(word) > 4`) пропускается; повторы убираются с сохранением порядка; не больше `MAX_NAMES`. Без транслитерации. Описания нет даже в параметрах. В докстроке: эхо в поле шло из шаблонного текста описаний (Label, Copyright, Patreon, Instagram, спонсоры); капс длиннее четырёх букв — выделение в заголовке (UDARIO, OFARBANOG, NAJGORIH), а короткие BMW и NATO — названия.

`initial_prompt(language, names)`: `PHRASES.get(family(language))`; фразы нет → `None`. Иначе имена: `to_latin` каждого; повторы, появившиеся после транслитерации, убрать (кириллический заголовок и латинские теги дают одно имя дважды); выбросить имена, где после сербских правил осталась кириллица (`CYRILLIC.search`: «Для» → «Dlя»); оставить не больше `MAX_NAMES` и здесь — воркер берёт список из командной строки, а переполнение съело бы фразу. Имён не осталось → фраза; иначе `f"{phrase} {NAMES_LEAD} {', '.join(names)}."`. Других вызовов `to_latin` на именах нет: тест BCMS здесь структурный — фраза есть только у семейства `sr`.

`is_echo(sentence, prompt)`: `prompt is None` → `False`. Слова обеих строк: `casefold()`, затем `"đ"` → `"dj"` (у đ нет разложения в NFKD), затем `unicodedata.normalize("NFKD", …)` без комбинирующих знаков (`unicodedata.combining`), затем множество `re.findall(r"\w+", …)`. Затравка делится по `NAMES_LEAD` (`prompt.partition(NAMES_LEAD)`): до вводки — фраза, после — имена. Эхо — когда у предложения не меньше двух разных слов, все они есть в затравке, хотя бы одно не из `ECHO_COMMON`, и при этом среди них есть слово фразы или самой вводки не из `ECHO_COMMON` (для `sr` — transkript, snimka, pominju) либо не меньше двух разных слов из имён, не считая слов `ECHO_COMMON`: заголовок «OVO JE NAJGORI AUTO NA SVETU» даёт имена OVO и AUTO, а «Ovo je auto.» — речь, как и «Šta je ovo?» под «ŠTA JE OVO?! …». В докстроке — отступление от буквы замысла: буквального совпадения с затравкой нет ни в одной из 66 620 закэшированных реплик, эхо — обрывки и перестановки её хвоста («Pominju se na srpskom jeziku.») и списки имён; обиходные слова фразы эхо не делают («Ovo je video.», «Na srpskom jeziku.»), одно слово тоже («Andrija, Andrija.»), и одно имя из заголовка в обиходной фразе тоже: заголовок «ŠTA SE KRILO U TITOVOM PLAVOM VOZU?» даёт имя ŠTA, и без этого условия «Šta je ovo?» уходило бы эхом.

`compression_ratio(text)` — степень сжатия по формуле faster-whisper (`faster_whisper.transcribe.get_compression_ratio`): `len(байты UTF-8) / len(zlib.compress(байты UTF-8))`, только стандартная библиотека. Совпадение с библиотекой пинает `test_compression_ratio_is_the_one_faster_whisper_uses`. Нужна демону для предложений внутри сегмента: метрику сегмента считает сама библиотека, а предложения — только мы.

`segment_verdict(segment)`: `"тишина"`, когда `no_speech_prob > NO_SPEECH_THRESHOLD` и `avg_logprob < LOG_PROB_THRESHOLD`; иначе `"повтор"`, когда `compression_ratio > COMPRESSION_RATIO_THRESHOLD`; иначе `None`. Поля нет или оно `None` — по нему не выбрасывать. В докстроке: «тишина» — правило, которым Whisper сам пропускает окно в последовательном режиме, и такой сегмент выбрасывается целиком; «повтор» — замена отката по температуре, которого в пакетном режиме нет (Whisper перерасшифровал бы кусок), но сегмент пакетного режима — целый кусок речи до 30 с, и петля часто начинается посреди него, после настоящего диалога. Поэтому «повтор» сегмент только помечает: внутри выбрасываются одни зациклившиеся предложения (`asr_cues`, шаг 12), а настоящая речь перед петлёй остаётся. Каждый выброс уходит в журнал (шаг 12).

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_prompt.py -v`
Expected: PASS, 55 passed

- [ ] **Step 5: Написать падающие тесты сборки реплик с отсевом**

```python
# tests/test_segment.py
"""Сборка реплик из сегментов распознавания: helper.segment."""

from helper.segment import build_cues

FILM_ECHO = (
    "Pominju se, Terminus, Full, Movie, Johnny, Hallyday, Karen, Allen, Jürgen, "
    "Prochnow, However, Genres, Action."
)


def word(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "word": text}


GREETING = {
    "start": 0.0,
    "end": 5.0,
    "text": "Dobar dan. Pominju se na srpskom jeziku. Kako ste?",
    "words": [
        word(0.0, 0.4, " Dobar"), word(0.5, 0.9, " dan."),
        word(1.0, 1.4, " Pominju"), word(1.5, 1.6, " se"), word(1.7, 1.8, " na"),
        word(1.9, 2.2, " srpskom"), word(2.3, 2.6, " jeziku."),
        word(4.0, 4.4, " Kako"), word(4.5, 5.0, " ste?"),
    ],
}


def timeline(cues) -> list[tuple[str, float, float]]:
    return [(c.text, round(c.start, 2), round(c.end, 2)) for c in cues]


def test_a_dropped_sentence_leaves_no_cue_and_its_neighbours_keep_their_words():
    cues = build_cues([GREETING], drop=lambda sentence, segment: sentence.startswith("Pominju"))

    assert timeline(cues) == [("Dobar dan.", 0.0, 0.9), ("Kako ste?", 4.0, 5.0)]


def test_a_long_sentence_is_dropped_whole_not_piece_by_piece():
    segment = {"start": 0.0, "end": 6.0, "text": FILM_ECHO, "words": []}

    assert len(build_cues([segment])) == 2
    assert build_cues([segment], drop=lambda sentence, segment: sentence == FILM_ECHO) == []


def test_a_predicate_that_drops_nothing_changes_nothing():
    assert build_cues([GREETING], drop=lambda sentence, segment: False) == build_cues([GREETING])
```

Первый тест ловит отсев до сопоставления слов: тогда «Kako ste?» легла бы на слова «Pominju se» и получила бы 1.0–1.6 с (проверено: выходит `[('Dobar dan. Kako ste?', 0.0, 1.6)]`). Второй — отсев по кускам: 108 знаков режутся `_split_balanced` на две реплики, и порознь ни одна не равна предложению.

- [ ] **Step 6: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: FAIL — 3 failed, `TypeError: build_cues() got an unexpected keyword argument 'drop'` (первая проверка второго теста, две реплики без отсева, проходит уже сейчас)

- [ ] **Step 7: Отсев предложений в `build_cues`**

`helper/segment.py`: импорт `from collections.abc import Callable`; сигнатура `build_cues(segments: list[dict], drop: Callable[[str, dict], bool] | None = None) -> list[Cue]`. Первой строкой тела цикла `for sentence, (start, end) in zip(sentences, spans):` (строка 195) — `if drop is not None and drop(sentence, segment): continue`; `segment` — словарь, по которому идёт сборка, со всеми его ключами: `normalize_cues` копирует их (`{**cue, ...}`), так что метрики сегмента доходят до `drop`. Комментарий над ней: интервалы сопоставляются со словами по всем предложениям сегмента, до отсева, — выбрось предложение раньше, и каждое следующее легло бы на чужие слова; отсев идёт по предложению целиком, до `_split_balanced` и `_enforce_min_duration`: длинное эхо иначе резалось бы на куски, порознь на эхо не похожие, а короткое склеивалось бы с соседней настоящей репликой («Da. Pominju se na srpskom jeziku.»). В докстроке `build_cues` — одна фраза про `drop`: зовётся как `drop(предложение, сегмент)`; `True` — предложение в реплики не идёт. Без `drop` поведение прежнее: на этом держится `spikes/asr_real.py:134`.

- [ ] **Step 8: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: PASS, 3 passed

- [ ] **Step 9: Написать падающие тесты воркера и демона**

`_detect` подменяется прямо в модуле воркера: тесты этой задачи про затравку и метрики, а не про окна, VAD и модель. Сегменты faster-whisper строит `whisper.segment(...)` фикстуры из задачи 3 — настоящий `Segment`, имена полей метрик сверяет сама библиотека. Метаданные — `dataclasses.replace(pipeline.info, …)`.

```python
# tests/test_asr_filters.py
"""Затравка под решённый язык и отсев мусора распознавания: воркер и демон."""

import logging
from dataclasses import replace

import numpy as np
import pytest

from helper import jobs
from helper.asr import worker
from helper.asr.prompt import initial_prompt
from helper.jobs import Stage
from helper.language import Detection, LanguageVerdict

VIDEO = "abcdefghijk"
PHRASE = "Ovo je transkript video snimka na srpskom jeziku."
NAMED = initial_prompt("sr", ["Mamini", "Epizoda"])
LOOP = ", ".join(["ne"] * 12)
# jWrDvxtofLk, 201,8–229,6 с: шесть настоящих предложений, потом петля «tak» в том же куске.
DIALOGUE = (
    "Pa nije to dobro. Kako nije? Šta nije dobro? Pa očajno je, pogledaj ti koliko je ovo "
    "krupno. Šta radiš sa ovim? Da ga jedem."
)
TAK = "Ideš, slušaj me, prvo uzduš, ovako, pa poprično, pa što sitnije, " + "tak, " * 70 + "tak"
MINUTE = np.zeros(60 * worker.SAMPLE_RATE, dtype="<f4")

# Восемь окон с речью, решает определитель. Сербская речь — как у канала Juske,
# который YouTube помечает en.
SERBIAN_SPEECH = Detection(
    language="sr", confidence=0.97, bcms_share=0.97, floor=False, votes=8, probed=8,
    windows=(("sr", 0.97, 25.0),) * 8,
)
ENGLISH_SPEECH = Detection(
    language="en", confidence=0.99, bcms_share=0.0, floor=False, votes=8, probed=8,
    windows=(("en", 0.99, 28.0),) * 8,
)

# Аргументы, которые пакетный режим faster-whisper 1.2.1 принимает и не применяет.
DEAD_GUARDS = {
    "temperature",
    "compression_ratio_threshold",
    "log_prob_threshold",
    "no_speech_threshold",
    "condition_on_previous_text",
    "hallucination_silence_threshold",
}


def heard(monkeypatch, detection: Detection) -> None:
    """Определитель «услышал» заданное: окна, VAD и модель здесь ни при чём."""
    monkeypatch.setattr(worker, "_detect", lambda model, audio: detection)


def segment(start, end, text, *, no_speech=0.05, logprob=-0.2, ratio=1.1) -> dict:
    """Сегмент так, как его приносит JSON воркера."""
    return {
        "start": start, "end": end, "text": text, "words": [],
        "no_speech_prob": no_speech, "avg_logprob": logprob, "compression_ratio": ratio,
    }


MIXED = [
    segment(0.0, 5.0, "Dobar dan. Pominju se na srpskom jeziku. Kako ste?"),
    segment(6.0, 9.0, "Hvala na gledanju.", no_speech=0.92, logprob=-1.4),
    segment(10.0, 14.0, LOOP, ratio=3.1),
]


def test_cue_carries_the_three_metrics_of_its_segment(whisper):
    from faster_whisper.transcribe import Word

    whole = whisper.segment(
        1.0, 2.5, " Zdravo svima.", no_speech_prob=0.12, avg_logprob=-0.31, compression_ratio=1.07,
        words=[
            Word(start=1.0, end=1.4, word=" Zdravo", probability=0.9),
            Word(start=1.5, end=2.5, word=" svima.", probability=0.8),
        ],
    )

    assert worker._cue(whole) == {
        "start": 1.0,
        "end": 2.5,
        "text": "Zdravo svima.",
        "words": [
            {"start": 1.0, "end": 1.4, "word": " Zdravo"},
            {"start": 1.5, "end": 2.5, "word": " svima."},
        ],
        "no_speech_prob": 0.12,
        "avg_logprob": -0.31,
        "compression_ratio": 1.07,
    }


def test_prompt_follows_the_decided_language_not_the_metadata(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)
    whisper.segments = [whisper.segment(0.0, 2.0, " Dobar dan.")]

    result = worker.transcribe(
        MINUTE, language="auto", hint="en", prompt_names=["Juske", "Беочин"]
    )

    assert result["language"] == "sr"
    assert result["prompt"] == f"{PHRASE} Pominju se: Juske, Beočin."
    assert whisper.kwargs["initial_prompt"] == result["prompt"]


def test_english_speech_is_decoded_without_any_prompt(whisper, monkeypatch):
    heard(monkeypatch, ENGLISH_SPEECH)
    whisper.segments = [whisper.segment(0.0, 2.0, " Hello there.")]

    result = worker.transcribe(MINUTE, language="auto", prompt_names=["Copyright", "Subscribe"])

    assert result["language"] == "en"
    assert result["prompt"] is None
    assert whisper.kwargs["initial_prompt"] is None


def test_a_pinned_croatian_keeps_its_token_and_gets_the_serbian_phrase(whisper, monkeypatch):
    heard(monkeypatch, ENGLISH_SPEECH)

    result = worker.transcribe(MINUTE, language="hr", prompt_names=["Zagreb"])

    assert whisper.kwargs["language"] == "hr"
    assert result["prompt"] == f"{PHRASE} Pominju se: Zagreb."


def test_without_prompt_names_there_is_no_prompt(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)

    result = worker.transcribe(MINUTE, language="sr")

    assert result["prompt"] is None
    assert whisper.kwargs["initial_prompt"] is None


def test_worker_returns_every_segment_with_its_metrics(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)
    whisper.segments = [
        whisper.segment(0.0, 2.0, " Dobar dan.", no_speech_prob=0.05, avg_logprob=-0.2),
        whisper.segment(2.0, 30.0, " Hvala na gledanju.", no_speech_prob=0.92, avg_logprob=-1.4),
    ]

    result = worker.transcribe(MINUTE, language="sr", prompt_names=[])

    assert [(c["text"], c["no_speech_prob"], c["avg_logprob"]) for c in result["cues"]] == [
        ("Dobar dan.", 0.05, -0.2),
        ("Hvala na gledanju.", 0.92, -1.4),
    ]
    assert result["prompt"] == PHRASE


def test_guards_that_batched_mode_ignores_are_not_passed(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)

    worker.transcribe(MINUTE, language="sr", prompt_names=[])

    assert DEAD_GUARDS.isdisjoint(whisper.kwargs)


def test_worker_takes_names_as_a_json_list_and_no_ready_prompt():
    parser = worker.build_parser()

    assert parser.parse_args(["--prompt-names", '["Мамини", "Beograd"]']).prompt_names == [
        "Мамини", "Beograd",
    ]
    assert parser.parse_args([]).prompt_names is None

    refused = (
        ["--initial-prompt", PHRASE],
        ["--prompt-names", '"Beograd"'],
        ["--prompt-names", "Beograd"],
    )

    for argv in refused:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_silence_repetition_and_echo_are_dropped_with_a_reason():
    cues, dropped = jobs.asr_cues(MIXED, "sr", NAMED)

    assert [c.text for c in cues] == ["Dobar dan.", "Kako ste?"]
    assert sorted(dropped) == sorted([
        ("тишина", "Hvala na gledanju."),
        ("повтор", LOOP),
        ("эхо затравки", "Pominju se na srpskom jeziku."),
    ])


def test_echo_is_judged_after_cyrillic_becomes_latin():
    cues, dropped = jobs.asr_cues([segment(0.0, 3.0, "Помињу се на српском језику.")], "sr", NAMED)

    assert cues == []
    assert dropped == [("эхо затравки", "Pominju se na srpskom jeziku.")]


def test_without_a_prompt_the_phrase_is_just_speech():
    cues, dropped = jobs.asr_cues([segment(0.0, 4.0, PHRASE)], "en", None)

    assert [c.text for c in cues] == [PHRASE]
    assert dropped == []


def test_a_loop_takes_only_its_own_sentence_not_the_dialogue_before_it():
    # Кусок целиком сжимается в 3.45 раза, а каждое настоящее предложение в нём — не
    # больше чем в 0.89: выброс куска унёс бы диалог вместе с петлёй.
    cues, dropped = jobs.asr_cues(
        [segment(201.8, 229.6, f"{DIALOGUE} {TAK}", ratio=3.45)], "sr", NAMED
    )

    assert [c.text for c in cues] == [
        "Pa nije to dobro.", "Kako nije?", "Šta nije dobro?",
        "Pa očajno je, pogledaj ti koliko je ovo krupno.", "Šta radiš sa ovim?", "Da ga jedem.",
    ]
    assert dropped == [("повтор", TAK)]


def test_a_two_sentence_loop_keeps_its_first_turn():
    looped = "Dobro jutro. Kako si? " + "Idemo dalje. Vidimo se. " * 6

    cues, dropped = jobs.asr_cues([segment(0.0, 30.0, looped, ratio=3.0)], "sr", NAMED)

    # Каждое предложение петли сжимается плохо, ловит её только повтор уже сказанного.
    assert [c.text for c in cues] == ["Dobro jutro.", "Kako si?", "Idemo dalje.", "Vidimo se."]
    assert dropped == [("повтор", "Idemo dalje."), ("повтор", "Vidimo se.")] * 5


def test_repeats_in_an_unmarked_segment_are_speech():
    cues, dropped = jobs.asr_cues([segment(0.0, 4.0, "Da. Ne. Da. Ne znam. Da.")], "sr", NAMED)

    assert " ".join(c.text for c in cues) == "Da. Ne. Da. Ne znam. Da."
    assert dropped == []


def test_dropped_lines_never_become_job_cues_and_each_is_logged(manager, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    job = manager.get_or_create(VIDEO, "ru", True)
    verdict = LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.97,
        reason="detector",
    )
    result = {
        "language": "sr",
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.97, "votes": 8, "probed": 8, "windows": [["sr", 0.97, 25.0]] * 8,
        },
        "duration": 14.0,
        "model": "large-v3",
        "prompt": NAMED,
        "cues": MIXED,
    }

    manager.runner._accept_asr(job, result)

    assert [(c.index, c.src) for c in job.cues] == [(0, "Dobar dan."), (1, "Kako ste?")]

    logged = [r.getMessage() for r in caplog.records if r.name == "helper.jobs"]

    assert f"затравка {VIDEO}: {NAMED}" in logged
    assert f"выброшено {VIDEO} (тишина): Hvala na gledanju." in logged
    assert f"выброшено {VIDEO} (повтор): {LOOP}" in logged
    assert f"выброшено {VIDEO} (эхо затравки): Pominju se na srpskom jeziku." in logged


async def test_daemon_sends_title_and_tag_names_not_a_ready_prompt(pipeline, manager):
    pipeline.info = replace(
        pipeline.info,
        title="Мамини синови - Епизода 5",
        tags=["Beograd"],
        description="Copyright Label Instagram",
    )
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    argv = pipeline.workers[-1].args
    parsed = worker.build_parser().parse_args(argv[3:])

    assert job.stage is Stage.DONE
    assert "--initial-prompt" not in argv
    assert argv[argv.index("--prompt-names") + 1] == '["Мамини", "Епизода", "Beograd"]'
    assert parsed.prompt_names == ["Мамини", "Епизода", "Beograd"]
```

- [ ] **Step 10: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_asr_filters.py -v`
Expected: FAIL — 16 failed:
- `test_cue_carries_the_three_metrics_of_its_segment` — `AttributeError: module 'helper.asr.worker' has no attribute '_cue'`;
- пять тестов, что зовут `transcribe(..., prompt_names=...)`, — `TypeError: transcribe() got an unexpected keyword argument 'prompt_names'`;
- `test_without_prompt_names_there_is_no_prompt` — `KeyError: 'prompt'`;
- `test_worker_takes_names_as_a_json_list_and_no_ready_prompt` — `SystemExit: 2` (`--prompt-names` парсер не знает);
- шесть тестов `asr_cues` — `AttributeError: module 'helper.jobs' has no attribute 'asr_cues'`;
- `test_dropped_lines_never_become_job_cues_and_each_is_logged` — в `job.cues` пять реплик: эхо, тишина и петля среди них;
- `test_daemon_sends_title_and_tag_names_not_a_ready_prompt` — `AssertionError: assert '--initial-prompt' not in [...]`.

- [ ] **Step 11: Воркер — затравка после решения о языке, метрики в ответе, имена вместо затравки**

`helper/asr/worker.py`:

1. Импорты: `from collections.abc import Sequence`; после `import helper.env` — `from helper.asr.prompt import initial_prompt`.
2. `_cue(segment) -> dict` — модульная функция вместо спискового выражения 192-203. Ключи: `start`, `end`, `text` (`segment.text.strip()`), `words` (`start`, `end`, `word` каждого слова из `segment.words or []`; `probability` не берётся, как и сегодня), плюс `no_speech_prob`, `avg_logprob`, `compression_ratio` как есть. Фильтр пустых `"cues": [c for c in cues if c["text"]]` остаётся.
3. `transcribe`: параметр `initial_prompt` заменить на `prompt_names: Sequence[str] | None = None` — после `hint`, порядок как в Interfaces. Сразу после решения о языке (задача 5: `verdict = …decide(…)`) — `prompt = initial_prompt(verdict.effective, prompt_names) if prompt_names is not None else None`; в `pipeline.transcribe` — `initial_prompt=prompt`; в ответе — `"prompt": prompt` рядом с `"model"`. `None` значит «флага не было»: так воркер зовут `helper/measure.py` и `spikes/asr_real.py`, и затравки у них нет, как сегодня; `[]` — фраза без имён. Комментарий у затравки: она по решённому языку, а не по объявленному — сербской речи под меткой en (Juske) нужна сербская фраза, английской речи никакая.
4. Из аргументов `pipeline.transcribe` убрать `temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`, `compression_ratio_threshold=2.4`, `log_prob_threshold=-1.0`, `no_speech_threshold=0.6`, `condition_on_previous_text=False`, `hallucination_silence_threshold=2.0` вместе с комментариями 164-165 и 170-172 — оба неверны для пакетного режима. На 1.2.1 поведение не меняется: пороги пакетный режим только хранит в опциях (`faster_whisper/transcribe.py:525-527`) и нигде не читает, лестницу температур урезает до первой (528-532; у умолчания она та же, 0.0), а `hallucination_silence_threshold=None` и `condition_on_previous_text=False` выставляет сам (546-547). Вместо них один комментарий: пороги не передаются, потому что пакетный режим их не применяет; тишину демон отсекает целым сегментом по его метрикам (`helper.asr.prompt.segment_verdict`), а в сегменте с «повтором» — только зациклившиеся предложения (`helper.jobs.asr_cues`); каждый выброс виден в журнале. `task`, `beam_size`, `word_timestamps`, `vad_filter`, `vad_parameters`, `hotwords`, `batch_size` — без изменений.
5. `build_parser` (задача 5): убрать `--initial-prompt`; добавить `--prompt-names` с `type=_prompt_names`, `default=None` и help «JSON-список имён из заголовка и тегов; затравку по решённому языку составит воркер». `_prompt_names(raw: str) -> list[str]` — `json.loads(raw)`; не список строк → `argparse.ArgumentTypeError("нужен JSON-список строк")`; битый JSON argparse и так превращает в ошибку разбора, выход 2. В `main` — `prompt_names=args.prompt_names` вместо `initial_prompt=args.initial_prompt`.
6. `tests/test_worker.py` (задача 5) — вслед за ответом и парсером: в `test_result_carries_the_verdict_and_the_windows` ожидаемый набор ключей становится `{"language", "verdict", "detection", "duration", "model", "prompt", "cues"}`; в `test_parser_keeps_the_cli_contract` в кортеже умолчаний `args.initial_prompt` заменить на `args.prompt_names` — без флага он по-прежнему `None`.

- [ ] **Step 12: Демон — имена вместо затравки, отсев до реплик**

`helper/jobs.py`:

1. Импорты: `import re` — к стандартным (между `logging` и `secrets`); `from helper.asr.prompt import COMPRESSION_RATIO_THRESHOLD, compression_ratio, harvest_names, is_echo, segment_verdict` — перед `from helper.asr.worker import SAMPLE_RATE`.
2. Удалить `_initial_prompt` (1443-1458) и `_proper_nouns` (1461-1478) целиком — вместе с докстрокой, неверной для пакетного режима (обусловливание на предыдущий текст, «половина 224-токенного контекста»). На их месте — модульная `asr_cues`:

```python
def asr_cues(
    segments: list[dict], language: str, prompt: str | None
) -> tuple[list[Cue], list[tuple[str, str]]]:
    """Реплики из сегментов распознавания и то, что выброшено по дороге: (причина, текст)."""
    dropped: list[tuple[str, str]] = []
    kept = []

    for segment in segments:
        if segment_verdict(segment) == "тишина":
            dropped.append(("тишина", segment["text"]))
        else:
            kept.append(segment)

    # Уже сказанное в сегменте: своё у каждого, петля живёт внутри одного куска.
    heard: dict[int, set[tuple[str, ...]]] = {}

    def drop(sentence: str, segment: dict) -> bool:
        if segment_verdict(segment) == "повтор":
            # Слова без регистра и знаков: «Idemo dalje» без точки — тот же повтор.
            words = tuple(re.findall(r"\w+", sentence.casefold()))
            seen = heard.setdefault(id(segment), set())
            looped = words in seen or compression_ratio(sentence) > COMPRESSION_RATIO_THRESHOLD
            seen.add(words)

            if looped:
                dropped.append(("повтор", sentence))

                return True

        # Эхо проверяется после normalize_cues: кириллический вывод сравнивается
        # с латинской затравкой уже латиницей.
        if is_echo(sentence, prompt):
            dropped.append(("эхо затравки", sentence))

            return True

        return False

    return build_cues(normalize_cues(kept, language), drop=drop), dropped
```

Правило отсева. «Тишина» выбрасывает сегмент целиком, до `normalize_cues`. В сегменте с «повтором» `drop` выбрасывает предложение как `("повтор", предложение)`, когда его своя степень сжатия выше `COMPRESSION_RATIO_THRESHOLD` или оно повторяет уже сказанное в этом же сегменте; сравниваются слова без регистра (`re.findall(r"\w+", sentence.casefold())`), так что потерянная точка повтор не прячет. Память своя у каждого сегмента. Петля из двух предложений (A. B. A. B. …) оставляет первые A и B. В сегментах без пометки повторы не выбрасываются никогда: «Da. Ne. Da.» — обычная речь. Эхо проверяется после повтора, как и прежде.

В докстроке ещё две вещи: фильтры только для распознавания — ветка дорожки автора в `_pipeline` не меняется; выбросы возвращаются, а не пишутся здесь, потому что журнал ведёт задание.

Параметр `language` (имя закреплено в Interfaces) затеняет модуль внутри `asr_cues`; модуль там не нужен, `language.*` в ней не звать.

3. `JobRunner._accept_asr` (задача 6): строку, что строит реплики (`job.cues = self._to_job_cues(build_cues(normalize_cues(result["cues"], verdict.effective)))`), заменить на:
   - `prompt = result.get("prompt")`;
   - `log.info("затравка %s: %s", job.video_id, prompt or "нет")`;
   - `cues, dropped = asr_cues(result["cues"], verdict.effective, prompt)`;
   - на каждый выброс `log.info("выброшено %s (%s): %s", job.video_id, reason, text)`;
   - `job.cues = self._to_job_cues(cues)`.

   Порядок прежний: язык, реплики, `segmented` — всё до `held.check()` в `_pipeline`. Затравку пишем потому, что её теперь составляет воркер, а его stderr при успехе демон выбрасывает: без этой строки эхо на экране не с чем сопоставить. Строка на выброс — потому что выброшенная «тишина» — это до 30 с звука, а «повтор» — предложение, и проверить пороги на этом корпусе нечем: в кэше метрик нет. Журнал — единственная проверка в поле. Индексы `JobCue` остаются сплошными: отсев идёт до `_to_job_cues`, а на `index` опираются `_next_batch` и `_context_for`.
4. `JobRunner._run_asr`: убрать строку 1153 (`prompt = _initial_prompt(job.info)`) и 1166-1167 (`--initial-prompt`). Сразу после блока `if hint: args += ["--hint", hint]` из задачи 5 — безусловно, в каждом запуске, даже со списком `[]`: `args += ["--prompt-names", json.dumps(harvest_names(job.info.title, job.info.tags), ensure_ascii=False)]`. `ensure_ascii=False` — имена читаемы в `ps` и в журнале. Комментарий: имена, а не готовая затравка — язык решает воркер после определителя, и только он знает, есть ли для этого языка фраза; описание не передаётся (см. `harvest_names`).

- [ ] **Step 13: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_asr_filters.py tests/test_prompt.py tests/test_segment.py -v`
Expected: PASS, 74 passed

Run: `grep -rn --include="*.py" -e "_initial_prompt" -e "_proper_nouns" -e "--initial-prompt" helper tests`
Expected: две строки, обе в `tests/test_asr_filters.py`: `["--initial-prompt", PHRASE],` и `assert "--initial-prompt" not in argv` (на `98a3ad2` эта же команда находит шесть строк в `helper/jobs.py` и `helper/asr/worker.py`)

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 442 passed

- [ ] **Step 14: Коммит**

```bash
/usr/bin/git add helper/asr/prompt.py helper/asr/worker.py helper/segment.py helper/jobs.py tests/test_prompt.py tests/test_segment.py tests/test_asr_filters.py tests/test_worker.py
/usr/bin/git commit -m "Затравка распознавания по решённому языку; тишина, петли повторов и эхо затравки не доходят до реплик"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 9: Нарезка не рвёт сокращения

`SENTENCE_END` (`helper/segment.py:32`) кончает предложение на любой точке перед пробелом. «Today we test the U.S. market, e.g. tech stocks.» сегодня — три части, `['Today we test the U.S.', 'market, e.g.', 'tech stocks.']`, «Rođen je 5. maja 1980. godine.» — тоже три, и обрывки уходят в переводчик как самостоятельные фразы. В локальном кэше это около 0,63% границ реплик распознавания, почти все — на сербских датах и годах (`21.` | `januara`, `1948.` | `godine`), английских — единицы. Правило не зависит от языка: часть, начинающаяся со строчной буквы, предложения не начинает. Списков сокращений по языкам нет, действующий язык не нужен.

**Files:**
- Modify: `helper/segment.py:62-72` (`split_sentences`)
- Test: `tests/test_segment.py` (из задачи 8, дописать)

**Interfaces:**
- Consumes: `tests/test_segment.py` с помощниками `word` и `timeline` (задача 8).
- Produces: `split_sentences(text: str) -> list[str]` — сигнатура прежняя; часть, первый знак которой — строчная буква, приклеивается к предыдущей через один пробел.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_segment.py` импорт заменить на `from helper.segment import build_cues, split_sentences` и дописать в конец:

```python
def test_abbreviations_do_not_end_a_sentence():
    assert split_sentences("Today we test the U.S. market, e.g. tech stocks.") == [
        "Today we test the U.S. market, e.g. tech stocks."
    ]


def test_ordinals_and_years_do_not_end_a_sentence():
    assert split_sentences("Rođen je 5. maja 1980. godine.") == ["Rođen je 5. maja 1980. godine."]


def test_ordinary_sentences_still_split():
    assert split_sentences("Prva rečenica. Druga rečenica! Treća?") == [
        "Prva rečenica.", "Druga rečenica!", "Treća?",
    ]
    assert split_sentences("Bilo je 1948. Tada je počelo.") == ["Bilo je 1948.", "Tada je počelo."]


def test_a_date_reaches_the_translator_as_one_cue():
    segment = {
        "start": 0.0,
        "end": 5.0,
        "text": "Rođen je 5. maja 1980. godine. Živio je u Beogradu.",
        "words": [
            word(0.0, 0.4, " Rođen"), word(0.5, 0.6, " je"), word(0.7, 0.9, " 5."),
            word(1.0, 1.3, " maja"), word(1.4, 1.9, " 1980."), word(2.0, 2.6, " godine."),
            word(3.5, 3.9, " Živio"), word(4.0, 4.1, " je"), word(4.2, 4.3, " u"),
            word(4.4, 5.0, " Beogradu."),
        ],
    }

    assert timeline(build_cues([segment])) == [
        ("Rođen je 5. maja 1980. godine.", 0.0, 2.6),
        ("Živio je u Beogradu.", 3.5, 5.0),
    ]
```

`test_ordinary_sentences_still_split` проходит уже сейчас — это страж: склейка не должна съесть настоящие границы, в том числе год в конце предложения перед заглавной.

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: FAIL — 3 failed, 4 passed:
- `test_abbreviations_do_not_end_a_sentence` — получено `['Today we test the U.S.', 'market, e.g.', 'tech stocks.']`;
- `test_ordinals_and_years_do_not_end_a_sentence` — получено `['Rođen je 5.', 'maja 1980.', 'godine.']`;
- `test_a_date_reaches_the_translator_as_one_cue` — получено `[('Rođen je 5.', 0.0, 0.9), ('maja 1980. godine.', 1.0, 2.6), ('Živio je u Beogradu.', 3.5, 5.0)]`.

- [ ] **Step 3: Склеивать часть со строчной буквы с предыдущей**

`split_sentences` (`helper/segment.py:62-72`): после разбиения по `SENTENCE_END` пройти части по порядку; часть, у которой `part[0].islower()`, дописать к предыдущей через один пробел; первая часть и части, начинающиеся с заглавной, цифры, кавычки или скобки, остаются отдельными. Пустой текст — как сейчас, `[]`.

В докстроку — правило и его границы. Точка перед строчной буквой предложения не кончает: порядковые числительные и годы («5. maja», «1980. godine»), сокращения («U.S. market», «e.g. tech», «npr.»), многоточие перед продолжением («ali... ali, nekako»). Что остаётся как было: продолжение с заглавной («Mr. Smith») режется по-прежнему; письменности без регистра не затронуты (`islower()` у них ложно); граница, совпавшая с краем куска VAD, лежит между сегментами и сюда не доходит.

Не трогать: `SENTENCE_END`, `CONJUNCTIONS` (перенос по союзам других языков отложен, см. «Отложено»), `_split_balanced`. `PROMPT_VERSION` не поднимать — это задача 11.

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: PASS, 7 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 446 passed

- [ ] **Step 5: Коммит**

```bash
/usr/bin/git add helper/segment.py tests/test_segment.py
/usr/bin/git commit -m "Нарезка не рвёт предложение на сокращениях, порядковых числительных и годах"
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---
### Task 10: Дорожка автора: метки нормализуются

Сейчас дорожку автора выбирает регулярка `BCMS_LANG = ^(sr|hr|bs|sh)(-|$)`, а письменность её текста решает точное
сравнение `language not in BCMS` в `helper/script.py:76`, куда конвейер передаёт сырую метку `track.lang`. Проверено на
98a3ad2: `normalize_cues` с меткой `sr-Cyrl`, `SR` или `sr_Latn` оставляет «Добар дан, људи.» кириллицей, и она уходит
в переводчик; метки `cnr` и `sr_Latn` регулярка не узнаёт вовсе; закреп хозяина в выборе не участвует, так что при
`[asr] language = "en"` сербская дорожка (то есть перевод) берётся как оригинал. Задача сводит все три места к одной
семье языка из `helper/language.py`. Дорожки на других языках в этом плане не выбираются (см. «Отложено»): сверять
метку с речью без карты нечем.

`helper/script.py` ни за одной задачей плана не числится, а его `BCMS` по общему правилу заменяет та задача, что трогает
файл. Гейт `normalize_cues` — та самая ошибка, которую чинит эта задача, поэтому замена здесь.

**Files:**
- Modify: `helper/sources/ytdlp.py:34-35` (комментарий и `BCMS_LANG` — удалить), `helper/sources/ytdlp.py:321-336` (`usable_manual_track`), импорт рядом с `helper/sources/ytdlp.py:21`
- Modify: `helper/script.py:24-25` (`BCMS` — удалить), `helper/script.py:60-86` (`normalize_cues`, проверка на строке 76), импорт рядом с `helper/script.py:16`
- Modify: `helper/jobs.py:813-817` (`JobRunner._pipeline`, выбор дорожки), `helper/jobs.py:854-856` (`JobRunner._pipeline`, ветка дорожки после удачной загрузки: `normalize_cues(segments, track.lang)`)
- Test: `tests/test_author_track.py` (новый)

**Interfaces:**
- Consumes: `helper.language.is_bcms(code: str | None) -> bool` (задача 4); `JobRunner._declaration(self, job) -> tuple[str | None, str | None]` — `(закрепление, подсказка метаданных)` (задача 5); решение по дорожке `verdict` — `LanguageVerdict(effective=language.hint(track.lang) or "sr", …, reason="track", source="manual", track=track.lang)`, которое задача 6 строит в ветке дорожки `_pipeline`, и `Job.language` (задача 6); фикстура `pipeline` и построитель `metadata(**overrides) -> VideoInfo` из `tests/conftest.py` (задача 3).
- Produces: `usable_manual_track(info: VideoInfo, pin: str | None = None) -> CaptionTrack | None`; `normalize_cues(cues: list[dict], language: str = "sr") -> list[dict]` латинизирует, когда `is_bcms(language)`; ветка дорожки в `_pipeline` нормализует текст по `verdict.effective`. `helper.sources.ytdlp.BCMS_LANG` и `helper.script.BCMS` больше не существуют.

- [ ] **Step 1: Написать падающие тесты**

Входы фикстуры — как их назвала задача 3: `pipeline.info` — что вернёт подменённый `probe`, `pipeline.track` — что
вернёт подменённый `fetch_manual_track`. Если в `tests/conftest.py` они названы иначе, взять те имена.

```python
# tests/test_author_track.py
"""Дорожка автора: какая метка годится и в какой письменности её текст идёт дальше."""

import pytest

from conftest import metadata
from helper.jobs import Job, Stage
from helper.script import normalize_cues
from helper.sources.ytdlp import CaptionTrack, usable_manual_track

# yt-dlp отдаёт каждую дорожку семью форматами, и probe кладёт по записи на каждый.
FORMATS = ("json3", "srv1", "srv2", "srv3", "ttml", "srt", "vtt")

CYRILLIC = [
    {"start": 1.0, "end": 3.0, "text": "Добар дан, људи.",
     "words": [{"start": 1.0, "end": 1.6, "word": "Добар"}]},
]


def tracks(*labels: str) -> list[CaptionTrack]:
    return [
        CaptionTrack(lang=label, url="", ext=ext, name="")
        for label in labels
        for ext in FORMATS
    ]


@pytest.mark.parametrize("label", ["sr", "sr-Latn", "sr-Cyrl", "SR", "sr_Latn", "sh", "hr", "cnr"])
def test_any_spelling_of_the_serbian_family_is_an_author_track(label):
    track = usable_manual_track(metadata(manual_tracks=tracks(label)))

    assert track is not None
    # В yt-dlp метка уходит как есть: --sub-langs ищет ровно этот ключ.
    assert track.lang == label


@pytest.mark.parametrize("label", ["en", "en-GB", "mk", "live_chat", "und"])
def test_other_languages_and_live_chat_are_never_author_tracks(label):
    assert usable_manual_track(metadata(manual_tracks=tracks(label))) is None


def test_seven_formats_are_one_track_and_the_first_serbian_label_wins():
    info = metadata(manual_tracks=[
        *tracks("en"),
        *tracks("sr-Latn"),
        *tracks("hr"),
        CaptionTrack(lang="live_chat", url="", ext="json", name=""),
    ])

    track = usable_manual_track(info)

    assert (track.lang, track.ext) == ("sr-Latn", "json3")


def test_youtube_metadata_never_refuses_a_serbian_track():
    # Случай Juske: YouTube называет речь английской, а она сербская.
    info = metadata(audio_language="en", manual_tracks=tracks("sr"))

    assert usable_manual_track(info).lang == "sr"


@pytest.mark.parametrize("pin", ["sr", "hr", "bs"])
def test_a_serbian_family_pin_keeps_the_track(pin):
    assert usable_manual_track(metadata(manual_tracks=tracks("sr")), pin).lang == "sr"


@pytest.mark.parametrize("pin", ["en", "ru", "mk"])
def test_a_pin_outside_the_serbian_family_refuses_the_track(pin):
    # Хозяин сказал, что речь не сербская: сербская дорожка у такого ролика — перевод.
    assert usable_manual_track(metadata(manual_tracks=tracks("sr")), pin) is None


@pytest.mark.parametrize("label", ["sr", "sr-Cyrl", "SR", "sr_Latn", "cnr"])
def test_cyrillic_cues_are_latinized_under_any_serbian_label(label):
    (cue,) = normalize_cues(CYRILLIC, label)

    assert cue["text"] == "Dobar dan, ljudi."
    assert cue["words"][0]["word"] == "Dobar"


@pytest.mark.parametrize("language", ["ru", "uk", "mk", "en"])
def test_cues_of_other_languages_keep_their_script(language):
    cues = [{"start": 1.0, "end": 3.0, "text": "Привет, как дела?", "words": []}]

    assert normalize_cues(cues, language) == cues


async def test_cyrillic_author_track_goes_on_in_latin(pipeline):
    pipeline.info = metadata(video_id="cyrtrack001", manual_tracks=tracks("sr-Cyrl"))
    pipeline.track = [{"start": 1.0, "end": 3.0, "text": "Добар дан, људи.", "words": []}]
    job = Job("cyrtrack001")

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.source == "manual:sr-Cyrl"
    assert [c.src for c in job.cues] == ["Dobar dan, ljudi."]
    assert (job.language.effective, job.language.track) == ("sr", "sr-Cyrl")


async def test_a_pin_outside_the_serbian_family_sends_the_video_to_asr(pipeline):
    pipeline.configure(language="en")
    pipeline.info = metadata(video_id="pinnedengl1", manual_tracks=tracks("sr"))
    job = Job("pinnedengl1")

    await pipeline.run(job)

    assert job.source == "asr"
    # Дорожку не брали вовсе, а не взяли и уронили.
    assert job.track_failed is False
    assert len(pipeline.workers) == 1
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_author_track.py -v`
Expected: FAIL — 13 failed, 19 passed. Шесть тестов закрепа — `TypeError: usable_manual_track() takes 1 positional argument but 2 were given`; метки `sr_Latn` и `cnr` — `assert None is not None` (регулярка их не знает); латиница под `sr-Cyrl`, `SR`, `sr_Latn` — `AssertionError: assert 'Добар дан, људи.' == 'Dobar dan, ljudi.'`; первый тест конвейера — кириллица в `job.cues` (задача 6 оставила в ветке дорожки `normalize_cues(segments, track.lang)`); второй — `assert 'manual:sr' == 'asr'`.

- [ ] **Step 3: `usable_manual_track` — по семье языка**

`helper/sources/ytdlp.py`:

- Удалить `BCMS_LANG` вместе с комментарием над ним (`:34-35`). `import re` остаётся: на нём `VIDEO_ID` и классификаторы отказов.
- Импорт `from helper.language import is_bcms` рядом с `from helper.env import YTDLP_CACHE` (`:21`). `helper/language.py` тянет только стандартную библиотеку, цикла нет.
- `def usable_manual_track(info: VideoInfo, pin: str | None = None) -> CaptionTrack | None` (`:321-336`). Закреп задан и `not is_bcms(pin)` — `None`, на дорожки не глядя. Иначе — первая запись `info.manual_tracks`, у которой `is_bcms(track.lang)`, или `None`. Отдельной дедупликации не нужно: семь форматов одной метки идут подряд, и вернуться может только первая запись метки — это и есть «одна дорожка на метку». Среди нескольких меток семьи (`sr-Latn`, `hr`) берётся первая по порядку метаданных; закреп внутри семьи (`hr`, `bs`) выбор не меняет. Метка возвращается как есть: по ней `fetch_manual_track` ищет ключ (`--sub-langs`, полное совпадение) и строится отпечаток `manual:<метка>`, нормализованный код туда не подставлять.
- Докстрока заменяется и говорит:
  1. годится только ручная дорожка сербского семейства в любой записи метки (`sr`, `sr-Latn`, `sr-Cyrl`, `SR`, `sh`, `cnr`, `hr`, `bs`): сверяется семья через `is_bcms`, а не строка; `live_chat` (чат повтора эфира) и метки, которых нет среди кодов Whisper, не годятся никогда;
  2. почему не другие языки: метку надо сверять с речью, а определителя без карты нет (на диске только large-v3 и turbo), зонд на карте отнял бы у дорожки автора её смысл — обойтись без карты; без сверки английская дорожка забрала бы ролик, где YouTube назвал сербскую речь английской (Juske);
  3. `pin` — закреп хозяина (`[asr] language`); вне сербского семейства он значит «речь не сербская», и сербская дорожка у такого ролика — перевод, а не оригинал. Метаданные YouTube (`audio_language`) в выборе не участвуют — именно они однажды и соврали;
  4. прежний довод остаётся: македонский, болгарский, словацкий, русский — акустические соседи сербского, и наличие дорожки не равно её пригодности;
  5. `automatic_captions` сюда не попадают по построению: `probe` кладёт в `manual_tracks` только ключ `subtitles`.

- [ ] **Step 4: `normalize_cues` — по семье языка**

`helper/script.py`:

- Удалить `BCMS` с комментарием (`:24-25`). Читателей у него вне файла нет: `grep -rn --include='*.py' -e "script import.*BCMS" -e "script\.BCMS" helper tests spikes` — пусто; внутри файла его читает только строка 76.
- Импорт `from helper.language import is_bcms` — функцией, а не модулем: параметр `language` у `normalize_cues` затенил бы имя модуля.
- `:76`: `if language not in BCMS:` → `if not is_bcms(language):`. Умолчание `language: str = "sr"` не трогать — на нём `spikes/asr_real.py:134`.
- В докстроку `normalize_cues` одно предложение: язык сверяется по семье (`is_bcms`), а не строкой — метка дорожки автора бывает `sr-Cyrl` или `SR`, и точное сравнение пропускало такую кириллицу в переводчик.

- [ ] **Step 5: Конвейер передаёт закреп и действующий язык**

`helper/jobs.py`, `JobRunner._pipeline`:

- Выбор дорожки (98a3ad2: `:813-817`): перед ним `pin, _ = self._declaration(job)`, затем `usable_manual_track(job.info, pin)`. `_declaration` здесь не бросает: `_pipeline` уже позвал `_pin()` до `probe` (задача 4), а негодное закрепление отказало бы задание там. Объявление — через `_declaration`, как у ключа кэша (задача 7): сюда же придёт объявление ссылки, см. «Отложено». К комментарию над выбором (`:804-812`) дописать: закреп вне сербского семейства дорожку не берёт, см. `usable_manual_track`.
- Ветка дорожки после удачной загрузки (98a3ad2: `:854-856`): `normalize_cues(segments, track.lang)` → `normalize_cues(segments, verdict.effective)`, где `verdict` — решение по дорожке, которое задача 6 строит прямо перед этим. Комментарий: письменность — по действующему языку решения, а не по сырой метке, иначе кириллица под `sr-Cyrl` или `SR` ушла бы в переводчик. `fetch_manual_track`, подпись стадии `source` и `job.source` по-прежнему получают сырую метку.

- [ ] **Step 6: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_author_track.py -v`
Expected: PASS, 32 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 478 passed

Run: `grep -rn --include='*.py' -e "BCMS_LANG" -e '"sh", "me", "cnr"' helper spikes`
Expected: пусто

- [ ] **Step 7: Коммит**

```bash
/usr/bin/git add helper/sources/ytdlp.py helper/script.py helper/jobs.py tests/test_author_track.py
/usr/bin/git commit -m "Дорожка автора: метка сверяется по семье языка, кириллица латинизируется при любой записи метки" -m "usable_manual_track берёт ручную дорожку сербского семейства в любой записи метки (sr-Latn, sr-Cyrl, SR, sh, cnr), live_chat — никогда; закреп вне семейства дорожку не берёт: сербская дорожка у несербской речи — перевод. normalize_cues сверяет семью через is_bcms, а ветка дорожки нормализует текст по действующему языку решения, а не по сырой метке: кириллица под sr-Cyrl или SR больше не уходит в переводчик. BCMS_LANG и script.BCMS удалены — семья языка теперь в одном месте, helper/language.py."
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 11: Версия кэша и документация

Задача закрывает работу: поднимает `PROMPT_VERSION` — версия входит в ключ и метит смену правил языка; поднятая, она
обесценивает записи прежних правил, — и записывает построенное: в `docs/running.md` для хозяина и пометками «Как
построено» в спецификации.
Поведения не меняет: кроме версии, правятся только комментарии и докстроки. `helper/asr/worker.py` задача не трогает:
его комментарии и мёртвые параметры правят задачи 5 и 8, а здесь это только проверяется (шаг 6).

**Files:**
- Modify: `helper/cache.py`: `PROMPT_VERSION` и комментарий над ним
- Modify: `helper/sources/ytdlp.py:285-290` (докстрока `probe`)
- Modify: `helper/jobs.py:201-203` (`JobCue.failed`), `:1134` (`_finish_translation`), `:1265-1266` (`_serve_cached`), `:1343-1344` (`_translate`) — только комментарии
- Modify: `docs/running.md` (заголовок в первой строке и новый раздел в конце)
- Modify: `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` (пометки после строк 290, 799, 909, 956, 965, 1178)
- Test: `tests/test_cache_version.py` (новый)

**Interfaces:**
- Consumes: фикстура `pipeline` (`run`, `workers`, `cache`, вход `pipeline.info`) и построитель `metadata` (задача 3); `Job`, `Stage` из `helper.jobs`. Для текстов — построенное задачами 4–10: константы `helper/language.py`, ключи `LanguageVerdict.wire()`, ключ кэша с закрепом и `SCHEMA_VERSION = 2` (задача 7), `helper/asr/prompt.py` (задача 8), правило склейки `split_sentences` (задача 9), `usable_manual_track` (задача 10).
- Produces: `PROMPT_VERSION = 2`; `docs/running.md` с заголовком «Демон: запуск и диагностика» и разделом «Какой язык решил демон»; пометки «Как построено» в языковых разделах спецификации.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_cache_version.py
"""Обновление обесценивает дорожки, сделанные прежними правилами языка."""

from conftest import metadata
from helper import cache
from helper.jobs import Job, Stage


async def test_a_track_made_before_the_update_is_made_again(pipeline, monkeypatch):
    pipeline.info = metadata(video_id="beforebump1")

    # Запись под версией 1 — правила языка до поднятия версии.
    with monkeypatch.context() as old_rules:
        old_rules.setattr(cache, "PROMPT_VERSION", 1)
        await pipeline.run(Job("beforebump1"))

    job = Job("beforebump1")
    await pipeline.run(job)

    assert job.stage is Stage.DONE
    # Распознано заново, а не отдано из кэша.
    assert len(pipeline.workers) == 2
    # Прежняя запись не удалена: удалять её — решение хозяина.
    assert len(pipeline.cache.variants("beforebump1")) == 2
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_cache_version.py -v`
Expected: FAIL — `assert 1 == 2` на `len(pipeline.workers)`: версия всё ещё 1, и второй прогон отдан из кэша записью под версией 1.

- [ ] **Step 3: Поднять версию**

`helper/cache.py`, `PROMPT_VERSION`: значение 2, а комментарий над ним (после задачи 7 он говорит только о `PROMPT_VERSION`) заменить целиком, чтобы он говорил, что версия покрывает всё, что делает дорожку:

```python
# Версия всего, что делает дорожку из звука или из дорожки автора, кроме самих
# моделей (они в ключе отдельно): правила выбора языка распознавания вместе с
# константами helper/language.py, затравки и фильтров распознавания, нарезки на
# реплики и промпта переводчика. Поднимать при любой их правке: версия входит в
# ключ, и прежние записи перестают находиться. С диска они при этом не уходят:
# вытеснение срабатывает, только когда кэш перерастёт бюджет.
# 2 — «Язык источника»: определитель всегда, затравка по языку, фильтры тишины,
# повторов и эха, сокращения в нарезке.
PROMPT_VERSION = 2
```

`SCHEMA_VERSION` и его комментарий (задача 7) не трогать.

- [ ] **Step 4: Запустить и убедиться, что проходит**

Run: `.venv/bin/python -m pytest tests/test_cache_version.py -v`
Expected: PASS, 1 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 479 passed

- [ ] **Step 5: Докстрока `probe`**

`helper/sources/ytdlp.py:285-290`, `probe`. Обещание «без него yt-dlp материализует ~200 tlang-вариантов» для yt-dlp 2026.08.19 неверно: экстрактор строит машинные переводы, только когда просят автосубтитры (`get_translated_subs` требует `writeautomaticsub` или `listsubtitles`, `yt_dlp/extractor/youtube/_video.py`), и кладёт их в `automatic_captions`, а не в `subtitles`. Новая докстрока:

```python
    """Метаданные ролика одним вызовом: дорожки, заголовок, описание, теги, главы.

    `skip=translated_subs` оставлен как страховка от смены версии yt-dlp, а не ради
    скорости: в 2026.08.19 машинные переводы дорожек строятся, только когда просят
    автосубтитры (--write-auto-subs или --list-subs), и ложатся в automatic_captions,
    а не в subtitles, так что ни на этот вызов, ни на fetch_manual_track флаг не
    влияет. Перевод, который автор загрузил сам, — обычная ручная дорожка, и флаг его
    не отсекает: от оригинала её отличает только язык речи (см. usable_manual_track).
    """
```

Сам флаг в `probe` и `fetch_manual_track` не трогать.

- [ ] **Step 6: Устаревшие комментарии**

Комментарии `helper/jobs.py` (строки 98a3ad2; искать по тексту, номера сдвинулись):

| Где | Что не так | Что сделать |
|---|---|---|
| `jobs.py:201-203` (`JobCue.failed`), `:1134` (`_finish_translation`), `:1265-1266` (`_serve_cached`), `:1343-1344` (`_translate`) | непереведённая реплика названа сербской, а источник теперь любой | «исходный сербский» → «исходный текст реплики, на языке ролика»; «исходным сербским» → «исходным текстом»; «с непереведённым сербским» → «с непереведённым исходным текстом»; «Показывать сербский вместо русского» → «Показывать исходный текст вместо русского». |

Затем проверить, что от прежних правил ничего не осталось:

Run: `grep -rn --include='*.py' -e "уедет по LRU" -e "язык берётся из метаданных" -e "def _source_language" -e "def _initial_prompt" -e "def _proper_nouns" -e "40% длительности" -e "VAD вернёт пусто" -e "смотрит только первые 30 секунд" -e "Лестницу оставляем" -e "вытесняет initial_prompt" -e "до приведения bs/hr/sr" -e "initial-prompt" -e "tlang-вариантов" -e "BCMS_LANG" -e '"sh", "me", "cnr"' -e "PROBE_WINDOWS" -e "_merge_bcms" -e "исходным сербским" -e "исходный сербский" -e "непереведённым сербским" -e "Показывать сербский вместо" helper`
Expected: пусто

Run: `grep -n "автодетект не используется" config.example.toml`
Expected: пусто

Из этих шаблонов сама задача снимает только `tlang-вариантов` (шаг 5) и четыре «сербских» (таблица выше). Остальные — работа других задач: «уедет по LRU» — задачи 7; «язык берётся из метаданных» и `config.example.toml` — задачи 4; `def _source_language`, «40% длительности», «VAD вернёт пусто», «смотрит только первые 30 секунд», «до приведения bs/hr/sr», `PROBE_WINDOWS`, `_merge_bcms` — задачи 5; `def _initial_prompt`, `def _proper_nouns`, «Лестницу оставляем», «вытесняет initial_prompt», `initial-prompt` — задачи 8; `BCMS_LANG` и шестикодовый `BCMS` — задач 5 и 10. Если что-то из этого нашлось — у задачи-владельца дефект: остановиться и доложить, здесь не чинить. `helper/asr/worker.py` в этой задаче не правится.

- [ ] **Step 7: Раздел в `docs/running.md`**

Заголовок первой строки `# Демон без графического сеанса` заменить на `# Демон: запуск и диагностика`: файл
теперь не только о сеансе. Разделы ниже него не трогать.

В конец файла (после раздела «Решение») добавить:

````markdown
## Какой язык решил демон

Язык речи демон решает у каждого ролика сам. Определитель Whisper слушает до
восьми окон по 30 секунд, разложенных по 10–90 % длины ролика, и окна голосуют;
окно, где речи меньше трёх секунд, не голосует. Метка языка от YouTube — только
подсказка: она решает, лишь когда не проголосовало ни одно окно, потому что
сербские каналы YouTube бывает называет английскими. Код в `[asr] language`
файла `~/.config/yt-trans/config.toml` — закреп: распознавание идёт им, а
определитель работает и при нём, так что расхождение видно. Дорожка автора
берётся только сербского семейства (sr, hr, bs в любой записи метки), и её язык —
метка дорожки: речь при этом не слушают.

Решение лежит в снимке задания, в поле `language` (`null`, пока не принято):

```
TOKEN=$(cat ~/.config/yt-trans/token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8756/v1/jobs/<id> | jq .language
```

`<id>` — идентификатор ролика из адреса (`watch?v=…`). Задания живут в памяти до
перезапуска демона, а готовых держится не больше двенадцати; ролик, который уже
лежит в кэше, смотрят через `/restore` — он ничего не запускает:

```
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8756/v1/jobs/<id>/restore | jq .snapshot.language
```

Ролик без единой реплики не кэшируется: новое задание по нему — после 12 других
готовых или перезапуска — снова качает звук и распознаёт под пропуском.

Пример — ролик, где YouTube назвал сербскую речь английской:

```json
{"effective": "sr", "declared": "en", "declaredBy": "metadata", "detected": "sr",
 "confidence": 0.97, "reason": "detector", "source": "asr", "track": null,
 "verified": null, "mismatch": true, "note": "YouTube указал en, по речи sr"}
```

| Поле | Что значит |
|---|---|
| `effective` | язык, которым распознано; у дорожки автора — язык её метки |
| `declared`, `declaredBy` | что объявлено и кем: `config` — закреп, `metadata` — YouTube; `null` — никто |
| `detected`, `confidence` | что услышал определитель и доля этого языка в голосах окон (0–1); `null` — речи не нашлось или её не слушали |
| `reason` | откуда `effective`: `pin` — закреп; `detector` — определитель; `floor` — победил язык, который не славянский сосед сербского (mk, bg, sl, ru, uk, be, cs, sk, pl), а сербская доля не ниже 0.2: так Whisper на сербской речи склонен называть английский; `hint` — речи не нашлось, взята метка YouTube; `fallback` — речи не нашлось и метки нет, принят сербский; `track` — дорожка автора |
| `source`, `track` | `asr` — распознавание, `manual` — дорожка автора; `track` — её метка |
| `verified` | пока всегда `null`: сверки дорожки автора с речью нет |
| `mismatch`, `note` | объявленное разошлось с услышанным (или с меткой дорожки) — и фраза об этом |

`mismatch: true` — повод заглянуть в ролик. Либо YouTube снова ошибся меткой, и
тогда всё верно: `effective` взят по речи. Либо ошибся определитель — тогда
средство одно: закреп в `[asr] language`. Он действует на все ролики сразу и
годится, только когда язык у всех один; объявить язык ролику или ссылке пока
нельзя. Закреп читается при старте — после правки перезапустить демон. Пока он
стоит, дорожки, сделанные без него, не находятся: каждый открытый ролик
распознаётся заново. Снятый закреп возвращает прежнюю запись вместе с прежней
ошибкой — исправить один ролик насовсем пока нельзя. В журнале у каждого
распознанного ролика есть строка с решением и голосами окон, а расхождение и
окна с речью, на которых определитель не ответил, выходят предупреждениями:

```
journalctl --user -u yt-trans --since today | grep <id>
```

### После обновления

Сразу после слияния в рабочую копию, из которой запущен демон (`~/Work/yt-trans`), —
`systemctl --user restart yt-trans`: демон держит код с запуска, а воркер
распознавания берёт с диска; прежний процесс шлёт новому воркеру `--initial-prompt`,
которого тот не знает, и каждое распознавание падает отказом «ASR не справился:
usage: …».

Обновление с этой работой (новый ключ кэша: `SCHEMA_VERSION` и `PROMPT_VERSION` 2
в `helper/cache.py`) обесценивает весь кэш дорожек: каждый уже переведённый ролик
при следующем просмотре распознаётся и переводится заново — один раз, дальше он
снова в кэше. До этого F5 на таком ролике показывает кнопку запуска, а не готовые
субтитры.

Прежние записи остаются на диске — около 18 МБ (169 записей на 2026-11-01) — и
сами не уйдут: вытеснение срабатывает, только когда кэш перерастёт 2 ГиБ.
Удалять ли их — решение хозяина. Решив удалить, сначала остановить демон: он
держит `index.sqlite` открытым, а при запуске заводит пустые индекс и каталог
заново. Удаляются только эти два пути: рядом в `~/.cache/yt-trans` лежат веса
моделей (`hf/`), их удалять нельзя. Вместе с прежними уходят и записи,
сделанные уже после обновления, — такие ролики распознаются ещё раз.

```
systemctl --user stop yt-trans
rm -r ~/.cache/yt-trans/tracks ~/.cache/yt-trans/index.sqlite
systemctl --user start yt-trans
```
````

Перед вставкой сверить с кодом каждое число и имя: окна, 3 с, 0.2 и список соседей — `helper/language.py`; ключи и
`reason` — `LanguageVerdict`; путь токена — `TOKEN_PATH` в `helper/app.py`. Где построенное разошлось с этим текстом,
писать построенное.

- [ ] **Step 8: Пометки «Как построено» в спецификации**

`docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md`, шесть вставок, каждая отдельным абзацем после
указанной строки (номера на 98a3ad2, место узнаётся по цитате). Дата в пометках — день коммита (`date +%F`). Где
построенное разошлось с текстом ниже, писать построенное.

После строки 290 («`targetLang` и `generation`; остальных трёх пока нет.»):

````markdown
**Как построено** (ветка `source-language`, ГГГГ-ММ-ДД): источник реплик и язык лежат
в объекте `language` (`source` — `asr` или `manual`, `track` — метка дорожки автора),
а не в поле `source` на верхнем уровне снимка: его расширение показало бы надписью
«качаю asr…» (`extension/content.js`, ветка `message.source`). Поля — в разделе
«Решения» ниже.
````

После строки 799 («дописывается исследованием и ляжет ниже.»):

````markdown
**Как построено** (ветка `source-language`, ГГГГ-ММ-ДД). Сохранённых ссылок в демоне
пока нет, и объявить язык человек может одним способом: кодом в `[asr] language`
файла `config.toml`, сразу для всех роликов (закреп); `auto` — не объявлять.
Определитель запускается на каждом распознаваемом ролике, и при закрепе тоже: закреп
решает токен декодера, а расхождение с услышанным уходит на провод (см. «Решения»).
Объявление на ссылку или на ролик отложено вместе со своим последствием: язык в ключ
реестра заданий не входит, а при объявлении на ссылку войти должен. Дешёвый путь для
английских роликов не открыт: дорожка автора по-прежнему берётся только сербского
семейства.
````

После строки 909 (конец пункта 10, «строку по признаку `failed`.»):

````markdown
**Как построено** (ветка `source-language`, ГГГГ-ММ-ДД). Пункт 3 закрыт: затравка
есть только у сербского семейства, имена берутся из заголовка и тегов, сегменты
отсеиваются по `no_speech_prob`, `avg_logprob` и `compression_ratio` (у «повтора» —
только зациклившиеся предложения), эхо затравки в реплики не попадает, а пустые в
пакетном режиме заградители из вызова убраны. Пункт 4 закрыт: раннего возврата на
метаданных нет, расхождение пишется в журнал предупреждением и уходит на провод.
Пункт 5 закрыт частично и иначе, чем предлагалось, — см. «Решения». Пункт 6 закрыт:
коды проверяются, `""` до Whisper не доходит. Пункт 7 закрыт наполовину: сокращения
(«U.S. market, e.g.», «5. maja») реплику больше не рвут, а `CONJUNCTIONS` не тронуты —
перенос строк до провода не доходит (`_to_job_cues` берёт только текст). Пункт 8 закрыт наполовину: метки сербского
семейства годятся в любой записи, другие языки отложены. Пункт 9 закрыт:
`targetLang` вышел из ключа кэша, в снимке остался. Пункты 1, 2 и 10 — работа
«Проверка перевода».
````

После строки 956 (конец списка «Решения», «…вытеснив собственное задание демона.»):

````markdown
**Как построено** (ветка `source-language`, ГГГГ-ММ-ДД), по решениям:

- **Определитель всегда.** Воркер слушает до восьми окон по 30 секунд, разложенных
  по 10–90 % длины (запись не длиннее окна — одно окно). Окно, где речи по VAD меньше
  трёх секунд, в модель не идёт и не голосует; окно, на котором модель упала, тоже.
  Доли bs/hr/sr складываются в одну семью, победитель — наибольшая средняя доля по
  голосовавшим окнам. Если победил не сербский язык и не славянский сосед (mk, bg,
  sl, ru, uk, be, cs, sk, pl), а сербская доля не ниже 0.2, решение — сербский
  (`reason: "floor"`): так Whisper проигрывал сербскую речь английскому
  (3XJLfTSZy6I: en 0.55 против 0.44). Не проголосовало ни одно окно — один проход по
  первым 30 секундам речи всего ролика; и там пусто — метка YouTube, а без неё
  сербский (`fallback`, `detected: null`). Метаданные решают только тогда; при
  голосах окон они лишь помечают расхождение (Juske: YouTube en, речь sr → sr,
  «YouTube указал en, по речи sr»). Закреп решает токен декодера как задан (hr и bs
  сохраняются), определитель работает и при нём. Всё — в том же запуске воркера и
  под тем же пропуском на карту: ни второй модели, ни второй загрузки. Константы — в
  `helper/language.py`, и любая их правка требует поднять `PROMPT_VERSION`.
- **На проводе** — объект `language` в снимке и событие `{"type": "language"}`; оно
  приходит раньше `segmented` и `cues` на каждом пути: распознавание, дорожка автора,
  кэш, восстановление. Поля: `effective`, `declared`, `declaredBy`, `detected`,
  `confidence` (два знака), `reason`, `source`, `track`, `verified`, `mismatch`,
  `note`. `note` — готовая фраза не длиннее 60 знаков: «YouTube указал en, по речи
  sr», «в настройках sr, по речи en», «речи почти нет, язык принят за sr». Задание,
  возобновлённое после паузы, решение сохраняет. Расширение его не показывает — оно
  заморожено; до экрана решение дойдёт со страницей для планшета, а пока видно в
  снимке и в журнале (`docs/running.md`, «Какой язык решил демон»).
- **Ключ кэша — отступление от «поиска по объявленному, записи под действующим».**
  В ключе объявление человека (закреп; `""`, если его нет), а не действующий язык:
  кэш обязан ответить до любой работы карты, а действующий язык появляется только
  внутри воркера. Где человек объявил, действующий язык с объявленным совпадает, и
  формула выполняется; где нет — решение лежит в записи и приезжает на провод при
  каждом просмотре из кэша. `targetLang` из ключа вышел (`Job.target_lang` и
  `targetLang` в снимке остались). `PROMPT_VERSION` и `SCHEMA_VERSION` подняты до 2:
  прежние записи не находятся, с диска не удалены (около 18 МБ на 2026-11-01; удалять
  ли — решает хозяин). Результат без единой реплики в кэш не пишется, а такую старую
  запись не отдают ни конвейер, ни восстановление. Восстановление перебирает записи
  от новых к старым и пересчитывает ключ по сохранённому отпечатку и нынешнему
  закрепу; версиям и моделям из файла не верит. Решение с низкой уверенностью
  кэшируется: повтор стоил бы карты на каждом просмотре, а средство от ошибки —
  пометка на проводе и закреп.
- **Коды.** Сто кодов faster-whisper 1.2.1 вписаны в `helper/language.py` и сверяются
  тестом с установленным пакетом. Замены: sh, me, cnr → sr; iw → he, jv → jw, fil →
  tl, nb → no, in → id. Неверный закреп не роняет демон: при запуске — `log.error`, а
  каждому заданию — отказ до запроса к YouTube фразой «неизвестный язык «…»: [asr]
  language в config.toml», без повтора. Метка YouTube, которой нет в списке, просто
  не подсказывает.
- **Затравка** — функция действующего языка: фраза есть только у сербского семейства,
  у остальных затравки нет вовсе. Имена — из заголовка и тегов, не из описания (эхо в
  поле шло из его шаблонных строк), латиницей; имя, оставшееся кириллицей,
  выбрасывается. Сегменты отсеиваются: «тишина» (`no_speech_prob` > 0.6 при
  `avg_logprob` < −1.0) выбрасывает сегмент целиком, а «повтор»
  (`compression_ratio` > 2.4) только помечает его: сегмент пакетного режима — кусок
  речи до 30 с, и из него уходят одни зациклившиеся предложения — со своей степенью
  сжатия выше 2.4 или повторяющие уже сказанное в этом сегменте; диалог перед петлёй
  остаётся. Каждый отсев пишется в журнал. Эхо — отступление от «совпадающая с
  затравкой»: точного совпадения нет ни в одной из 66 620 реплик кэша, поэтому эхом
  считается предложение хотя бы из двух разных слов, все слова которого есть в
  затравке, хотя бы одно не из служебных (ovo, je, video, na, se, srpskom, jeziku), и
  среди них есть необиходное слово фразы или вводки имён (transkript, snimka, pominju)
  либо два разных имени не из служебных: одно имя заголовка в обиходной фразе
  («Šta je ovo?») — речь.
- **Дорожка автора на любом языке — отложено.** Сверять метку с речью без карты нечем:
  определителя меньше large-v3 и turbo на диске нет, зонд на карте отнял бы у пути
  дорожки его смысл, а брать чужую дорожку без сверки значит вернуть случай Juske
  (метка en на сербской речи). Условия возврата — в плане, раздел «Отложено». Сделано
  меньшее: метки сербского семейства годятся в любой записи (sr-Latn, sr-Cyrl, SR, sh,
  cnr) и латинизируются, `live_chat` не годится никогда, закреп вне семейства
  дорожку не берёт. Решение по дорожке: `source: "manual"`, `reason: "track"`,
  `detected` и `verified` — `null`.
- **Не сделано здесь** (работа «Проверка перевода»): типизированная вторая строка,
  приговор проверяльщиков, языконезависимая защита от съезда реплик и видимые пометки
  «выравнивание не проверено» и «вторая строка не удостоверена». «Одна модель на все
  направления» верна без правок.
````

После строки 965 («…Это одна строка.»):

````markdown
**Как построено** (ветка `source-language`, ГГГГ-ММ-ДД): в `config.example.toml`
стоит `language = "auto"` и объяснение, что определитель работает всегда, а код
закрепляет язык каждого ролика и годится, только когда язык у всех один; sh, me и
cnr принимаются как sr.
````

После строки 1178 («— не выбор одного из двух молча, а пометка на экране.»):

````markdown
**Как построено** (ветка `source-language`, ГГГГ-ММ-ДД). Первое сделано, см. «Решения».
Зонда для дорожки автора нет: он отложен вместе с дорожками на других языках (там
же). Пока дорожки берутся только сербского семейства, и решение по ним честно
говорит, что речь не слушали: `detected` и `verified` — `null`, а расхождение
считается между объявлением и меткой дорожки («YouTube указал en, дорожка автора
sr»).
````

- [ ] **Step 9: Проверить**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 479 passed

Run: `/usr/bin/git status --short`
Expected: только `helper/cache.py`, `helper/sources/ytdlp.py`, `helper/jobs.py`, `docs/running.md`, спецификация и новый `tests/test_cache_version.py`; `helper/asr/worker.py` и `extension/` не тронуты (и сам файл плана, если в нём отмечали шаги, — в этот коммит его не добавлять).

- [ ] **Step 10: Коммит**

```bash
/usr/bin/git add helper/cache.py helper/sources/ytdlp.py helper/jobs.py docs/running.md docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md tests/test_cache_version.py
/usr/bin/git commit -m "PROMPT_VERSION 2: дорожки прежних правил языка делаются заново; как читать решение о языке" -m "Версия покрывает всё, что делает дорожку, кроме моделей: правила языка и константы helper/language.py, затравку и фильтры распознавания, нарезку, промпт переводчика. После обновления каждый ролик распознаётся и переводится заново один раз; прежние записи (около 18 МБ) остаются на диске, удалять их — решение хозяина. docs/running.md объясняет, как прочесть поле language снимка; пометки «Как построено» в спецификации записывают построенное и отступления (ключ кэша по закрепу, правило эха, дорожки на других языках отложены). Поправлены устаревшие докстроки и комментарии: skip=translated_subs и «сербский» у непереведённых реплик. docs/running.md теперь называется «Демон: запуск и диагностика»."
```

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---

### Task 12: Калибровка на карте

Ручной шаг для хозяина: нужны карта и YouTube, автотеста нет и быть не может. Делается после задачи 11 и до слияния
ветки. Константы определителя (`MIN_SPEECH_SECONDS = 3.0`, `BCMS_FLOOR = 0.2`, `SLAVIC_NEIGHBOURS`) выведены из
полевых чисел, снятых, когда окна без речи ещё голосовали; после задачи 5 они не голосуют, и доли сдвинутся. Шаг
проверяет, что на тех же роликах решение осталось верным, и ничего не трогает, если так. Заодно он прогоняет ответ
воркера через `asr_cues` демона и смотрит, что выбрасывают фильтры задачи 8: их пороги на этом корпусе не проверены, в
кэше метрик нет.

Четвёртый ролик — с русской речью: `2zcvNJdxPHE` (525 с), он есть в кэше хозяина, и в его репликах русские буквы ы,
э, ъ, ё и ни одной сербской. Запасной — `tXuNg8BxieA` (украинская речь, 1126 с, оттуда же). Пятый — `jWrDvxtofLk`: в
кэше у него на 201,8–229,6 с шесть реплик диалога, а за ними в том же куске Whisper ушёл в петлю «tak, tak…»; ролик
проверяет, что «повтор» уносит петлю, а не диалог. Шестой — сербская песня со вступлением без слов; id выбирает хозяин
и вписывает в переменную `song` в начале шага 2. Песня проверяет путь, которого нет в тестах: как Silero слышит пение,
проход по всему файлу, запасной язык и что фильтры делают со строками песни.

**Files:**
- Modify (только если приёмка не прошла): `helper/language.py` — `BCMS_FLOOR`, `SLAVIC_NEIGHBOURS` (задача 5); с ними `tests/test_language.py` — литерал в `test_detector_constants_are_pinned`, при смене `BCMS_FLOOR` первые две строки `test_floor_boundary`; `docs/running.md` — порог и список соседей (таблица `reason`, строка `floor`)
- Modify (только если константа менялась, а ветку уже развернули): `helper/cache.py` — `PROMPT_VERSION` 3 и строка истории над ним; с ним число в `docs/running.md` («После обновления»)
- Modify: `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` — запись калибровки в конце пометки «Как построено» после списка «Решения» (её добавила задача 11)
- Test: ручная проверка на карте

**Interfaces:**
- Consumes: CLI воркера из задачи 5 — `--language auto`, `--hint CODE`, `--pcm FILE` — и его ответ: `verdict` (`LanguageVerdict.wire()`), `detection` (`bcmsShare`, `votes`, `probed`, `windows`, где окно — `[семья или null, её доля в окне, секунды речи]`); константы `helper/language.py` (задача 5). Из задачи 8: флаг `--prompt-names JSON`, `harvest_names(title, tags)` из `helper/asr/prompt.py`, в ответе — `prompt` и у каждой реплики `no_speech_prob`, `avg_logprob`, `compression_ratio`; `helper.jobs.asr_cues(segments, language, prompt)` — реплики и выбросы `(причина, текст)`, ровно то, что делает с ответом `_accept_asr`. `helper.sources.ytdlp.probe` и `audio_pcm`; `helper.language.hint` (задача 4); `PROMPT_VERSION = 2` (задача 11).
- Produces: запись калибровки в спецификации; если приёмка шага 7 не прошла — исправленные константы определителя (шаг 8). Пороги фильтров здесь не меняются.

- [ ] **Step 1: Освободить карту**

```bash
systemctl --user stop yt-trans
nvidia-smi --query-gpu=memory.free --format=csv,noheader
```

Expected: свободно не меньше 6144 MiB — столько просит large-v3/float16 (`ASR_VRAM_MIB` в `helper/config.py`). Меньше — в карте ещё переводчик: `ollama ps`, затем `ollama stop qwen3.8:27b` (сам он уходит через минуту после последнего запроса); и тогда мало — память занял рабочий стол, закрыть тяжёлые окна. Демон именно остановить, а не оставить праздным: задание, начатое вкладкой посреди калибровки, встало бы ждать карту, а сторож счёл бы воркер чужой работой.

- [ ] **Step 2: Скачать звук, метку языка и имена**

Из корня рабочей копии ветки `source-language` (иначе `helper` не импортируется). В `song=` вписать id песни вместо `ID_ПЕСНИ`. Список роликов ложится в `$dir/ids`, и следующие шаги берут его оттуда — каждый блок самостоятелен и годится для новой оболочки:

```bash
dir=/var/tmp/yt-trans-calibration
song=ID_ПЕСНИ
mkdir -p "$dir"
echo Q2QcYOBk0Jo 3XJLfTSZy6I 3UbtKc9MlXg 2zcvNJdxPHE jWrDvxtofLk "$song" > "$dir/ids"
for id in $(cat "$dir/ids"); do
  .venv/bin/python -c '
import json
import sys
from pathlib import Path
from helper.asr.prompt import harvest_names
from helper.language import hint
from helper.sources.ytdlp import audio_pcm, probe
video, folder = sys.argv[1], Path(sys.argv[2])
info = probe(video)
(folder / f"{video}.hint").write_text(hint(info.audio_language) or "")
names = harvest_names(info.title, info.tags)
(folder / f"{video}.names").write_text(json.dumps(names, ensure_ascii=False))
audio_pcm(video).tofile(folder / f"{video}.f32")
print(video, "метка YouTube:", info.audio_language, "длина, с:", info.duration)
' "$id" "$dir"
done
```

Expected: шесть строк; у `Q2QcYOBk0Jo` метка скорее всего `en`, у `3XJLfTSZy6I` — `None`. В `$dir/$id.names` — тот же JSON-список имён из заголовка и тегов, что демон передал бы воркеру в `--prompt-names` (`JobRunner._run_asr`, задача 8). В `$dir/$id.hint` — метка, приведённая `language.hint`, как в `JobRunner._declaration`: `en-US` → `en`, незнакомая — пусто, и тогда шаг 3 `--hint` не передаёт; в строке вывода — сырая метка YouTube. Звук берётся тем же `audio_pcm`, что у демона: та же сортировка `-S lang,+size`, то есть оригинальная дорожка, а не автодубляж. `/var/tmp` — на диске (`/tmp` в Fedora — память), а файл — 64 КБ на секунду ролика. Отказ 403 от googlevideo бывает разовым — повторить команду для этого ролика.

- [ ] **Step 3: Прогнать воркер**

```bash
dir=/var/tmp/yt-trans-calibration
for id in $(cat "$dir/ids"); do
  hint=$(cat "$dir/$id.hint")
  names=$(cat "$dir/$id.names")
  if [ -n "$hint" ]; then
    .venv/bin/python -m helper.asr.worker --language auto --hint "$hint" --prompt-names "$names" --pcm "$dir/$id.f32" > "$dir/$id.json"
  else
    .venv/bin/python -m helper.asr.worker --language auto --prompt-names "$names" --pcm "$dir/$id.f32" > "$dir/$id.json"
  fi
  echo "$id: код выхода $?"
done
```

Expected: `код выхода 0` шесть раз, по десятку-другому секунд на ролик. `--hint` и `--prompt-names` — ровно то, что передал бы демон (`JobRunner._declaration`, `JobRunner._run_asr`), так что решение о языке и затравка те же, что увидит зритель: без `--prompt-names` воркер распознавал бы вовсе без затравки, и отсев шага 5 мерился бы не на том выводе. Не ноль — в `$dir/$id.json` лежит `{"error": …}`, причина — в выводе выше; `CUDA out of memory` значит, что шаг 1 не освободил карту.

- [ ] **Step 4: Снять поля**

```bash
dir=/var/tmp/yt-trans-calibration
for id in $(cat "$dir/ids"); do
  echo "== $id"
  jq -c '.verdict' "$dir/$id.json"
  jq -c '.detection | {bcmsShare, votes, probed}' "$dir/$id.json"
  jq -c '.detection.windows[]' "$dir/$id.json"
done
```

Expected: по каждому ролику — `verdict` целиком, доля BCMS с числом голосовавших и пробованных окон, затем окна по строке вида `["sr",0.81,24.1]` (семья, её доля в окне, секунды речи; `null` вместо семьи — окно не голосовало). У песни секунды речи в каждом окне, `votes`/`probed` и решение записываются как есть: это и есть замер. Весь вывод сохранить: он идёт в запись и в коммит.

- [ ] **Step 5: Прогнать ответ воркера через фильтры демона**

Выбрасывает демон, а не воркер: ответ несёт все сегменты с метриками, и `asr_cues` из `helper/jobs.py` (задача 8) делает с ним ровно то, что `_accept_asr`, — «тишина» уходит сегментом, «повтор» и эхо затравки — предложением:

```bash
dir=/var/tmp/yt-trans-calibration
for id in $(cat "$dir/ids"); do
  .venv/bin/python -c '
import collections
import json
import sys
from helper.jobs import asr_cues
video, path = sys.argv[1], sys.argv[2]
result = json.loads(open(path, encoding="utf-8").read())
cues, dropped = asr_cues(result["cues"], result["language"], result.get("prompt"))
print("==", video, "затравка:", result.get("prompt"))
print("сегментов", len(result["cues"]), "реплик", len(cues), "выброшено", dict(collections.Counter(reason for reason, _ in dropped)))
for reason, text in dropped:
    print(f"  {reason}: {text}")
' "$id" "$dir/$id.json"
done
```

Реплики `jWrDvxtofLk` вокруг петли:

```bash
dir=/var/tmp/yt-trans-calibration
.venv/bin/python -c '
import json
import sys
from helper.jobs import asr_cues
result = json.loads(open(sys.argv[1], encoding="utf-8").read())
cues, _ = asr_cues(result["cues"], result["language"], result.get("prompt"))
for cue in cues:
    if 195 <= cue.start <= 235:
        print(f"{cue.start:7.1f}–{cue.end:7.1f}  {cue.text}")
' "$dir/jWrDvxtofLk.json"
```

Expected: по каждому ролику — затравка (`None` у английского и русского: фраза есть только у сербского семейства), число сегментов и реплик, выбросы по причинам и текст каждого выброса — все, без обрезки; затем реплики `jWrDvxtofLk` с 195 по 235 с. Приёмка:
- выброшенное — шум, а не речь: тишина с выдуманной фразой («Hvala na gledanju.»), петли повторов, эхо затравки;
- `jWrDvxtofLk`: диалог перед петлёй (около 201,8–229,6 с) — в репликах, петля, если есть, выброшена как «повтор»; если в этот раз не зациклилось — так и записать;
- песня: ни одна строка текста не выброшена как «тишина»; «повтор» может убрать только строку, повторённую в том же куске, а её первое исполнение остаётся; записать, вышло ли у песни ноль реплик.

Если среди выброшенного есть речь — пороги не менять: записать числа и тексты в запись калибровки (шаг 9) и отдать решение хозяину. Вывод сохранить вместе с выводом шага 4.

- [ ] **Step 6: Вернуть демон**

```bash
systemctl --user start yt-trans
systemctl --user is-active yt-trans
```

Expected: `active`

- [ ] **Step 7: Сверить с приёмкой**

| Ролик | Речь | `verdict.effective` | И ещё |
|---|---|---|---|
| `Q2QcYOBk0Jo` (Juske) | сербская; YouTube называл её `en` | `sr` | `reason` `detector` (`floor`, если шаг 8 опускал `BCMS_FLOOR` ради этого ролика); если метка была `en` — `declared` `en`, `declaredBy` `metadata`, `mismatch` `true`, `note` «YouTube указал en, по речи sr» |
| `3XJLfTSZy6I` (IZA MRAKA) | сербская, метки нет | `sr` | `reason` `detector` или `floor`; `bcmsShare` не ниже `BCMS_FLOOR` (0.2, пока шаг 8 его не менял) |
| `3UbtKc9MlXg` | английская | `en` | `reason` `detector`; `bcmsShare` ниже `BCMS_FLOOR` (0.2, пока шаг 8 его не менял; в поле было 0.00 во всех восьми окнах) |
| `2zcvNJdxPHE` | русская | `ru` или код, который шаг 8 внёс в `SLAVIC_NEIGHBOURS` | `reason` `detector`, не `floor` |
| `jWrDvxtofLk` | сербская: диалог, за ним петля в том же куске | `sr` | диалог 201,8–229,6 с — в репликах, петля, если есть, выброшена как «повтор» (шаг 5) |
| песня (`song`) | сербское пение, вступление без слов | как вышло | ни одной строки текста в «тишине»; «повтор» убрал только строку, повторённую в том же куске, а её первое исполнение осталось (шаг 5) |

У первых пяти `votes` не меньше 1: речь есть везде; у песни `votes` — как вышло, это и записывается. Сошлись все строки — константы не трогать, сразу шаг 9.

- [ ] **Step 8: Если строка не сошлась**

- `3XJLfTSZy6I` дал `en` с `reason` `detector`: сербская доля ниже порога. Если его `bcmsShare` хотя бы на 0.1 выше, чем у `3UbtKc9MlXg`, опустить `BCMS_FLOOR` до середины между ними, округлив вниз до 0.05. Зазор меньше 0.1 — константы не трогать: порог эти два ролика не разделяет; записать числа и отдать решение хозяину (средство для такого канала — закреп).
- `3UbtKc9MlXg` дал `sr` с `reason` `floor`: английский набрал сербскую долю выше порога. То же правило середины, но вверх; при зазоре меньше 0.1 — так же стоп.
- `Q2QcYOBk0Jo` дал не `sr`: с `reason` `hint` — окна не проголосовали, см. пункт про `votes` ниже; с `detector` и `en` — как первый пункт.
- `2zcvNJdxPHE` дал `sr`: с `reason` `floor` — победитель не значится соседом; добавить в `SLAVIC_NEIGHBOURS` код, который в `detection.windows` стоит первым в большинстве окон. С `reason` `detector` — сербский у определителя наверху сам по себе, константы этого не лечат: записать и отдать хозяину.
- `votes` 0 или меньше половины `probed` у ролика с речью: смотреть третье число окон, секунды речи. Если у окон, где речь заведомо есть, оно меньше 3 — VAD считает не так, как ждала задача 5. Это разбор её кода, а не правка константы: остановиться.
- `jWrDvxtofLk` или песня не сошлись — константы и пороги по ним не трогать: записать числа и тексты в шаг 9 и отдать решение хозяину.

После правки константы:

1. Значение — в `helper/language.py`, у константы — строка с замером (ролик, доля, дата). Вместе с ним: литерал в `test_detector_constants_are_pinned` (`tests/test_language.py`); если `BCMS_FLOOR` стал f, первые две строки `test_floor_boundary` становятся `([("en", 1−f), ("sr", f)], "sr", True)` и `([("en", 1−f+0.01), ("sr", f−0.01)], "en", False)`, литералами с двумя знаками (для 0.15 — `([("en", 0.85), ("sr", 0.15)], "sr", True)` и `([("en", 0.86), ("sr", 0.14)], "en", False)`); порог и список соседей — в `docs/running.md` (таблица «Какой язык решил демон», строка `floor`) и в пометке спецификации после «Решений». `IZA_MRAKA`, `ENGLISH`, `RUSSIAN`, строку с `mk` и тесты воркера не трогать.
2. `.venv/bin/python -m pytest -q` — PASS, 479 passed. Тесты полевых случаев — `test_floor_turns_the_english_bias_into_serbian`, `test_serbian_speech_under_an_english_label`, `test_english_without_bcms_stays_english`, `test_slavic_neighbour_keeps_its_language_over_the_floor` — не правятся и обязаны остаться зелёными: значение, которое ломает их, противоречит полю — его не брать.
3. Шаги 1 и 3–7 заново, на всех роликах (звук, метки, имена и список роликов в `$dir/ids` уже есть, шаг 2 не нужен).
4. `PROMPT_VERSION` второй раз не поднимать: задача 11 уже подняла его до 2, а под двойкой вне тестов ничего не закэшировано — ветка ещё не развёрнута. Если ветку уже развернули, поднять до 3 (правило из комментария к константам): над `PROMPT_VERSION` в `helper/cache.py` — строка истории `# 3 — калибровка …`, число — в `docs/running.md` («После обновления») и в пометке спецификации («`PROMPT_VERSION` и `SCHEMA_VERSION` подняты до 2»); `helper/cache.py` — в `git add` шага 10.

- [ ] **Step 9: Записать калибровку**

В спецификации, в конец пометки «Как построено», которую задача 11 поставила после списка «Решения», добавить абзац
с числами из шагов 4 и 5:

````markdown
Калибровка на карте (ГГГГ-ММ-ДД, `python -m helper.asr.worker --language auto` с меткой YouTube в `--hint` и
именами в `--prompt-names`, как их передал бы демон; выбросы — `asr_cues` демона по ответу воркера):

| Ролик | Речь | Окна (голоса/всего) | Доля BCMS | Решение | `reason` | Выброшено (тишина / повтор / эхо) |
|---|---|---|---|---|---|---|
| Q2QcYOBk0Jo | сербская, метка en | … | … | sr | detector | … / … / … из … |
| 3XJLfTSZy6I | сербская, метки нет | … | … | sr | … | … / … / … из … |
| 3UbtKc9MlXg | английская | … | … | en | detector | … / … / … из … |
| 2zcvNJdxPHE | русская | … | … | ru | detector | … / … / … из … |
| jWrDvxtofLk | сербская, петля в куске | … | … | sr | … | … / … / … из …; диалог 201,8–229,6 с: … |
| … (песня) | сербское пение | … | … | … | … | … / … / … из …; реплик … |

Константы не менялись. Выброшенное фильтрами — шум: …
````

Последние строки — по факту: если константа менялась, «`BCMS_FLOOR` 0.2 → …: …» с доводом из шага 8; если фильтры
выбросили речь — так и написать, с числами и текстами из шага 5, и оставить пороги на решение хозяина.

- [ ] **Step 10: Коммит и уборка**

Коммитить после шага 6, а не посреди калибровки: хук грузит `qwen3.8:27b` в карту.

```bash
/usr/bin/git add docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md
/usr/bin/git commit -m "Калибровка определителя языка на карте: Juske, IZA MRAKA, английский и русский ролики дают sr, sr, en, ru" -m "<по строке на ролик: verdict.effective, reason, bcmsShare, votes/probed и окна из шага 4, выбросы фильтров из шага 5>"
rm -r /var/tmp/yt-trans-calibration
```

В `git add` — ровно то, что менялось. Константа менялась — первая строка становится `/usr/bin/git add docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md helper/language.py tests/test_language.py docs/running.md`, а в первой строке сообщения названа правка; поднимали ещё и `PROMPT_VERSION` до 3 — к ним `helper/cache.py`.

После слияния в `~/Work/yt-trans` — `systemctl --user restart yt-trans`: демон держит код с запуска, а воркер распознавания берёт с диска (`docs/running.md`, «После обновления»).

Каждая команда git — отдельным вызовом Bash, через `/usr/bin/git`; таймаут не меньше 360000 мс (хук гоняет обзор диффа до 300 с); `--no-verify` не использовать; сообщение по-русски, без строк `Co-Authored-By` и других подписей.

---
