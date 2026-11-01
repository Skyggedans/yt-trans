# Демон выдерживает второго клиента — план реализации

## Как сделано

Сводка на 2026-11-01, ветка `worktree-daemon-second-client`. Текст задач ниже —
исходный план, он не переписан; где построенное разошлось с ним, верно сказанное
здесь. Решения, принятые по ходу работы, записаны здесь по существу: журнал, в
котором их принимали, в репозиторий не входит.

**Архитектура.** Не «решает первый подписавшийся»: задание ключуется парой (ролик,
`use_author_track`). «Первый решает» сломал переключатель дорожки автора в
расширении: оно рассчитывает, что опрос уносит настройку на каждом витке и
сменённый признак даёт другое задание, а при «первом решающем» переключение не
делало ничего, пока задание не вытеснят, не сбросят или не перезапустят демон.
`targetLang` в ключ не входит, остаётся за первым обратившимся и виден в снимке.
Каждый журнал событий начинается со случайного 48-битного основания, а не с нуля:
курсор чужого журнала (другого варианта, сброшенного задания) не попадает в
диапазон этого и получает снимок обычной веткой `Job.poll`. 48 бит — чтобы курсор
оставался точным целым в JavaScript (до 2^53): расширение носит его числом JSON.
Цена ключа по паре: два клиента с разным признаком — два конвейера на ролик, а
переключение посреди идущего задания оставляет прежний вариант держать карту до
конца, и выбранный ждёт.

- **Задача 2.** Клиенты с разными признаками получают два задания, а не одно:
  вместо `second is first` тесты проверяют, что у каждого варианта своё задание,
  запущенное ровно один раз, и что сто расходящихся опросов дают ровно два
  задания. Курсор впереди журнала получает снимок с `ahead["cursor"] ==
  job.journal_end`, а не 0: `journal_end` — конец журнала с учётом основания.
  Ветка повторяемого отказа снимает только своё задание (`_remove`), а не весь
  ролик через `drop`: соседний вариант может идти у другого клиента. Пересозданное
  после отказа получает `generation` на единицу больше прежнего; после `/drop`
  счёт снова с 0. Где ролик назван без признака: `get` и снимок
  `GET /v1/jobs/{id}` берут самый недавно тронутый вариант, позиция просмотра и
  `unwatch` идут на все варианты, сброс без ключа отменяет все.
- **Задача 3.** `authorTrack` в теле `POST /v1/jobs` и в `watch` обязан быть
  настоящим boolean: нет его — `true`, всё остальное (`null`, `"false"`, 0, 1) —
  отказ без задания: 400 у `POST`, у `watch` событие `state` со стадией `error` и
  `retryable: false`. Иначе не-boolean лёг бы в ключ призрачным третьим вариантом.
  Сокет следит не больше чем за одним вариантом ролика: `watch` с другим
  признаком переносит его очередь на тот вариант.
- **Задача 4.** Вместо «пустой набор → 0.0»: когда записи всех клиентов
  просрочены, `aim()` отдаёт самую свежую позицию среди них, иначе замороженная
  фоновая вкладка единственного зрителя дёрнула бы окно перевода к началу ролика;
  0.0 — только когда записей нет вовсе. `playhead` — взгляд без хода: что `aim()`
  отдаст следующим, без поворота круга (в плане он был самим `aim()`). Живость
  решает одно место, `Job.live_slots()`, и запись ровно `PLAYHEAD_TTL` секунд от
  роду ещё жива.
- **Задача 5.** `/drop` принимает запрос вовсе без тела — так шлёт расширение.
  Негодный `clientId` (пустой, не строка, длиннее 64 знаков) — это клиент
  `"legacy"`: снимает только его запись и чужую работу не отменяет. `clientId: null`
  или его отсутствие — отмена всех вариантов, как раньше. Регистрация — живая
  запись позиции: подписавшийся, но не сообщивший позицию клиент от чужого сброса
  не защищён. Тело, которое не JSON-объект (список, текст, форма), — 422.
