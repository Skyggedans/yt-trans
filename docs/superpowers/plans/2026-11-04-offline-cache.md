# Офлайн-кеш роликов на планшете: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Планшет хранит у себя копию ролика и титров, которую хозяин включил одним переключателем на сцене, и играет её с устройства — в том числе когда компьютер с демоном выключен.

**Architecture:** Новый `page/cache.js` — IndexedDB (`videos`, `meta`) и потоковое скачивание mp4 со счётчиком байт; новый `page/sw.js` — сервис-воркер, который держит оболочку страницы, пока демона нет. `page/app.js` получает переключатель «кеш», копирование с ожиданием готового mp4, воспроизведение из копии через `URL.createObjectURL` и офлайн-главную, собранную из `meta.links` и сохранённых роликов. Чистые решения (байты, проценты, приговор по месту, подписи, слияние реплик, выбор источника) живут в `page/logic.js` и проверяются `node --test`.

**Tech Stack:** ванильные ES-модули без сборки, IndexedDB и Cache Storage в браузере, node 24 (`node --test`) для чистой логики, pytest для проводки и отдачи статики.

**Spec:** `docs/superpowers/specs/2026-11-04-offline-cache-design.md`

## Global Constraints

- Кеш — про текущий ролик и только по нажатию; по умолчанию выключен. Без нажатия не сохраняется ничего, включая титры.
- Включённый «кеш» не запоминается: после перезапуска страницы его надо нажать снова — иначе 400 МБ уходили бы без свежего спроса.
- Копирование начинается, как только готов mp4, не дожидаясь конца перевода; титры дописываются по мере прихода. Повторное копирование ролика заменяет прежнюю копию (ключ записи — `videoId`).
- Ничего не удаляется автоматически. Не влезает — отказ с цифрой: `нужно ещё X, свободно Y`.
- На устройство попадает только целый mp4: недокачанное отбрасывается, в базе не остаётся ничего.
- Полная копия — предпочтительный источник воспроизведения всегда, и онлайн тоже.
- Запас свободного места перед копированием — **200 МиБ** (`SPARE_BYTES`); срок ожидания списка ссылок у демона — **3000 мс** (`LINKS_TIMEOUT_MS`).
- Страница без сборки: только `<script type="module">`, никаких пакетов и CDN.
- Все видимые человеку строки — по-русски, коротко, в духе существующих.
- `/v1/*` требует токен, `page/*` публично; сервис-воркер лежит в `page/` и отдаётся тем же `StaticFiles`.
- Чистая логика — только в `page/logic.js`; `page/app.js` — DOM и сеть; проверка проводки регулярками в `tests/test_page_wiring.py`.
- Тесты: `.venv/bin/python -m pytest` (на старте **582** зелёных) и `node --test page/logic.test.mjs`.
- Коммиты по-русски, одной строкой, без трейлера `Co-Authored-By` (правило хозяина). Предкоммитный обзор идёт до 300 с: запускать с таймаутом ≥ 360 с, `--no-verify` не использовать.

## Review Focus

Проверено, что каждая строка ниже получает тест в задаче, которая владеет кодом.

1. **Демон жив, но не отвечает** (компьютер спит): запрос висит, а не падает. Домашний список должен появиться из базы примерно за три секунды: `AbortSignal.timeout(LINKS_TIMEOUT_MS)` в `loadLinks` и запасной путь из `meta`. Тест: задача 5.
2. **`navigator.storage.estimate()` недоступен или врёт нулём.** Приговор `unknown` не запрещает копирование: файл качается, а нехватка места выходит ошибкой записи, а не молчанием. Тесты: задача 1 (`copyVerdict` → `unknown`), задача 2 (отказ демона не становится копией).
3. **Копирование отменили посреди загрузки.** Скачивание рвётся, ничего не пишется в базу, кнопка возвращается в «кеш». Тест: задача 2 (`AbortError` в `node --test`); путь отмены в странице — задача 3 (`cancelCopy`).
4. **Браузер вытеснил копию между сеансами.** Метаданные есть, а Blob'а нет: строка не должна врать, ролик играет с демона, копия вычищается. Тест: задача 4 (`savedVideos.delete` в `playLocal`).
5. **Шаринг из YouTube при мёртвом демоне не стирает список с пометками.** Сегодня `consumeShare` при ошибке рисует пустой список; при офлайне это выбросило бы всё сохранённое с экрана. Тест: задача 5.

## Решения плана, которых нет в спеке

Спека оставила строителю имена, раскладку и мелочи; здесь выбраны сознательно:

- **`meta` хранит только `links`.** Спека называла ещё ключ `episodes` (`linkId → {title, videos}`), но сохранённая запись ролика сама несёт `linkId` и `linkTitle`, поэтому офлайн-списки эпизодов собираются из `videos` — второй копии тех же данных не нужно.
- **Прогресс виден на кнопке, а не в статусе сцены.** Кнопка уже показывает «кеш 37%»; вторая строка с тем же числом была бы шумом. Статус говорит только итог: «сохранено на устройстве» или причину отказа.
- **Одна копия за раз.** Нажатие «кеш» на другом ролике отменяет начатую: два mp4 по 400 МБ разом — не то, чего просили.
- **Пока mp4 ещё склеивается, кнопка показывает «кеш…».** Нажатие не отклоняется и не копирует недоделанное: `startCopy` ждёт состояния `ready` тем же опросом, что и сцена.
- **Офлайн — признак страницы, а не сети.** Флаг `offline` ставят неудачи запросов к демону и закрытие сокета; снимает его любой успешный ответ. `navigator.onLine` не спрашивается вовсе.

## Files

**Создаются:**
- `page/cache.js` — IndexedDB и скачивание копии.
- `page/cache.test.mjs` — тесты скачивания для `node --test`.
- `page/sw.js` — сервис-воркер оболочки.

**Меняются:**
- `page/logic.js`, `page/logic.test.mjs` — байты, проценты, приговоры и подписи, слияние реплик, выбор источника.
- `page/index.html`, `page/style.css` — полоса сцены с «кеш» и ⛶, пометки в списке.
- `page/app.js` — копирование, воспроизведение из копии, офлайн-главная, регистрация воркера.
- `tests/test_page_logic.py` — мост ко второму набору node-тестов.
- `tests/test_page_wiring.py`, `tests/test_page_serving.py`.
- `docs/tablet.md`.

**Не входит в план** (названо, чтобы не достраивали): фоновая докачка после закрытия страницы, докачка частичного mp4, синхронизация кешей между устройствами, автоудаление и лимиты, перевод офлайн, удаление копии со сцены, общий размер кеша на главной, обновление оболочки по кнопке.

