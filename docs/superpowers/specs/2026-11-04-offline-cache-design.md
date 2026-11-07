# Offline video cache on the tablet

Design. 2026-11-04.

## Why

The daemon lives on the computer and works while the computer is on. The tablet lies in
another room, and the owner wants to watch already translated videos when the
computer is off — in the evening, on the road, whenever. Today this is impossible:
the page is served by the daemon, and the subtitles come over a socket from it as well. No daemon — no
page either.

So the tablet must get its own copy: the page, the video and both
subtitle lines. The computer remains the translation engine; the tablet is also a store
of what has already been translated.

## Scene

The owner presses “Share” in the YouTube app, our page opens,
the daemon starts translating. On the scene there is a “cache” toggle, off by default. The owner turns it on — the video starts copying to the tablet.
Copying does not wait for the translation to end: the finished mp4 appears long before the last
translated phrase, so it is the mp4 that is downloaded, and the subtitles are appended as they arrive.
By the end of the translation the device holds both the picture and the subtitles. From then on the video plays
from the copy.

In the evening the computer is off. The owner opens the app on the tablet — sees
the same list, with saved videos marked “on device”. Presses —
watches with subtitles. If he presses a video that is not on the device, he
sees an honest message that there is no daemon and this video is not saved.

## Owner's decisions

1. Caching is enabled manually: a “cache” toggle on the scene, off by default.
   Without explicit permission nothing settles on the device.
2. Copying starts as soon as the mp4 is ready, without waiting for the end of the translation.
3. Nothing is deleted automatically. Doesn't fit — copying does not start, and
   the screen shows a number: how much must be freed. Deleting a saved one is manual.
4. The offline list is the same list as online, only with marks on the saved ones.
5. Pressing a non-saved video offline — a check and a message that it is not
   on the device and the daemon is unavailable.
6. The cache is about the current video, not a global setting.

## My decisions (to be checked on reading)

- The “cache” toggle sits top left next to ⛶ and shows the state itself:
  “cache” (off), “cache 37%” (copying), “cache ✓ · 400 MB” (saved). Pressing again
  during copying cancels it and removes the partially downloaded data.
- A full copy is the preferred playback source always, not only
  offline: if it exists, the video plays from the tablet, and a connection drop with the daemon no longer affects
  viewing.
- When copying finishes during viewing, the player switches to
  the copy without losing position, pause or lines.
- Subtitles are saved as they arrive, not at the end. So offline will show what
  managed to get translated; beyond that there are no lines — and this is stated honestly in the status.
- While the toggle is off, nothing is saved, including subtitles: a subtitle cache
  without asking is the same cache. Turned on mid-video — everything that has arrived in the browser by that
  moment is saved.
- Mark in the list: “on device” and the size (“on device · 400 MB”) next to
  the existing “ready/preparing” mark; a saved video has a “delete” button.
- Free space is asked of the browser (`navigator.storage.estimate()`) before
  copying starts, with a 200 MB margin — otherwise the quota would run out mid-write.
- On the first copy the app asks the browser for persistent storage
  (`navigator.storage.persist()`), so that the cache is not evicted when the tablet runs short of space.
- Re-copying a video replaces the previous copy.
- Copying lives in the open page: went from the scene to the list — it
  continues; closed the app or the screen slept — it stopped, and on the next
  visit with a live daemon it will start over. There is no background download after closing in this
  design.
- The size before copying starts is learned from the file itself with a short HEAD request:
  `/v1/videos/{id}` returns only state and a message, and going into the daemon for a new
  field for the sake of one number is pointless. The body is not sent on HEAD, so the promise
  “doesn't fit — we don't start” holds.
- An enabled “cache” is not remembered: after the app is closed the video is not
  resumed by itself, it must be pressed again. Otherwise 400 MB would go without a fresh
  request.
- While copying is running, the video stays subscribed to cues, even if
  the viewer went to the list: otherwise the saved subtitles would break off at the moment of leaving.
  The subscription is removed together with the end of copying.
- Pressing “cache ✓” does nothing: deleting a saved one is only by the button in
  the list, so that an expensive file cannot be wiped with one wrong touch.
