# Tablet page: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daemon serves a page that plays a video from the mp4 it downloaded itself and draws two lines of subtitles — so that a tablet in another room works without the extension and without YouTube's built-in player.

**Architecture:** The daemon downloads a pair of yt-dlp tracks, muxes H.264≤1080p + AAC into mp4 (`-c copy`, `+faststart`), keeps the file in a cache with a cap and serves it with `Range`. The page — static files from the same origin: a normal `<video>`, a shadow tree over it with two lines, a WebSocket for events and `POST /v1/jobs` for translation. The extension does not change by a line: wire fields are only added, there are no new stages.

**Tech Stack:** FastAPI 0.162 / Starlette 1.10 (`FileResponse` with `Range`), httpx in tests, yt-dlp + ffmpeg, a vanilla ES module without a build, node 24 (`node --test`) for the page's pure logic.

**Spec:** `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md`

## Global Constraints

- `extension/` does not change by a single line. No new `Stage` values appear; wire fields are only added (the extension reads them by name, ignores extras).
- The install token is required for all `/v1/*` except `/v1/health`; `?token=` is as legitimate a way as `Authorization`, because `<video>` sends no headers.
- By default the daemon listens on `127.0.0.1`; the network is enabled only by the `[server] host` setting.
- All human-visible strings are in Russian, as in the rest of the repository.
- Page without a build: only `<script type="module">`, no packages and no CDN.
- One video profile for all devices: H.264 no higher than 1080p + AAC in mp4, without transcoding.
- Video cache cap — **20 GiB** (`20 * 1024**3`), eviction by age; the freshest file is never evicted.
- Subtitles are not burned into the file: the mp4 goes to the browser clean, the page draws the lines.
- Tests: `.venv/bin/python -m pytest` (487 green at the start), `asyncio_mode = auto`, `pythonpath = .`.
- Commits in Russian, without the `Co-Authored-By` trailer (the owner's rule).

## Review Focus

1. **Seeking and the first frame.** A person will open a video and drag the slider: the video must seek (206 on `Range`) and start playing before the full download (`+faststart`), otherwise the tablet will show a black screen for a minute. Tests: task 3 (`faststart` in the command), task 5 (206 and `content-range`).
2. **The extension must not break.** New fields go next to the old ones, no name in the snapshot is renamed, no `Stage` value is added. Test: task 6 (the updated key-set check in `tests/test_language_wire.py`).
3. **The daemon does not open itself to the world.** A foreign page still gets 403 over HTTP and 4401 on the socket, even when the daemon listens on `0.0.0.0`. Test: task 1.
4. **The cache does not eat the disk.** Age-based eviction keeps the sum under the cap; a file larger than the cap is not deleted right after download; a partially downloaded file is never served as ready. Test: task 4.
5. **A foreign URL does not reach yt-dlp.** `POST /v1/links` accepts only YouTube addresses (otherwise 400), and a malformed video identifier is rejected before the process starts. Tests: task 7, task 5.

## Builder's decisions: where the plan diverges from the spec

The spec allows changing conditional names (“Names are conditional, the builder may change them”) and leaves the video cache to the builder. Three divergences are chosen deliberately:

- **The endpoints are called `/v1/videos/{id}` and `/v1/links…`**, not `/v1/video/{videoId}` and `/v1/playlists…`: the page and the daemon are written together, and the plural is more honest — a single video has two things (`state` and `file`), while “playlist” does not cover a channel and a single video, which are stored in the same list.
- **Picture readiness lives in `/v1/videos/{id}`, not in job events.** The spec proposed “video preparation state — a separate stage in job events”, but that would tie `Job` to the video registry and add a stage that the extension draws with its badge. The page polls the state every 2 seconds until `src` is set — a dozen requests per video.
- **The file is served only when ready, otherwise 409 with the state phrase.** The page first asks for the state and only then sets `src`, so `<video>` never gets a non-200; the 409 exists for manual diagnostics and for an honest “still preparing” answer, not for playback.

## Not part of the plan

Named so that the implementer does not build beyond and the reviewer does not count it as a miss:

- Prefetching the next episode of a playlist.
- A typed second line `{text, lang, kind}`, the translation checkers' verdict, the “alignment not verified” and “second line not attested” marks — that is the “Translation check” work.
- Language as a property of a saved link (currently the detector decides, and its answer is visible on screen via `languageText`).
- Snapshot fields `duration`, `source`, `studyLang`: the page does not need them, and `study` is empty on a cue when study mode is off — the second line is simply not drawn.
- `GET /v1/jobs/{id}/track?format=srt|vtt|json` — the page does not use it, it is a separate fallback path.
- Rate limiting of job creation: the only one who has the token is the owner, and he is also the only client.
- Cancelling an orphaned yt-dlp that went into `asyncio.to_thread` (a known limitation from the spec).
- https, PWA and Wake Lock: a normal `<video>` over http plays, the screen does not go dark.

---

## Files

**Created:**
- `helper/video.py` — a registry of downloaded videos: states, cap, eviction.
- `helper/links.py` — saved links in `~/.config/yt-trans/links.json`.
- `page/index.html`, `page/style.css` — page markup and styles.
- `page/logic.js` — pure drawing functions (imported both by the page and by node tests).
- `page/logic.test.mjs` — tests of the pure logic for `node --test`.
- `page/app.js` — wiring: token, links, `<video>`, socket, subtitle layer.
- `page/package.json` — `{"type": "module"}`: without it node reads `logic.js` as CommonJS.
- `tests/test_server.py`, `tests/test_page_serving.py`, `tests/test_video_source.py`, `tests/test_video_store.py`, `tests/test_video_endpoints.py`, `tests/test_readiness.py`, `tests/test_links.py`, `tests/test_page_logic.py`, `tests/test_page_wiring.py`.
- `docs/tablet.md` — how to enable and how to use.

**Modified:**
- `helper/config.py` — `ServerConfig`, the `Config.server` field.
- `helper/app.py` — `HOST` from the config, `same_origin`, page serving, video and link endpoints.
- `helper/jobs.py` — `Job.readiness()`, `manager.videos`.
- `helper/sources/ytdlp.py` — `VIDEO_FORMAT`, `fetch_video`, `probe_url`, `playlist` in `_ytdlp_cmd`.
- `tests/conftest.py` — `FakeSocket.close`/`accepted`.
- `tests/test_language_wire.py` — the snapshot key set.
- `config.example.toml`, `README.md`.

---

### Task 1: Network: host from config and its own origin

**Files:**
- Modify: `helper/config.py:134-153, 182-220`
- Modify: `helper/app.py:42, 116-142, 327-338`
- Modify: `config.example.toml`
- Modify: `tests/conftest.py:189-202`
- Test: `tests/test_server.py` (create)

**Interfaces:**
- Produces: `ServerConfig(host: str = "127.0.0.1")`; `Config.server`; `app.HOST: str`; `same_origin(origin: str, host: str | None) -> bool`; `check_origin(origin: str | None, host: str | None = None) -> None`; `FakeSocket.accepted: bool`, `FakeSocket.closed: int | None`, `async FakeSocket.close(code: int = 1000)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Network: the address from the config; the daemon's own page calls it from the same origin."""

import asyncio

from conftest import FakeSocket
from helper import app as helper_app
from helper.config import load


def test_the_daemon_listens_on_the_loop_by_default(tmp_path):
    assert load(tmp_path / "config.toml").server.host == "127.0.0.1"


def test_the_host_comes_from_the_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")

    assert load(path).server.host == "0.0.0.0"


def test_the_same_authority_is_another_spelling_of_ourselves():
    assert helper_app.same_origin("http://127.0.0.1:8756", "127.0.0.1:8756")
    assert helper_app.same_origin("https://Tablet.local:8756", "tablet.local:8756")
    assert not helper_app.same_origin("http://127.0.0.1:8756", "127.0.0.1:9000")
    assert not helper_app.same_origin("chrome-extension://abc", "127.0.0.1:8756")
    assert not helper_app.same_origin("http://127.0.0.1:8756", None)


async def test_the_page_served_by_the_daemon_may_call_the_api(client):
    # 404, not 403: the request reached the handler and found no job.
    response = await client.get(
        "/v1/jobs/zzzzzzzzzzz", headers={"Origin": "http://test"}
    )

    assert response.status_code == 404


async def test_a_foreign_page_still_cannot(client):
    response = await client.get(
        "/v1/jobs/zzzzzzzzzzz", headers={"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "origin не разрешён"


async def test_the_socket_still_refuses_a_foreign_page():
    socket = FakeSocket(helper_app.TOKEN)
    socket.headers = {"origin": "http://evil.example", "host": "127.0.0.1:8756"}

    await helper_app.websocket(socket)

    assert socket.closed == 4401
    assert socket.accepted is False


async def test_the_socket_accepts_the_page_the_daemon_serves(manager):
    socket = FakeSocket(helper_app.TOKEN)
    socket.headers = {"origin": "http://127.0.0.1:8756", "host": "127.0.0.1:8756"}
    socket.handler = asyncio.create_task(helper_app.websocket(socket))

    try:
        await socket.say({"op": "watch", "videoId": "abcdefghijk"})

        assert socket.accepted is True
        assert (await socket.hear())["type"] == "snapshot"
    finally:
        await socket.disconnect()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_server.py -v`
Expected: FAIL — `ImportError: cannot import name 'ServerConfig'` / `AttributeError: same_origin`, as well as `TypeError: check_origin() takes 1 positional argument`.

- [ ] **Step 3: Add `ServerConfig` to `helper/config.py`**

Before `Config`:

```python
@dataclass(frozen=True)
class ServerConfig:
    # The address the daemon listens on. Loopback by default: the daemon issues
    # requests to YouTube at the client's command, and opening it to the network
    # "just in case" is not allowed. A tablet in another room means host = "0.0.0.0"
    # and the token from ~/.config/yt-trans/token as the only protection.
    host: str = "127.0.0.1"
```

In `Config` add `server: ServerConfig = ServerConfig()` as the last field; in `load()` — `server = ServerConfig(**_section(raw, "server", ServerConfig))` and pass `server=server` into `Config(...)`.

- [ ] **Step 4: Implement the origin rule in `helper/app.py`**

`from urllib.parse import urlsplit`, `Request` into the fastapi import. Replace `HOST = "127.0.0.1"` with `HOST = manager.config.server.host` (line 42 comes after `manager = JobManager()` — move `HOST` below it).

```python
def same_origin(origin: str, host: str | None) -> bool:
    """Origin of the page served by the daemon itself: the same host and port as in Host.

    The whole authority is compared, not just the name: the browser sends the port
    in Origin exactly when it is present in the address. This lets through pages
    served by the daemon, and no other site.
    """
    if not host:
        return False

    parts = urlsplit(origin)

    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.lower()


def check_origin(origin: str | None, host: str | None = None) -> None:
    """Let extensions, our own page and requests without Origin (curl) through, but not foreign sites."""
    if origin and not origin.startswith(ALLOWED_ORIGIN_SCHEMES) and not same_origin(origin, host):
        raise HTTPException(status_code=403, detail="origin не разрешён")
```

`auth` gets `request: Request` as its first parameter and calls `check_origin(origin, request.headers.get("host"))`. In `websocket` replace the common guard with two steps:

```python
    origin = socket.headers.get("origin")

    try:
        check_origin(origin, socket.headers.get("host"))
    except HTTPException:
        await socket.close(code=4401)

        return

    token = socket.query_params.get("token")

    if not (token and secrets.compare_digest(token, TOKEN)):
        await socket.close(code=4401)

        return
```

- [ ] **Step 5: Teach `FakeSocket` to be closed and accepted**

In `tests/conftest.py` in `__init__`: `self.closed: int | None = None`, `self.accepted = False`. Replace `async def accept(self) -> None: pass` with `self.accepted = True`; add:

```python
    async def close(self, code: int = 1000) -> None:
        self.closed = code
```

- [ ] **Step 6: Document the setting in `config.example.toml`**

```toml
[server]
# Which address the daemon listens on. Loopback only by default: the daemon issues
# requests to YouTube at the client's command, and opening it to the network just
# like that is not allowed. A tablet in another room requires "0.0.0.0". Then
# the daemon is available to everyone on its network, and its only protection
# is the token from ~/.config/yt-trans/token.
# host = "0.0.0.0"
```

- [ ] **Step 7: Stop trusting forwarded addresses**

In `main()`: `uvicorn.run(app, host=HOST, port=PORT, log_level="warning", proxy_headers=False)`. By default uvicorn trusts `X-Forwarded-For` from loopback and substitutes the caller's address; on loopback that was harmless, on `0.0.0.0` anyone on the network will forge their address in the log. There is no separate test: this is a header-trust setting, not our logic.

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_server.py -v && .venv/bin/python -m pytest -q`
Expected: PASS, the whole suite green (487 + 7).

- [ ] **Step 9: Commit**

```bash
git add helper/config.py helper/app.py config.example.toml tests/conftest.py tests/test_server.py
git commit -m "Адрес демона берётся из [server]; страница своего источника проходит проверку Origin"
```

---

### Task 2: The daemon serves the page

**Files:**
- Create: `page/index.html`
- Modify: `helper/app.py` (end of the module, after `websocket`)
- Test: `tests/test_page_serving.py` (create)

**Interfaces:**
- Produces: `app.PAGE_DIR: Path`; the page at `/`, without a token.
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

```python
"""The tablet page is served by the daemon itself: from the same origin and without a token."""

import httpx


async def test_the_page_is_served_at_the_root(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>yt-trans" in response.text


async def test_the_page_needs_no_token(manager):
    from helper import app as helper_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app), base_url="http://test"
    ) as open_client:
        assert (await open_client.get("/")).status_code == 200