---

### Task 1: Чистые числа и подписи копии

**Files:**
- Modify: `page/logic.js` (в конец, после `readFontSize`)
- Test: `page/logic.test.mjs` (в конец)

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `formatBytes(bytes: unknown) -> string` — `"400 МБ"`, `"1.2 ГБ"`, `"—"` для мусора;
  - `percent(seen: number, total: number) -> number` — 0…100;
  - `SPARE_BYTES: number` — 200 МиБ;
  - `copyVerdict(need: number, free: number, reserve = SPARE_BYTES) -> "ok" | "tight" | "unknown"`;
  - `copyState({ waiting?, active?, percent?, size?, failed? }) -> string` — текст кнопки;
  - `cacheLabel(size: number) -> string` — пометка строки списка;
  - `mergeCues(saved: object[], incoming: object[]) -> object[]`;
  - `playSource(hasCopy: boolean, daemonAlive: boolean) -> "copy" | "daemon" | "none"`.

- [ ] **Step 1: Write the failing tests**

Дописать в конец `page/logic.test.mjs` — отдельным импортом из `./logic.js` (повторный импорт того же модуля законен) и тестами:

```js
import {
  cacheLabel, copyState, copyVerdict, formatBytes, mergeCues, percent, playSource,
} from "./logic.js";

test("bytes are shown the way a person reads them", () => {
  assert.equal(formatBytes(0), "0 Б");
  assert.equal(formatBytes(999), "999 Б");
  assert.equal(formatBytes(1024), "1 КБ");
  assert.equal(formatBytes(1536), "1.5 КБ");
  assert.equal(formatBytes(419430400), "400 МБ");
  assert.equal(formatBytes(1288490188), "1.2 ГБ");
  // Мусор и отрицательные — не «-1 Б» и не «NaN МБ».
  assert.equal(formatBytes(-1), "—");
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes("400"), "—");
});

test("the progress bar never divides by zero and never passes a hundred", () => {
  assert.equal(percent(0, 100), 0);
  assert.equal(percent(50, 100), 50);
  assert.equal(percent(120, 100), 100);
  assert.equal(percent(1, 0), 0);
  assert.equal(percent(-5, 100), 0);
});

test("a copy is refused only when the space is really short", () => {
  const fourHundred = 419430400;
  const gib = 1073741824;

  assert.equal(copyVerdict(fourHundred, gib), "ok");
  // 500 МиБ минус запас в 200 МиБ — меньше 400 МиБ: запись кончилась бы на половине.
  assert.equal(copyVerdict(fourHundred, 524288000), "tight");
  // Размер не спросили или места не знают: судить не о чем, но и запрещать нечем.
  assert.equal(copyVerdict(0, gib), "unknown");
  assert.equal(copyVerdict(fourHundred, 0), "unknown");
  assert.equal(copyVerdict(fourHundred, 524288000, 0), "ok");
});

test("the cache switch says what it is doing", () => {
  assert.equal(copyState({}), "кеш");
  assert.equal(copyState({ waiting: true }), "кеш…");
  assert.equal(copyState({ active: true, percent: 37 }), "кеш 37%");
  assert.equal(copyState({ size: 419430400 }), "кеш ✓ · 400 МБ");
  assert.equal(copyState({ failed: true }), "кеш: ошибка");
  // Идёт копирование — проценты важнее старого размера; ошибка важнее всего.
  assert.equal(copyState({ active: true, percent: 5, size: 1000 }), "кеш 5%");
  assert.equal(copyState({ failed: true, size: 1000 }), "кеш: ошибка");
});

test("a saved episode is marked with its size", () => {
  assert.equal(cacheLabel(419430400), "на устройстве · 400 МБ");
  assert.equal(cacheLabel(0), "на устройстве");
});

test("saved and arrived cues are merged by their number", () => {
  const saved = [
    { i: 2, start: 4, lines: ["b"] },
    { i: 1, start: 0, lines: ["a"] },
  ];
  const incoming = [
    { i: 2, start: 4, lines: ["b", "c"] },
    { i: 3, start: 8, lines: ["d"] },
  ];

  assert.deepEqual(mergeCues([], incoming), [
    { i: 2, start: 4, lines: ["b", "c"] },
    { i: 3, start: 8, lines: ["d"] },
  ]);
  assert.deepEqual(mergeCues(saved, incoming), [
    { i: 1, start: 0, lines: ["a"] },
    { i: 2, start: 4, lines: ["b", "c"] },
    { i: 3, start: 8, lines: ["d"] },
  ]);
  assert.deepEqual(mergeCues(undefined, []), []);
});

test("the copy wins over the daemon, and without either there is nothing", () => {
  assert.equal(playSource(true, true), "copy");
  assert.equal(playSource(true, false), "copy");
  assert.equal(playSource(false, true), "daemon");
  assert.equal(playSource(false, false), "none");
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test page/logic.test.mjs 2>&1 | tail -20`
Expected: FAIL — `SyntaxError: The requested module './logic.js' does not provide an export named 'cacheLabel'`.

- [ ] **Step 3: Implement the helpers in `page/logic.js`**

`formatBytes`: байты до 1024 — `"N Б"`; дальше делить на 1024⁴…1024 по первой подходящей единице (`ТБ`, `ГБ`, `МБ`, `КБ`); меньше десяти — один знак после точки с отбрасыванием `.0`, иначе округлить до целого. Не число, `NaN`, отрицательное — `"—"`.

`percent`: `total <= 0` или `seen <= 0` → `0`; иначе `Math.min(100, Math.floor(seen / total * 100))`.

`copyState`: порядок проверок `failed` → `waiting` → `active` → `size > 0` → `"кеш"`, тексты ровно как в тестах (`"кеш ✓ · "` + `formatBytes(size)`).

`mergeCues(saved, incoming)`: `Map` по `cue.i`, вначале сохранённые, потом пришедшие (пришедшие побеждают), на выходе `[...map.values()].sort((a, b) => a.start - b.start)`.

`playSource(hasCopy, daemonAlive)`: `hasCopy ? "copy" : daemonAlive ? "daemon" : "none"`.

Каждой функции — комментарий в духе файла: почему запас 200 МиБ и почему мусор отдаёт `"—"`, а не `NaN`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test page/logic.test.mjs 2>&1 | tail -8 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `# pass 34` (27 прежних + 7 новых) и `582 passed`.

- [ ] **Step 5: Commit**

```bash
git add page/logic.js page/logic.test.mjs
git commit -m "Размер копии, проценты и приговор по месту считаются отдельно от страницы"
```

---

### Task 2: `page/cache.js` — база и скачивание копии

