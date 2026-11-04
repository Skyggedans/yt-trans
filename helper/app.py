"""Локальный демон: HTTP для управления, WebSocket для потока реплик.

Слушает петлю по умолчанию. Транспорт — WebSocket, а не SSE: в MV3 service worker
умирает, если ответ fetch идёт дольше 30 секунд, а распознавание с переводом
двадцатиминутного ролика занимает минуты. Трафик WebSocket при этом сбрасывает
таймер простоя воркера, поэтому соединение само себя поддерживает.

Демон делает исходящие запросы к YouTube по команде со страницы, то есть является
классическим «запутавшимся заместителем». Отсюда токен на каждый запрос и проверка
Origin: без них любая открытая вкладка и любое расширение могли бы через нас качать
что угодно. Расширение ходит только сюда и никогда напрямую в ollama — иначе
пришлось бы выставить OLLAMA_ORIGINS с подстановочным знаком, открыв ollama
пользователя всем установленным расширениям разом.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from helper.env import CONFIG_DIR  # импортируется первым: раскладывает кэши и подключает CUDA

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from helper.jobs import CapacityError, JobManager, Stage, refusal_snapshot

log = logging.getLogger(__name__)

PORT = int(os.environ.get("YT_TRANS_PORT", "8756"))
TOKEN_PATH = CONFIG_DIR / "token"
PAGE_DIR = Path(__file__).resolve().parent.parent / "page"

# Пинг раз в 15 секунд: он не даёт service worker'у в Chrome уснуть по таймауту
# простоя и заодно быстро вскрывает разрыв соединения.
HEARTBEAT = 15.0

# Потолок ожидания в длинном опросе. Меньше тридцати секунд намеренно:
# в Chrome MV3 service worker убивают, если ответ fetch идёт дольше.
POLL_TIMEOUT = 25.0

ALLOWED_ORIGIN_SCHEMES = ("chrome-extension://", "moz-extension://", "safari-web-extension://")

# Признак дорожки автора принимают два места, где разбирается сырой JSON: тело
# POST /v1/jobs и сообщение watch. Оба требуют настоящий boolean и остальное к нему
# не приводят. Признак входит в ключ реестра, и не-boolean лёг бы туда как есть:
# строка "false" или None — призрачным третьим вариантом ролика, которого не найдёт
# ни один точный get (а get без признака к тому же читает None как «признака нет»).
# Числа 0 и 1 в ключ сошли бы за False и True, но в Job.use_author_track и в снимок
# попали бы числами. Параметр /events сюда не относится: его строго разбирает сам
# FastAPI (authorTrack: bool).
AUTHOR_TRACK_REFUSAL = "authorTrack должен быть true или false"


def refusal_event(video_id: str, message: str, retryable: bool) -> dict:
    """Отказ на watch событием сокета: вид тот же, что у падения задания, плюс videoId.

    Задания, от чьего имени сообщить, у отказа нет, а сокету иначе не узнать, о каком
    ролике речь.
    """
    return {
        "type": "state",
        "stage": "error",
        "videoId": video_id,
        "message": message,
        "retryable": retryable,
    }


def load_token() -> str:
    """Токен установки. Создаётся при первом запуске, читается расширением из файла."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()

        if token:
            return token

    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    TOKEN_PATH.chmod(0o600)

    return token


TOKEN = load_token()
manager = JobManager()

# Адрес берётся из [server] после того, как появился сам менеджер и его конфиг.
HOST = manager.config.server.host

app = FastAPI(title="yt-trans helper", docs_url=None, redoc_url=None)

# Заголовки CORS обязательны даже для петли: запрос из service worker несёт
# Origin: chrome-extension://<id> и проходит предварительную проверку. Именно
# отсутствие этих заголовков даёт классические 403 при обращении к ollama.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome|moz|safari-web)-extension://.*$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
    max_age=86400,
)


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
    """Пускаем расширения, свою страницу и запросы без Origin (curl), но не чужие сайты.

    Страница в браузере не может подделать Origin, поэтому это рабочая защита
    от того, чтобы произвольный сайт дёргал демон через привязку DNS к петле.
    """
    if origin and not origin.startswith(ALLOWED_ORIGIN_SCHEMES) and not same_origin(origin, host):
        raise HTTPException(status_code=403, detail="origin не разрешён")


async def auth(
    request: Request,
    origin: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    check_origin(origin, request.headers.get("host"))

    supplied = token

    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    # Сравнение постоянного времени: длина и содержимое токена не должны утекать
    # через время ответа.
    if not supplied or not secrets.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=401, detail="нужен токен установки")