- **Задача 6.** Предел — 16, а не 4: это защита от злоупотребления, а не глубина
  очереди, и пятая вкладка обычного вечера отказа получать не должна. Отказ —
  `CapacityError`, а не `PermanentJobError`: переполнение проходит само, а
  `PermanentJobError` бегун помечает неповторяемым. Отказывают все три пути, что
  создают задание, а не два: `POST /v1/jobs` — 429 с фразой; `watch` — событие
  `state` со стадией `error` и `retryable: true`, без подписки;
  `/events?create=true`, единственный путь, которым задания заводит расширение, —
  200 со снимком в стадии `error` и с курсором клиента, как прислан (на ответ не
  200 расширение пишет «демон ответил …» и повторяет, а снимок с `error`
  показывает фразой и опрос останавливает). Брошенная пауза — `PAUSED`, задачи
  нет, никто не обращался дольше `PAUSED_AUDIO_TTL` — в счёт не идёт; признак один
  на счёт мест и на уборку её звука (`Job.is_stale_pause`). Упавшее задание с
  остывшим отказом при пределе отдаётся как есть, а не пересоздаётся: снять его и
  отказать значило бы потерять номер поколения и подписчиков. Бюджет звука убирает
  файлы по старшинству и, когда файл числится за заданием, — через его
  `discard_audio()`. Тестовые идентификаторы — 11 знаков (`f"video{i:05d}a"`), как
  у настоящих. При пределе отказ получает и ролик, чья дорожка уже в кэше. Уборка
  звука ошибкой диска запрос не роняет (добавлено финальной правкой).
- **Задача 7.** Только документ, `docs/running.md`. Lingering с привязкой звука и
  ай-трекера к сеансу через вставки (`ConditionEnvironment`, `PartOf`) пробовали и
  откатили: юнит, пропущенный по условию, свои `Wants=`/`Requires=` всё равно
  тянет. Сейчас `Linger=no`, `packaging/yt-trans.service` не менялся; как
  запускать демон без сеанса, решает хозяин — варианты в `docs/running.md`.

Что отложено — в разделе «Отложено в следующий план».

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** сделать так, чтобы появление второго клиента на одном ролике не пересоздавало задание, не отменяло чужую работу и не загоняло перевод в гонку за плейхедом.

**Architecture:** правки внутри `helper/jobs.py` и `helper/app.py`. Тождество задания перестаёт зависеть от настроек клиента (первый подписавшийся решает, расхождение видно в снимке). Плейхед и подписка становятся величинами на клиента, ключ — необязательный `clientId`; клиент, который его не присылает, ведёт себя ровно как сегодня, поэтому расширение не трогается. Плюс пределы на незавершённые задания и на отложенный звук.

**Tech Stack:** Python 3.14, FastAPI, pytest + pytest-asyncio, httpx ASGITransport.

**Spec:** `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` (разделы «Что надо починить до начала» и «Две дырки, которые замысел пропустил»).

## Global Constraints

- Изменения протокола **только добавляющие**. Новых значений `Stage` не вводить: расширение сверяется с известным списком стадий, незнакомая оставит его с устаревшей надписью и опросом, который никогда не остановится.
- `extension/` не меняется ни строкой.
- Клиент без `clientId` сохраняет сегодняшнее поведение во всех путях.
- Тесты не касаются сети, карты и реальных путей кэша. `YT_TRANS_CACHE` и `YT_TRANS_CONFIG` переопределяются **до** первого импорта `helper.*`: `JobManager.__init__` выполняет `shutil.rmtree(PAUSED_AUDIO_DIR)`, то есть прогон без изоляции удалит отложенный звук пользователя.
- Русский язык в комментариях и сообщениях об отказах, как во всём репозитории.

## Отложено в следующий план

Из списка предусловий замысла здесь намеренно нет двух пунктов: ограничения
частоты создания заданий по обратившемуся и `proxy_headers=False` у uvicorn.
Оба осмысленны только когда порт доступен из сети, и оба опираются на адрес
обратившегося, которого при привязке к петле не существует. Они входят в план
про вывод демона в сеть.