async def test_the_page_does_not_swallow_the_api(client):
    assert (await client.get("/v1/health")).status_code == 200


async def test_an_unknown_page_is_a_plain_404(client):
    assert (await client.get("/no-such-file")).status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_serving.py -v`
Expected: FAIL — 404 on `/`.

- [ ] **Step 3: Create the page stub**

`page/index.html`:

```html
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt-trans — субтитры на планшете</title>
</head>
<body>
<p id="boot">yt-trans</p>
</body>
</html>
```

- [ ] **Step 4: Mount the page after all routes**

In `helper/app.py`: `from pathlib import Path` and `from fastapi.staticfiles import StaticFiles`; next to `TOKEN_PATH`:

```python
PAGE_DIR = Path(__file__).resolve().parent.parent / "page"
```

At the very end of the module, after `websocket` and before `def main()`:

```python
# Mounted last: the routes above were added earlier and win over the mount,
# so /v1/* stays the API, while everything else is looked up as a file. The page
# is public — there are no secrets in it, and it asks the person for the token itself.
app.mount("/", StaticFiles(directory=PAGE_DIR, html=True), name="page")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_serving.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add page/index.html helper/app.py tests/test_page_serving.py
git commit -m "Демон раздаёт страницу планшета с того же источника"
```

---

### Task 3: Download the picture and mux the mp4

**Files:**
- Modify: `helper/sources/ytdlp.py:210-238` (`_ytdlp_cmd`), after `audio_pcm`
- Test: `tests/test_video_source.py` (create)

**Interfaces:**
- Produces: `VIDEO_FORMAT: str`; `fetch_video(video_id: str, target: Path, timeout: float = 1800.0, *, cookies: Path | None = None) -> Path`.
- Consumes: `_check_id`, `_run`, `_ytdlp_cmd`, `YtDlpError` (all in the same module).

- [ ] **Step 1: Write the failing tests**

```python
"""Downloading the picture: H.264 no higher than 1080p, native audio, muxing without transcoding."""

from pathlib import Path

import pytest

from helper.sources import ytdlp

VIDEO = "abcdefghijk"


def fake_download(calls, payload=b"mp4"):
    def run(cmd, timeout):
        calls.append((cmd, timeout))
        template = Path(cmd[cmd.index("-o") + 1])
        template.with_name("video.mp4").write_bytes(payload)

        return ""

    return run


