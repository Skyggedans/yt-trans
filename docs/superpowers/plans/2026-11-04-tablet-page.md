# Страница для планшета: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Демон раздаёт страницу, которая играет ролик из скачанного им же mp4 и рисует две строки субтитров — так, чтобы планшет в другой комнате работал без расширения и без встроенного плеера YouTube.

**Architecture:** Демон качает пару дорожек yt-dlp, клеит H.264≤1080p + AAC в mp4 (`-c copy`, `+faststart`), держит файл в кэше с потолком и отдаёт его с `Range`. Страница — статические файлы из того же источника: обычный `<video>`, теневое дерево поверх него с двумя строками, WebSocket за событиями и `POST /v1/jobs` за переводом. Расширение не меняется ни на строку: поля провода только добавляются, новых стадий нет.

**Tech Stack:** FastAPI 0.162 / Starlette 1.10 (`FileResponse` с `Range`), httpx в тестах, yt-dlp + ffmpeg, ванильный ES-модуль без сборки, node 24 (`node --test`) для чистой логики страницы.

**Spec:** `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md`

## Global Constraints

- `extension/` не меняется ни на строку. Новых значений `Stage` не появляется; поля провода только добавляются (расширение читает их по имени, лишние игнорирует).
- Токен установки обязателен для всех `/v1/*`, кроме `/v1/health`; `?token=` — такой же законный способ, как `Authorization`, потому что `<video>` заголовков не шлёт.
- Демон по умолчанию слушает `127.0.0.1`; сеть включается только настройкой `[server] host`.
- Все видимые человеку строки — по-русски, как в остальном репозитории.
- Страница без сборки: только `<script type="module">`, никаких пакетов и CDN.
- Профиль видео один на все устройства: H.264 не выше 1080p + AAC в mp4, без перекодирования.
- Потолок кэша видео — **20 ГиБ** (`20 * 1024**3`), вытеснение по давности; самый свежий файл не вытесняется никогда.
- Титры в файл не запекаются: mp4 уходит в браузер чистым, строки рисует страница.
- Тесты: `.venv/bin/python -m pytest` (на старте 487 зелёных), `asyncio_mode = auto`, `pythonpath = .`.
- Коммиты по-русски, без трейлера `Co-Authored-By` (правило хозяина).

## Review Focus

1. **Перемотка и первый кадр.** Человек откроет ролик и потянет ползунок: видео должно перематываться (206 на `Range`) и начинать играть до полной загрузки (`+faststart`), иначе планшет покажет чёрный экран на минуту. Тесты: задача 3 (`faststart` в команде), задача 5 (206 и `content-range`).
2. **Расширение не должно сломаться.** Новые поля идут рядом со старыми, ни одно имя в снимке не переименовано, стадия `Stage` не добавлена. Тест: задача 6 (обновлённая проверка набора ключей в `tests/test_language_wire.py`).
3. **Демон не открывается миру.** Чужая страница по-прежнему получает 403 на HTTP и 4401 на сокете, даже когда демон слушает `0.0.0.0`. Тест: задача 1.
4. **Кэш не съедает диск.** Вытеснение по давности держит сумму под потолком; файл больше потолка не удаляется сразу после скачивания; недокачанный файл никогда не отдаётся как готовый. Тест: задача 4.
5. **Чужой URL не уходит в yt-dlp.** `POST /v1/links` принимает только YouTube-адреса (иначе 400), а битый идентификатор ролика отсекается до запуска процесса. Тесты: задача 7, задача 5.

## Решения строителя: где план расходится со спекой

Спека разрешает менять условные имена («Имена условные, строитель вправе их изменить») и оставляет строителю кэш видео. Три расхождения выбраны сознательно:

- **Эндпоинты называются `/v1/videos/{id}` и `/v1/links…`**, а не `/v1/video/{videoId}` и `/v1/playlists…`: страница и демон пишутся вместе, а множественное число честнее — у одного ролика две вещи (`состояние` и `file`), а «playlist» не покрывает канал и одиночный ролик, которые хранятся в том же списке.
- **Готовность картинки живёт в `/v1/videos/{id}`, а не в событиях задания.** Спека предлагала «состояние подготовки видео — отдельная стадия в событиях задания», но это связало бы `Job` с реестром видео и добавило бы стадию, которую расширение рисует своим бейджем. Страница опрашивает состояние раз в 2 секунды, пока `src` не выставлен, — на один ролик это десяток запросов.
- **Файл отдаётся только готовым, иначе 409 с фразой состояния.** Страница сначала спрашивает состояние и лишь потом ставит `src`, поэтому `<video>` никогда не получает не-200; 409 существует для ручной диагностики и для честного ответа «ещё готовится», а не для проигрывания.

## Не входит в план

Названо, чтобы исполнитель не достраивал и ревьюер не считал пропуском:

- Предзагрузка следующего эпизода плейлиста.
- Типизированная вторая строка `{text, lang, kind}`, приговор проверяльщиков перевода, пометки «выравнивание не проверено» и «вторая строка не удостоверена» — работа «Проверка перевода».
- Язык как свойство сохранённой ссылки (сейчас решает определитель, а его ответ виден на экране через `languageText`).
- Поля снимка `duration`, `source`, `studyLang`: странице они не нужны, а `study` у реплики пуст, когда режим изучения выключен, — вторая строка просто не рисуется.
- `GET /v1/jobs/{id}/track?format=srt|vtt|json` — страница им не пользуется, это отдельный запасной путь.
- Ограничение частоты создания заданий: единственный, у кого есть токен, — хозяин, и он же единственный клиент.
- Отмена осиротевшего yt-dlp, ушедшего в `asyncio.to_thread` (известное ограничение из спеки).
- https, PWA и Wake Lock: обычный `<video>` по http играет, экран не гасит.

---

## Files

**Создаются:**
- `helper/video.py` — реестр скачанных роликов: состояния, потолок, вытеснение.
- `helper/links.py` — сохранённые ссылки в `~/.config/yt-trans/links.json`.
- `page/index.html`, `page/style.css` — разметка и стили страницы.
- `page/logic.js` — чистые функции отрисовки (импортируются и страницей, и node-тестами).
- `page/logic.test.mjs` — тесты чистой логики для `node --test`.
- `page/app.js` — проводка: токен, ссылки, `<video>`, сокет, слой субтитров.
- `page/package.json` — `{"type": "module"}`: без него node читает `logic.js` как CommonJS.
- `tests/test_server.py`, `tests/test_page_serving.py`, `tests/test_video_source.py`, `tests/test_video_store.py`, `tests/test_video_endpoints.py`, `tests/test_readiness.py`, `tests/test_links.py`, `tests/test_page_logic.py`, `tests/test_page_wiring.py`.
- `docs/tablet.md` — как включить и как пользоваться.

**Меняются:**
- `helper/config.py` — `ServerConfig`, поле `Config.server`.
- `helper/app.py` — `HOST` из конфига, `same_origin`, раздача страницы, эндпоинты видео и ссылок.
- `helper/jobs.py` — `Job.readiness()`, `manager.videos`.
- `helper/sources/ytdlp.py` — `VIDEO_FORMAT`, `fetch_video`, `probe_url`, `playlist` в `_ytdlp_cmd`.
- `tests/conftest.py` — `FakeSocket.close`/`accepted`.
- `tests/test_language_wire.py` — набор ключей снимка.
- `config.example.toml`, `README.md`.

---

### Task 1: Сеть: хост из конфига и свой источник

**Files:**
- Modify: `helper/config.py:134-153, 182-220`
- Modify: `helper/app.py:42, 116-142, 327-338`
- Modify: `config.example.toml`
- Modify: `tests/conftest.py:189-202`
- Test: `tests/test_server.py` (создать)

**Interfaces:**
- Produces: `ServerConfig(host: str = "127.0.0.1")`; `Config.server`; `app.HOST: str`; `same_origin(origin: str, host: str | None) -> bool`; `check_origin(origin: str | None, host: str | None = None) -> None`; `FakeSocket.accepted: bool`, `FakeSocket.closed: int | None`, `async FakeSocket.close(code: int = 1000)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Сеть: адрес из конфига; страница самого демона ходит к нему с того же источника."""

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
    # 404, а не 403: запрос дошёл до обработчика и не нашёл задания.
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
Expected: FAIL — `ImportError: cannot import name 'ServerConfig'` / `AttributeError: same_origin`, а также `TypeError: check_origin() takes 1 positional argument`.

- [ ] **Step 3: Add `ServerConfig` to `helper/config.py`**

Перед `Config`:

```python
@dataclass(frozen=True)
class ServerConfig:
    # Адрес, на котором слушает демон. По умолчанию петля: демон запускает
    # запросы к YouTube по команде клиента, и открывать его в сеть «на всякий
    # случай» нельзя. Планшет в другой комнате — это host = "0.0.0.0"
    # и токен из ~/.config/yt-trans/token как единственная защита.
    host: str = "127.0.0.1"
```

В `Config` добавить последним полем `server: ServerConfig = ServerConfig()`; в `load()` — `server = ServerConfig(**_section(raw, "server", ServerConfig))` и передать `server=server` в `Config(...)`.

- [ ] **Step 4: Implement the origin rule in `helper/app.py`**

`from urllib.parse import urlsplit`, `Request` в импорт fastapi. Заменить `HOST = "127.0.0.1"` на `HOST = manager.config.server.host` (строка 42 идёт после `manager = JobManager()` — перенести `HOST` ниже него).

```python
def same_origin(origin: str, host: str | None) -> bool:
    """Origin страницы, которую раздаёт сам демон: тот же хост и порт, что в Host.

    Сравнивается authority целиком, а не только имя: браузер присылает порт
    в Origin тогда же, когда он есть в адресе. Страницы, раздаваемой демоном,
    это пускает, а любой другой сайт — нет.
    """
    if not host:
        return False

    parts = urlsplit(origin)

    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.lower()