**Files:**
- Create: `page/cache.js`
- Test: `page/cache.test.mjs`, `tests/test_page_logic.py` (второй мост)

**Interfaces:**
- Consumes: ничего (модуль не трогает DOM и не импортирует `logic.js`).
- Produces:
  - `DB_NAME = "yt-trans"`, `DB_VERSION = 1`, `VIDEOS = "videos"`, `META = "meta"`;
  - `openDb() -> Promise<IDBDatabase>`; таблицы `videos` (ключ `videoId`) и `meta` (ключ `key`, запись `{key, value}`);
  - `putVideo(db, record) -> Promise<void>` — запись считается сделанной по `tx.oncomplete`, а не по `onsuccess`;
  - `getVideo(db, videoId) -> Promise<object | undefined>` — запись целиком, с `blob`;
  - `listVideos(db) -> Promise<object[]>` — записи **без** `blob`;
  - `deleteVideo(db, videoId) -> Promise<void>`;
  - `getMeta(db, key) -> Promise<unknown>` (`null`, если нет), `putMeta(db, key, value) -> Promise<void>`;
  - `downloadVideo(url, { onProgress = () => {}, signal = null } = {}) -> Promise<{ blob: Blob, size: number }>`;
  - `freeBytes() -> Promise<number>` (`0`, если браузер не сказал), `askPersist() -> Promise<boolean>`.

Запись `videos`: `{videoId, linkId, linkTitle, title, duration, size, savedAt, language, cues, blob}`.

- [ ] **Step 1: Write the failing test**

`page/cache.test.mjs`:

```js
// Скачивание копии проверяется настоящим Response и настоящими потоками:
// подделанная сеть здесь — только `fetch`, всё остальное живёт по-настоящему.
import test from "node:test";
import assert from "node:assert/strict";

import { downloadVideo } from "./cache.js";

const bytes = (text) => new TextEncoder().encode(text);

test("a finished copy is the whole file and its size", async () => {
  globalThis.fetch = async () => new Response(bytes("0123456789"), {
    headers: { "Content-Length": "10" },
  });

  const seen = [];
  const copy = await downloadVideo("/v1/videos/x/file", {
    onProgress: (loaded, total) => seen.push([loaded, total]),
  });

  assert.equal(copy.size, 10);
  assert.equal(await copy.blob.text(), "0123456789");
  assert.deepEqual(seen.at(-1), [10, 10]);
});

test("progress reaches every chunk, not only the last", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes("abc"));
      controller.enqueue(bytes("de"));
      controller.close();
    },
  });
  globalThis.fetch = async () => new Response(stream, { headers: { "Content-Length": "5" } });

  const seen = [];
  await downloadVideo("/v1/videos/x/file", {
    onProgress: (loaded, total) => seen.push([loaded, total]),
  });

  assert.deepEqual(seen, [[3, 5], [5, 5]]);
});

test("a file without a length is still a copy", async () => {
  globalThis.fetch = async () => new Response(bytes("abc"));

  const copy = await downloadVideo("/v1/videos/x/file");

  assert.equal(copy.size, 3);
});

test("a daemon that refuses does not become a copy", async () => {
  globalThis.fetch = async () => new Response("нет", { status: 404 });

  await assert.rejects(downloadVideo("/v1/videos/x/file"), /демон ответил 404/);
});

test("cancelling stops the copy and leaves nothing behind", async () => {
  // Поток, который сам никогда не кончится: остановить его может только отмена.
  const stream = new ReadableStream({
    pull(controller) {
      controller.enqueue(bytes("abcd"));
    },
  });
  globalThis.fetch = async () => new Response(stream);

  const controller = new AbortController();
  const copy = downloadVideo("/v1/videos/x/file", { signal: controller.signal });
  controller.abort();

  await assert.rejects(copy, (error) => error.name === "AbortError");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test page/cache.test.mjs 2>&1 | tail -12`
Expected: FAIL — `Cannot find module .../page/cache.js`.

- [ ] **Step 3: Write `page/cache.js`**

Скачивание — чтением потока, а не `new Response(stream).blob()`: отмена должна срабатывать, пока чтение висит.

```js
const abortError = () => new DOMException("копирование отменено", "AbortError");

/** Чтение куска, которое не зависает после отмены: гонка чтения и сигнала. */
function readOrAbort(reader, signal) {
  if (!signal) {
    return reader.read();
  }

  if (signal.aborted) {
    return Promise.reject(abortError());
  }

  return Promise.race([
    reader.read(),
    new Promise((_, fail) => signal.addEventListener("abort", () => fail(abortError()), { once: true })),
  ]);
}
```

`downloadVideo`: `fetch(url, { signal })`; `!response.ok` → `Error(\`демон ответил ${response.status}\`)`; `total` из `Content-Length` (`Number(... || 0)`); цикл `readOrAbort` → копить куски и `onProgress(seen, total)` после каждого; `finally` — `reader.cancel().catch(() => {})`; на выходе `new Blob(chunks, { type: "video/mp4" })` и `{blob, size: blob.size}`.

IndexedDB: `ask(request)` — обещание по `onsuccess`/`onerror`. `openDb` — `indexedDB.open(DB_NAME, DB_VERSION)`, в `onupgradeneeded` создать `VIDEOS` с `keyPath: "videoId"` и `META` с `keyPath: "key"`. `putVideo`/`deleteVideo`/`putMeta` резолвятся по `tx.oncomplete`. `listVideos` — `getAll()` и `map(({blob, ...rest}) => rest)`. `getMeta` — `(await ask(store.get(key)))?.value ?? null`.

`freeBytes`: `navigator.storage.estimate()` → `Math.max(0, quota - usage)`, при исключении `0`. `askPersist`: `navigator.storage.persist()` в `try/catch`, при исключении `false`.

- [ ] **Step 4: Add the pytest bridge**

В `tests/test_page_logic.py` — второй тест рядом с первым:

```python
@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_the_cache_module_passes_its_own_tests():
    proc = subprocess.run(
        ["node", "--test", "page/cache.test.mjs"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_logic.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `2 passed` и `583 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/cache.js page/cache.test.mjs tests/test_page_logic.py
git commit -m "Модуль кеша: ролик качается потоком, база хранит его целиком"
```

---

### Task 3: Переключатель «кеш» на сцене

**Files:**
- Modify: `page/index.html` (`#stage`, вокруг `#fullscreen`), `page/style.css` (после `#fullscreen`), `page/app.js` (импорт, `els`, состояние, функции, `boot`, `leaveStage`, `handleMessage`)
- Test: `tests/test_page_wiring.py`