- The offline home doesn't wait silently: if a saved list exists, it is drawn immediately,
  and the request to the daemon goes with a short timeout (about three seconds) and replaces the list
  when it answers; if it didn't answer — we show the offline state.

## Non-goals

- Translating offline. It requires a model and a GPU — that is the computer's engine.
- Adding links and downloading new videos offline.
- Syncing caches between devices or browsers.
- Automatic deletion, limits and eviction by age.
- Resuming a partially downloaded mp4 after the app is closed.
- Caching a partial mp4: only a whole file lands on the device.

## Constraints

- The page currently comes entirely from the daemon (`StaticFiles` on `/`). Offline without
  a service worker is impossible — it is not an optimization but a condition of the task.
- A service worker requires https; it already exists, with its own CA installed on the tablet.
- 26 minutes of video is about 400 MB. The storage quota in Chromium is a share of free
  disk; the browser may evict data when space runs short, `persist()` only
  lowers that probability.
- The cache belongs to a specific browser on a specific tablet: clearing site
  data destroys it.
- A project without a build: ES modules, pure logic in `page/logic.js` (checked
  by `node --test`), DOM and network in `page/app.js`, the wiring is checked by pytest.
- The daemon serves `/v1/*` only with a token; `page/*` is public. The service worker
  lies next to the page and is served by the same `StaticFiles`.

## Device

### Service worker: a page without the daemon

A new `page/sw.js`, registered from `app.js`. On install it puts the shell into
Cache Storage: `/`, `/app.js`, `/logic.js`, `/style.css`,
`/manifest.json`, `/icon-192.png`, `/icon-512.png`. Requests to `/v1/*` bypass
it — these are live data, it doesn't cache them.

The serving rule is network, and on failure cache (network-first): while the daemon is alive,
the page updates right after deployment; without the daemon the last
saved shell is served. The shell is updated on every successful network response.

The cache version is a constant in `sw.js`; when the file set changes it changes, and
the old cache is deleted on activation.

### Storage: IndexedDB

Database `yt-trans`, two tables:

- `videos` — key `videoId`, fields: `videoId`, `linkId`, `linkTitle`, `title`,
  `duration`, `size`, `savedAt`, `blob` (the mp4 itself), `cues` (array of cues in the
  form in which the daemon sends them), `language`.
- `meta` — key `links`: the last known response of `/v1/links`; key `episodes`
  — video lists of links that were opened online (`linkId` → `{title,
  videos}`). This is enough for the offline home to look like the online one.

The copy is put into the database only whole: while the mp4 is downloading, it lives as a Blob in
memory/Chromium's disk blob storage and enters the database in one action at
the end. An interrupted copy leaves nothing in the database.

Chromium keeps large Blobs on disk, memory does not grow; `Blob.slice` is cheap,
so playback from a copy requires neither a service worker nor Range
handling.

### Copying

1. Before start: `navigator.storage.persist()` (once per page lifetime),
   a HEAD request to `/v1/videos/{id}/file` for `Content-Length` and `estimate()`. If
   free space is less than the file size plus the margin — refusal with a number; the body is not
   downloaded.
2. `fetch('/v1/videos/{id}/file?token=…')`. The size is checked against the `Content-Length`
   of the response; the stream is wrapped in a byte counter that moves the percentages on the
   button.
3. `new Response(counter).blob()` → write to `videos` together with the accumulated
   subtitles. If new subtitles arrived during copying, the current set goes into
   the database.
4. Network error, cancellation by the toggle, page leaving — the partial is discarded,
   the button returns to “off” (or to “error”, if the cause is the network).

### Playback

- A copy exists → play from it: `URL.createObjectURL(blob)` in `src`, the address
  is revoked when leaving the scene or changing the video.
- No copy → as today, `src` to the daemon; offline — a message.
- Copying finished during viewing → `src` changes to the copy,
  `currentTime` and the “paused/playing” state are restored on
  `loadedmetadata`.

### Subtitles

- Each `snapshot`/`cues` event for a video with caching enabled is appended to
  `videos.cues` (`takeCues` already merges by `i`).
- Opening a video: if a copy exists, the subtitles are taken from the database immediately — they do not depend
  on the socket; when the daemon is alive, live events are laid over them (they are more complete).
- Offline with a copy: we draw the saved one, in the status “no daemon — subtitles from the
  saved copy”.