Отложено по ходу работы (2026-11-01):

- **Граница готовности на проводе** (до какой секунды перевод доведён) и вид «ещё
  не готово» — в план страницы для планшета.
- **Поле «сейчас переводим для другого устройства»** — туда же: в демоне его пока
  нет.
- **Регистрация на вариант, а не на ролик.** Позиция приходит без признака дорожки
  и ложится на все варианты, так что сброс с ключом не отменит вариант, который
  никто не смотрит, пока смотрят соседний. Работа лишняя, но не потерянная.
- **Прежний вариант при переключении — в паузу.** Сейчас он держит карту до конца,
  а выбранный ждёт. При создании варианта идущий соседний стоит переводить в
  `PAUSED`, откуда он продолжится при следующем обращении; отменять его нельзя —
  это и давало шторм.
- **Готовые ролики мимо предела.** При пределе отказ получает и ролик, чья дорожка
  в кэше: дешёвой проверки при создании нет, ключу нужны длительность и отпечаток
  дорожки. План предзагрузки должен пускать готовое мимо предела и не тратить
  места зрителя.
- **Осиротевший yt-dlp при отмене.** `cancel` не останавливает загрузку, уже идущую
  в `asyncio.to_thread`. Шторм, который это умножал, ушёл, но дыра осталась.
- **Запуск без сеанса.** Lingering не включён: сначала юниты звука и ай-трекера
  надо привязать к сеансу в их источнике (`system-config`, `tobii`). Решает
  хозяин, варианты в `docs/running.md`.

## Review Focus

- Два клиента с разным `targetLang` на одном ролике: второй молча получает язык первого, и в снимке об этом нечего прочитать. Тест в задаче 2.
- Клиент исчез без `/drop` (вкладку убили): его плейхед остаётся в наборе навсегда и продолжает рулить переводом. Тест в задаче 4.
- `clientId` отсутствует, пустой, не строка или абсурдно длинный — он идёт ключом в словарь. Тест в задаче 4.
- Курсор опроса впереди журнала: клиент обязан получить один снимок и дальше идти нормально, а не крутить плотный цикл. Тест в задаче 2.
- `/drop` по ролику, у которого задания нет. Тест в задаче 5.

---

### Task 1: Тестовая оснастка

**Files:**
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/conftest.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: ничего.
- Produces: фикстура `manager` → `helper.jobs.JobManager` на временных путях; фикстура `client` → `httpx.AsyncClient` поверх `helper.app.app` с действующим токеном.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_harness.py
from pathlib import Path

def test_cache_paths_are_isolated():
    from helper import env

    assert Path.home() not in env.CACHE_DIR.parents
    assert Path.home() not in env.CONFIG_DIR.parents


def test_manager_fixture_builds(manager):
    assert manager.stats()["jobs"] == 0
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: FAIL — `pytest: command not found` либо `fixture 'manager' not found`

- [ ] **Step 3: Завести оснастку**

`requirements-dev.txt`: `pytest`, `pytest-asyncio`. `pytest.ini`: секция `[pytest]` с `asyncio_mode = auto` и `testpaths = tests`.

`tests/conftest.py` задаёт `os.environ["YT_TRANS_CACHE"]` и `["YT_TRANS_CONFIG"]` через `tempfile.mkdtemp()` **на уровне модуля, до любого импорта `helper.*`** — фикстура `tmp_path_factory` для этого не годится, она доступна только внутри фикстур, а conftest импортируется раньше тестовых модулей. Затем фикстуры:

- `manager` — `JobManager(config=...)`, где конфигурация собрана в памяти через `helper.config.Config` без чтения диска;
- `client` — `httpx.AsyncClient(transport=ASGITransport(app=helper.app.app), base_url="http://test")`, заголовок `Authorization: Bearer <helper.app.TOKEN>`.

- [ ] **Step 4: Запустить и убедиться, что проходит**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Коммит**

