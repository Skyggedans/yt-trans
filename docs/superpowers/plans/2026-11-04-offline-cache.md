# Offline cache of videos on the tablet: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The tablet keeps a copy of the video and subtitles, which the owner enabled with one switch on the stage, and plays it from the device — including when the computer with the daemon is turned off.

**Architecture:** A new `page/cache.js` — IndexedDB (`videos`, `meta`) and streaming download of the mp4 with a byte counter; a new `page/sw.js` — a service worker that holds the page shell while the daemon is gone. `page/app.js` gets a “cache” switch, copying with a wait for the ready mp4, playback from the copy via `URL.createObjectURL` and an offline home assembled from `meta.links` and saved videos. The pure decisions (bytes, percentages, verdict by space, labels, cue merging, source choice) live in `page/logic.js` and are checked by `node --test`.

**Tech Stack:** vanilla ES modules without a build, IndexedDB and Cache Storage in the browser, node 24 (`node --test`) for the pure logic, pytest for the wiring and serving of static files.

**Spec:** `docs/superpowers/specs/2026-11-04-offline-cache-design.md`

## Global Constraints

- The cache is about the current video and only on a press; off by default. Without a press nothing is saved, including subtitles.
- An enabled “cache” is not remembered: after the page restarts it must be pressed again — otherwise 400 MB would go without fresh demand.
- Copying starts as soon as the mp4 is ready, without waiting for the translation to finish; subtitles are appended as they arrive. Re-copying a video replaces the previous copy (the record key is `videoId`).
- Nothing is deleted automatically. Does not fit — a refusal with a number: `нужно ещё X, свободно Y`.
- Only a whole mp4 reaches the device: a partially downloaded one is dropped, nothing remains in the database.
- A full copy is the preferred source of playback always, online too.
- The free-space margin before copying — **200 MiB** (`SPARE_BYTES`); the timeout for the daemon's links list — **3000 ms** (`LINKS_TIMEOUT_MS`).
- Page without a build: only `<script type="module">`, no packages and no CDN.
- All human-visible strings are in Russian, short, in the spirit of the existing ones.
- `/v1/*` requires the token, `page/*` is public; the service worker lives in `page/` and is served by the same `StaticFiles`.
- Pure logic — only in `page/logic.js`; `page/app.js` — DOM and network; wiring checks with regexes in `tests/test_page_wiring.py`.
- Tests: `.venv/bin/python -m pytest` (**582** green at the start) and `node --test page/logic.test.mjs`.
- Commits in Russian, one line, without the `Co-Authored-By` trailer (the owner's rule). The pre-commit review runs up to 300 s: run with a timeout ≥ 360 s, do not use `--no-verify`.

## Review Focus

It has been checked that each line below gets a test in the task that owns the code.

1. **The daemon is alive but does not respond** (the computer is asleep): the request hangs rather than fails. The home list must appear from the database in about three seconds: `AbortSignal.timeout(LINKS_TIMEOUT_MS)` in `loadLinks` and the fallback path from `meta`. Test: task 5.
2. **`navigator.storage.estimate()` is unavailable or lies with zero.** The `unknown` verdict does not forbid copying: the file is downloaded, and a lack of space comes out as a write error, not as silence. Tests: task 1 (`copyVerdict` → `unknown`), task 2 (a daemon refusal does not become a copy).
3. **Copying was cancelled mid-download.** The download is aborted, nothing is written to the database, the button returns to “кеш”. Test: task 2 (`AbortError` in `node --test`); the cancel path in the page — task 3 (`cancelCopy`).
4. **The browser evicted the copy between sessions.** The metadata is there, but the Blob is not: the row must not lie, the video plays from the daemon, the copy is cleaned up. Test: task 4 (`savedVideos.delete` in `playLocal`).
5. **Sharing from YouTube with a dead daemon does not wipe the list with marks.** Today `consumeShare` draws an empty list on error; offline that would throw everything saved off the screen. Test: task 5.

## Plan decisions that are not in the spec

The spec left names, layout and details to the builder; here they are chosen deliberately:

- **`meta` stores only `links`.** The spec also named an `episodes` key (`linkId → {title, videos}`), but a saved video record itself carries `linkId` and `linkTitle`, so offline episode lists are assembled from `videos` — no second copy of the same data is needed.
- **Progress is visible on the button, not in the stage status.** The button already shows “кеш 37%”; a second line with the same number would be noise. The status says only the outcome: “сохранено на устройстве” or the reason for the refusal.
- **One copy at a time.** Pressing “кеш” on another video cancels the started one: two 400 MB mp4s at once is not what was asked for.
- **While the mp4 is still being muxed, the button shows “кеш…”** The press is not rejected and does not copy unfinished work: `startCopy` waits for the `ready` state with the same poll as the stage.
- **Offline is a property of the page, not of the network.** The `offline` flag is set by failures of requests to the daemon and by socket closure; it is cleared by any successful response. `navigator.onLine` is not consulted at all.

## Files

**Created:**
- `page/cache.js` — IndexedDB and copy downloading.
- `page/cache.test.mjs` — download tests for `node --test`.
- `page/sw.js` — the shell service worker.

**Modified:**
- `page/logic.js`, `page/logic.test.mjs` — bytes, percentages, verdicts and labels, cue merging, source choice.
- `page/index.html`, `page/style.css` — the stage bar with “кеш” and ⛶, marks in the list.
- `page/app.js` — copying, playback from the copy, offline home, worker registration.
- `tests/test_page_logic.py` — a bridge to the second set of node tests.
- `tests/test_page_wiring.py`, `tests/test_page_serving.py`.
- `docs/tablet.md`.

**Not part of the plan** (named so that it is not built beyond): background resumption after the page closes, resuming a partial mp4, syncing caches between devices, auto-deletion and limits, offline translation, deleting the copy from the stage, the total cache size on the home page, updating the shell with a button.

---

### Task 1: Pure numbers and copy labels

**Files:**
- Modify: `page/logic.js` (at the end, after `readFontSize`)
- Test: `page/logic.test.mjs` (at the end)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `formatBytes(bytes: unknown) -> string` — `"400 МБ"`, `"1.2 ГБ"`, `"—"` for garbage;
  - `percent(seen: number, total: number) -> number` — 0…100;
  - `SPARE_BYTES: number` — 200 MiB;
  - `copyVerdict(need: number, free: number, reserve = SPARE_BYTES) -> "ok" | "tight" | "unknown"`;
  - `copyState({ waiting?, active?, percent?, size?, failed? }) -> string` — the button text;
  - `cacheLabel(size: number) -> string` — the mark for a list row;
  - `mergeCues(saved: object[], incoming: object[]) -> object[]`;
  - `playSource(hasCopy: boolean, daemonAlive: boolean) -> "copy" | "daemon" | "none"`.

- [ ] **Step 1: Write the failing tests**

Append to the end of `page/logic.test.mjs` — with a separate import from `./logic.js` (re-importing the same module is legal) and the tests:

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
  // Garbage and negatives — not "-1 Б" and not "NaN МБ".
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
  // 500 MiB minus the 200 MiB margin — less than 400 MiB: the write would run out halfway.
  assert.equal(copyVerdict(fourHundred, 524288000), "tight");
  // The size was not asked or the space is unknown: there is nothing to judge by, but nothing to forbid with either.
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
  // Copying is under way — percentages matter more than the old size; an error matters most of all.
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

`formatBytes`: bytes up to 1024 — `"N Б"`; beyond that divide by 1024⁴…1024 by the first suitable unit (`ТБ`, `ГБ`, `МБ`, `КБ`); below ten — one digit after the point with `.0` dropped, otherwise round to a whole number. Not a number, `NaN`, negative — `"—"`.

`percent`: `total <= 0` or `seen <= 0` → `0`; otherwise `Math.min(100, Math.floor(seen / total * 100))`.

`copyState`: the order of checks `failed` → `waiting` → `active` → `size > 0` → `"кеш"`, the texts exactly as in the tests (`"кеш ✓ · "` + `formatBytes(size)`).

`mergeCues(saved, incoming)`: a `Map` by `cue.i`, saved first, then arrived (arrived win), the output `[...map.values()].sort((a, b) => a.start - b.start)`.

`playSource(hasCopy, daemonAlive)`: `hasCopy ? "copy" : daemonAlive ? "daemon" : "none"`.

Each function gets a comment in the spirit of the file: why the margin is 200 MiB and why garbage gives `"—"`, not `NaN`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test page/logic.test.mjs 2>&1 | tail -8 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `# pass 34` (27 previous + 7 new) and `582 passed`.

- [ ] **Step 5: Commit**

```bash
git add page/logic.js page/logic.test.mjs
git commit -m "Размер копии, проценты и приговор по месту считаются отдельно от страницы"
```

---

### Task 2: `page/cache.js` — the database and copy downloading

**Files:**
- Create: `page/cache.js`
- Test: `page/cache.test.mjs`, `tests/test_page_logic.py` (the second bridge)

**Interfaces:**
- Consumes: nothing (the module does not touch the DOM and does not import `logic.js`).
- Produces:
  - `DB_NAME = "yt-trans"`, `DB_VERSION = 1`, `VIDEOS = "videos"`, `META = "meta"`;
  - `openDb() -> Promise<IDBDatabase>`; the tables `videos` (key `videoId`) and `meta` (key `key`, record `{key, value}`);
  - `putVideo(db, record) -> Promise<void>` — the write counts as done on `tx.oncomplete`, not on `onsuccess`;
  - `getVideo(db, videoId) -> Promise<object | undefined>` — the whole record, with `blob`;
  - `listVideos(db) -> Promise<object[]>` — records **without** `blob`;
  - `deleteVideo(db, videoId) -> Promise<void>`;
  - `getMeta(db, key) -> Promise<unknown>` (`null` if absent), `putMeta(db, key, value) -> Promise<void>`;
  - `downloadVideo(url, { onProgress = () => {}, signal = null } = {}) -> Promise<{ blob: Blob, size: number }>`;
  - `freeBytes() -> Promise<number>` (`0` if the browser did not say), `askPersist() -> Promise<boolean>`.

The `videos` record: `{videoId, linkId, linkTitle, title, duration, size, savedAt, language, cues, blob}`.

- [ ] **Step 1: Write the failing test**

`page/cache.test.mjs`:

```js
// Copying is checked with a real Response and real streams: the faked network here
// is only `fetch`, everything else lives for real.
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
  // A stream that never ends by itself: only cancellation can stop it.
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

Downloading — by reading the stream, not `new Response(stream).blob()`: cancellation must fire while the read is hanging.

```js
const abortError = () => new DOMException("копирование отменено", "AbortError");

/** A chunk read that does not hang after cancellation: a race between the read and the signal. */
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

`downloadVideo`: `fetch(url, { signal })`; `!response.ok` → `Error(\`демон ответил ${response.status}\`)`; `total` from `Content-Length` (`Number(... || 0)`); the `readOrAbort` loop → accumulate chunks and `onProgress(seen, total)` after each; `finally` — `reader.cancel().catch(() => {})`; the output is `new Blob(chunks, { type: "video/mp4" })` and `{blob, size: blob.size}`.

IndexedDB: `ask(request)` — a promise on `onsuccess`/`onerror`. `openDb` — `indexedDB.open(DB_NAME, DB_VERSION)`, in `onupgradeneeded` create `VIDEOS` with `keyPath: "videoId"` and `META` with `keyPath: "key"`. `putVideo`/`deleteVideo`/`putMeta` resolve on `tx.oncomplete`. `listVideos` — `getAll()` and `map(({blob, ...rest}) => rest)`. `getMeta` — `(await ask(store.get(key)))?.value ?? null`.

`freeBytes`: `navigator.storage.estimate()` → `Math.max(0, quota - usage)`, `0` on an exception. `askPersist`: `navigator.storage.persist()` in `try/catch`, `false` on an exception.

- [ ] **Step 4: Add the pytest bridge**

In `tests/test_page_logic.py` — a second test next to the first:

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
Expected: `2 passed` and `583 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/cache.js page/cache.test.mjs tests/test_page_logic.py
git commit -m "Модуль кеша: ролик качается потоком, база хранит его целиком"
```

---

### Task 3: The “cache” switch on the stage

**Files:**
- Modify: `page/index.html` (`#stage`, around `#fullscreen`), `page/style.css` (after `#fullscreen`), `page/app.js` (import, `els`, state, functions, `boot`, `leaveStage`, `handleMessage`)
- Test: `tests/test_page_wiring.py`

**Interfaces:**
- Consumes: task 1 (`copyState`, `percent`, `copyVerdict`, `formatBytes`, `SPARE_BYTES`, `mergeCues`), task 2 (`openDb`, `putVideo`, `downloadVideo`, `freeBytes`, `askPersist`), the existing `api`, `note`, `paint`, `send`, `unwatchMessage`, `POLL_VIDEO_MS`.
- Produces (for tasks 4 and 5):
  - `let db = null`, `let savedVideos = new Map()`, `let copy = null`, `let copyFailure = null`, `let objectUrl = null`, `let offline = false`, `let daemonAlive = true`, `let persistAsked = false`;
  - `async function openCache()` — open the database and fill `savedVideos` (`listVideos`);
  - `function drawCacheButton()` — the button text from `copyState`;
  - `function toggleCache()`, `async function startCopy()`, `function cancelCopy()`, `async function finishCopy(blob, size)`;
  - `let follow = null` — a saved copy whose subtitles are still being appended;
  - `function rememberCues(message)` — cues into the in-progress and into the saved copy;
  - `function followSoon()` — deferred writing of saved subtitles;
  - `function filePath(videoId) -> string` — `/v1/videos/{id}/file?token=…`.

- [ ] **Step 1: Write the failing tests**

At the end of `tests/test_page_wiring.py`:

```python
def test_the_stage_has_a_cache_switch_next_to_fullscreen():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = re.search(r'<div id="stage-bar">(.*?)</div>', html, re.S)

    assert bar is not None
    assert 'id="cache"' in bar.group(1)
    assert 'id="fullscreen"' in bar.group(1)
    # The cache is off by default: without a press nothing settles on the device.
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
    # A second press during copying cancels it and removes the partially downloaded data.
    assert "controller.abort()" in source
    assert "copy = null" in source


def test_the_base_is_named_in_one_place():
    # The database and table names live in cache.js; the page does not repeat them, otherwise
    # a single rename would have to be looked for in two files.
    cache = (ROOT / "page" / "cache.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'DB_NAME = "yt-trans"' in cache
    assert '"yt-trans"' not in source
    assert "openDb(" in source


def test_the_copy_keeps_the_rolled_video_subscribed():
    # Left the stage with copying in progress — subtitles must keep being appended,
    # otherwise what was saved would break off at the moment of leaving.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function leaveStage"):source.index("// --- Запуск")]

    assert body.index("copy") < body.index("unwatchMessage")


def test_the_copy_catches_cues_its_own_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleMessage"):source.index("function takeSnapshot")]

    assert "rememberCues(" in body
    assert body.index("rememberCues(") < body.index("isForVideo(")


def test_the_saved_cues_keep_arriving_after_the_picture_is_stored():
    # The translation runs longer than the download: by the end of the translation the device
    # must hold both the picture and all the lines, not only those that made it by the end of the download.
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
Expected: FAIL on the seven new tests (`id="stage-bar"` not found, `els.cache` absent).

- [ ] **Step 3: Add the markup and styles**

In `page/index.html`, instead of the lone `<button id="fullscreen" …>` in `#stage`:

```html
    <!-- Top left: ⛶ and the cache switch. The video stays at hand while the
         lines go on the right. "Cache" is off by default: without a press
         nothing settles on the device. -->
    <div id="stage-bar">
      <button id="fullscreen" type="button" aria-label="во весь экран"
        title="во весь экран">⛶</button>
      <button id="cache" type="button" aria-pressed="false"
        aria-label="копировать ролик на устройство"
        title="копировать ролик на устройство">кеш</button>
    </div>
```

In `page/style.css` replace the `#fullscreen` rule with a bar: `#stage-bar { position: absolute; left: 12px; top: calc(12px + env(safe-area-inset-top, 0px)); display: flex; gap: 8px; }`, and `#stage-bar button { padding: 8px 10px; font-size: 15px; line-height: 1; background: rgba(20, 20, 24, .82); }`. Move the comment above the rule into the new one, explaining why the bar is at the top left.

- [ ] **Step 4: Wire the switch in `page/app.js`**

In the import from `./logic.js` add `SPARE_BYTES, copyState, copyVerdict, formatBytes, mergeCues, percent`; in `els` — `cache: document.getElementById("cache")`; in the constants — `const LINKS_TIMEOUT_MS = 3000;`.

In the `state` literal add `link: { id: "", title: "" }` and `duration: 0`: the copy records which link the video came from and how long it is, and `play` in task 4 fills them. Without default values task 3 would access `undefined.id`.

The order of copying:

```js
const filePath = (videoId) =>
  `/v1/videos/${encodeURIComponent(videoId)}/file?token=${encodeURIComponent(token)}`;

/** The size of the ready mp4: HEAD does not return a body, but it does have Content-Length. */
async function fileSize(path) {
  try {
    const response = await fetch(path, { method: "HEAD" });

    return Number(response.headers.get("Content-Length") || 0);
  } catch {
    // The size was not asked — not a reason to refuse: the verdict will be "unknown".
    return 0;
  }
}

/** Waits for the ready mp4 the way the stage waits for it; -1 — the daemon said there will be none. */
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

1. `if (!db) await openCache();` — there is no database, say `note("браузер не дал хранилище")` and exit.
2. Set up `copy = { videoId, controller: new AbortController(), waiting: true, seen: 0, total: 0, cues: new Map(cuesByIndex), linkId: state.link.id, linkTitle: state.link.title, title: state.title, duration: state.duration, language: state.language }`; `copyFailure = null`; `drawCacheButton()`.
3. Once per page lifetime: `if (!persistAsked) { persistAsked = true; askPersist().catch(() => {}); }`.
4. `const size = await waitForFile(videoId)`; if `copy` is no longer the same one — exit; if `size < 0` — `copy = null`, `drawCacheButton()`.
5. `const free = await freeBytes()`; on `copyVerdict(size, free) === "tight"` — `note(\`не хватает места: нужно ещё ${formatBytes(size + SPARE_BYTES - free)}, свободно ${formatBytes(free)}\`)`, `copy = null`, `drawCacheButton()`, exit.
6. `copy.waiting = false; copy.total = size; drawCacheButton();`
7. `await downloadVideo(filePath(videoId), { signal: copy.controller.signal, onProgress })`; in `onProgress` recompute `percent(copy.seen, copy.total)` and call `drawCacheButton()` only when the percentage changed.
8. Success — `finishCopy(blob, size)`. Error — remember whether it was cancelled, zero out `copy`, on cancellation just `drawCacheButton()`, otherwise `copyFailure = videoId`, `note(\`копия не сохранилась: ${error.message}\`)`, `drawCacheButton()` and, if the viewer is no longer on this video, `send(unwatchMessage(videoId))`.

`finishCopy(blob, size)`: build the record `{videoId, linkId, linkTitle, title, duration, size, savedAt: Date.now(), language, cues: mergeCues([], [...copy.cues.values()]), blob}`, `await putVideo(db, record)` (a write error — the same “копия не сохранилась” path), then `copy = null`, `savedVideos.set(videoId, record)`, `drawCacheButton()`, and redraw the list if the home is visible (task 5 will add marks to the list; before it, `drawLinks(knownLinks, "")` is enough). Then a fork — the subtitles are not over yet:

- the viewer is on this video — `note("сохранено на устройстве")`, `switchToCopy(record)` (in task 4) and `follow = { videoId, record, timer: null }`: the translation goes on, and the saved subtitles must keep being appended while the viewer is here;
- the viewer is no longer on it — `send(unwatchMessage(videoId))`.

`toggleCache()`: no `state.videoId` — exit; this video is being copied — `cancelCopy()`; a copy of this video already exists — nothing (`“кеш ✓” does nothing`); otherwise — if another copy is in progress, `cancelCopy()`, then `startCopy()`.

`cancelCopy()`: remember `{videoId, controller}`, `copy = null`, `controller.abort()`, unsubscribe if the viewer is not on this video, `drawCacheButton()`.

`drawCacheButton()`: `mine = copy && copy.videoId === state.videoId ? copy : null`, `saved = savedVideos.get(state.videoId)`, the text `copyState({ waiting: Boolean(mine && mine.waiting), active: Boolean(mine), percent: mine ? percent(mine.seen, mine.total) : 0, size: saved ? saved.size : 0, failed: copyFailure === state.videoId })`, `aria-pressed` — `"true"` when there is a copy or copying is under way.

Cues go in two directions — into the in-progress copy and into the already saved one:

```js
const FOLLOW_MS = 5000;

let follow = null;   // { videoId, record, timer } — the saved one whose subtitles are being appended

function rememberCues(message) {
  if (message.videoId === undefined || message.videoId === null) {
    return;   // a job event: it does not relate to a video
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

/** Writing a whole mp4 is an expensive business; we append subtitles no more than once every five seconds. */
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

The first line of `handleMessage` — `rememberCues(message);` before the `isForVideo` check: equality by `videoId` is more honest here than `isForVideo`, because an event without `videoId` does not relate to the copy.

`leaveStage`: `const copying = copy && copy.videoId === state.videoId;`; if `follow` is of this video — clear the timer, write immediately (`putVideo(db, follow.record).catch(() => {})`) and `follow = null`; unsubscription — under the condition `if (state.videoId && !copying)`.

`boot`: `els.cache.addEventListener("click", toggleCache);`

Re-copying the same video (after a cancel or an error) simply writes a record with the same `videoId` over the previous one — a separate “replace” is not needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `7 passed` among the new ones and `590 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/index.html page/style.css page/app.js tests/test_page_wiring.py
git commit -m "На сцене появился переключатель «кеш»: копия начинается с готового mp4"
```

---

### Task 4: The copy as the playback source

**Files:**
- Modify: `page/app.js` (`play`, `prepareVideo`, `openLink`, `leaveStage`, `createJob`)
- Test: `tests/test_page_wiring.py`

**Interfaces:**
- Consumes: task 3 (`savedVideos`, `db`, `copy`, `follow`, `objectUrl`, `daemonAlive`, `rememberCues`, `drawCacheButton`), task 1 (`playSource`, `mergeCues`), task 2 (`getVideo`, `deleteVideo`).
- Produces:
  - `play(video, link = {})` — instead of `play(videoId, title)`; `state.link = {id, title}` and `state.duration`;
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
    # Spec: without a copy the stage does not open offline at all — the message stays
    # on the home page, so that a black screen does not pretend to be a breakdown.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function play("):source.index("async function createJob")]

    assert "демона нет, этого ролика на устройстве нет" in body
    assert body.index("playSource(") < body.index("els.home.hidden = true")


def test_an_evicted_copy_does_not_lie_in_the_list():
    # The browser has the right to evict a Blob: the record is in the database, but there is nothing to play.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "savedVideos.delete(" in body
    assert "deleteVideo(" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -10`
Expected: FAIL — `URL.createObjectURL` is not in `app.js`.

- [ ] **Step 3: Implement the copy as the source**

`play(video, link = {})`: at the start — if `playSource(savedVideos.has(video.videoId), daemonAlive) === "none"`, then `drawLinks(knownLinks, "демона нет, этого ролика на устройстве нет")` and exit without opening the stage. Then the previous fields plus `link: { id: link.id || "", title: link.title || "" }`, `duration: Number(video.duration) || 0`; `copyFailure = null`; `drawCacheButton()`. Both calls in `openLink` — `play(videos[0], { id, title })` and `play(video, { id, title })`.

Order in the file: `playLocal` sits right above `switchToCopy` — the test cuts out its body by these two names.

`switchToCopy(record, { resume = 0, autoplay = false } = {})`: `from = resume || els.video.currentTime`, `wasPlaying = !els.video.paused || autoplay`; revoke the previous `objectUrl`; `objectUrl = URL.createObjectURL(record.blob)`; on `loadedmetadata` (`{once: true}`) — `currentTime = from` if `from > 0`, and `play()` if `wasPlaying` (swallow the error: outside a press the browser has the right to refuse); assign `src`. The parameter is named `autoplay`, not `play`, so as not to shadow the `play` function.

`seedCues(record)`: if `record.cues` is empty — exit; otherwise clear `cuesByIndex`, lay out the saved ones by `cue.i`, `state.cues` — the sorted list, `state.language = record.language || state.language`, `state.translated = countTranslated()`, `paintedKey = null`, `render()`, `paint()`. A snapshot from the daemon will later replace them entirely — which is what is wanted: the live ones are fuller.

`playLocal(videoId)`: if `!savedVideos.has(videoId) || !db` — `false`; `record = await getVideo(db, videoId)`; if `videoId !== state.videoId` — `false`; if there is no record (the browser evicted the Blob) — `savedVideos.delete(videoId)`, `deleteVideo(db, videoId).catch(() => {})`, `drawCacheButton()` and `false`; otherwise `switchToCopy(record, { resume: positionFor(positions, videoId), autoplay: true })`, `seedCues(record)`, on `!daemonAlive` — `note("демона нет — играю сохранённое")`, `true`.

`prepareVideo`: after `stopVideoPoll()` — `if (playSource(savedVideos.has(videoId), daemonAlive) === "copy" && await playLocal(videoId)) return;`, then as now. In the `catch` of the first `POST` — the same line `"демона нет, этого ролика на устройстве нет"` when `playSource(false, daemonAlive) === "none"` (the daemon died after entering the stage), otherwise `error.message`.

`createJob` `catch` — on `!daemonAlive && savedVideos.has(videoId)` say `"демона нет — играю сохранённое"`, otherwise `error.message`.

`leaveStage`: before removing `src` — revoke `objectUrl` and zero it out.

`finishCopy`: add `switchToCopy(record)` to `note("сохранено на устройстве")` without losing the position (`resume` is not needed: the function takes `currentTime`), so that the copy is picked up mid-viewing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `594 passed`.

- [ ] **Step 5: Commit**

```bash
git add page/app.js tests/test_page_wiring.py
git commit -m "Сохранённый ролик играет с устройства, а не с демона"
```

---

### Task 5: The home list without the daemon

**Files:**
- Modify: `page/app.js` (`api`, `connectSocket`, `start`, `loadLinks`, `openLink`, `drawLinks`, `consumeShare`, the new `savedForLink`, `savedRows`, `drawEpisodes`, `dropCopy`), `page/style.css` (`#links li`)
- Test: `tests/test_page_wiring.py`

**Interfaces:**
- Consumes: task 2 (`getMeta`, `putMeta`, `listVideos`, `deleteVideo`), task 4 (`play(video, link)`), `offline`, `savedVideos`, `daemonAlive`, `LINKS_TIMEOUT_MS`.
- Produces: `function savedForLink(linkId) -> object[]`, `function savedRows(records) -> HTMLElement[]`, `function drawEpisodes(title, videos)`, `async function dropCopy(videoId)`; `let currentEpisodes = null`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_home_stays_with_the_saved_copy_when_the_daemon_is_gone():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function loadLinks"):source.index("function savedForLink")]

    # A silent daemon is not a refusal: the previous list is drawn at once, while the request
    # to the daemon gets a short deadline and replaces it when it answers.
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
    # With a dead daemon, sharing from YouTube must not leave an empty screen:
    # what was saved with marks matters more than the failed addition.
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
Expected: FAIL on the four new tests.

- [ ] **Step 3: Learn when the daemon is gone**

`api`: wrap `fetch` in `try/catch` — on an exception `daemonAlive = false` and rethrow the error; after a response `daemonAlive = true`. A comment: 401 is a live daemon with someone else's token, offline has nothing to do with it.

`connectSocket`: on `open` — `daemonAlive = true`, on `close` — `daemonAlive = false` before reconnecting.

`start` becomes `async` and waits for the database before the list:

```js
async function start() {
  await openCache();
  connectSocket();
  await loadLinks();
  consumeShare();
}
```

`openCache()`: `db = await openDb()` in `try/catch` (`null` on refusal), then `savedVideos` from `listVideos(db)` (if the database exists). Called from `start` and from `startCopy`.

- [ ] **Step 4: Draw the home without the daemon**

`loadLinks`: first take the previous list from the database and draw it in the usual form, then ask the daemon — with a short deadline. A failed request must not leave the person in front of an empty screen for all three seconds.

```js
async function loadLinks() {
  const last = db ? await getMeta(db, "links") : null;

  if (last && last.length) {
    // The previous list is already there — we draw it at once, not after the daemon answers.
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

The test above requires `getMeta` to stand in this function before `api(`: it cuts the body of `loadLinks` by the name `savedForLink`, so that function goes right after `loadLinks`.

`savedForLink(linkId)`: saved records with this `linkId`, freshest first by `savedAt`.

`savedRows(records)`: per record — `<li class="saved">` with a button `\`${episodeLabel(record)} · ${cacheLabel(record.size)}\`` (leads to `play(record, { id: record.linkId, title: record.linkTitle })`) and a button `"удалить"` (`dropCopy(record.videoId)`); `episodeLabel` already exists in `app.js` — do not write a second one.

`drawLinks`: inside the loop over the visible links, after the two buttons — the saved episodes of its link as a separate list under the row:

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

An `<li>` cannot be nested inside an `<li>` — hence the `ul` wrapper, which is what the `#links li > ul` rule below is designed for. In the `!items.length` branch: on `offline && savedVideos.size` — `els.links.append(...savedRows([...savedVideos.values()]))` (here `#links` is the list itself, no wrapper is needed) and exit; the empty caption on `offline` — `"демона нет, на устройстве ничего не сохранено"`, otherwise the previous `"пока пусто, добавь ссылку"`.

`openLink`: move episode drawing into `drawEpisodes(title, videos)` (remembers `currentEpisodes = {title, videos}`, sets the heading, clears the list, when there are no videos — `"в ссылке нет роликов"`, on a saved video the mark `· ${cacheLabel(saved.size)}` and a `"удалить"` button, at the end `els.episodes.hidden = false`). In `catch` — on `daemonAlive` as now, otherwise `const saved = savedForLink(id); drawLinks(knownLinks, saved.length ? "демона нет — в списке сохранённые ролики этой ссылки" : "демона нет, роликов этой ссылки на устройстве нет");`.

`dropCopy(videoId)`: if `follow` is of this video — clear the timer and zero it out; `if (db) await deleteVideo(db, videoId)`, `savedVideos.delete(videoId)`, `if (currentEpisodes) drawEpisodes(currentEpisodes.title, currentEpisodes.videos)`, `drawLinks(knownLinks, "")`.

`consumeShare` `catch`: on `!daemonAlive` — `drawLinks(knownLinks, "демона нет — ссылку из YouTube добавить нельзя")`, otherwise the previous line about the failure.

`addLink` and `dropLink` in `catch` — the same fork: on `!daemonAlive` draw `drawLinks(knownLinks, "демона нет — ссылку добавить нельзя")` / `"демона нет — ссылку удалить нельзя"`, otherwise the previous lines. Without this, offline any failed press would take everything saved off the screen.

`style.css`: `#links li` gets `flex-wrap: wrap`; a new rule `#links li > ul { flex-basis: 100%; }` and `#links li > ul { list-style: none; margin: 0; padding: 0 0 0 12px; display: flex; flex-direction: column; gap: 8px; }` — saved episodes stand under their link.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `598 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/app.js page/style.css tests/test_page_wiring.py
git commit -m "Без демона домашний список показывает сохранённое"
```

---

### Task 6: The page shell in the service worker

**Files:**
- Create: `page/sw.js`
- Modify: `page/app.js` (`registerWorker`, `boot`, `loadLinks`)
- Test: `tests/test_page_wiring.py`, `tests/test_page_serving.py`

**Interfaces:**
- Consumes: the shell files that the worker itself lists (`/`, `/app.js`, `/logic.js`, `/cache.js`, `/style.css`, `/manifest.json`, `/icon-192.png`, `/icon-512.png`).
- Produces: registration of `/sw.js` from the page root.

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
    # The daemon's live data and the certificate are not the shell.
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
    # The worker lies at the root of its scope, so Service-Worker-Allowed
    # is not needed, and the type must be javascript: otherwise the browser will refuse.
    response = await client.get("/sw.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py tests/test_page_serving.py -q 2>&1 | tail -8`
Expected: FAIL — `page/sw.js` cannot be read.

- [ ] **Step 3: Write `page/sw.js`**

```js
// The page shell without the daemon. While the computer is on, the page comes from
// it and updates at once; when it is off — from this cache.
const VERSION = "yt-trans-1";
const SHELL = [
  "/", "/app.js", "/logic.js", "/cache.js", "/style.css",
  "/manifest.json", "/icon-192.png", "/icon-512.png",
];
```

`install` — `cache.addAll(SHELL)`, then `skipWaiting()`. `activate` — delete all caches except `VERSION`, then `clients.claim()`.

`fetch`: only `GET` of its own origin; `/v1/` and `/ca.crt` are passed through without `respondWith` (these are answers “for now”). The rest — the network, on success a copy into the cache and the response out, on failure — `caches.match(request)`, for navigation — the fallback `caches.match("/")`, otherwise `new Response("", { status: 504 })`.

A comment in the file: the version name changes together with the set of files — the old cache is then deleted on activation.

- [ ] **Step 4: Register it from the page**

In `page/app.js`:

```js
const WORKER_KEY = "yt-trans-no-worker";

let workerMissing = false;

/** The shell on the device: without it the page opens only with a live daemon. */
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

Say this once per browser lifetime: in `loadLinks` before the successful drawing —

```js
  let warning = "";

  if (workerMissing && !store(WORKER_KEY)) {
    store(WORKER_KEY, "1");
    warning = "браузер не сохранит страницу для офлайна — нужен https и современный браузер";
  }

  drawLinks(data.links || [], warning);
```

The call to `registerWorker()` — in `boot()` before the branch with the token, so that registration happens on the token screen too. `boot` becomes `async` and **awaits** it: `loadLinks` asks about `workerMissing` immediately, and without awaiting, the warning about the missing worker would not be shown at all. The tail of `boot()` — `await registerWorker();` before `if (token)`, while further down the file the previous `boot();` remains.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py tests/test_page_serving.py -q 2>&1 | tail -3 && .venv/bin/python -m pytest -q 2>&1 | tail -1`
Expected: `601 passed`.

- [ ] **Step 6: Commit**

```bash
git add page/sw.js page/app.js tests/test_page_wiring.py tests/test_page_serving.py
git commit -m "Страница открывается без демона: сервис-воркер держит оболочку"
```

---

### Task 7: Tell the owner how it works

**Files:**
- Modify: `docs/tablet.md` (a new section after “Диск”, a row in the “Если не работает” table)

**Interfaces:**
- Consumes: the behaviour of tasks 3–6.

- [ ] **Step 1: Write the section**

The section `## Без компьютера` right after “Диск”: the “кеш” switch at the top left on the stage, off by default; copying starts as soon as the mp4 is ready and goes while the page is open (closed the app or the screen slept — it stopped, press again); the device holds the video and the subtitles that had time to be translated; what is saved is marked in the list `на устройстве · 400 МБ`, deleted with the `удалить` button; the copy plays online too — a connection break no longer gets in the way; the offline list unfolds the saved videos under their links, the remaining links give a message that the daemon is gone; the cache lives in the browser's storage on the tablet, clearing the site data wipes it, and it fully takes up disk space — not GiB of the daemon's cache.

In the “Если не работает” table — two rows: “демона нет, этого ролика на устройстве нет” → “ролик не сохранён на планшете” → “включить „кеш“ при следующем просмотре с живым демоном”; “копия не сохранилась: …” → “кончилось место или браузер отказал” → “освободить место, удалить ненужное кнопкой „удалить“”.

In the “Ссылки” section the phrase “своих кнопок на сцене три” will become outdated: there will be four buttons (RU, CNR, ⛶, “кеш”). Fix it there too, without leaving a count that would drift with the next button: “свои кнопки на сцене — выключатели строк RU и CNR, ⛶ и „кеш“”.

- [ ] **Step 2: Check the prose mentions the real names**

Run: `grep -n "кеш\|на устройстве\|удалить" docs/tablet.md`
Expected: the new section and both table rows are visible.

- [ ] **Step 3: Commit**

```bash
git add docs/tablet.md
git commit -m "В docs/tablet.md рассказано про просмотр без компьютера"
```

---

### Task 8: A check in a real browser

**Files:** nothing is created; what is found goes back into the tests of its own task.

**Interfaces:** Consumes: everything above.

- [ ] **Step 1: Bring up the page and the daemon**

Run: `.venv/bin/python -m uvicorn helper.app:app --host 127.0.0.1 --port 8899` in the background and open `http://127.0.0.1:8899/` in Chromium via Playwright.

- [ ] **Step 2: Copy a video and see it in the database**

Take a saved video, wait for the ready mp4, press “кеш”; make sure the button goes “кеш…” → “кеш 37%” → “кеш ✓ · N МБ”, and a record with `blob` and `cues` has appeared in IndexedDB (`yt-trans` → `videos`). Reload the page — “кеш ✓” is still there.

- [ ] **Step 3: Turn the daemon off and open the page again**

Stop uvicorn, reload the page. Expected: the page opens (the service worker), the list shows the saved videos with marks and `удалить`. An unsaved video is pressed and gives “демона нет, этого ролика на устройстве нет”.

- [ ] **Step 4: Watch the saved one**

Press a saved video. Expected: it plays, seeks, the subtitles are drawn from the saved ones; the status says “демона нет — играю сохранённое”.

- [ ] **Step 5: Write down what was found**

What is found — a new task with a test in the same form as above (first a failing test, then the fix); if nothing was found, say so in the plan execution report.

---

## Order and dependencies

Tasks go strictly by number: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. Task 3 will not build without 1 and 2 (there is no `copyState` and `downloadVideo`), task 4 without 3 (there is no `savedVideos`), task 5 without 4 (there is no `play(video, link)`), task 6 without 2 (the shell lists `/cache.js`). Within a task the steps go in the order RED → GREEN → commit, and each commit leaves the test suite green.