def test_the_video_is_downloaded_as_h264_mp4_with_its_own_sound(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ytdlp, "_run", fake_download(calls))
    target = tmp_path / f"{VIDEO}.mp4"

    ytdlp.fetch_video(VIDEO, target)

    cmd, timeout = calls[0]
    assert cmd[cmd.index("-f") + 1] == ytdlp.VIDEO_FORMAT
    # Sorting starts with res, not with file size: 1080p is the ceiling,
    # lower is ok, higher is not. lang,+size — native audio, not auto-dubbing.
    assert cmd[cmd.index("-S") + 1] == "res:1080,lang,+size"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert cmd[cmd.index("--postprocessor-args") + 1] == "Merger+ffmpeg:-movflags +faststart"
    assert cmd[cmd.index("-o") + 1].endswith("video.%(ext)s")
    assert timeout == 1800.0
    assert target.read_bytes() == b"mp4"
    # No temporary directory is left next to it: it lived inside the target's parent.
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_a_failed_download_leaves_nothing_behind(tmp_path, monkeypatch):
    def run(cmd, timeout):
        raise ytdlp.YtDlpError("HTTP Error 403", transient=True)

    monkeypatch.setattr(ytdlp, "_run", run)
    target = tmp_path / f"{VIDEO}.mp4"

    with pytest.raises(ytdlp.YtDlpError):
        ytdlp.fetch_video(VIDEO, target)

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_bad_id_never_reaches_yt_dlp(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ytdlp, "_run", fake_download(calls))

    with pytest.raises(ytdlp.YtDlpError):
        ytdlp.fetch_video("../../etc/passwd", tmp_path / "x.mp4")

    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_video_source.py -v`
Expected: FAIL — `AttributeError: module 'helper.sources.ytdlp' has no attribute 'VIDEO_FORMAT'`.

- [ ] **Step 3: Add `VIDEO_FORMAT` and `fetch_video`**

Next to `AUDIO_FORMAT`:

```python
# Picture and sound as separate tracks, because YouTube has no pre-muxed 1080p.
# First mp4 with H.264 — it plays everywhere; failing that, any H.264 (in a webm
# container the mux still ends up in mp4 with -c copy); finally, anything, so the
# video does not disappear because of one rare codec.
VIDEO_FORMAT = (
    "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "b[ext=mp4]/b"
)
```

After `audio_pcm` (the `os`, `tempfile` imports — at the top of the module):

```python
def fetch_video(
    video_id: str, target: Path, timeout: float = 1800.0, *, cookies: Path | None = None
) -> Path:
    """Download a video and mux it into mp4 without transcoding. Returns the path to the file.

    The temporary directory is created next to the target, not in /tmp: os.replace is atomic
    only within one filesystem. The finished file appears under its name whole — there is never
    a partially downloaded mp4 under the name that is served to the viewer.
    """
    _check_id(video_id)

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{video_id}-", dir=target.parent) as work:
        _run(
            _ytdlp_cmd(
                "-f",
                VIDEO_FORMAT,
                # First of all the resolution ceiling, then the native audio language and only
                # then the size: -S lang must come before +size, otherwise on a video
                # with auto-dubbing the smallest track will be taken — that is, someone else's.
                "-S",
                "res:1080,lang,+size",
                "--merge-output-format",
                "mp4",
                # Without faststart the index lies at the end of the file, and the browser waits
                # for the full download before showing the first frame and allowing a seek.
                "--postprocessor-args",
                "Merger+ffmpeg:-movflags +faststart",
                "--no-progress",
                "-o",
                str(Path(work) / "video.%(ext)s"),
                f"https://www.youtube.com/watch?v={video_id}",
                warnings=True,
                cookies=cookies,
            ),
            timeout,
        )
        produced = Path(work) / "video.mp4"

        if not produced.exists():
            raise YtDlpError("yt-dlp не оставил склеенного файла", permanent=True)

        os.replace(produced, target)

    return target
```

`--cookies` is not appended to the command: `_ytdlp_cmd` adds it itself if `cookies=` is passed. The `os` and `tempfile` imports — at the top of the module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_video_source.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add helper/sources/ytdlp.py tests/test_video_source.py
git commit -m "Скачивание ролика: H.264 не выше 1080p, родной звук, склейка в mp4 с faststart"
```

---

### Task 4: A registry of downloaded videos with a cap

**Files:**
- Create: `helper/video.py`
- Test: `tests/test_video_store.py` (create)

**Interfaces:**
- Consumes: `helper.env.CACHE_DIR`, `ytdlp.fetch_video`, `ytdlp.YtDlpError`.
- Produces:
  - `VIDEO_DIR: Path`, `VIDEO_CACHE_MAX_BYTES: int`, `VIDEO_TIMEOUT: float = 1800.0`
  - `VideoStatus` with the fields `state: str` (`"absent" | "preparing" | "ready" | "error"`), `message: str`, `path: Path | None` and the method `wire() -> dict` (`{"state", "message"}`)
  - `VideoStore(directory=VIDEO_DIR, max_bytes=VIDEO_CACHE_MAX_BYTES, fetch=fetch_video)` with `path(video_id) -> Path`, `status(video_id) -> VideoStatus`, `ensure(video_id, *, cookies=None) -> VideoStatus`, `async settled(video_id) -> VideoStatus`, `evict() -> int`

- [ ] **Step 1: Write the failing tests**

```python
"""Video cache: readiness by the file on disk, atomic writing, the cap and eviction."""

import asyncio
import os

import pytest

from helper.sources.ytdlp import YtDlpError
from helper.video import VideoStore

VIDEO = "abcdefghijk"


def writer(payload=b"mp4"):
    def fetch(video_id, target, timeout, *, cookies=None):
        target.write_bytes(payload)
        return target

    return fetch


async def test_a_file_on_disk_is_ready_before_anything_is_asked(tmp_path):
    (tmp_path / f"{VIDEO}.mp4").write_bytes(b"mp4")
    store = VideoStore(tmp_path, fetch=writer())

    status = store.status(VIDEO)

    assert (status.state, status.path.name) == ("ready", f"{VIDEO}.mp4")
    assert status.wire() == {"state": "ready", "message": ""}


async def test_the_first_ask_starts_the_download_and_settles_ready(tmp_path):
    calls = []

    def fetch(video_id, target, timeout, *, cookies=None):
        calls.append((video_id, timeout, cookies))
        target.write_bytes(b"mp4")
        return target

    store = VideoStore(tmp_path, fetch=fetch)

    assert store.ensure(VIDEO).state == "preparing"
    assert (await store.settled(VIDEO)).state == "ready"
    assert calls == [(VIDEO, 1800.0, None)]


async def test_a_second_ask_while_preparing_does_not_start_another_download(tmp_path):
    calls = []

    def fetch(video_id, target, timeout, *, cookies=None):
        calls.append(video_id)

    store = VideoStore(tmp_path, fetch=fetch)

    store.ensure(VIDEO)
    store.ensure(VIDEO)
    await store.settled(VIDEO)

    assert calls == [VIDEO]


async def test_a_failure_keeps_the_phrase_and_leaves_no_file(tmp_path):
    def fetch(video_id, target, timeout, *, cookies=None):
        raise YtDlpError("HTTP Error 403")

    store = VideoStore(tmp_path, fetch=fetch)

    store.ensure(VIDEO)
    status = await store.settled(VIDEO)

    assert status.state == "error"
    assert status.message == "HTTP Error 403"
    assert not (tmp_path / f"{VIDEO}.mp4").exists()


async def test_a_retry_after_a_failure_starts_over(tmp_path):
    attempts = []

    def fetch(video_id, target, timeout, *, cookies=None):
        attempts.append(video_id)

        if len(attempts) == 1:
            raise YtDlpError("HTTP Error 403")

        target.write_bytes(b"mp4")

    store = VideoStore(tmp_path, fetch=fetch)

    store.ensure(VIDEO)
    assert (await store.settled(VIDEO)).state == "error"

    assert store.ensure(VIDEO).state == "preparing"
    assert (await store.settled(VIDEO)).state == "ready"
    assert len(attempts) == 2


def age_limited(*, with_account):
    def fetch(video_id, target, timeout, *, cookies=None):
        if cookies is None:
            raise YtDlpError("Sign in to confirm your age", permanent=True, age_restricted=True)

        with_account.append(cookies)
        target.write_bytes(b"mp4")

    return fetch


async def test_an_age_restricted_video_is_retried_with_the_account(tmp_path):
    with_account = []
    store = VideoStore(tmp_path, fetch=age_limited(with_account=with_account))
    account = tmp_path / "cookies.txt"

    store.ensure(VIDEO, cookies=account)
    status = await store.settled(VIDEO)

    assert status.state == "ready"
    assert with_account == [account]


async def test_without_an_account_the_age_limit_is_final(tmp_path):
    store = VideoStore(tmp_path, fetch=age_limited(with_account=[]))

    store.ensure(VIDEO)
    status = await store.settled(VIDEO)

    assert status.state == "error"
    assert "age" in status.message


async def test_the_oldest_video_makes_room(tmp_path):
    for name, stamp in (("old.mp4", 1000), ("new.mp4", 2000)):
        path = tmp_path / name
        path.write_bytes(b"x" * 10)
        os.utime(path, (stamp, stamp))

    store = VideoStore(tmp_path, max_bytes=25, fetch=writer(b"y" * 10))
    store.ensure(VIDEO)
    await store.settled(VIDEO)

    assert not (tmp_path / "old.mp4").exists()
    assert (tmp_path / "new.mp4").exists()
    assert (tmp_path / f"{VIDEO}.mp4").exists()


async def test_a_video_bigger_than_the_whole_cache_is_not_thrown_away(tmp_path):
    store = VideoStore(tmp_path, max_bytes=5, fetch=writer(b"x" * 20))

    store.ensure(VIDEO)
    status = await store.settled(VIDEO)

    assert status.state == "ready"
    assert (tmp_path / f"{VIDEO}.mp4").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_video_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'helper.video'`.

