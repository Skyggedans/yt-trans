"""Локальный демон: HTTP для управления, WebSocket для потока реплик.

Слушает только петлю. Транспорт — WebSocket, а не SSE: в MV3 service worker
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

from helper.env import CONFIG_DIR  # импортируется первым: раскладывает кэши и подключает CUDA

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from helper.jobs import JobManager, Stage

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = int(os.environ.get("YT_TRANS_PORT", "8756"))
TOKEN_PATH = CONFIG_DIR / "token"

# Пинг раз в 15 секунд: он не даёт service worker'у в Chrome уснуть по таймауту
# простоя и заодно быстро вскрывает разрыв соединения.
HEARTBEAT = 15.0

# Потолок ожидания в длинном опросе. Меньше тридцати секунд намеренно:
# в Chrome MV3 service worker убивают, если ответ fetch идёт дольше.
POLL_TIMEOUT = 25.0

ALLOWED_ORIGIN_SCHEMES = ("chrome-extension://", "moz-extension://", "safari-web-extension://")


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


def check_origin(origin: str | None) -> None:
    """Пускаем расширения и запросы без Origin (curl), но не обычные страницы.

    Страница в браузере не может подделать Origin, поэтому это рабочая защита
    от того, чтобы произвольный сайт дёргал демон через привязку DNS к петле.
    """
    if origin and not origin.startswith(ALLOWED_ORIGIN_SCHEMES):
        raise HTTPException(status_code=403, detail="origin не разрешён")


async def auth(
    origin: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    check_origin(origin)

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

    if not video_id:
        raise HTTPException(status_code=400, detail="нужен videoId")

    job = manager.get_or_create(video_id, target)

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
    # означало бы, что F5 сам начал качать аудио и занимать карту.
    job = manager.get_or_create(video_id, target, authorTrack) if create else manager.attach(video_id)

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
        "cursor": job._dropped + len(job.events),
        "snapshot": job.snapshot(),
    }


@app.post("/v1/jobs/{video_id}/playhead", dependencies=[Depends(auth)])
async def set_playhead(video_id: str, payload: dict) -> dict:
    """Куда смотрит зритель — двигает приоритет очереди перевода."""
    job = manager.get(video_id)

    if job is None:
        raise HTTPException(status_code=404, detail="задание не найдено")

    job.playhead = float((payload or {}).get("t", 0.0))

    return {"ok": True}


@app.post("/v1/jobs/{video_id}/drop", dependencies=[Depends(auth)])
async def drop_job(video_id: str) -> dict:
    manager.drop(video_id)

    return {"dropped": video_id}


@app.websocket("/v1/ws")
async def websocket(socket: WebSocket) -> None:
    """Один сокет на вкладку. Клиент говорит, что смотрит, демон шлёт реплики."""
    origin = socket.headers.get("origin")
    token = socket.query_params.get("token")

    if (origin and not origin.startswith(ALLOWED_ORIGIN_SCHEMES)) or not (
        token and secrets.compare_digest(token, TOKEN)
    ):
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
                    # get_or_create вызывается ВСЕГДА, а не только на первой подписке:
                    # это единственное место, откуда возобновляется приостановленное
                    # задание. Под прежним условием повторный watch по уже наблюдаемому
                    # ролику был полной пустышкой, и после вытеснения задание висело
                    # в паузе, пока клиент не разорвёт соединение целиком.
                    job = manager.get_or_create(video_id, message.get("targetLang", "ru"))

                    if video_id not in watched:
                        job.subscribe(events)
                        watched.add(video_id)

            elif op == "playhead":
                job = manager.get(message.get("videoId", ""))

                if job:
                    # Двигает приоритет очереди: следующий батч берётся вокруг
                    # текущего места просмотра, а не с начала ролика.
                    job.playhead = float(message.get("t", 0.0))

            elif op == "unwatch":
                video_id = message.get("videoId", "")
                job = manager.get(video_id)
                watched.discard(video_id)

                if job:
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
            job = manager.get(video_id)

            if job:
                job.unsubscribe(events)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Сторож карты опрашивает ollama раз в секунду, и httpx пишет каждый опрос
    # строкой INFO — 86 тысяч строк в сутки, в которых тонет всё остальное.
    # Диагностика зависшего перевода начиналась с того, что этот шум надо было
    # сначала отфильтровать, чтобы увидеть единственную важную строку.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("токен установки лежит в %s", TOKEN_PATH)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