def check_origin(origin: str | None, host: str | None = None) -> None:
    """Пускаем расширения, свою страницу и запросы без Origin (curl), но не чужие сайты."""
    if origin and not origin.startswith(ALLOWED_ORIGIN_SCHEMES) and not same_origin(origin, host):
        raise HTTPException(status_code=403, detail="origin не разрешён")
```

`auth` получает первым параметром `request: Request` и зовёт `check_origin(origin, request.headers.get("host"))`. В `websocket` заменить общий сторож на два шага:

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

В `tests/conftest.py` в `__init__`: `self.closed: int | None = None`, `self.accepted = False`. Заменить `async def accept(self) -> None: pass` на `self.accepted = True`; добавить:

```python
    async def close(self, code: int = 1000) -> None:
        self.closed = code
```

- [ ] **Step 6: Document the setting in `config.example.toml`**

```toml
[server]
# На каком адресе слушает демон. По умолчанию только петля: демон запускает
# запросы к YouTube по команде клиента, и открывать его в сеть просто так нельзя.
# Планшет в другой комнате требует "0.0.0.0". Тогда демон доступен всем в своей
# сети, и единственная защита у него — токен из ~/.config/yt-trans/token.
# host = "0.0.0.0"
```

- [ ] **Step 7: Stop trusting forwarded addresses**

В `main()`: `uvicorn.run(app, host=HOST, port=PORT, log_level="warning", proxy_headers=False)`. По умолчанию uvicorn верит `X-Forwarded-For` с петли и подменяет адрес обратившегося; на петле это было безобидно, на `0.0.0.0` любой в сети подделает себе адрес в журнале. Отдельного теста нет: это настройка доверия к заголовку, а не наша логика.

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_server.py -v && .venv/bin/python -m pytest -q`
Expected: PASS, весь набор зелёный (487 + 7).

- [ ] **Step 9: Commit**

```bash
git add helper/config.py helper/app.py config.example.toml tests/conftest.py tests/test_server.py
git commit -m "Адрес демона берётся из [server]; страница своего источника проходит проверку Origin"
```

---

### Task 2: Демон раздаёт страницу

**Files:**
- Create: `page/index.html`
- Modify: `helper/app.py` (конец модуля, после `websocket`)
- Test: `tests/test_page_serving.py` (создать)

**Interfaces:**
- Produces: `app.PAGE_DIR: Path`; страница по `/`, без токена.
- Consumes: ничего.

- [ ] **Step 1: Write the failing tests**

```python
"""Страница планшета раздаётся самим демоном: с того же источника и без токена."""

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
Expected: FAIL — 404 на `/`.

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

В `helper/app.py`: `from pathlib import Path` и `from fastapi.staticfiles import StaticFiles`; рядом с `TOKEN_PATH`:

```python
PAGE_DIR = Path(__file__).resolve().parent.parent / "page"
```

В самом конце модуля, после `websocket` и до `def main()`:

```python
# Монтируется последним: маршруты выше добавлены раньше и выигрывают у монтирования,
# так что /v1/* остаётся API, а всё прочее ищется файлом. Страница публичная —
# секретов в ней нет, а токен она спрашивает у человека сама.
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

### Task 3: Скачать картинку и склеить mp4

**Files:**
- Modify: `helper/sources/ytdlp.py:210-238` (`_ytdlp_cmd`), после `audio_pcm`
- Test: `tests/test_video_source.py` (создать)

**Interfaces:**
- Produces: `VIDEO_FORMAT: str`; `fetch_video(video_id: str, target: Path, timeout: float = 1800.0, *, cookies: Path | None = None) -> Path`.
- Consumes: `_check_id`, `_run`, `_ytdlp_cmd`, `YtDlpError` (всё в том же модуле).

- [ ] **Step 1: Write the failing tests**