**Interfaces:**
- Consumes: задача 1 (`copyState`, `percent`, `copyVerdict`, `formatBytes`, `SPARE_BYTES`, `mergeCues`), задача 2 (`openDb`, `putVideo`, `downloadVideo`, `freeBytes`, `askPersist`), существующие `api`, `note`, `paint`, `send`, `unwatchMessage`, `POLL_VIDEO_MS`.
- Produces (для задач 4 и 5):
  - `let db = null`, `let savedVideos = new Map()`, `let copy = null`, `let copyFailure = null`, `let objectUrl = null`, `let offline = false`, `let daemonAlive = true`, `let persistAsked = false`;
  - `async function openCache()` — открыть базу и наполнить `savedVideos` (`listVideos`);
  - `function drawCacheButton()` — текст кнопки из `copyState`;
  - `function toggleCache()`, `async function startCopy()`, `function cancelCopy()`, `async function finishCopy(blob, size)`;
  - `let follow = null` — сохранённая копия, чьи титры ещё дописываются;
  - `function rememberCues(message)` — реплики в недокачанную и в сохранённую копию;
  - `function followSoon()` — отложенная запись сохранённых титров;
  - `function filePath(videoId) -> string` — `/v1/videos/{id}/file?token=…`.

- [ ] **Step 1: Write the failing tests**

В конец `tests/test_page_wiring.py`:

```python
def test_the_stage_has_a_cache_switch_next_to_fullscreen():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = re.search(r'<div id="stage-bar">(.*?)</div>', html, re.S)

    assert bar is not None
    assert 'id="cache"' in bar.group(1)
    assert 'id="fullscreen"' in bar.group(1)
    # По умолчанию кеш выключен: без нажатия на устройстве не оседает ничего.
    assert 'aria-pressed="false"' in bar.group(1)


def test_the_cache_switch_copies_the_rolled_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    logic = (ROOT / "page" / "logic.js").read_text(encoding="utf-8")

    assert 'els.cache.addEventListener("click"' in source
    for name in (
        "copyState(", "copyVerdict(", "formatBytes(", "percent(", "mergeCues(",
        "SPARE_BYTES", "downloadVideo(", "putVideo(", "freeBytes(", "askPersist(",
    ):
        assert name in source, name
    assert "кеш ✓ · " in logic
    # Повторное нажатие во время копирования отменяет его и убирает недокачанное.
    assert "controller.abort()" in source
    assert "copy = null" in source


def test_the_base_is_named_in_one_place():
    # Имя базы и таблиц живёт в cache.js; страница их не повторяет, иначе одно
    # переименование пришлось бы искать в двух файлах.
    cache = (ROOT / "page" / "cache.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'DB_NAME = "yt-trans"' in cache
    assert '"yt-trans"' not in source
    assert "openDb(" in source


def test_the_copy_keeps_the_rolled_video_subscribed():
    # Ушёл со сцены с идущим копированием — титры должны дописываться дальше,
    # иначе сохранённое оборвётся на моменте ухода.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function leaveStage"):source.index("// --- Запуск")]

    assert body.index("copy") < body.index("unwatchMessage")


def test_the_copy_catches_cues_its_own_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleMessage"):source.index("function takeSnapshot")]

    assert "rememberCues(" in body
    assert body.index("rememberCues(") < body.index("isForVideo(")


def test_the_saved_cues_keep_arriving_after_the_picture_is_stored():
    # Перевод идёт дольше скачивания: к концу перевода на устройстве должны
    # лежать и картинка, и все строки, а не те, что успели к концу загрузки.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "let follow = null" in source
    assert "followSoon(" in source
    assert "FOLLOW_MS" in source


def test_the_stage_bar_holds_both_buttons():
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#stage-bar \{[^}]*display: flex", css, re.S)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -12`
Expected: FAIL на семи новых тестах (`id="stage-bar"` не найден, `els.cache` нет).

- [ ] **Step 3: Add the markup and styles**

В `page/index.html` вместо одинокого `<button id="fullscreen" …>` в `#stage`:

```html
    <!-- Слева сверху: ⛶ и переключатель кеша. Ролик остаётся под рукой, пока
         строки идут справа. «Кеш» по умолчанию выключен: без нажатия на
         устройстве не оседает ничего. -->
    <div id="stage-bar">
      <button id="fullscreen" type="button" aria-label="во весь экран"
        title="во весь экран">⛶</button>
      <button id="cache" type="button" aria-pressed="false"
        aria-label="копировать ролик на устройство"
        title="копировать ролик на устройство">кеш</button>
    </div>
```

В `page/style.css` правило `#fullscreen` заменить на полосу: `#stage-bar { position: absolute; left: 12px; top: calc(12px + env(safe-area-inset-top, 0px)); display: flex; gap: 8px; }`, а `#stage-bar button { padding: 8px 10px; font-size: 15px; line-height: 1; background: rgba(20, 20, 24, .82); }`. Комментарий над правилом перенести в новый, объяснив, почему полоса слева сверху.

- [ ] **Step 4: Wire the switch in `page/app.js`**

В импорт из `./logic.js` добавить `SPARE_BYTES, copyState, copyVerdict, formatBytes, mergeCues, percent`; в `els` — `cache: document.getElementById("cache")`; в константы — `const LINKS_TIMEOUT_MS = 3000;`.

В литерал `state` добавить `link: { id: "", title: "" }` и `duration: 0`: копия записывает, из какой ссылки и какой длины ролик, а заполняет их `play` в задаче 4. Без значений по умолчанию задача 3 обращалась бы к `undefined.id`.

Порядок копирования:

```js
const filePath = (videoId) =>
  `/v1/videos/${encodeURIComponent(videoId)}/file?token=${encodeURIComponent(token)}`;

/** Размер готового mp4: HEAD не отдаёт тело, но Content-Length у него есть. */
async function fileSize(path) {
  try {
    const response = await fetch(path, { method: "HEAD" });

    return Number(response.headers.get("Content-Length") || 0);
  } catch {
    // Размер не спросили — не повод отказывать: приговор будет «unknown».
    return 0;
  }
}

/** Ждёт готовый mp4, как ждёт его сцена; -1 — демон сказал, что не будет. */
async function waitForFile(videoId) {
  const mine = copy;

  for (;;) {
    if (!copy || copy !== mine) {
      return 0;
    }

    const status = await api(`/v1/videos/${encodeURIComponent(videoId)}`, { method: "POST" })
      .catch(() => null);

    if (copy !== mine) {
      return 0;
    }

    if (status && status.state === "error") {
      note(status.message || "видео не скачалось");

      return -1;
    }

    if (status && status.state === "ready") {
      return fileSize(filePath(videoId));
    }

    await new Promise((resolve) => setTimeout(resolve, POLL_VIDEO_MS));
  }
}
```