@app.get("/v1/health")
async def health() -> dict:
    """Без токена: расширение по нему находит демон, а человек — диагностирует.

    Секретов не раскрывает: только версии моделей и состояние карты.
    """
    state = await asyncio.to_thread(manager.lease.state)

    return {
        "ok": True,
        "models": manager.config.describe(),
        "gpu": {
            "free_mib": state.free_mib,
            "busy": state.busy,
            "foreign_models": state.foreign_models,
        },
        "cache": manager.cache.stats(),
        "jobs": manager.stats(),
        "token_path": str(TOKEN_PATH),
    }


@app.post("/v1/jobs", dependencies=[Depends(auth)])
async def create_job(payload: dict) -> dict:
    video_id = (payload or {}).get("videoId", "")
    target = (payload or {}).get("targetLang", "ru")
    author_track = (payload or {}).get("authorTrack", True)

    if not video_id:
        raise HTTPException(status_code=400, detail="нужен videoId")

    if not isinstance(author_track, bool):
        raise HTTPException(status_code=400, detail=AUTHOR_TRACK_REFUSAL)

    try:
        job = manager.get_or_create(video_id, target, author_track)
    except CapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return {"videoId": job.video_id, "stage": job.stage.value}


@app.get("/v1/jobs/{video_id}", dependencies=[Depends(auth)])
async def job_snapshot(video_id: str) -> dict:
    job = manager.get(video_id)

    if job is None:
        raise HTTPException(status_code=404, detail="задание не найдено")

    return job.snapshot()


@app.get("/v1/jobs/{video_id}/events", dependencies=[Depends(auth)])
async def job_events(
    video_id: str,
    cursor: int = 0,
    target: str = "ru",
    authorTrack: bool = True,
    create: bool = True,
) -> dict:
    """Длинный опрос: события после cursor, ожидание до POLL_TIMEOUT секунд.

    Обычный HTTP вместо WebSocket. Firefox повышает ws:// до wss:// при обращении
    из контекста расширения — исключение для локального адреса, работающее для
    http://, на WebSocket не распространяется, и соединение молча не поднимается.
    Замерено ловушкой: 13 попыток TLS против 10 успешных обычных запросов.

    Ожидание ограничено 25 секундами намеренно: в Chrome MV3 service worker
    умирает, если ответ идёт дольше 30, а короткие повторяющиеся запросы,
    наоборот, сбрасывают его таймер простоя.
    """
    # create=false — для вкладки, которая ничего не запускала, а подхватила уже
    # сделанное после перезагрузки страницы. Подписка на ролик обычно И ЕСТЬ
    # согласие на работу, поэтому обычный опрос задание создаёт. Но восстановленная
    # вкладка согласия не давала: исчезни задание из реестра, и создание нового
    # означало бы, что F5 сам начал качать аудио и занимать карту. Вариант дорожки
    # берётся тот же, что у обычного опроса: у ролика бывает по заданию на каждый,
    # и вкладка должна подхватить своё, а не соседнее.
    if create:
        try:
            job = manager.get_or_create(video_id, target, authorTrack)
        except CapacityError as exc:
            # Отказ — ответ 200 со снимком в стадии error, а не 429. На ответ не 200
            # расширение пишет «демон ответил …», как о сломанном демоне, и повторяет
            # запрос через паузу, а снимок со стадией error показывает фразой и опрос
            # останавливает: зритель повторит кнопкой, когда захочет. Это единственный
            # путь, которым расширение создаёт задания. Курсор клиента возвращается как
            # есть: у отказа нет журнала, и двигаться клиенту некуда, как в ветке gone ниже.
            return {
                "cursor": cursor,
                "events": [],
                "snapshot": refusal_snapshot(
                    video_id, target, authorTrack, str(exc), retryable=True
                ),
            }
    else:
        job = manager.attach(video_id, authorTrack)

    if job is None:
        return {"gone": True, "cursor": cursor, "events": []}

    return await job.poll(cursor, POLL_TIMEOUT)