```bash
git add requirements-dev.txt pytest.ini tests/
git commit -m "Оснастка для тестов на временных путях, чтобы прогон не трогал кэш"
```

---

### Task 2: Признак дорожки автора не пересоздаёт живое задание

**Files:**
- Modify: `helper/jobs.py:1245-1261` (`JobManager.get_or_create`), `helper/jobs.py:306-320` (`Job.snapshot`)
- Test: `tests/test_job_identity.py`

**Interfaces:**
- Consumes: фикстура `manager` из задачи 1.
- Produces: `Job.snapshot()` дополнительно отдаёт `"authorTrack": bool`, `"targetLang": str`, `"generation": int`. `Job.generation` — счётчик, растущий при каждом пересоздании задания по тому же ролику.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_job_identity.py
def test_second_client_with_other_author_track_flag_reuses_job(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    assert second is first
    assert first.use_author_track is True


def test_hundred_disagreeing_polls_create_one_job(manager):
    for i in range(100):
        manager.get_or_create("abcdefghijk", "ru", i % 2 == 0)

    assert manager.stats()["jobs"] == 1
    assert manager.get("abcdefghijk").generation == 0


def test_snapshot_reports_the_settings_the_track_was_made_with(manager):
    job = manager.get_or_create("abcdefghijk", "sr", False)
    snapshot = job.snapshot()

    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0


def test_second_client_with_other_target_lang_reuses_job_and_can_see_it(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "sr", True)

    assert second is first
    assert second.snapshot()["targetLang"] == "ru"


async def test_cursor_ahead_of_the_journal_resyncs_once(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    ahead = await job.poll(cursor=9999, timeout=0.01)

    assert "snapshot" in ahead
    assert ahead["cursor"] == 0

    again = await job.poll(cursor=ahead["cursor"], timeout=0.01)

    assert "snapshot" not in again
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_job_identity.py -v`
Expected: FAIL — первый тест на `second is first`, остальные на отсутствующих ключах снимка

- [ ] **Step 3: Убрать ветку пересоздания и дополнить снимок**

В `get_or_create` удалить ветку `if job is not None and job.use_author_track != use_author_track` целиком вместе с `drop`/`replaced`. Ветку повторяемого отказа (`Stage.ERROR` + `RETRY_COOLDOWN`) сохранить без изменений — она про другое.

Добавить `self.generation = 0` в `Job.__init__` и увеличивать его в `get_or_create`, когда задание пересоздаётся по отказу. В `Job.snapshot()` добавить три ключа.

Заменить комментарий у снятой ветки на объяснение правила: решает первый подписавшийся, расхождение настроек видно клиенту по `authorTrack`/`targetLang` в снимке, а повторный прогон с другой настройкой — это явный `/drop` и новая подписка, а не молчаливое пересоздание под чужим опросом.

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_job_identity.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Коммит**

```bash
git add helper/jobs.py tests/test_job_identity.py
git commit -m "Решает первый подписавшийся: расхождение настроек больше не пересоздаёт задание"
```

---

### Task 3: Оба создающих пути передают настоящий признак

**Files:**
- Modify: `helper/app.py:140` (`create_job`), `helper/app.py:290` (операция `watch` в `websocket`)
- Test: `tests/test_create_paths.py`

**Interfaces:**
- Consumes: фикстура `client` из задачи 1; правило «первый решает» из задачи 2.
- Produces: `POST /v1/jobs` принимает `authorTrack: bool` (умолчание `true`); операция `watch` принимает `authorTrack` в том же смысле.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_create_paths.py
async def test_post_jobs_honours_author_track_false(client, manager):
    await client.post("/v1/jobs", json={"videoId": "abcdefghijk", "authorTrack": False})

    assert manager.get("abcdefghijk").use_author_track is False


async def test_post_jobs_defaults_author_track_to_true(client, manager):
    await client.post("/v1/jobs", json={"videoId": "abcdefghijk"})

    assert manager.get("abcdefghijk").use_author_track is True
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `.venv/bin/python -m pytest tests/test_create_paths.py -v`
Expected: FAIL — первый тест: `use_author_track` равен `True`, потому что значение зашито умолчанием

- [ ] **Step 3: Прокинуть признак**

В `create_job` читать `(payload or {}).get("authorTrack", True)` и передавать третьим аргументом. В операции `watch` читать `message.get("authorTrack", True)` и передавать так же. Нигде не менять умолчание: `true` сохраняет сегодняшнее поведение.

- [ ] **Step 4: Запустить и убедиться, что проходит**

Run: `.venv/bin/python -m pytest tests/test_create_paths.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Коммит**

```bash
git add helper/app.py tests/test_create_paths.py
git commit -m "POST /v1/jobs и watch передают признак дорожки автора, а не умолчание"
```

---

### Task 4: Плейхед на клиента, с очередью по кругу

**Files:**
- Modify: `helper/jobs.py:193` (`Job.__init__`), `helper/jobs.py:1151` (`_next_batch`), `helper/app.py:221-230` (`set_playhead`), `helper/app.py:296-302` (операция `playhead`)
- Test: `tests/test_playhead.py`

**Interfaces:**
- Consumes: фикстура `manager`.
- Produces: `Job.report_playhead(client_id: str | None, t: float) -> None`; `Job.aim() -> float` — позиция, вокруг которой берётся следующий батч, по кругу между различными плейхедами; `Job.forget_client(client_id: str | None) -> None`. Атрибут `Job.playhead` сохраняется как свойство только для чтения и равен `aim()`.
- `POST /v1/jobs/{id}/playhead` и операция `playhead` принимают необязательный `clientId: str`. Отсутствие ключа означает единственного клиента `"legacy"` — поведение расширения не меняется.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_playhead.py
import time

from helper.jobs import PLAYHEAD_TTL


def test_two_clients_take_turns(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("a", 120.0)
    job.report_playhead("b", 1200.0)

    aims = [job.aim() for _ in range(4)]

    assert aims == [120.0, 1200.0, 120.0, 1200.0]


def test_one_client_always_gets_its_own_position(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("a", 42.0)

    assert [job.aim() for _ in range(3)] == [42.0, 42.0, 42.0]


def test_client_without_id_keeps_todays_behaviour(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead(None, 7.0)

    assert job.aim() == 7.0
    assert job.playhead == 7.0


def test_silent_client_stops_steering_after_the_ttl(manager, monkeypatch):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("gone", 1200.0)
    job.report_playhead("here", 60.0)

    later = time.monotonic() + PLAYHEAD_TTL + 1
    monkeypatch.setattr(time, "monotonic", lambda: later)
    job.report_playhead("here", 61.0)

    assert [job.aim() for _ in range(2)] == [61.0, 61.0]


def test_absurd_client_id_is_refused_not_stored(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("x" * 500, 5.0)
    job.report_playhead("", 6.0)

    assert job.aim() == 6.0
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_playhead.py -v`
Expected: FAIL — `AttributeError: 'Job' object has no attribute 'report_playhead'`

- [ ] **Step 3: Реализовать**

В `Job.__init__` заменить `self.playhead = 0.0` на `self._playheads: dict[str, tuple[float, float]]` (позиция и монотонное время) и `self._turn = 0`. Константа `PLAYHEAD_TTL = 120.0` рядом с `PAUSED_AUDIO_TTL`.

`report_playhead` нормализует ключ: пустой, не строка или длиннее 64 символов — считается `"legacy"`; хранит `(t, time.monotonic())`. `aim()` отбрасывает записи старше `PLAYHEAD_TTL`, берёт различные позиции в порядке ключей и выдаёт следующую по кругу через `self._turn`, увеличивая счётчик; пустой набор даёт `0.0`. `playhead` — свойство, возвращающее `aim()`.

Правило «по кругу» выбрано вместо «самый отстающий»: при двух зрителях на второй и двадцатой минуте минимум заставил бы второго ждать весь ролик, а перевод и так обгоняет зрителя примерно в двадцать раз, так что половина скорости остаётся десятикратным запасом.

В `_next_batch` заменить `job.playhead` на `job.aim()` — один вызов на батч, иначе счётчик круга провернётся лишний раз.

В `set_playhead` и в операции `playhead` читать `clientId` и звать `report_playhead`.

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_playhead.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Коммит**

```bash
git add helper/jobs.py helper/app.py tests/test_playhead.py
git commit -m "Плейхед на клиента: окно перевода идёт по кругу, а не достаётся последнему писавшему"
```

---

### Task 5: `/drop` отменяет только своё

**Files:**
- Modify: `helper/app.py:233-237` (`drop_job`), `helper/jobs.py:1464-1468` (`JobManager.drop`)
- Test: `tests/test_drop.py`

**Interfaces:**
- Consumes: `Job.forget_client` из задачи 4.
- Produces: `JobManager.drop(video_id: str, client_id: str | None = None) -> bool` — возвращает, было ли задание отменено. `POST /v1/jobs/{id}/drop` принимает необязательный `clientId` и отвечает `{"dropped": <video_id>, "cancelled": bool}`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_drop.py
def test_drop_by_one_of_two_clients_keeps_the_job(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    assert manager.drop("abcdefghijk", "desktop") is False
    assert manager.get("abcdefghijk") is job


def test_drop_by_the_last_client_cancels(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("tablet", 10.0)

    assert manager.drop("abcdefghijk", "tablet") is True
    assert manager.get("abcdefghijk") is None


def test_drop_without_client_id_cancels_as_today(manager):
    manager.get_or_create("abcdefghijk", "ru", True)

    assert manager.drop("abcdefghijk") is True
    assert manager.get("abcdefghijk") is None


def test_drop_of_an_unknown_video_is_quiet(manager):
    assert manager.drop("zzzzzzzzzzz", "tablet") is False


async def test_drop_endpoint_reports_whether_it_cancelled(client, manager):
    manager.get_or_create("abcdefghijk", "ru", True)
    response = await client.post("/v1/jobs/abcdefghijk/drop", json={})

    assert response.json() == {"dropped": "abcdefghijk", "cancelled": True}
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: FAIL — `drop() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: Реализовать**

`JobManager.drop` с `client_id`: при переданном ключе снять его регистрацию через `job.forget_client`; отменять и вынимать задание из реестра только когда живых регистраций не осталось. Без ключа — отменять, как сегодня.

Это намеренно оставляет расширению возможность отменить задание планшета, если расширение подписалось без ключа: добавляющее изменение не может отнять у старого клиента то, что он уже умеет. Записать это комментарием, чтобы правило не выглядело недоделкой.

`drop_job` читает `clientId` из тела и отдаёт `cancelled`.

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Коммит**

```bash
git add helper/app.py helper/jobs.py tests/test_drop.py
git commit -m "Отмена задания снимает регистрацию клиента, а не чужую работу"
```

---

### Task 6: Пределы на незавершённые задания и на отложенный звук

**Files:**
- Modify: `helper/jobs.py:73` (константы), `helper/jobs.py:1432-1460` (`_evict`), `helper/jobs.py:1245` (`get_or_create`)
- Test: `tests/test_limits.py`

**Interfaces:**
- Consumes: фикстура `manager`.
- Produces: `MAX_ACTIVE_JOBS = 4`, `PAUSED_AUDIO_MAX_BYTES = 4 * 1024**3`. `get_or_create` поднимает `PermanentJobError` при переполнении; `create_job` и операция `watch` превращают его в отказ с человеческой фразой.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_limits.py
import pytest
from helper.jobs import MAX_ACTIVE_JOBS, PermanentJobError


def test_active_jobs_are_capped(manager):
    for i in range(MAX_ACTIVE_JOBS):
        manager.get_or_create(f"video{i:06d}a", "ru", True)

    with pytest.raises(PermanentJobError):
        manager.get_or_create("overflowxyz", "ru", True)


def test_the_cap_counts_only_unfinished_jobs(manager):
    from helper.jobs import Stage

    for i in range(MAX_ACTIVE_JOBS):
        job = manager.get_or_create(f"video{i:06d}a", "ru", True)
        job.stage = Stage.DONE

    assert manager.get_or_create("overflowxyz", "ru", True) is not None


def test_paused_audio_over_budget_is_discarded_oldest_first(manager):
    import os

    from helper.jobs import PAUSED_AUDIO_DIR, PAUSED_AUDIO_MAX_BYTES

    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    half = PAUSED_AUDIO_MAX_BYTES // 2 + 1
    made = []

    for i, name in enumerate(("old.f32", "middle.f32", "fresh.f32")):
        path = PAUSED_AUDIO_DIR / name
        with path.open("wb") as handle:        # разреженный файл: места не занимает
            handle.truncate(half)
        os.utime(path, (1000 + i, 1000 + i))
        made.append(path)

    manager._evict()

    assert not made[0].exists()
    assert made[2].exists()
    assert sum(f.stat().st_size for f in PAUSED_AUDIO_DIR.iterdir()) <= PAUSED_AUDIO_MAX_BYTES
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: FAIL — `DID NOT RAISE PermanentJobError`

- [ ] **Step 3: Реализовать**

`MAX_ACTIVE_JOBS = 4` рядом с `MAX_RETAINED_JOBS`: четыре — это столько одновременных заданий, сколько имеет смысл при одном пропуске на карту, остальные всё равно стояли бы в очереди. Проверка в `get_or_create` перед созданием, по числу заданий с `finished is False`; отказ — `PermanentJobError` с фразой о том, сколько роликов уже в работе.

`PAUSED_AUDIO_MAX_BYTES = 4 * 1024**3`: в `_evict`, после нынешней чистки по времени, сложить размеры файлов в `PAUSED_AUDIO_DIR` и удалять самые старые, пока сумма выше предела. Трёхчасовой ролик даёт около 659 МиБ float32, так что четыре гигабайта — это примерно шесть заданий в паузе.

В `create_job` и в операции `watch` обернуть вызов и отдать `HTTPException(status_code=429, detail=...)` соответственно отказ в событиях.

- [ ] **Step 4: Запустить и убедиться, что проходят**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Коммит**

```bash
git add helper/jobs.py helper/app.py tests/test_limits.py
git commit -m "Предел на незавершённые задания и бюджет на отложенный звук"
```

---

### Task 7: Демон живёт без графического сеанса

**Files:**
- Create: `docs/running.md`
- Modify: `packaging/yt-trans.service`
- Test: проверка командой, автотеста нет — это состояние системы, а не код.

**Interfaces:**
- Consumes: ничего.
- Produces: документированная предпосылка запуска.

- [ ] **Step 1: Проверить нынешнее состояние**

Run: `loginctl show-user "$USER" | grep Linger`
Expected: `Linger=no` — юнит существует только внутри графического сеанса, то есть при выходе хозяина демон пропадает

- [ ] **Step 2: Включить**

Run: `loginctl enable-linger "$USER"`

- [ ] **Step 3: Убедиться**

Run: `loginctl show-user "$USER" | grep Linger`
Expected: `Linger=yes`

- [ ] **Step 4: Записать в `docs/running.md`**

Почему это нужно: планшет обращается к демону из другой комнаты, и компьютер в это время может стоять на экране входа. Без `linger` пользовательский юнит не поднимется, а отказ будет выглядеть как «нет связи с демоном» — то же, что даёт обрыв на петле, так что перепутать легко. Там же проверить, что в `packaging/yt-trans.service` нет зависимости от графического сеанса (`WantedBy=default.target`).

- [ ] **Step 5: Коммит**

```bash
git add docs/running.md packaging/yt-trans.service
git commit -m "Демон поднимается без графического сеанса: linger и заметка о запуске"
```