`startCopy()`:

1. `if (!db) await openCache();` — базы нет, сказать `note("браузер не дал хранилище")` и выйти.
2. Завести `copy = { videoId, controller: new AbortController(), waiting: true, seen: 0, total: 0, cues: new Map(cuesByIndex), linkId: state.link.id, linkTitle: state.link.title, title: state.title, duration: state.duration, language: state.language }`; `copyFailure = null`; `drawCacheButton()`.
3. Один раз за жизнь страницы: `if (!persistAsked) { persistAsked = true; askPersist().catch(() => {}); }`.
4. `const size = await waitForFile(videoId)`; если `copy` уже не тот — выйти; если `size < 0` — `copy = null`, `drawCacheButton()`.
5. `const free = await freeBytes()`; при `copyVerdict(size, free) === "tight"` — `note(\`не хватает места: нужно ещё ${formatBytes(size + SPARE_BYTES - free)}, свободно ${formatBytes(free)}\`)`, `copy = null`, `drawCacheButton()`, выйти.
6. `copy.waiting = false; copy.total = size; drawCacheButton();`
7. `await downloadVideo(filePath(videoId), { signal: copy.controller.signal, onProgress })`; в `onProgress` пересчитывать `percent(copy.seen, copy.total)` и звать `drawCacheButton()`, только когда процент изменился.
8. Успех — `finishCopy(blob, size)`. Ошибка — запомнить отменяли ли, обнулить `copy`, при отмене просто `drawCacheButton()`, иначе `copyFailure = videoId`, `note(\`копия не сохранилась: ${error.message}\`)`, `drawCacheButton()` и, если зритель уже не на этом ролике, `send(unwatchMessage(videoId))`.

`finishCopy(blob, size)`: собрать запись `{videoId, linkId, linkTitle, title, duration, size, savedAt: Date.now(), language, cues: mergeCues([], [...copy.cues.values()]), blob}`, `await putVideo(db, record)` (ошибка записи — тот же путь «копия не сохранилась»), затем `copy = null`, `savedVideos.set(videoId, record)`, `drawCacheButton()`, и перерисовать список, если виден дом (задача 5 добавит в список пометки; до неё достаточно `drawLinks(knownLinks, "")`). Дальше развилка — титры ещё не кончились:

- зритель на этом ролике — `note("сохранено на устройстве")`, `switchToCopy(record)` (в задаче 4) и `follow = { videoId, record, timer: null }`: перевод идёт дальше, и сохранённые титры должны дописываться, пока зритель тут;
- зритель уже не на нём — `send(unwatchMessage(videoId))`.

`toggleCache()`: нет `state.videoId` — выйти; копируется этот ролик — `cancelCopy()`; копия этого ролика уже есть — ничего (`«кеш ✓» ничего не делает`); иначе — если идёт другая копия, `cancelCopy()`, потом `startCopy()`.

`cancelCopy()`: запомнить `{videoId, controller}`, `copy = null`, `controller.abort()`, отписаться, если зритель не на этом ролике, `drawCacheButton()`.

`drawCacheButton()`: `mine = copy && copy.videoId === state.videoId ? copy : null`, `saved = savedVideos.get(state.videoId)`, текст `copyState({ waiting: Boolean(mine && mine.waiting), active: Boolean(mine), percent: mine ? percent(mine.seen, mine.total) : 0, size: saved ? saved.size : 0, failed: copyFailure === state.videoId })`, `aria-pressed` — `"true"`, когда есть копия или идёт копирование.

Реплики идут в две стороны — в недокачанную копию и в уже сохранённую:

```js
const FOLLOW_MS = 5000;

let follow = null;   // { videoId, record, timer } — сохранённое, чьи титры дописываются

function rememberCues(message) {
  if (message.videoId === undefined || message.videoId === null) {
    return;   // событие задания: к ролику не относится
  }

  const list = message.type === "snapshot" || message.type === "cues" ? message.cues || [] : [];

  if (copy && message.videoId === copy.videoId) {
    for (const cue of list) { copy.cues.set(cue.i, cue); }
    copy.language = message.language || copy.language;
  }

  if (follow && message.videoId === follow.videoId) {
    follow.record.cues = mergeCues(follow.record.cues, list);
    follow.record.language = message.language || follow.record.language;
    followSoon();
  }
}

/** Запись целого mp4 — дорогое дело; титры дописываем не чаще раза в пять секунд. */
function followSoon() {
  if (follow.timer !== null) {
    return;
  }

  follow.timer = setTimeout(() => {
    if (!follow) {
      return;
    }

    follow.timer = null;
    putVideo(db, follow.record).catch(() => {});
  }, FOLLOW_MS);
}
```

Первая строка `handleMessage` — `rememberCues(message);` перед проверкой `isForVideo`: равенство по `videoId` здесь честнее, чем `isForVideo`, потому что событие без `videoId` к копии не относится.

`leaveStage`: `const copying = copy && copy.videoId === state.videoId;`; если `follow` этого ролика — снять таймер, записать сразу (`putVideo(db, follow.record).catch(() => {})`) и `follow = null`; отписка — под условием `if (state.videoId && !copying)`.

`boot`: `els.cache.addEventListener("click", toggleCache);`

Повторная копия того же ролика (после отмены или ошибки) просто пишет запись с тем же `videoId` поверх прежней — отдельного «заменить» не нужно.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `7 passed` среди новых и `590 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/index.html page/style.css page/app.js tests/test_page_wiring.py
git commit -m "На сцене появился переключатель «кеш»: копия начинается с готового mp4"
```

---

### Task 4: Копия как источник воспроизведения

**Files:**
- Modify: `page/app.js` (`play`, `prepareVideo`, `openLink`, `leaveStage`, `createJob`)
- Test: `tests/test_page_wiring.py`