@app.get("/v1/jobs/{video_id}/restore", dependencies=[Depends(auth)])
async def restore_job(
    video_id: str,
    target: str = "ru",
    authorTrack: bool = True,
    duration: float | None = None,
) -> dict:
    """Что уже готово по ролику. Ничего не запускает.

    Отличается от /events именно этим: тот создаёт задание, а этот только смотрит.
    Нужен для перезагрузки страницы — согласие зрителя спрашивают один раз за ролик,
    и показать уже сделанное не значит начать качать аудио и занимать карту.

    Чтение идёт прямо в цикле событий, без отдельного потока: файлы дорожек это
    сотни килобайт, разбор укладывается в единицы миллисекунд, а сборка задания
    трогает asyncio.Event, который из чужого потока трогать нельзя.
    """
    job = manager.restore(video_id, target, authorTrack, duration)

    if job is None:
        return {"found": False}

    return {
        "found": True,
        # Стадия нужна расширению, чтобы решить, поднимать ли опрос вообще.
        # По готовому заданию ждать больше нечего, а лишний опрос — это лишние
        # обращения к создающему эндпоинту на каждой открытой вкладке.
        "stage": job.stage.value,
        "cursor": job.journal_end,
        "snapshot": job.snapshot(),
    }


@app.post("/v1/jobs/{video_id}/playhead", dependencies=[Depends(auth)])
async def set_playhead(video_id: str, payload: dict) -> dict:
    """Куда смотрит зритель — двигает приоритет очереди перевода.

    Тело: t — секунды от начала ролика; clientId — необязательный ключ клиента. С ним
    у каждого клиента своя позиция, и перевод идёт по кругу между ними. Без него все
    такие запросы считаются одним клиентом, как до появления ключа.
    """
    jobs = manager.jobs_for(video_id)

    if not jobs:
        raise HTTPException(status_code=404, detail="задание не найдено")

    # Позиция и ключ клиента разбираются один раз, до цикла: проверка одна на все
    # варианты ролика. Отсутствие t, как и раньше, значит начало ролика. Ключ уходит
    # как есть: пригоден он или нет, решает report_playhead, а не обработчик.
    # Расширение ключа не присылает и остаётся единственным клиентом "legacy": для него
    # всё работает, как с одним плейхедом.
    position = float((payload or {}).get("t", 0.0))
    client_id = (payload or {}).get("clientId")

    # Позицию присылают без признака дорожки, и какой из вариантов ролика смотрит
    # клиент, демон не знает. Двигаем все: лишний сдвиг безвреден, а пропущенный
    # оставил бы перевод у зрителя с начала ролика.
    for job in jobs:
        job.report_playhead(client_id, position)

    return {"ok": True}


@app.post("/v1/jobs/{video_id}/drop", dependencies=[Depends(auth)])
async def drop_job(video_id: str, payload: dict | None = Body(default=None)) -> dict:
    """Сбросить задание ролика. Тело необязательно; в нём clientId — ключ клиента.

    С clientId сбрасывается только то, что нужно одному этому клиенту: с ролика снимается
    его запись, а задание отменяется, лишь когда живых клиентов не осталось. Без clientId
    отменяется всё, как до появления ключей. Расширение шлёт этот запрос вовсе без тела и
    без Content-Type (extension/background.js, повтор после отказа), и обязательное тело
    превратило бы его в 422. Ответ несёт cancelled: отменено ли хоть одно задание.
    """
    # Ключ уходит как есть: пригоден он или нет, решает Job.forget_client, а не обработчик.
    client_id = (payload or {}).get("clientId")

    return {"dropped": video_id, "cancelled": manager.drop(video_id, client_id)}