- [ ] **Step 3: Implement `helper/video.py`**

The class keeps three dictionaries: `_states`, `_errors`, `_tasks`. Downloading runs as one task per video under a shared `asyncio.Semaphore(1)`; `ensure` returns the state immediately and never waits. Readiness is determined by the presence of the file, not by process memory: after a daemon restart, what was downloaded stays ready.

The rules that the tests check:

- `status`: file exists → `ready` (even if it remembers nothing about the video); otherwise `self._states.get(video_id, "absent")` and the error text from `_errors`.
- `ensure`: `ready`/`preparing` — return as is; otherwise record `preparing`, drop the old error and create the `_prepare` task.
- `_prepare`: under the semaphore call `fetch(video_id, path, VIDEO_TIMEOUT, cookies=cookies)`; on `YtDlpError` with `age_restricted=True` and non-empty `cookies` — one retry with the account. Success → `ready` + `evict()`. Any exception → `error` + `str(exc)`. In `finally` remove the task from `_tasks`.
- `settled`: wait for the task if there is one and return `status`.
- `evict`: `*.mp4` in ascending `st_mtime`; delete until the sum is greater than `max_bytes`, but do not touch the freshest file and stop if it is the only one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_video_store.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add helper/video.py tests/test_video_store.py
git commit -m "Кэш видео: атомарная запись, потолок 20 ГиБ и вытеснение по давности"
```

---

### Task 5: Video endpoints with Range support

**Files:**
- Modify: `helper/jobs.py:1736-1751` (`JobManager.__init__`)
- Modify: `helper/app.py` (after `/v1/jobs/{video_id}/drop`)
- Test: `tests/test_video_endpoints.py` (create)

**Interfaces:**
- Consumes: `VideoStore` from task 4; `ytdlp.VIDEO_ID`.
- Produces: `manager.videos: VideoStore`; `POST /v1/videos/{video_id}` → `{"state","message"}`; `GET /v1/videos/{video_id}` → the same; `GET /v1/videos/{video_id}/file` → mp4 or 409 with `{"detail": "..."}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Video on the wire: preparation, state and a file that knows Range."""

import pytest

from helper.video import VideoStore

VIDEO = "abcdefghijk"


def writer(payload=b"0123456789"):
    def fetch(video_id, target, timeout, *, cookies=None):
        target.write_bytes(payload)
        return target

    return fetch


async def test_a_video_is_prepared_and_then_served(client, manager, tmp_path):
    manager.videos = VideoStore(tmp_path, fetch=writer())

    started = await client.post(f"/v1/videos/{VIDEO}")
    await manager.videos.settled(VIDEO)
    ready = await client.get(f"/v1/videos/{VIDEO}")
    served = await client.get(f"/v1/videos/{VIDEO}/file")

    assert started.json()["state"] == "preparing"
    assert ready.json() == {"state": "ready", "message": ""}
    assert served.status_code == 200
    assert served.content == b"0123456789"
    assert served.headers["accept-ranges"] == "bytes"


async def test_a_range_request_gets_206_and_a_slice(client, manager, tmp_path):
    manager.videos = VideoStore(tmp_path, fetch=writer())
    manager.videos.ensure(VIDEO)
    await manager.videos.settled(VIDEO)

    response = await client.get(
        f"/v1/videos/{VIDEO}/file", headers={"Range": "bytes=2-4"}
    )

    assert response.status_code == 206
    assert response.content == b"234"
    assert response.headers["content-range"] == "bytes 2-4/10"


async def test_a_file_that_is_not_ready_is_a_409_with_a_phrase(client, manager, tmp_path):
    manager.videos = VideoStore(tmp_path)

    response = await client.get(f"/v1/videos/{VIDEO}/file")

    assert response.status_code == 409
    assert response.json()["detail"]


async def test_a_bad_video_id_is_a_400_before_any_download(client, manager, tmp_path):
    calls = []
    manager.videos = VideoStore(tmp_path, fetch=lambda *a, **k: calls.append(a))

    response = await client.post("/v1/videos/short")

    assert response.status_code == 400
    assert calls == []


async def test_the_file_needs_the_token(manager, tmp_path):
    import httpx

    from helper import app as helper_app

    manager.videos = VideoStore(tmp_path, fetch=writer())
    manager.videos.ensure(VIDEO)
    await manager.videos.settled(VIDEO)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app), base_url="http://test"
    ) as open_client:
        assert (await open_client.get(f"/v1/videos/{VIDEO}/file")).status_code == 401
        assert (
            await open_client.get(
                f"/v1/videos/{VIDEO}/file", params={"token": helper_app.TOKEN}
            )
        ).status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_video_endpoints.py -v`
Expected: FAIL — 404 on `POST /v1/videos/...`.

- [ ] **Step 3: Give the manager its store**

In `helper/jobs.py` — the import `from helper.video import VideoStore` and in `JobManager.__init__` after `self.cache = TrackCache()`: `self.videos = VideoStore()`.

- [ ] **Step 4: Add the endpoints**

In `helper/app.py`: `from fastapi.responses import FileResponse`, `from helper.sources import ytdlp`.

```python
@app.post("/v1/videos/{video_id}", dependencies=[Depends(auth)])
async def prepare_video(video_id: str) -> dict:
    """Start preparing the mp4. Responds immediately; readiness — via a separate GET."""
    if not ytdlp.VIDEO_ID.match(video_id):
        raise HTTPException(status_code=400, detail="неправильный идентификатор ролика")

    return manager.videos.ensure(
        video_id, cookies=manager.config.youtube.cookies_path
    ).wire()


@app.get("/v1/videos/{video_id}", dependencies=[Depends(auth)])
async def video_status(video_id: str) -> dict:
    if not ytdlp.VIDEO_ID.match(video_id):
        raise HTTPException(status_code=400, detail="неправильный идентификатор ролика")

    return manager.videos.status(video_id).wire()


@app.get("/v1/videos/{video_id}/file", dependencies=[Depends(auth)])
async def video_file(video_id: str) -> FileResponse:
    """The whole file or a chunk: Range is parsed by FileResponse, which also sets 206.

    The token is also accepted in the query string (?token=): <video src> sends no headers.
    While there is no file — 409 with the state phrase. The page first asks for the state
    and only then sets src, so <video> never gets a non-200.
    """
    status = manager.videos.status(video_id)

    if status.state != "ready" or status.path is None:
        raise HTTPException(
            status_code=409, detail=status.message or "видео ещё готовится"
        )

    return FileResponse(status.path, media_type="video/mp4")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_video_endpoints.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add helper/app.py helper/jobs.py tests/test_video_endpoints.py
git commit -m "Эндпоинты видео: подготовка, состояние и файл с Range по токену из строки"
```

---

### Task 6: Translation readiness on the wire

**Files:**
- Modify: `helper/jobs.py:442-472` (`snapshot`), `1006`, `1285`, `1510`, `1589`, `1977`
- Modify: `tests/test_language_wire.py:34-38, 103`
- Test: `tests/test_readiness.py` (create)

**Interfaces:**
- Produces: `Job.readiness() -> dict` with the keys `translatedThrough: float | None` (the start of the first untranslated stretch; `None` — no untranslated one remains) and `pending: list[list[float]]` (merged intervals `[start, end]` of untranslated cues in ascending order). The same two keys appear in `snapshot()` and in the `segmented` and `cues` events.

- [ ] **Step 1: Write the failing tests**

```python
"""Readiness on the wire: where the translation ends and which stretches are still empty."""

from conftest import preempt_after_asr
from helper import jobs
from helper.jobs import JobCue, Stage

VIDEO = "ready000001"
CUES = [
    {"start": 0.0, "end": 2.0, "text": "Dobar dan.", "words": []},
    {"start": 2.0, "end": 4.0, "text": "Kako si?", "words": []},
]


def job_with(manager, lines):
    job = manager.get_or_create(VIDEO, "ru", True)
    job.cues = [
        JobCue(index=i, start=float(i * 2), end=float(i * 2 + 2), src=f"c{i}", lines=lines[i])
        for i in range(len(lines))
    ]

    return job


def test_the_snapshot_says_where_the_translation_stops(manager):
    job = job_with(manager, [["А"], [], ["В"], []])

    snapshot = job.snapshot()

    assert snapshot["pending"] == [[2.0, 4.0], [6.0, 8.0]]
    assert snapshot["translatedThrough"] == 2.0


def test_a_fully_translated_job_has_nothing_pending(manager):
    job = job_with(manager, [["А"], ["Б"]])

    assert job.snapshot()["pending"] == []
    assert job.snapshot()["translatedThrough"] is None


def test_a_job_without_cues_has_nothing_pending(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert job.snapshot()["pending"] == []
    assert job.snapshot()["translatedThrough"] is None


def test_neighbouring_gaps_are_one_stretch(manager):
    job = job_with(manager, [["А"], [], [], ["Г"]])

    assert job.snapshot()["pending"] == [[2.0, 6.0]]


async def test_the_segmented_event_says_the_whole_transcript_is_still_pending(
    manager, pipeline, monkeypatch
):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = {
        "language": "hr",
        "duration": 4.0,
        "model": "large-v3",
        "cues": CUES,
    }
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED
    segmented = next(e for e in job.events if e["type"] == "segmented")
    assert segmented["total"] == 2
    assert segmented["pending"] == [[0.0, 4.0]]
    assert segmented["translatedThrough"] == 0.0


async def test_the_cues_event_says_nothing_is_left_when_the_batch_finished(
    manager, pipeline
):
    job = job_with(manager, [[], [], []])

    async with pipeline.lease.acquire(0) as held:
        await pipeline.runner._translate(job, held)

    event = job.events[-1]
    assert event["type"] == "cues"
    assert event["pending"] == []
    assert event["translatedThrough"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_readiness.py -v`
Expected: FAIL — `KeyError: 'pending'`.

- [ ] **Step 3: Implement `Job.readiness()`**

Before `snapshot`:

```python
    def readiness(self) -> dict:
        """What has already been translated and what is still empty — for the page that waits in place.

        The viewer pauses the video where they are waiting: the translation proceeds from the
        current playhead in batches, so "30 of 40 translated" says nothing about whether
        subtitles exist right here. pending — merged intervals of cues the hand has not
        reached yet. translatedThrough — the start of the first such interval, that is,
        up to which second the translation runs continuously; None means that
        nothing untranslated remains at all.
        """
        pending: list[list[float]] = []

        for cue in self.cues:
            if cue.translated:
                continue

            if pending and cue.start <= pending[-1][1]:
                pending[-1][1] = cue.end
            else:
                pending.append([cue.start, cue.end])

        return {
            "translatedThrough": pending[0][0] if pending else None,
            "pending": pending,
        }
```

In `snapshot()` add `**self.readiness()` as the last element of the dictionary.

- [ ] **Step 4: Add readiness to the two event shapes**

In each of the three places `_emit({"type": "cues", "cues": ...})` (lines 1510, 1589, 1977) and the two `_emit({"type": "segmented", "total": ...})` (1006, 1285) add `**job.readiness()`:

```python
job._emit({"type": "segmented", "total": len(job.cues), **job.readiness()})
job._emit({"type": "cues", "cues": [c.payload() for c in batch_cues], **job.readiness()})
```

- [ ] **Step 5: Update the snapshot-keys check**

In `tests/test_language_wire.py` replace the comment and the check on line 103 with:

```python
# Snapshot keys before the language plan. pending and translatedThrough were added by the
# tablet page plan; the extension reads none of the three, so any fourth new key is a bug.
assert snapshot.keys() - OLD_SNAPSHOT_KEYS == {"language", "pending", "translatedThrough"}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_readiness.py tests/test_language_wire.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add helper/jobs.py tests/test_readiness.py tests/test_language_wire.py
git commit -m "Снимок и события говорят, до какой секунды перевод идёт сплошняком"
```

---

### Task 7: Saved links and playlist parsing

**Files:**
- Modify: `helper/sources/ytdlp.py:210-238` (`_ytdlp_cmd` — `playlist`), after `fetch_video`
- Create: `helper/links.py`
- Modify: `helper/app.py` (after the video endpoints)
- Test: `tests/test_links.py` (create)

**Interfaces:**
- Produces:
  - `probe_url(url: str, timeout: float = 60.0, *, cookies: Path | None = None) -> dict` → `{"kind": "video" | "playlist", "title": str, "videos": [{"videoId", "title", "duration"}]}`
  - `helper.links`: `LINKS_PATH`, `MAX_LINKS = 200`, `Link(id, url, title, kind)` with `wire()`, `link_id(url) -> str`, `is_youtube_url(url) -> bool`, `load(path=None) -> list[Link]`, `save(links, path=None)`, `add(url, title, kind, path=None) -> Link`, `remove(link_id, path=None) -> bool`
  - `GET /v1/links` → `{"links": [...]}`; `POST /v1/links` (body `{"url"}`) → `{"link": {...}}`; `DELETE /v1/links/{id}` → `{"removed": bool}`; `GET /v1/links/{id}/videos` → `{"title": str, "videos": [{"videoId", "title", "duration", "state"}]}`

- [ ] **Step 1: Write the failing tests**

```python
"""Links: YouTube only, no duplicates, and playlist parsing into a flat list."""

import json

import pytest

from helper import links
from helper.sources import ytdlp


def test_the_same_address_is_the_same_link():
    assert links.link_id("https://youtu.be/abcdefghijk") == links.link_id(
        "https://youtu.be/abcdefghijk"
    )
    assert links.link_id("https://youtu.be/abcdefghijk") != links.link_id(
        "https://youtu.be/zzzzzzzzzzz"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://youtube.com/playlist?list=PL123",
        "https://m.youtube.com/watch?v=abcdefghijk",
        "https://youtu.be/abcdefghijk",
    ],
)
def test_youtube_addresses_are_accepted(url):
    assert links.is_youtube_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "не адрес",
        "https://evil.example/watch?v=abcdefghijk",
        "https://youtube.com.evil.example/watch?v=x",
        "file:///etc/passwd",
    ],
)
def test_anything_else_is_refused(url):
    assert not links.is_youtube_url(url)


def test_adding_the_same_address_twice_keeps_one_record(tmp_path):
    path = tmp_path / "links.json"

    first = links.add("https://youtu.be/abcdefghijk", "Ролик", "video", path=path)
    second = links.add("https://youtu.be/abcdefghijk", "Другое имя", "video", path=path)

    assert first.id == second.id
    assert [link.title for link in links.load(path)] == ["Ролик"]


def test_removing_an_unknown_link_says_so(tmp_path):
    assert links.remove("nope", path=tmp_path / "links.json") is False


def test_a_broken_file_does_not_take_the_daemon_down(tmp_path):
    path = tmp_path / "links.json"
    path.write_text("{не json", encoding="utf-8")

    assert links.load(path) == []


def test_the_cap_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(links, "MAX_LINKS", 1)
    path = tmp_path / "links.json"
    links.add("https://youtu.be/abcdefghijk", "Первый", "video", path=path)

    with pytest.raises(links.TooManyLinks):
        links.add("https://youtu.be/zzzzzzzzzzz", "Второй", "video", path=path)


def test_a_playlist_is_read_flat(monkeypatch):
    raw = {
        "_type": "playlist",
        "title": "Сербские песни",
        "entries": [
            {"_type": "url", "id": "abcdefghijk", "title": "Первая", "duration": 120},
            {"_type": "url", "id": None, "title": "битая"},
        ],
    }
    calls = []
    monkeypatch.setattr(ytdlp, "_run", lambda cmd, timeout: calls.append(cmd) or json.dumps(raw))

    found = ytdlp.probe_url("https://youtube.com/playlist?list=PL123")

    assert found == {
        "kind": "playlist",
        "title": "Сербские песни",
        "videos": [{"videoId": "abcdefghijk", "title": "Первая", "duration": 120}],
    }
    assert "--flat-playlist" in calls[0]
    assert "--yes-playlist" in calls[0]
    assert "--no-playlist" not in calls[0]


def test_a_single_video_is_a_list_of_one(monkeypatch):
    raw = {"_type": "video", "id": "abcdefghijk", "title": "Ролик", "duration": 300}
    monkeypatch.setattr(ytdlp, "_run", lambda cmd, timeout: json.dumps(raw))

    found = ytdlp.probe_url("https://youtu.be/abcdefghijk")

    assert found == {
        "kind": "video",
        "title": "Ролик",
        "videos": [{"videoId": "abcdefghijk", "title": "Ролик", "duration": 300}],
    }
```

Plus endpoint tests in the same file (a fixture — so that the test order means nothing: `LINKS_PATH` is shared by the session):

```python
@pytest.fixture(autouse=True)
def isolated_links(tmp_path, monkeypatch):
    monkeypatch.setattr(links, "LINKS_PATH", tmp_path / "links.json")


def probe_stub(monkeypatch):
    def probe(url, timeout=60.0, *, cookies=None):
        return {
            "kind": "video",
            "title": "Ролик",
            "videos": [{"videoId": "abcdefghijk", "title": "Ролик", "duration": 300}],
        }

    monkeypatch.setattr("helper.app.probe_url", probe)


async def test_a_link_is_saved_and_listed(client, monkeypatch):
    probe_stub(monkeypatch)

    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    listed = await client.get("/v1/links")

    assert added.status_code == 200
    assert added.json()["link"]["kind"] == "video"
    assert [link["title"] for link in listed.json()["links"]] == ["Ролик"]


async def test_a_foreign_address_is_refused_without_asking_yt_dlp(client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("yt-dlp не должен был запускаться")

    monkeypatch.setattr("helper.app.probe_url", explode)

    response = await client.post("/v1/links", json={"url": "https://evil.example/x"})

    assert response.status_code == 400


async def test_a_link_opens_as_a_playlist(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    (tmp_path / "abcdefghijk.mp4").write_bytes(b"mp4")
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    link_id = added.json()["link"]["id"]

    response = await client.get(f"/v1/links/{link_id}/videos")

    assert response.status_code == 200
    payload = response.json()
    assert [video["videoId"] for video in payload["videos"]] == ["abcdefghijk"]
    # The list immediately says what can be watched instantly.
    assert payload["videos"][0]["state"] == "ready"


async def test_an_unprepared_episode_says_so(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})

    response = await client.get(f"/v1/links/{added.json()['link']['id']}/videos")

    assert response.json()["videos"][0]["state"] == "absent"


async def test_an_unknown_link_is_a_404(client):
    assert (await client.get("/v1/links/nope/videos")).status_code == 404


async def test_a_link_can_be_removed(client, monkeypatch):
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    link_id = added.json()["link"]["id"]

    assert (await client.delete(f"/v1/links/{link_id}")).json() == {"removed": True}
    assert (await client.get("/v1/links")).json()["links"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_links.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'helper.links'`.

- [ ] **Step 3: Add `playlist` to `_ytdlp_cmd` and `probe_url`**

In `_ytdlp_cmd` replace `"--no-playlist"` with `*(["--yes-playlist"] if playlist else ["--no-playlist"])`, adding the parameter `playlist: bool = False` (docstring: “the playlist is read only for the links page; everything else still takes a single video”).

```python
def probe_url(url: str, timeout: float = 60.0, *, cookies: Path | None = None) -> dict:
    """What the link points to: a single video or a playlist, as a flat list without entering each one.

    --flat-playlist takes only what YouTube returned in the response itself: for a playlist
    that is a batch of ids and titles, one request instead of a hundred.
    """
    raw = json.loads(
        _run(
            _ytdlp_cmd(
                "-J",
                "--skip-download",
                "--flat-playlist",
                url,
                warnings=True,
                cookies=cookies,
                playlist=True,
            ),
            timeout,
        )
    )
    entries = raw.get("entries") or []

    if raw.get("_type") == "playlist":
        videos = [
            {
                "videoId": entry["id"],
                "title": entry.get("title") or "",
                "duration": entry.get("duration"),
            }
            for entry in entries
            if entry.get("id")
        ]

        return {"kind": "playlist", "title": raw.get("title") or "", "videos": videos}

    return {
        "kind": "video",
        "title": raw.get("title") or "",
        "videos": [
            {
                "videoId": raw.get("id") or "",
                "title": raw.get("title") or "",
                "duration": raw.get("duration"),
            }
        ],
    }
```

- [ ] **Step 4: Implement `helper/links.py`**

`LINKS_PATH = CONFIG_DIR / "links.json"`, `MAX_LINKS = 200`, `class TooManyLinks(RuntimeError)`. `is_youtube_url` — `urlsplit`, scheme `http`/`https`, host exactly `youtu.be`, `youtube.com` or ending in `.youtube.com`. `link_id` — `hashlib.sha1(url.encode()).hexdigest()[:12]`. The functions accept `path: Path | None = None` and take `LINKS_PATH` when it is `None` (otherwise `monkeypatch.setattr(links, "LINKS_PATH", ...)` would not work: the default value is bound at function definition). `load` returns `[]` on a broken or missing file; `save` writes via `path.with_suffix(".tmp")` + `os.replace`, creating the directory. `add` is idempotent by `link_id`, checks `MAX_LINKS` before writing, puts the new link first.

- [ ] **Step 5: Add the endpoints**

In `helper/app.py`: `from helper import links`, `from helper.sources.ytdlp import YtDlpError, probe_url`.

```python
LINKS_REFUSAL = "нужна ссылка на YouTube"


@app.get("/v1/links", dependencies=[Depends(auth)])
async def list_links() -> dict:
    return {"links": [link.wire() for link in links.load()]}


@app.post("/v1/links", dependencies=[Depends(auth)])
async def add_link(payload: dict) -> dict:
    """Save a link. The title and kind are taken from YouTube, not from the person."""
    url = (payload or {}).get("url", "")

    if not isinstance(url, str) or not links.is_youtube_url(url):
        raise HTTPException(status_code=400, detail=LINKS_REFUSAL)

    try:
        found = await asyncio.to_thread(probe_url, url)
    except YtDlpError as exc:
        raise HTTPException(status_code=502, detail=f"не удалось прочитать ссылку: {exc}") from exc

    try:
        link = links.add(url, found["title"] or url, found["kind"])
    except links.TooManyLinks as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"link": link.wire()}


@app.delete("/v1/links/{link_id}", dependencies=[Depends(auth)])
async def drop_link(link_id: str) -> dict:
    return {"removed": links.remove(link_id)}


@app.get("/v1/links/{link_id}/videos", dependencies=[Depends(auth)])
async def link_videos(link_id: str) -> dict:
    """The link's videos as a flat list. The picture state — for each at once.

    The list answers the question "what can be watched instantly": ready is already
    in the cache, preparing is downloading, absent — there is nothing. Asking about each video
    separately would mean a dozen requests to open a playlist.
    """
    link = next((item for item in links.load() if item.id == link_id), None)

    if link is None:
        raise HTTPException(status_code=404, detail="ссылки нет")

    try:
        found = await asyncio.to_thread(probe_url, link.url)
    except YtDlpError as exc:
        raise HTTPException(status_code=502, detail=f"не удалось прочитать ссылку: {exc}") from exc

    videos = [
        {
            **video,
            "state": (
                manager.videos.status(video["videoId"]).state
                if ytdlp.VIDEO_ID.match(video["videoId"])
                else "absent"
            ),
        }
        for video in found["videos"]
    ]

    return {"title": found["title"], "videos": videos}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_links.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add helper/links.py helper/sources/ytdlp.py helper/app.py tests/test_links.py
git commit -m "Сохранённые ссылки: только YouTube, без дублей, плейлист плоским списком"
```

---

### Task 8: Page markup and styles

**Files:**
- Modify: `page/index.html` (replace the stub)
- Create: `page/style.css`, `page/app.js` (stub)
- Test: `tests/test_page_serving.py` (extend)

**Interfaces:**
- Produces: markup with the identifiers that `page/app.js` relies on (task 10): `setup`, `token-input`, `token-save`, `setup-message`, `app`, `home`, `links`, `link-form`, `link-url`, `episodes`, `episodes-title`, `stage`, `video`, `overlay`, `status`, `fullscreen`, `back`, `title`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_page_serving.py`:

```python
async def test_the_page_brings_its_styles_and_scripts(client):
    assert (await client.get("/style.css")).status_code == 200
    assert (await client.get("/app.js")).status_code == 200


async def test_the_page_has_the_screen_for_asking_the_token(client):
    text = (await client.get("/")).text

    assert 'id="token-input"' in text
    assert 'id="setup"' in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_serving.py -v`
Expected: FAIL — 404 on `/style.css`.

- [ ] **Step 3: Write `page/index.html`**

Three screens in one document, switched by the `hidden` attribute:

- `#setup` — the `#token-input` field, the `#token-save` button, `#setup-message`;
- `#home` — the list of saved links `#links`, the form `#link-form` with the `#link-url` field and a submit button, the episodes block `#episodes` with the `#episodes-title` heading;
- `#stage` — `<video id="video" playsinline preload="metadata">`, the `#overlay` layer (empty, `app.js` fills it), the `#status` line, the `#fullscreen` button, the `#back` button and `#title` with the video's name.

Included are `<link rel="stylesheet" href="/style.css">` and `<script type="module" src="/app.js"></script>`. `style.css` is written here in full. `page/app.js` is created as a one-line comment stub: the link from the markup must not lead to a 404, while the real wiring is task 10.

- [ ] **Step 4: Write `page/style.css`**

Dark background `#0d0d0f`, the video in the centre, `#stage` fills the screen. The `#overlay` layer — the same look as in `extension/content.js`: a `22px` line on `rgba(8,8,8,.78)`, bottom padding `calc(8% + var(--lift, 0px))`, width no more than `88%`, `.line` in white, `.box.failed .line` in `#ffd9a0` and italic, `.box.hidden` hides the layer entirely. A larger font on a wide screen: `@media (min-width: 900px) { :root { --font: 34px } }`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_serving.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add page/index.html page/style.css page/app.js tests/test_page_serving.py
git commit -m "Разметка страницы планшета: экран токена, ссылки, сцена с видео и слоем"
```

---

### Task 9: Pure page logic

**Files:**
- Create: `page/package.json`, `page/logic.js`, `page/logic.test.mjs`
- Test: `tests/test_page_logic.py` (create, a bridge to node)

**Interfaces:**
- Produces (an ES module, the exported names are the contract for task 10):
  - `activeCueIndex(cues, t) -> number` — the index of the cue where `start <= t < end`, otherwise `-1`
  - `pendingAt(pending, t) -> boolean` — whether `t` falls into an untranslated interval
  - `stageText(stage, state) -> string` — the caption about the work in progress; empty on `done`
  - `languageText(language) -> string` — the line about the speech language; empty if there is no decision
  - `cueLines(cue) -> {target: string, secondary: string, failed: boolean}`
  - `formatTime(seconds) -> string`

- [ ] **Step 1: Write the failing tests**

`page/logic.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeCueIndex, cueLines, formatTime, languageText, pendingAt, stageText,
} from "./logic.js";

const CUES = [
  { i: 0, start: 0, end: 2, lines: ["Раз"], study: "Jedan", failed: false },
  { i: 1, start: 2, end: 4, lines: ["Два"], study: "", failed: false },
];

test("replica is active from its start up to, but not including, its end", () => {
  assert.equal(activeCueIndex(CUES, -1), -1);
  assert.equal(activeCueIndex(CUES, 0), 0);
  assert.equal(activeCueIndex(CUES, 1.999), 0);
  assert.equal(activeCueIndex(CUES, 2), 1);
  assert.equal(activeCueIndex(CUES, 4), -1);
  assert.equal(activeCueIndex([], 1), -1);
});

test("a gap is where the translation has not arrived yet", () => {
  const pending = [[0, 2], [4, 6]];

  assert.equal(pendingAt(pending, 1), true);
  assert.equal(pendingAt(pending, 2), false);
  assert.equal(pendingAt(pending, 5), true);
  assert.equal(pendingAt(pending, 6), false);
  assert.equal(pendingAt([], 1), false);
});

test("each stage has its own words, and a done job says nothing", () => {
  assert.equal(stageText("queued", { waiting: "gpu" }), "ждём видеокарту: её занимает другая работа");
  assert.equal(stageText("queued", {}), "в очереди…");
  assert.equal(stageText("probing", {}), "читаю ролик…");
  assert.equal(stageText("fetching", {}), "качаю звук…");
  assert.equal(stageText("asr", {}), "распознаю речь…");
  assert.equal(stageText("translating", { total: 10, translated: 3 }), "перевожу… 3 из 10");
  assert.equal(stageText("paused", {}), "пауза: не хватило видеопамяти");
  assert.equal(stageText("done", {}), "");
  assert.equal(stageText("error", { error: "нет связи", retryable: true }), "нет связи — можно повторить");
  assert.equal(stageText("error", { error: "нет связи", retryable: false }), "нет связи");
});

test("the language line names the verdict and repeats the daemon's note", () => {
  assert.equal(languageText(null), "");
  assert.equal(
    languageText({
      effective: "sr", reason: "detector", confidence: 0.97,
      note: "YouTube указал en, по речи sr",
    }),
    "язык речи: sr; по речи, 97%; YouTube указал en, по речи sr",
  );
  assert.equal(
    languageText({ effective: "en", reason: "hint", confidence: null, note: null }),
    "язык речи: en",
  );
  assert.equal(
    languageText({
      effective: "sr", reason: "track", track: "sr",
      note: "YouTube указал en, дорожка автора sr",
    }),
    "язык речи: sr; дорожка автора sr; YouTube указал en, дорожка автора sr",
  );
});

test("a cue gives two lines, and no study line without a translation", () => {
  assert.deepEqual(cueLines(CUES[0]), { target: "Раз", secondary: "Jedan", failed: false });

  assert.deepEqual(
    cueLines({ lines: ["A", "B"], study: "", failed: true }),
    { target: "A\nB", secondary: "", failed: true },
  );
  // An untranslated cue: study without a translation does not go on screen.
  assert.deepEqual(
    cueLines({ lines: [], study: "Jedan", failed: false }),
    { target: "", secondary: "", failed: false },
  );
  // The second line matched the first one — we do not show it.
  assert.deepEqual(
    cueLines({ lines: ["Jedan"], study: "Jedan", failed: false }),
    { target: "Jedan", secondary: "", failed: false },
  );
});

test("a failed Russian line does not take the Montenegrin line away", () => {
  // The spec's rule: the second line goes dark only when its language matched the first one,
  // not when the translation failed. Otherwise a Russian failure takes away exactly what
  // the whole thing was started for.
  assert.deepEqual(
    cueLines({ lines: ["Zdravo svima."], study: "Zdravo svima, narode.", failed: true }),
    { target: "Zdravo svima.", secondary: "Zdravo svima, narode.", failed: true },
  );
});

test("time is read the way a player shows it", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(3723), "1:02:03");
});
```

`tests/test_page_logic.py`:

```python
"""A bridge to the page tests: the pure logic runs under node --test, from here — under a single pytest."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_the_page_logic_passes_its_own_tests():
    proc = subprocess.run(
        ["node", "--test", "page/logic.test.mjs"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_logic.py -v`
Expected: FAIL — there is no `page/logic.js`.

- [ ] **Step 3: Add `page/package.json` and `page/logic.js`**

`page/package.json`: `{"type": "module", "private": true}` — without it node reads `logic.js` as CommonJS and fails on `export`, while the browser does not care about the field.

`logic.js` — pure functions only, without `document` and `fetch`; the caption values exactly as in the tests. `cueLines` assembles `target` via `"\n"` from `cue.lines || []`, `secondary` — `cue.study`, if it is non-empty and not equal to `target`. `formatTime` — the hours are printed only when they exist.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_logic.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add page/package.json page/logic.js page/logic.test.mjs tests/test_page_logic.py
git commit -m "Чистая логика страницы: активная реплика, пустоты перевода и надписи стадий"
```

---

### Task 10: Page wiring

**Files:**
- Modify: `page/app.js` (stub → the real file)
- Test: `tests/test_page_wiring.py` (create)

**Interfaces:**
- Consumes: `page/logic.js` (all six functions), `POST /v1/jobs`, `POST/GET /v1/videos/{id}`, `GET /v1/videos/{id}/file`, `/v1/links*`, `/v1/ws`, `POST /v1/jobs/{id}/playhead`.
- Produces: a working page; no new server contracts.

- [ ] **Step 1: Write the failing test**

```python
"""Page wiring: every element that app.js looks for exists in the markup."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ids_the_page_looks_for():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    return set(re.findall(r'getElementById\("([^"]+)"\)', source))


def test_the_page_looks_for_something():
    assert ids_the_page_looks_for()


def test_every_element_the_page_looks_for_exists():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    present = set(re.findall(r'id="([^"]+)"', html))

    assert ids_the_page_looks_for() - present == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py -v`
Expected: FAIL — `assert set()` (the stub has not a single `getElementById`).

- [ ] **Step 3: Implement `page/app.js`**

A module that imports six functions from `./logic.js`. State — one object: `videoId`, `cues` (by `i`), `pending`, `language`, `stage`, `waiting`, `error`, `retryable`, `total`, `translated`, `title`. The token — in `localStorage` under the key `yt-trans-token`.

Responsibilities, each as a separate function:

- `api(path, options)` — `fetch` with `Authorization: Bearer`, JSON parsing; on 401 show `#setup`, on any other non-200 throw `Error(detail)`.
- `saveToken()` — save the value of `#token-input`, hide `#setup`, call `start()`.
- `start()` — `connectSocket()`, `loadLinks()`.
- `loadLinks()` — `GET /v1/links`, draw a button per link and a delete button in `#links`; empty list — the caption “пока пусто, добавь ссылку”.
- `addLink(event)` — `preventDefault`, `POST /v1/links`, redraw.
- `dropLink(id)` — `DELETE /v1/links/{id}`, redraw.
- `openLink(id)` — `GET /v1/links/{id}/videos`, `#episodes-title` = the title, `#episodes` — episode buttons with the title and a state mark (`ready` — “готово”, `preparing` — “готовится”, `error` — “не скачалось”, `absent` — empty); a single video — `play(videoId, title)` right away.
- `play(videoId, title)` — hide `#home`, show `#stage`, `#title` = the title; `POST /v1/jobs` with `{videoId, targetLang: "ru", authorTrack: true}`; `watch(videoId)`; `prepareVideo()`.
- `connectSocket()` — `ws://<host>/v1/ws?token=…`; on `open` repeat `watch(videoId)` if a video is selected; on `close` — reconnect in 1 s; on `message` — `handleMessage`.
- `watch(videoId)` — send `{op: "watch", videoId, targetLang: "ru", authorTrack: true}` and immediately after it `{op: "playhead", videoId, t: 0, clientId}`. `clientId` — `crypto.randomUUID()` from `localStorage`, one per page: without it the daemon counts the tablet as the same client as the tabs on the computer.
- `handleMessage(message)` — by `message.type`: `snapshot` replaces the whole state entirely; `state` writes `stage`, `waiting`, `error` (from `message.message`), `retryable`; `language` writes `language`; `segmented` writes `total` and `pending`; `cues` merges cues by `i` and writes `pending`; `ping` is ignored. After each — `render()`.
- `render()` — `#status` = `stageText(state.stage, state)`, hide when empty; the language caption — `languageText(state.language)` on a separate line in `#status`.
- `prepareVideo()` — `POST /v1/videos/{id}`, then poll `GET /v1/videos/{id}` every 2 s; `ready` → `video.src = "/v1/videos/" + id + "/file?token=" + encodeURIComponent(token)`, `video.play()`; `error` → `#status` = the message and stop polling; `preparing` → `#status` = “готовлю картинку…”.
- `paint()` — `index = activeCueIndex(state.cues, video.currentTime)`; index found → `cueLines`; otherwise `pendingAt(state.pending, video.currentTime)` → `{target: "перевод ещё не готов…", secondary: "", failed: true}`; otherwise hide the layer. Builds the shadow tree once (`attachShadow({mode: "open"})` on `#overlay`) and from then on only changes `textContent` and classes, like `extension/content.js`.
- `startClock()` — `requestVideoFrameCallback`, if it exists, otherwise the `timeupdate` event; each tick — `paint()`.
- `reportPlayhead()` — once a second, while `!video.paused`, `{op: "playhead", videoId, t: video.currentTime, clientId}`.
- `fullscreen()` — `#stage.requestFullscreen()` / `document.exitFullscreen()`.
- `back()` — stop the video (`video.pause()`, `video.removeAttribute("src")`, `video.load()`), show `#home`, hide `#stage`.

The layer must mirror `extension/content.js`: two lines, the second hidden when empty, `failed` gives a special look. **A difference from the extension, mandatory per the spec:** `failed` never removes the second line — it goes dark only if it is empty or matched the first (this is already done in `cueLines` from task 9), while the `failed` class only changes the look of the first line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py tests/test_page_serving.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add page/app.js tests/test_page_wiring.py
git commit -m "Проводка страницы: токен, ссылки, видео с демона, сокет и слой субтитров"
```

---

### Task 11: Documentation

**Files:**
- Create: `docs/tablet.md`
- Modify: `README.md`, `docs/running.md:199`

**Interfaces:**
- Produces: instructions; does not change code.

- [ ] **Step 1: Write `docs/tablet.md`**

Sections, one paragraph each: “Что это” (two lines over the video, without the extension and without the YouTube player; subtitles are not burned in, the mp4 goes clean); “Включить сеть” (`[server] host = "0.0.0.0"`, `systemctl --user restart yt-trans`, `loginctl enable-linger` per `docs/running.md`); “Токен” (take it from `~/.config/yt-trans/token`, paste it on the page once); “Открыть на планшете” (`http://<адрес-компьютера>:8756/`); “Ссылки” (that only YouTube addresses are saved, that a playlist unfolds as a list); “Почему видео готовится” (downloading and muxing, ~400 MB for 26 minutes); “Диск” (the cache `~/.cache/yt-trans/videos`, a 20 GiB cap, old ones are evicted); “Если не работает” (a blank screen — check the token; “готовлю картинку…” for a long time — look at the daemon log; 403 — look at Origin).

- [ ] **Step 2: Point README and running.md at it**

In `README.md` — a short section “Страница для планшета” with a link to `docs/tablet.md`. In `docs/running.md` at the end of the section “Какой язык решил демон” — the line “Страница для планшета и сеть: `docs/tablet.md`”.

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, the count grew by the number of added tests.

- [ ] **Step 4: Commit**

```bash
git add docs/tablet.md README.md docs/running.md
git commit -m "Как включить страницу планшета: сеть, токен, кэш и что делать, если пусто"
```