**Interfaces:**
- Consumes: задача 3 (`savedVideos`, `db`, `copy`, `follow`, `objectUrl`, `daemonAlive`, `rememberCues`, `drawCacheButton`), задача 1 (`playSource`, `mergeCues`), задача 2 (`getVideo`, `deleteVideo`).
- Produces:
  - `play(video, link = {})` — вместо `play(videoId, title)`; `state.link = {id, title}` и `state.duration`;
  - `function switchToCopy(record, { resume = 0, autoplay = false } = {})`;
  - `function seedCues(record)`;
  - `async function playLocal(videoId) -> Promise<boolean>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_saved_video_plays_from_the_device():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "URL.createObjectURL" in source
    assert "URL.revokeObjectURL" in source
    assert "switchToCopy(" in source
    assert "playLocal(" in source
    assert "playSource(" in source


def test_the_copy_carries_its_cues_without_the_daemon():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "seedCues(" in source
    assert "mergeCues(" in source
    assert "демона нет — играю сохранённое" in source


def test_offline_says_why_a_rolled_video_does_not_play():
    # Спека: без копии офлайн сцена не открывается вовсе — сообщение остаётся
    # на домашней странице, чтобы чёрный экран не притворялся поломкой.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function play("):source.index("async function createJob")]

    assert "демона нет, этого ролика на устройстве нет" in body
    assert body.index("playSource(") < body.index("els.home.hidden = true")


def test_an_evicted_copy_does_not_lie_in_the_list():
    # Браузер вправе вытеснить Blob: запись в базе есть, играть нечего.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "savedVideos.delete(" in body
    assert "deleteVideo(" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -10`
Expected: FAIL — `URL.createObjectURL` в `app.js` нет.

- [ ] **Step 3: Implement the copy as the source**

`play(video, link = {})`: в начале — если `playSource(savedVideos.has(video.videoId), daemonAlive) === "none"`, то `drawLinks(knownLinks, "демона нет, этого ролика на устройстве нет")` и выйти, не открывая сцену. Дальше прежние поля плюс `link: { id: link.id || "", title: link.title || "" }`, `duration: Number(video.duration) || 0`; `copyFailure = null`; `drawCacheButton()`. Оба вызова в `openLink` — `play(videos[0], { id, title })` и `play(video, { id, title })`.

Порядок в файле: `playLocal` стоит прямо над `switchToCopy` — тест вырезает её тело по этим двум именам.

`switchToCopy(record, { resume = 0, autoplay = false } = {})`: `from = resume || els.video.currentTime`, `wasPlaying = !els.video.paused || autoplay`; отозвать прежний `objectUrl`; `objectUrl = URL.createObjectURL(record.blob)`; на `loadedmetadata` (`{once: true}`) — `currentTime = from`, если `from > 0`, и `play()` при `wasPlaying` (ошибку глотать: вне нажатия браузер вправе отказать); присвоить `src`. Параметр назван `autoplay`, а не `play`, чтобы не затенять функцию `play`.

`seedCues(record)`: если `record.cues` пуст — выйти; иначе очистить `cuesByIndex`, разложить сохранённые по `cue.i`, `state.cues` — отсортированный список, `state.language = record.language || state.language`, `state.translated = countTranslated()`, `paintedKey = null`, `render()`, `paint()`. Снимок с демона потом заменит их целиком — это и нужно: живые полнее.

`playLocal(videoId)`: если `!savedVideos.has(videoId) || !db` — `false`; `record = await getVideo(db, videoId)`; если `videoId !== state.videoId` — `false`; если записи нет (браузер вытеснил Blob) — `savedVideos.delete(videoId)`, `deleteVideo(db, videoId).catch(() => {})`, `drawCacheButton()` и `false`; иначе `switchToCopy(record, { resume: positionFor(positions, videoId), autoplay: true })`, `seedCues(record)`, при `!daemonAlive` — `note("демона нет — играю сохранённое")`, `true`.

`prepareVideo`: после `stopVideoPoll()` — `if (playSource(savedVideos.has(videoId), daemonAlive) === "copy" && await playLocal(videoId)) return;`, дальше как сейчас. В `catch` первого `POST` — та же строка `"демона нет, этого ролика на устройстве нет"`, когда `playSource(false, daemonAlive) === "none"` (демон умер уже после входа на сцену), иначе `error.message`.

`createJob` `catch` — при `!daemonAlive && savedVideos.has(videoId)` сказать `"демона нет — играю сохранённое"`, иначе `error.message`.

`leaveStage`: перед снятием `src` — отозвать `objectUrl` и обнулить его.

`finishCopy`: к `note("сохранено на устройстве")` добавить `switchToCopy(record)` без потери места (`resume` не нужен: функция берёт `currentTime`), чтобы копия подхватилась посреди просмотра.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `594 passed`.

- [ ] **Step 5: Commit**

```bash
git add page/app.js tests/test_page_wiring.py
git commit -m "Сохранённый ролик играет с устройства, а не с демона"
```

---

### Task 5: Домашний список без демона

**Files:**
- Modify: `page/app.js` (`api`, `connectSocket`, `start`, `loadLinks`, `openLink`, `drawLinks`, `consumeShare`, новые `savedForLink`, `savedRows`, `drawEpisodes`, `dropCopy`), `page/style.css` (`#links li`)
- Test: `tests/test_page_wiring.py`

**Interfaces:**
- Consumes: задача 2 (`getMeta`, `putMeta`, `listVideos`, `deleteVideo`), задача 4 (`play(video, link)`), `offline`, `savedVideos`, `daemonAlive`, `LINKS_TIMEOUT_MS`.
- Produces: `function savedForLink(linkId) -> object[]`, `function savedRows(records) -> HTMLElement[]`, `function drawEpisodes(title, videos)`, `async function dropCopy(videoId)`; `let currentEpisodes = null`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_home_stays_with_the_saved_copy_when_the_daemon_is_gone():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function loadLinks"):source.index("function savedForLink")]

    # Молчащий демон — не отказ: прошлый список рисуется сразу, а запрос
    # к демону получает короткий срок и заменяет его, когда ответит.
    assert "AbortSignal.timeout(LINKS_TIMEOUT_MS)" in body
    assert 'getMeta(db, "links")' in body
    assert body.index("getMeta") < body.index("api(")
    assert "offline = true" in body
    assert "savedForLink(" in source
    assert "на устройстве" in source


def test_a_saved_video_is_marked_in_the_list_and_can_be_deleted():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "cacheLabel(" in source
    assert "deleteVideo(" in source
    assert "dropCopy(" in source
    assert 'button("удалить"' in source


def test_a_shared_link_does_not_wipe_the_saved_home():
    # При мёртвом демоне шаринг из YouTube не должен оставлять пустой экран:
    # сохранённое с пометками важнее неудавшегося добавления.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function consumeShare"):source.index("function row(")]

    assert "drawLinks(knownLinks" in body