- Offline without a copy: a message, the scene does not open.

### Offline home

- `loadLinks()` failed — we take `meta.links` from the database and draw the same list of links.
- Links whose videos are saved are expanded immediately: under the link row the
  saved episodes are visible, marked `on device` and with a size, with a `delete`
  button. There is no need to press the link to see them — offline it cannot
  answer anyway.
- Links without a saved one remain in the list; pressing them gives a message that there is
  no daemon and no videos of this link on the device.
- If there is no `meta.links` but there are saved videos (for example, the database did not have time
  to be filled with the list), we show them as a flat “on device” list.
- No videos either — “no daemon, nothing saved on the device”.

### How we know there is no daemon

Not `navigator.onLine`: there may be internet and no daemon, and vice versa. The sign is
that the last request to the daemon failed (fetch threw, the socket did not open); it is cleared
by the next successful response. Offline mode is a UI state, not a network fact.

## What the owner sees

- The “cache” button: “cache” / “cache 37%” / “cache ✓ · 400 MB”.
- List row: “Title · 26:00 · ready · on device · 400 MB” and
  “delete” on a saved one. Offline, instead of “ready” — “saved, no daemon”.
- Status on the scene: “copying to device… 37%”, “saved on device”,
  “not enough space: 1.2 GB more needed, 300 MB free”.
- Offline: “no daemon — playing the saved copy”, “no daemon, this video is not
  on the device”.

## Failures and edge cases

- Space ran out before the start — refusal with a number, nothing is created.
- Space ran out during the process (the file grew or the quota was shifted) — copying
  stops, the partial is discarded, a message.
- The app was closed mid-copy — nothing is in the database; on the next
  visit copying does not resume by itself: “cache” is off again, it must be pressed.
- The daemon died during viewing of a non-cached video — the player plays from the buffer
  while it can; offline mode turns on, there is no copy, further — as the browser decided.
- The service worker did not register (http, old browser) — online works
  as before; offline is unavailable, this is said once.
- The browser evicted the copy — on opening it is not found, the row is cleaned, the video
  plays from the daemon if it is alive.
- The same video copied again — the old copy is replaced.
- The cache was turned on when the translation had already partly arrived — everything in the page's
  memory at that moment is saved.

## Verification

Pure functions in `page/logic.js`, `node --test page/logic.test.mjs`:

- `formatBytes` — bytes to “400 MB” / “1.2 GB”, rounding and separator.
- `copyState` — copying state by (enabled, progress, size, error).
- `copyVerdict` — whether it can start by (needed, free, margin).
- `cacheLabel` — the list row mark.
- `mergeCues` — merging saved and incoming cues by `i`.
- `playSource` — copy / daemon / refusal by (copy exists, daemon alive).
- `percent` — percentages without division by zero and capped at 100.

Wiring, `tests/test_page_wiring.py`:

- the “cache” button is in the scene markup, `app.js` looks it up and attaches a handler;
- `index.html` includes the service worker registration (or `app.js` calls it);
- `page/sw.js` exists, lists the shell and does not intercept `/v1/`;
- the database and table names occur both in `app.js` and in the test — in one place.

Serving, `tests/test_page_serving.py`:

- `/sw.js` is served without a token with the JavaScript type and is suitable for registration at
  the root (`Service-Worker-Allowed` is not needed, the file lies in the root of the scope).

Manual check in Chromium via Playwright:

- a local mp4 is served as static, the app copies it, the copy appears in
  IndexedDB;
- the server is killed, the page reloads and opens from the service worker;
- the saved video plays, seeking works, subtitles are drawn;
- a non-saved video gives a message.

Not covered by automated tests and acknowledged as a risk: service worker behaviour when the tab is
closed, real eviction by the browser, memory and time on a 400 MB file,
Android behaviour when the screen sleeps.

## What is left to the builder

- Exact names of functions and modules, the IndexedDB layout by files, the database schema
  version.
- The text of messages and labels — in the spirit of the existing ones, short.
- How exactly the byte counter is arranged and which events send percentages (redraw
  frequency).
- The order of switching `src` without flicker.
- The subtitle storage format: as is (`i`, `start`, `end`, `lines`, `study`,
  `failed`) or only the displayed fields.