@app.websocket("/v1/ws")
async def websocket(socket: WebSocket) -> None:
    """Один сокет на вкладку. Клиент говорит, что смотрит, демон шлёт реплики."""
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

    await socket.accept()

    # Одна очередь на соединение: все наблюдаемые задания пишут в неё, читатель
    # ждёт ровно на ней. Ожидание на нескольких очередях сразу теряет сообщения —
    # брошенная после FIRST_COMPLETED задача `get()` уже успевает забрать элемент.
    events: asyncio.Queue = asyncio.Queue()
    watched: set[str] = set()
    pump: asyncio.Task | None = None

    async def forward() -> None:
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=HEARTBEAT)
            except TimeoutError:
                # Пинг держит service worker живым: трафик по сокету сбрасывает
                # его таймер простоя в 30 секунд.
                await socket.send_json({"type": "ping"})
                continue

            await socket.send_json(event)

    try:
        pump = asyncio.create_task(forward())

        while True:
            message = await socket.receive_json()
            op = message.get("op")

            if op == "watch":
                video_id = message.get("videoId", "")

                if video_id:
                    author_track = message.get("authorTrack", True)

                    if not isinstance(author_track, bool):
                        # Отказ уходит через очередь сокета, а не прямой отправкой: в
                        # сокет пишет один насос, и отказ встаёт в общий порядок с
                        # остальными событиями. Отправка из цикла приёма была бы вторым
                        # писателем. Подписка при этом не делается.
                        events.put_nowait(
                            refusal_event(video_id, AUTHOR_TRACK_REFUSAL, retryable=False)
                        )

                        continue

                    # get_or_create вызывается ВСЕГДА, а не только на первой подписке:
                    # это единственное место, откуда возобновляется приостановленное
                    # задание. Под прежним условием повторный watch по уже наблюдаемому
                    # ролику был полной пустышкой, и после вытеснения задание висело
                    # в паузе, пока клиент не разорвёт соединение целиком.
                    try:
                        job = manager.get_or_create(
                            video_id, message.get("targetLang", "ru"), author_track
                        )
                    except CapacityError as exc:
                        # Через ту же очередь и тем же видом, что отказ выше, но
                        # повторяемый: место освободится само, и зрителю нужна кнопка
                        # повтора. Подписки нет — не на что.
                        events.put_nowait(refusal_event(video_id, str(exc), retryable=True))

                        continue

                    # Сокет следит не больше чем за одним вариантом ролика, и watch
                    # с другим признаком значит «теперь мне нужен этот»: очередь
                    # переезжает на его задание. Прежнее остаётся как есть — оно идёт
                    # у другого клиента или понадобится при обратном переключении, —
                    # но события из него сюда больше не приходят. Кроме снимка, события
                    # заданий не несут ни ролика, ни варианта, и сокет, подписанный на
                    # оба, получал бы два потока в одном, которые клиенту не разделить.
                    for other in manager.jobs_for(video_id):
                        if other is not job:
                            other.unsubscribe(events)

                    # Подписка шлёт догоняющий снимок, поэтому очередь, что уже стоит на
                    # этом задании, не подписываем заново: повторный watch (он же
                    # возобновление, см. выше) слал бы весь снимок каждый раз. Спрашиваем
                    # у самого задания, а не по `video_id in watched`: тот знает ролик,
                    # но не вариант, и после переезда на другой вариант ответил бы
                    # неверно.
                    if not job.is_subscribed(events):
                        job.subscribe(events)

                    # Подписывать ли, решает не watched, а само задание. Он остался
                    # списком роликов сокета: по нему unwatch и очистка при разрыве
                    # находят, с каких заданий снять очередь.
                    watched.add(video_id)

            elif op == "playhead":
                # Двигает приоритет очереди: следующий батч берётся вокруг
                # текущего места просмотра, а не с начала ролика. Сдвигаются все
                # варианты ролика, как и у POST /playhead: признака дорожки тут нет.
                # Позиция и ключ клиента разбираются так же, как в set_playhead, и тоже
                # до цикла.
                position = float(message.get("t", 0.0))
                client_id = message.get("clientId")

                for job in manager.jobs_for(message.get("videoId", "")):
                    job.report_playhead(client_id, position)

            elif op == "unwatch":
                video_id = message.get("videoId", "")
                watched.discard(video_id)

                # Очередь сокета снимается со всех вариантов ролика: на каком из них
                # она оказалась, сокет не помнит, а снятая с лишнего ничего не меняет.
                for job in manager.jobs_for(video_id):
                    job.unsubscribe(events)

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("сокет упал")
    finally:
        if pump:
            pump.cancel()

            # Гасим и обычные исключения, не только отмену. Насос падает на мёртвом
            # сокете штатно: send_json поднимает ClientDisconnected, который starlette
            # превращает в WebSocketDisconnect — это Exception, а не CancelledError.
            # Пропущенное наружу, оно вылетало бы из finally ДО отписки, навсегда
            # оставляя очередь соединения в подписчиках задания.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await pump

        for video_id in watched:
            for job in manager.jobs_for(video_id):
                job.unsubscribe(events)


# Монтируется последним: маршруты выше добавлены раньше и выигрывают у монтирования,
# так что /v1/* остаётся API, а всё прочее ищется файлом. Страница публичная —
# секретов в ней нет, а токен она спрашивает у человека сама.
app.mount("/", StaticFiles(directory=PAGE_DIR, html=True), name="page")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Сторож карты опрашивает ollama раз в секунду, и httpx пишет каждый опрос
    # строкой INFO — 86 тысяч строк в сутки, в которых тонет всё остальное.
    # Диагностика зависшего перевода начиналась с того, что этот шум надо было
    # сначала отфильтровать, чтобы увидеть единственную важную строку.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("токен установки лежит в %s", TOKEN_PATH)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