def test_saved_episodes_stand_under_their_link():
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#links li \{[^}]*flex-wrap: wrap", css, re.S)
    assert re.search(r"#links li > ul \{[^}]*flex-basis: 100%", css, re.S)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -12`
Expected: FAIL на четырёх новых тестах.

- [ ] **Step 3: Learn when the daemon is gone**

`api`: обернуть `fetch` в `try/catch` — при исключении `daemonAlive = false` и пробросить ошибку; после ответа `daemonAlive = true`. Комментарий: 401 — это живой демон с чужим токеном, офлайн тут ни при чём.

`connectSocket`: на `open` — `daemonAlive = true`, на `close` — `daemonAlive = false` перед переподключением.

`start` становится `async` и ждёт базу до списка:

```js
async function start() {
  await openCache();
  connectSocket();
  await loadLinks();
  consumeShare();
}
```

`openCache()`: `db = await openDb()` в `try/catch` (`null` при отказе), затем `savedVideos` из `listVideos(db)` (если база есть). Вызывается из `start` и из `startCopy`.

- [ ] **Step 4: Draw the home without the daemon**

`loadLinks`: сначала взять из базы прошлый список и нарисовать его же в обычном виде, потом спросить демона — с коротким сроком. Упавший запрос не должен оставлять человека перед пустым экраном на все три секунды.

```js
async function loadLinks() {
  const last = db ? await getMeta(db, "links") : null;

  if (last && last.length) {
    // Прошлый список уже есть — рисуем его сразу, а не после ответа демона.
    offline = false;
    drawLinks(last, "");
  }

  let data;

  try {
    data = await api("/v1/links", { signal: AbortSignal.timeout(LINKS_TIMEOUT_MS) });
  } catch (error) {
    if (daemonAlive) {
      offline = false;
      drawLinks([], error.message);

      return;
    }

    offline = true;
    drawLinks(last || [], "");

    return;
  }

  offline = false;

  if (db) {
    await putMeta(db, "links", data.links || []);
  }

  drawLinks(data.links || [], "");
}
```

Тест выше требует, чтобы `getMeta` стоял в этой функции раньше `api(`: он режет тело `loadLinks` по имени `savedForLink`, поэтому та функция идёт сразу за `loadLinks`.

`savedForLink(linkId)`: сохранённые записи с этим `linkId`, свежие первыми по `savedAt`.

`savedRows(records)`: на запись — `<li class="saved">` с кнопкой `\`${episodeLabel(record)} · ${cacheLabel(record.size)}\`` (ведёт в `play(record, { id: record.linkId, title: record.linkTitle })`) и кнопкой `"удалить"` (`dropCopy(record.videoId)`); `episodeLabel` уже есть в `app.js` — вторую такую функцию не писать.

`drawLinks`: внутри цикла по видимым ссылкам, после двух кнопок, — сохранённые эпизоды своей ссылки отдельным списком под строкой:

```js
if (offline) {
  const saved = savedForLink(link.id);

  if (saved.length) {
    const more = document.createElement("ul");
    more.append(...savedRows(saved));
    item.append(more);
  }
}
```

`<li>` внутрь `<li>` вкладывать нельзя — отсюда обёртка `ul`, на неё и рассчитано правило `#links li > ul` ниже. В ветке `!items.length`: при `offline && savedVideos.size` — `els.links.append(...savedRows([...savedVideos.values()]))` (здесь `#links` сам список, обёртка не нужна) и выйти; пустая надпись при `offline` — `"демона нет, на устройстве ничего не сохранено"`, иначе прежняя `"пока пусто, добавь ссылку"`.

`openLink`: отрисовку эпизодов вынести в `drawEpisodes(title, videos)` (запоминает `currentEpisodes = {title, videos}`, ставит заголовок, чистит список, при отсутствии роликов — `"в ссылке нет роликов"`, у сохранённого ролика метка `· ${cacheLabel(saved.size)}` и кнопка `"удалить"`, в конце `els.episodes.hidden = false`). В `catch` — при `daemonAlive` как сейчас, иначе `const saved = savedForLink(id); drawLinks(knownLinks, saved.length ? "демона нет — в списке сохранённые ролики этой ссылки" : "демона нет, роликов этой ссылки на устройстве нет");`.

`dropCopy(videoId)`: если `follow` этого ролика — снять таймер и обнулить; `if (db) await deleteVideo(db, videoId)`, `savedVideos.delete(videoId)`, `if (currentEpisodes) drawEpisodes(currentEpisodes.title, currentEpisodes.videos)`, `drawLinks(knownLinks, "")`.

`consumeShare` `catch`: при `!daemonAlive` — `drawLinks(knownLinks, "демона нет — ссылку из YouTube добавить нельзя")`, иначе прежняя строка про неудачу.

`addLink` и `dropLink` в `catch` — та же развилка: при `!daemonAlive` рисовать `drawLinks(knownLinks, "демона нет — ссылку добавить нельзя")` / `"демона нет — ссылку удалить нельзя"`, иначе прежние строки. Без этого офлайн любое неудачное нажатие уносило бы с экрана всё сохранённое.

`style.css`: `#links li` получает `flex-wrap: wrap`; новое правило `#links li > ul { flex-basis: 100%; }` и `#links li > ul { list-style: none; margin: 0; padding: 0 0 0 12px; display: flex; flex-direction: column; gap: 8px; }` — сохранённые эпизоды стоят под своей ссылкой.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `598 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/app.js page/style.css tests/test_page_wiring.py
git commit -m "Без демона домашний список показывает сохранённое"
```

---

### Task 6: Оболочка страницы в сервис-воркере

**Files:**
- Create: `page/sw.js`
- Modify: `page/app.js` (`registerWorker`, `boot`, `loadLinks`)
- Test: `tests/test_page_wiring.py`, `tests/test_page_serving.py`

**Interfaces:**
- Consumes: файлы оболочки, которые перечисляет сам воркер (`/`, `/app.js`, `/logic.js`, `/cache.js`, `/style.css`, `/manifest.json`, `/icon-192.png`, `/icon-512.png`).
- Produces: регистрацию `/sw.js` с корня страницы.

- [ ] **Step 1: Write the failing tests**

`tests/test_page_wiring.py`:

```python
def test_the_shell_is_kept_for_the_day_the_computer_is_off():
    worker = (ROOT / "page" / "sw.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'serviceWorker.register("/sw.js")' in source
    assert "registerWorker()" in source
    for name in ("/app.js", "/logic.js", "/cache.js", "/style.css", "/manifest.json"):
        assert name in worker
    # Живые данные демона и сертификат — не оболочка.
    assert 'startsWith("/v1/")' in worker
    assert "/ca.crt" in worker


def test_a_browser_without_a_worker_says_so_once():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "workerMissing = true" in source
    assert '"yt-trans-no-worker"' in source
    assert "не сохранит страницу для офлайна" in source
```

`tests/test_page_serving.py`:

```python
async def test_the_service_worker_is_served_from_the_root(client):
    # Воркер лежит в корне области видимости, поэтому Service-Worker-Allowed
    # не нужен, а тип должен быть javascript: иначе браузер откажется.
    response = await client.get("/sw.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py tests/test_page_serving.py -q 2>&1 | tail -8`
Expected: FAIL — `page/sw.js` не читается.

- [ ] **Step 3: Write `page/sw.js`**

```js
// Оболочка страницы без демона. Пока компьютер включён, страница приходит от
// него и обновляется сразу; когда выключен — из этого кеша.
const VERSION = "yt-trans-1";
const SHELL = [
  "/", "/app.js", "/logic.js", "/cache.js", "/style.css",
  "/manifest.json", "/icon-192.png", "/icon-512.png",
];
```

`install` — `cache.addAll(SHELL)`, затем `skipWaiting()`. `activate` — удалить все кеши, кроме `VERSION`, затем `clients.claim()`.

`fetch`: только `GET` своего источника; `/v1/` и `/ca.crt` пропускать без `respondWith` (это ответы «на сейчас»). Остальное — сеть, при удаче копия в кеш и ответ наружу, при неудаче — `caches.match(request)`, для навигации — запасной `caches.match("/")`, иначе `new Response("", { status: 504 })`.

Комментарий в файле: имя версии меняется вместе с набором файлов — старый кеш тогда удаляется на активации.

- [ ] **Step 4: Register it from the page**

В `page/app.js`:

```js
const WORKER_KEY = "yt-trans-no-worker";

let workerMissing = false;

/** Оболочка на устройстве: без неё страница открывается только с живым демоном. */
async function registerWorker() {
  if (!("serviceWorker" in navigator)) {
    workerMissing = true;

    return;
  }

  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch {
    workerMissing = true;
  }
}
```

Сказать об этом один раз за жизнь браузера: в `loadLinks` перед успешной отрисовкой —

```js
  let warning = "";

  if (workerMissing && !store(WORKER_KEY)) {
    store(WORKER_KEY, "1");
    warning = "браузер не сохранит страницу для офлайна — нужен https и современный браузер";
  }

  drawLinks(data.links || [], warning);
```

Вызов `registerWorker()` — в `boot()` до ветки с токеном, чтобы регистрация случилась и на экране токена. `boot` становится `async` и **ждёт** её: `loadLinks` спрашивает `workerMissing` сразу, и без ожидания предупреждение о пропавшем воркере не показалось бы вовсе. Хвост `boot()` — `await registerWorker();` перед `if (token)`, а ниже по файлу остаётся прежний `boot();`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py tests/test_page_serving.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `601 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/sw.js page/app.js tests/test_page_wiring.py tests/test_page_serving.py
git commit -m "Страница открывается без демона: сервис-воркер держит оболочку"
```

---

### Task 7: Рассказать хозяину, как это работает

**Files:**
- Modify: `docs/tablet.md` (новый раздел после «Диск», строка в таблицу «Если не работает»)

**Interfaces:**
- Consumes: поведение задач 3–6.

- [ ] **Step 1: Write the section**

Раздел `## Без компьютера` сразу после «Диск»: переключатель «кеш» слева сверху на сцене, по умолчанию выключен; копирование начинается, как только готов mp4, и идёт, пока открыта страница (закрыл приложение или уснул экран — встало, нажать заново); на устройстве лежат ролик и титры, которые успели перевестись; сохранённое помечено в списке `на устройстве · 400 МБ`, удаляется кнопкой `удалить`; копия играет и онлайн — обрыв связи больше не мешает; офлайн-список раскрывает сохранённые ролики под их ссылками, остальные ссылки дают сообщение, что демона нет; кеш живёт в хранилище браузера на планшете, очистка данных сайта его стирает, и полностью занимает место на диске — не ГиБ кэша демона.

В таблицу «Если не работает» — две строки: «демона нет, этого ролика на устройстве нет» → «ролик не сохранён на планшете» → «включить „кеш“ при следующем просмотре с живым демоном»; «копия не сохранилась: …» → «кончилось место или браузер отказал» → «освободить место, удалить ненужное кнопкой „удалить“».

В разделе «Ссылки» фраза «своих кнопок на сцене три» устареет: кнопок станет четыре (RU, CNR, ⛶, «кеш»). Поправить её там же, не оставляя счёта, который разъедется при следующей кнопке: «свои кнопки на сцене — выключатели строк RU и CNR, ⛶ и „кеш“».

- [ ] **Step 2: Check the prose mentions the real names**

Run: `grep -n "кеш\|на устройстве\|удалить" docs/tablet.md`
Expected: новый раздел и обе строки таблицы видны.

- [ ] **Step 3: Commit**

```bash
git add docs/tablet.md
git commit -m "В docs/tablet.md рассказано про просмотр без компьютера"
```

---

### Task 8: Проверка в настоящем браузере

**Files:** ничего не создаётся; найденное возвращается в тесты своей задачи.

**Interfaces:** Consumes: всё выше.

- [ ] **Step 1: Поднять страницу и демон**

Run: `.venv/bin/python -m uvicorn helper.app:app --host 127.0.0.1 --port 8899` в фоне и открыть `http://127.0.0.1:8899/` в Chromium через Playwright.

- [ ] **Step 2: Скопировать ролик и увидеть его в базе**

Взять сохранённый ролик, дождаться готового mp4, нажать «кеш»; убедиться, что кнопка проходит «кеш…» → «кеш 37%» → «кеш ✓ · N МБ», а в IndexedDB (`yt-trans` → `videos`) появилась запись с `blob` и `cues`. Перезагрузить страницу — «кеш ✓» на месте.

- [ ] **Step 3: Выключить демон и открыть страницу снова**

Остановить uvicorn, перезагрузить страницу. Expected: страница открывается (сервис-воркер), список показывает сохранённые ролики с пометками и `удалить`. Несохранённый ролик нажимается и даёт «демона нет, этого ролика на устройстве нет».

- [ ] **Step 4: Посмотреть сохранённое**

Нажать сохранённый ролик. Expected: играет, перематывается, титры рисуются из сохранённых; статус говорит «демона нет — играю сохранённое».

- [ ] **Step 5: Записать, что нашлось**

Найденное — новая задача с тестом в той же форме, что выше (сначала падающий тест, потом правка); если ничего не нашлось, сказать это в отчёте о выполнении плана.

---

## Порядок и зависимости

Задачи идут строго по номерам: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. Задача 3 без 1 и 2 не соберётся (нет `copyState` и `downloadVideo`), задача 4 без 3 (нет `savedVideos`), задача 5 без 4 (нет `play(video, link)`), задача 6 без 2 (оболочка перечисляет `/cache.js`). Внутри задачи шаги идут в порядке RED → GREEN → коммит, и каждый коммит оставляет набор тестов зелёным.