```python
"""Скачивание картинки: H.264 не выше 1080p, родной звук, склейка без перекодирования."""

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
    # Сортировка начинается с res, а не с размера файла: 1080p — потолок,
    # ниже можно, выше нельзя. lang,+size — родной звук, а не автодубляж.
    assert cmd[cmd.index("-S") + 1] == "res:1080,lang,+size"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert cmd[cmd.index("--postprocessor-args") + 1] == "Merger+ffmpeg:-movflags +faststart"
    assert cmd[cmd.index("-o") + 1].endswith("video.%(ext)s")
    assert timeout == 1800.0
    assert target.read_bytes() == b"mp4"
    # Временного каталога рядом не осталось: он живёт внутри родителя цели.
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

Рядом с `AUDIO_FORMAT`:

```python
# Картинка и звук отдельными дорожками, потому что склеенных 1080p у YouTube нет.
# Сначала mp4 с H.264 — он играет везде; при отсутствии берём любой H.264 (в контейнере
# webm склейка всё равно уйдёт в mp4 при -c copy); в конце — что угодно, чтобы ролик
# не пропадал из-за одного редкого кодека.
VIDEO_FORMAT = (
    "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "b[ext=mp4]/b"
)
```

После `audio_pcm` (импорты `os`, `tempfile` — в шапку модуля):

```python
def fetch_video(
    video_id: str, target: Path, timeout: float = 1800.0, *, cookies: Path | None = None
) -> Path:
    """Скачать ролик и склеить в mp4 без перекодирования. Возвращает путь к файлу.

    Временный каталог создаётся рядом с целью, а не в /tmp: os.replace атомарен
    только внутри одной файловой системы. Готовый файл появляется под своим именем
    целиком — недокачанного mp4 под именем, которое отдают зрителю, не бывает.
    """
    _check_id(video_id)

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{video_id}-", dir=target.parent) as work:
        _run(
            _ytdlp_cmd(
                "-f",
                VIDEO_FORMAT,
                # Первым делом потолок разрешения, затем родной язык звука и лишь
                # потом размер: -S lang обязана идти раньше +size, иначе на ролике
                # с автодубляжом возьмётся самая маленькая дорожка — то есть чужая.
                "-S",
                "res:1080,lang,+size",
                "--merge-output-format",
                "mp4",
                # Без faststart индекс лежит в конце файла, и браузер ждёт полной
                # загрузки, прежде чем показать первый кадр и дать перемотать.
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

`--cookies` в команду не дописывается: его добавляет сам `_ytdlp_cmd`, если передать `cookies=`. Импорты `os` и `tempfile` — в шапку модуля.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_video_source.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add helper/sources/ytdlp.py tests/test_video_source.py
git commit -m "Скачивание ролика: H.264 не выше 1080p, родной звук, склейка в mp4 с faststart"
```

---

### Task 4: Реестр скачанных роликов с потолком

**Files:**
- Create: `helper/video.py`
- Test: `tests/test_video_store.py` (создать)

**Interfaces:**
- Consumes: `helper.env.CACHE_DIR`, `ytdlp.fetch_video`, `ytdlp.YtDlpError`.
- Produces:
  - `VIDEO_DIR: Path`, `VIDEO_CACHE_MAX_BYTES: int`, `VIDEO_TIMEOUT: float = 1800.0`
  - `VideoStatus` с полями `state: str` (`"absent" | "preparing" | "ready" | "error"`), `message: str`, `path: Path | None` и методом `wire() -> dict` (`{"state", "message"}`)
  - `VideoStore(directory=VIDEO_DIR, max_bytes=VIDEO_CACHE_MAX_BYTES, fetch=fetch_video)` с `path(video_id) -> Path`, `status(video_id) -> VideoStatus`, `ensure(video_id, *, cookies=None) -> VideoStatus`, `async settled(video_id) -> VideoStatus`, `evict() -> int`

- [ ] **Step 1: Write the failing tests**

```python
"""Кэш видео: готовность по файлу на диске, атомарная запись, потолок и вытеснение."""

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

Класс хранит три словаря: `_states`, `_errors`, `_tasks`. Скачивание идёт одной задачей на ролик под общим `asyncio.Semaphore(1)`; `ensure` возвращает состояние сразу и никогда не ждёт. Готовность определяется наличием файла, а не памятью процесса: после перезапуска демона скачанное остаётся готовым.

Правила, которые проверяют тесты:

- `status`: файл есть → `ready` (даже если о ролике ничего не помнит); иначе `self._states.get(video_id, "absent")` и текст ошибки из `_errors`.
- `ensure`: `ready`/`preparing` — вернуть как есть; иначе записать `preparing`, снять старую ошибку и создать задачу `_prepare`.
- `_prepare`: под семафором вызвать `fetch(video_id, path, VIDEO_TIMEOUT, cookies=cookies)`; на `YtDlpError` с `age_restricted=True` и непустыми `cookies` — один повтор с аккаунтом. Успех → `ready` + `evict()`. Любое исключение → `error` + `str(exc)`. В `finally` убрать задачу из `_tasks`.
- `settled`: дождаться задачи, если она есть, и вернуть `status`.
- `evict`: `*.mp4` по возрастанию `st_mtime`; удалять, пока сумма больше `max_bytes`, но не трогать самый свежий файл и остановиться, если он один.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_video_store.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add helper/video.py tests/test_video_store.py
git commit -m "Кэш видео: атомарная запись, потолок 20 ГиБ и вытеснение по давности"
```

---

### Task 5: Эндпоинты видео с поддержкой Range

**Files:**
- Modify: `helper/jobs.py:1736-1751` (`JobManager.__init__`)
- Modify: `helper/app.py` (после `/v1/jobs/{video_id}/drop`)
- Test: `tests/test_video_endpoints.py` (создать)

**Interfaces:**
- Consumes: `VideoStore` из задачи 4; `ytdlp.VIDEO_ID`.
- Produces: `manager.videos: VideoStore`; `POST /v1/videos/{video_id}` → `{"state","message"}`; `GET /v1/videos/{video_id}` → то же; `GET /v1/videos/{video_id}/file` → mp4 или 409 с `{"detail": "..."}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Видео на проводе: подготовка, состояние и файл, который умеет Range."""

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
Expected: FAIL — 404 на `POST /v1/videos/...`.

- [ ] **Step 3: Give the manager its store**

В `helper/jobs.py` — импорт `from helper.video import VideoStore` и в `JobManager.__init__` после `self.cache = TrackCache()`: `self.videos = VideoStore()`.

- [ ] **Step 4: Add the endpoints**

В `helper/app.py`: `from fastapi.responses import FileResponse`, `from helper.sources import ytdlp`.

```python
@app.post("/v1/videos/{video_id}", dependencies=[Depends(auth)])
async def prepare_video(video_id: str) -> dict:
    """Начать готовить mp4. Отвечает сразу; готовность — отдельным GET."""
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
    """Файл целиком или кусок: Range разбирает FileResponse, он же ставит 206.

    Токен принимается и в строке запроса (?token=): <video src> заголовков не шлёт.
    Пока файла нет — 409 с фразой состояния. Страница сначала спрашивает состояние
    и лишь потом ставит src, поэтому <video> не-200 не получает никогда.
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

### Task 6: Готовность перевода на проводе

**Files:**
- Modify: `helper/jobs.py:442-472` (`snapshot`), `1006`, `1285`, `1510`, `1589`, `1977`
- Modify: `tests/test_language_wire.py:34-38, 103`
- Test: `tests/test_readiness.py` (создать)

**Interfaces:**
- Produces: `Job.readiness() -> dict` с ключами `translatedThrough: float | None` (начало первого непереведённого куска; `None` — непереведённого не осталось) и `pending: list[list[float]]` (слитые интервалы `[start, end]` непереведённых реплик по возрастанию). Те же два ключа появляются в `snapshot()` и в событиях `segmented` и `cues`.

- [ ] **Step 1: Write the failing tests**

```python
"""Готовность на проводе: где перевод кончается и какие куски ещё пусты."""

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

Перед `snapshot`:

```python
    def readiness(self) -> dict:
        """Что уже переведено, а что ещё пусто — для страницы, которая ждёт на месте.

        Зритель ставит ролик на паузу там, где ждёт: перевод идёт от текущего места
        просмотра батчами, поэтому «переведено 30 из 40» ничего не говорит о том,
        есть ли субтитры именно здесь. pending — слитые интервалы реплик, до которых
        рука ещё не дошла. translatedThrough — начало первого такого интервала,
        то есть до какой секунды перевод идёт сплошняком; None значит, что
        непереведённого не осталось вовсе.
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

В `snapshot()` добавить `**self.readiness()` последним элементом словаря.

- [ ] **Step 4: Add readiness to the two event shapes**

В каждом из трёх мест `_emit({"type": "cues", "cues": ...})` (строки 1510, 1589, 1977) и двух `_emit({"type": "segmented", "total": ...})` (1006, 1285) добавить `**job.readiness()`:

```python
job._emit({"type": "segmented", "total": len(job.cues), **job.readiness()})
job._emit({"type": "cues", "cues": [c.payload() for c in batch_cues], **job.readiness()})
```

- [ ] **Step 5: Update the snapshot-keys check**

В `tests/test_language_wire.py` комментарий и проверку на строке 103 заменить на:

```python
# Ключи снимка до плана о языке. pending и translatedThrough добавил план страницы
# для планшета; расширение не читает ни одного из трёх, поэтому любой четвёртый
# новый ключ — ошибка.
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

### Task 7: Сохранённые ссылки и разбор плейлиста

**Files:**
- Modify: `helper/sources/ytdlp.py:210-238` (`_ytdlp_cmd` — `playlist`), после `fetch_video`
- Create: `helper/links.py`
- Modify: `helper/app.py` (после эндпоинтов видео)
- Test: `tests/test_links.py` (создать)

**Interfaces:**
- Produces:
  - `probe_url(url: str, timeout: float = 60.0, *, cookies: Path | None = None) -> dict` → `{"kind": "video" | "playlist", "title": str, "videos": [{"videoId", "title", "duration"}]}`
  - `helper.links`: `LINKS_PATH`, `MAX_LINKS = 200`, `Link(id, url, title, kind)` с `wire()`, `link_id(url) -> str`, `is_youtube_url(url) -> bool`, `load(path=None) -> list[Link]`, `save(links, path=None)`, `add(url, title, kind, path=None) -> Link`, `remove(link_id, path=None) -> bool`
  - `GET /v1/links` → `{"links": [...]}`; `POST /v1/links` (тело `{"url"}`) → `{"link": {...}}`; `DELETE /v1/links/{id}` → `{"removed": bool}`; `GET /v1/links/{id}/videos` → `{"title": str, "videos": [{"videoId", "title", "duration", "state"}]}`

- [ ] **Step 1: Write the failing tests**

```python
"""Ссылки: только YouTube, без дублей, и разбор плейлиста плоским списком."""

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

Плюс тесты эндпоинтов в том же файле (фикстура — чтобы порядок тестов ничего не значил: `LINKS_PATH` у сеанса общий):

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
    # Список сразу говорит, что можно смотреть мгновенно.
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

В `_ytdlp_cmd` заменить `"--no-playlist"` на `*(["--yes-playlist"] if playlist else ["--no-playlist"])`, добавив параметр `playlist: bool = False` (докстрока: «плейлист читается только для страницы ссылок; всё остальное по-прежнему берёт один ролик»).

```python
def probe_url(url: str, timeout: float = 60.0, *, cookies: Path | None = None) -> dict:
    """Что лежит по ссылке: один ролик или плейлист, плоским списком без захода в каждый.

    --flat-playlist берёт только то, что YouTube отдал в самом ответе: для списка
    воспроизведения это пачка id и названий, один запрос вместо сотни.
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

`LINKS_PATH = CONFIG_DIR / "links.json"`, `MAX_LINKS = 200`, `class TooManyLinks(RuntimeError)`. `is_youtube_url` — `urlsplit`, схема `http`/`https`, хост ровно `youtu.be`, `youtube.com` или оканчивается на `.youtube.com`. `link_id` — `hashlib.sha1(url.encode()).hexdigest()[:12]`. Функции принимают `path: Path | None = None` и берут `LINKS_PATH` при `None` (иначе `monkeypatch.setattr(links, "LINKS_PATH", ...)` не подействует: значение по умолчанию привязывается при определении функции). `load` на битом или отсутствующем файле возвращает `[]`; `save` пишет через `path.with_suffix(".tmp")` + `os.replace`, каталог создаёт. `add` идемпотентен по `link_id`, сверяет `MAX_LINKS` до записи, ставит новую ссылку первой.

- [ ] **Step 5: Add the endpoints**

В `helper/app.py`: `from helper import links`, `from helper.sources.ytdlp import YtDlpError, probe_url`.

```python
LINKS_REFUSAL = "нужна ссылка на YouTube"


@app.get("/v1/links", dependencies=[Depends(auth)])
async def list_links() -> dict:
    return {"links": [link.wire() for link in links.load()]}


@app.post("/v1/links", dependencies=[Depends(auth)])
async def add_link(payload: dict) -> dict:
    """Сохранить ссылку. Название и вид берём у YouTube, а не у человека."""
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
    """Ролики ссылки плоским списком. Состояние картинки — у каждого сразу.

    Список отвечает на вопрос «что можно смотреть мгновенно»: ready уже лежит
    в кэше, preparing качается, absent — ничего нет. Спрашивать про каждый ролик
    отдельно значило бы десяток запросов на открытие плейлиста.
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

### Task 8: Разметка и стили страницы

**Files:**
- Modify: `page/index.html` (заменить заглушку)
- Create: `page/style.css`, `page/app.js` (заглушка)
- Test: `tests/test_page_serving.py` (дополнить)

**Interfaces:**
- Produces: разметку с идентификаторами, на которые рассчитывает `page/app.js` (задача 10): `setup`, `token-input`, `token-save`, `setup-message`, `app`, `home`, `links`, `link-form`, `link-url`, `episodes`, `episodes-title`, `stage`, `video`, `overlay`, `status`, `fullscreen`, `back`, `title`.

- [ ] **Step 1: Write the failing tests**

Дописать в `tests/test_page_serving.py`:

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
Expected: FAIL — 404 на `/style.css`.

- [ ] **Step 3: Write `page/index.html`**

Три экрана в одном документе, переключаются атрибутом `hidden`:

- `#setup` — поле `#token-input`, кнопка `#token-save`, `#setup-message`;
- `#home` — список сохранённых ссылок `#links`, форма `#link-form` с полем `#link-url` и кнопкой отправки, блок эпизодов `#episodes` с заголовком `#episodes-title`;
- `#stage` — `<video id="video" playsinline preload="metadata">`, слой `#overlay` (пустой, его заполняет `app.js`), строка `#status`, кнопка `#fullscreen`, кнопка `#back` и `#title` с названием ролика.

Подключаются `<link rel="stylesheet" href="/style.css">` и `<script type="module" src="/app.js"></script>`. `style.css` пишется здесь целиком. `page/app.js` создаётся заглушкой из одной строки-комментария: ссылка из разметки не должна вести в 404, а настоящая проводка — задача 10.

- [ ] **Step 4: Write `page/style.css`**

Тёмный фон `#0d0d0f`, видео по центру, `#stage` занимает экран целиком. Слой `#overlay` — тот же вид, что в `extension/content.js`: строка `22px` на `rgba(8,8,8,.78)`, отступ снизу `calc(8% + var(--lift, 0px))`, ширина не больше `88%`, `.line` белым, `.box.failed .line` цветом `#ffd9a0` и курсивом, `.box.hidden` скрывает слой целиком. Крупнее шрифт на широком экране: `@media (min-width: 900px) { :root { --font: 34px } }`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_serving.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add page/index.html page/style.css page/app.js tests/test_page_serving.py
git commit -m "Разметка страницы планшета: экран токена, ссылки, сцена с видео и слоем"
```

---

### Task 9: Чистая логика страницы

**Files:**
- Create: `page/package.json`, `page/logic.js`, `page/logic.test.mjs`
- Test: `tests/test_page_logic.py` (создать, мост к node)

**Interfaces:**
- Produces (ES-модуль, экспортируемые имена — контракт для задачи 10):
  - `activeCueIndex(cues, t) -> number` — индекс реплики, где `start <= t < end`, иначе `-1`
  - `pendingAt(pending, t) -> boolean` — попадает ли `t` в непереведённый интервал
  - `stageText(stage, state) -> string` — надпись о ходе работы; пустая на `done`
  - `languageText(language) -> string` — строка о языке речи; пустая, если решения нет
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
  // Непереведённая реплика: study без перевода на экран не идёт.
  assert.deepEqual(
    cueLines({ lines: [], study: "Jedan", failed: false }),
    { target: "", secondary: "", failed: false },
  );
  // Вторая строка совпала с первой — её не показываем.
  assert.deepEqual(
    cueLines({ lines: ["Jedan"], study: "Jedan", failed: false }),
    { target: "Jedan", secondary: "", failed: false },
  );
});

test("a failed Russian line does not take the Montenegrin line away", () => {
  // Правило спеки: вторая строка гаснет только когда её язык совпал с первой,
  // а не когда не удался перевод. Иначе неудача русского отнимает ровно то,
  // ради чего всё затеяно.
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
"""Мост к тестам страницы: чистая логика гоняется node --test, отсюда — одним pytest."""

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
Expected: FAIL — нет `page/logic.js`.

- [ ] **Step 3: Add `page/package.json` and `page/logic.js`**

`page/package.json`: `{"type": "module", "private": true}` — без него node читает `logic.js` как CommonJS и падает на `export`, а браузеру поле безразлично.

`logic.js` — только чистые функции, без `document` и `fetch`; значения надписей ровно те, что в тестах. `cueLines` собирает `target` через `"\n"` из `cue.lines || []`, `secondary` — `cue.study`, если он непуст и не равен `target`. `formatTime` — часы выводятся, только когда они есть.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_logic.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add page/package.json page/logic.js page/logic.test.mjs tests/test_page_logic.py
git commit -m "Чистая логика страницы: активная реплика, пустоты перевода и надписи стадий"
```

---

### Task 10: Проводка страницы

**Files:**
- Modify: `page/app.js` (заглушка → настоящий файл)
- Test: `tests/test_page_wiring.py` (создать)

**Interfaces:**
- Consumes: `page/logic.js` (все шесть функций), `POST /v1/jobs`, `POST/GET /v1/videos/{id}`, `GET /v1/videos/{id}/file`, `/v1/links*`, `/v1/ws`, `POST /v1/jobs/{id}/playhead`.
- Produces: рабочую страницу; никаких новых серверных контрактов.

- [ ] **Step 1: Write the failing test**

```python
"""Проводка страницы: каждый элемент, который ищет app.js, есть в разметке."""

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
Expected: FAIL — `assert set()` (в заглушке нет ни одного `getElementById`).

- [ ] **Step 3: Implement `page/app.js`**

Модуль, импортирует шесть функций из `./logic.js`. Состояние — один объект: `videoId`, `cues` (по `i`), `pending`, `language`, `stage`, `waiting`, `error`, `retryable`, `total`, `translated`, `title`. Токен — в `localStorage` под ключом `yt-trans-token`.

Обязанности, каждая отдельной функцией:

- `api(path, options)` — `fetch` с `Authorization: Bearer`, разбор JSON; на 401 показать `#setup`, на прочий не-200 бросить `Error(detail)`.
- `saveToken()` — сохранить значение `#token-input`, убрать `#setup`, вызвать `start()`.
- `start()` — `connectSocket()`, `loadLinks()`.
- `loadLinks()` — `GET /v1/links`, нарисовать в `#links` кнопку на ссылку и кнопку удаления; пустой список — надпись «пока пусто, добавь ссылку».
- `addLink(event)` — `preventDefault`, `POST /v1/links`, перерисовать.
- `dropLink(id)` — `DELETE /v1/links/{id}`, перерисовать.
- `openLink(id)` — `GET /v1/links/{id}/videos`, `#episodes-title` = название, `#episodes` — кнопки эпизодов с названием и пометкой состояния (`ready` — «готово», `preparing` — «готовится», `error` — «не скачалось», `absent` — пусто); одиночный ролик — сразу `play(videoId, title)`.
- `play(videoId, title)` — спрятать `#home`, показать `#stage`, `#title` = название; `POST /v1/jobs` с `{videoId, targetLang: "ru", authorTrack: true}`; `watch(videoId)`; `prepareVideo()`.
- `connectSocket()` — `ws://<host>/v1/ws?token=…`; на `open` повторить `watch(videoId)`, если ролик выбран; на `close` — переподключение через 1 с; на `message` — `handleMessage`.
- `watch(videoId)` — отправить `{op: "watch", videoId, targetLang: "ru", authorTrack: true}` и сразу за ним `{op: "playhead", videoId, t: 0, clientId}`. `clientId` — `crypto.randomUUID()` из `localStorage`, один на страницу: без него демон считает планшет тем же клиентом, что и вкладки на компьютере.
- `handleMessage(message)` — по `message.type`: `snapshot` заменяет всё состояние целиком; `state` пишет `stage`, `waiting`, `error` (из `message.message`), `retryable`; `language` пишет `language`; `segmented` пишет `total` и `pending`; `cues` вливает реплики по `i` и пишет `pending`; `ping` игнорируется. После каждого — `render()`.
- `render()` — `#status` = `stageText(state.stage, state)`, скрыть, когда пусто; подпись о языке — `languageText(state.language)` отдельной строкой в `#status`.
- `prepareVideo()` — `POST /v1/videos/{id}`, затем опрос `GET /v1/videos/{id}` раз в 2 с; `ready` → `video.src = "/v1/videos/" + id + "/file?token=" + encodeURIComponent(token)`, `video.play()`; `error` → `#status` = сообщение и остановить опрос; `preparing` → `#status` = «готовлю картинку…».
- `paint()` — `index = activeCueIndex(state.cues, video.currentTime)`; индекс найден → `cueLines`; иначе `pendingAt(state.pending, video.currentTime)` → `{target: "перевод ещё не готов…", secondary: "", failed: true}`; иначе спрятать слой. Собирает теневое дерево один раз (`attachShadow({mode: "open"})` на `#overlay`) и дальше только меняет `textContent` и классы, как `extension/content.js`.
- `startClock()` — `requestVideoFrameCallback`, если он есть, иначе событие `timeupdate`; каждый тик — `paint()`.
- `reportPlayhead()` — раз в секунду, пока `!video.paused`, `{op: "playhead", videoId, t: video.currentTime, clientId}`.
- `fullscreen()` — `#stage.requestFullscreen()` / `document.exitFullscreen()`.
- `back()` — остановить видео (`video.pause()`, `video.removeAttribute("src")`, `video.load()`), показать `#home`, спрятать `#stage`.

Слой должен зеркалить `extension/content.js`: две строки, вторая скрывается, когда пуста, `failed` даёт особый вид. **Отличие от расширения, обязательное по спеке:** `failed` никогда не убирает вторую строку — она гаснет только если пуста или совпала с первой (это уже сделано в `cueLines` из задачи 9), а класс `failed` лишь меняет вид первой строки.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_page_wiring.py tests/test_page_serving.py -v && .venv/bin/python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add page/app.js tests/test_page_wiring.py
git commit -m "Проводка страницы: токен, ссылки, видео с демона, сокет и слой субтитров"
```

---

### Task 11: Документация

**Files:**
- Create: `docs/tablet.md`
- Modify: `README.md`, `docs/running.md:199`

**Interfaces:**
- Produces: инструкцию; кода не меняет.

- [ ] **Step 1: Write `docs/tablet.md`**

Разделы, по одному абзацу каждый: «Что это» (две строки поверх ролика, без расширения и без плеера YouTube; титры не запекаются, mp4 идёт чистым); «Включить сеть» (`[server] host = "0.0.0.0"`, `systemctl --user restart yt-trans`, `loginctl enable-linger` по `docs/running.md`); «Токен» (взять из `~/.config/yt-trans/token`, вставить на странице один раз); «Открыть на планшете» (`http://<адрес-компьютера>:8756/`); «Ссылки» (что сохраняются только YouTube-адреса, что плейлист раскрывается списком); «Почему видео готовится» (скачивание и склейка, ~400 МБ на 26 минут); «Диск» (кэш `~/.cache/yt-trans/videos`, потолок 20 ГиБ, старые вытесняются); «Если не работает» (пустой экран — проверь токен; «готовлю картинку…» долго — смотри журнал демона; 403 — смотри Origin).

- [ ] **Step 2: Point README and running.md at it**

В `README.md` — короткий раздел «Страница для планшета» со ссылкой на `docs/tablet.md`. В `docs/running.md` в конце раздела «Какой язык решил демон» — строка «Страница для планшета и сеть: `docs/tablet.md`».

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, счёт вырос на число добавленных тестов.

- [ ] **Step 4: Commit**

```bash
git add docs/tablet.md README.md docs/running.md
git commit -m "Как включить страницу планшета: сеть, токен, кэш и что делать, если пусто"
```
