"""Local daemon: HTTP for control, WebSocket for the utterance stream.

Listens on loopback by default. The transport is WebSocket, not SSE: in MV3 the service
worker dies if a fetch response takes longer than 30 seconds, and recognizing and
translating a twenty-minute video takes minutes. WebSocket traffic meanwhile resets the
worker's idle timer, so the connection keeps itself alive.

The daemon makes outgoing requests to YouTube on command from the page, i.e. it is a
classic "confused deputy". There's no install token: the owner removed it so that the
tablet page and the extension wouldn't require a key. Tabs of foreign sites are guarded
against by the Origin check — it remained; requests without Origin (curl, <video>,
non-browser apps) pass. The extension talks only here and never directly to ollama —
otherwise one would have to expose OLLAMA_ORIGINS with a wildcard, opening the user's
ollama to all installed extensions at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from helper.env import CONFIG_DIR  # imported first: lays out the caches and loads CUDA

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from helper.jobs import CapacityError, JobManager, Stage, refusal_snapshot
from helper import links
from helper.sources import ytdlp
from helper.sources.ytdlp import YtDlpError, probe_url

log = logging.getLogger(__name__)

PORT = int(os.environ.get("YT_TRANS_PORT", "8756"))
# Root certificate of the same directory that packaging/make-tls.sh issues.
# The path is deliberately not from the config: the file is needed BEFORE https is
# enabled, when nothing about tls is written in config.toml yet.
CA_PATH = CONFIG_DIR / "tls" / "ca.crt"
PAGE_DIR = Path(__file__).resolve().parent.parent / "page"

# A ping every 15 seconds: it keeps Chrome's service worker from falling asleep on the
# idle timeout and at the same time quickly exposes a broken connection.
HEARTBEAT = 15.0

# The wait cap in long polling. Deliberately less than thirty seconds:
# in Chrome MV3 the service worker is killed if a fetch response takes longer.
POLL_TIMEOUT = 25.0

ALLOWED_ORIGIN_SCHEMES = ("chrome-extension://", "moz-extension://", "safari-web-extension://")

# The author-track flag is accepted in two places where raw JSON is parsed: the
# POST /v1/jobs body and the watch message. Both require a real boolean and don't coerce
# anything else to it. The flag is part of the registry key, and a non-boolean would land
# there as is: the string "false" or None — as a phantom third variant of the video that no
# exact get would find (and a get without the flag also reads None as "no flag").
# The numbers 0 and 1 would pass for False and True in the key, but would land in
# Job.use_author_track and in the snapshot as numbers. The /events parameter doesn't
# belong here: FastAPI parses it strictly itself (authorTrack: bool).
AUTHOR_TRACK_REFUSAL = "authorTrack must be true or false"


def refusal_event(video_id: str, message: str, retryable: bool) -> dict:
    """Refusal on watch as a socket event: the same shape as a job failure, plus videoId.

    A refusal has no job in whose name to report, and otherwise the socket wouldn't know
    which video is meant.
    """
    return {
        "type": "state",
        "stage": "error",
        "videoId": video_id,
        "message": message,
        "retryable": retryable,
    }


manager = JobManager()

# The address is taken from [server] after the manager itself and its config have appeared.
HOST = manager.config.server.host

# Addresses on which the daemon is audible only to the owner: there /v1/health tells everything.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

app = FastAPI(title="yt-trans helper", docs_url=None, redoc_url=None)

# CORS headers are mandatory even for loopback: a request from the service worker carries
# Origin: chrome-extension://<id> and goes through a preflight. It's precisely
# the absence of these headers that gives the classic 403s when accessing ollama.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome|moz|safari-web)-extension://.*$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type"],
    max_age=86400,
)


def same_origin(origin: str, host: str | None) -> bool:
    """The origin of the page the daemon itself serves: the same host and port as in Host.

    The authority is compared whole, not just the name: the browser sends the port
    in Origin exactly when it's in the address. Pages served by the daemon
    this lets through, any other site — not.
    """
    if not host:
        return False

    parts = urlsplit(origin)

    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.lower()


def origin_allowed(origin: str | None, host: str | None = None) -> bool:
    """Extensions, our own page and requests without Origin (curl) pass; foreign sites don't.

    A page in a browser can't forge Origin, so this is a working defense
    against an arbitrary site pulling the daemon via DNS binding to loopback.
    """
    if not origin:
        return True

    return origin.startswith(ALLOWED_ORIGIN_SCHEMES) or same_origin(origin, host)


def check_origin(origin: str | None, host: str | None = None) -> None:
    if not origin_allowed(origin, host):
        raise HTTPException(status_code=403, detail="origin is not allowed")


async def guard(
    request: Request,
    origin: str | None = Header(default=None),
) -> None:
    """The only pass to the API: Origin. There's no install token anymore.

    The extension and the tablet page go without a key, so only Origin closes the door:
    a foreign page in a browser can't forge it.
    """
    check_origin(origin, request.headers.get("host"))


@app.get("/v1/health")
async def health(request: Request, origin: str | None = Header(default=None)) -> dict:
    """Open to all: the extension uses it to find the daemon, and a person — to diagnose.

    "Alive" goes out to everyone. Models, the card, the cache and the job count go only
    to the owner: to a client on the machine itself, and not to a page from the internet
    that DNS rebinding brought to loopback. Model names, job state, cache size and the
    path to settings with the user's name are a map of someone else's machine: the home
    network does not need it, and a foreign page must not have it.

    Who asks is the peer address, not the bind address. [server] host = "0.0.0.0" is set
    for the tablet in the next room — and it used to hide the models from the extension
    on the same machine, which printed "undefined" in the settings instead.
    """
    peer = request.client
    if peer is None or peer.host not in LOOPBACK_HOSTS:
        return {"ok": True}

    if not origin_allowed(origin, request.headers.get("host")):
        return {"ok": True}

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
    }


@app.post("/v1/jobs", dependencies=[Depends(guard)])
async def create_job(payload: dict) -> dict:
    video_id = (payload or {}).get("videoId", "")
    target = (payload or {}).get("targetLang", "ru")
    author_track = (payload or {}).get("authorTrack", True)

    if not video_id:
        raise HTTPException(status_code=400, detail="videoId is required")

    if not isinstance(author_track, bool):
        raise HTTPException(status_code=400, detail=AUTHOR_TRACK_REFUSAL)

    try:
        job = manager.get_or_create(video_id, target, author_track)
    except CapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return {"videoId": job.video_id, "stage": job.stage.value}


@app.get("/v1/jobs/{video_id}", dependencies=[Depends(guard)])
async def job_snapshot(video_id: str) -> dict:
    job = manager.get(video_id)

    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    return job.snapshot()


@app.get("/v1/jobs/{video_id}/events", dependencies=[Depends(guard)])
async def job_events(
    video_id: str,
    cursor: int = 0,
    target: str = "ru",
    authorTrack: bool = True,
    create: bool = True,
) -> dict:
    """Long polling: events after cursor, waiting up to POLL_TIMEOUT seconds.

    Plain HTTP instead of WebSocket. Firefox upgrades ws:// to wss:// when accessed
    from an extension context — the local-address exception that works for
    http:// doesn't extend to WebSocket, and the connection silently fails to come up.
    Measured with a trap: 13 TLS attempts versus 10 successful plain requests.

    The wait is limited to 25 seconds deliberately: in Chrome MV3 the service worker
    dies if the response takes longer than 30, while short repeated requests,
    on the contrary, reset its idle timer.
    """
    # create=false — for a tab that didn't start anything but picked up what was
    # already done after a page reload. Subscribing to a video usually IS
    # consent to work, so a normal poll creates a job. But a restored
    # tab didn't give consent: if the job disappears from the registry, creating a new
    # one would mean F5 itself started downloading audio and occupying the card. The track
    # variant is taken the same as for a normal poll: a video has a job for each,
    # and the tab must pick up its own, not the neighbouring one.
    if create:
        try:
            job = manager.get_or_create(video_id, target, authorTrack)
        except CapacityError as exc:
            # A refusal is a 200 response with a snapshot in the error stage, not a 429. On a
            # non-200 response the extension writes "the daemon answered …", as about a broken daemon, and repeats
            # the request after a pause, while a snapshot with the error stage shows it as a phrase and stops
            # the poll: the viewer will repeat with the button when they want. This is the only
            # path by which the extension creates jobs. The client's cursor is returned as
            # is: a refusal has no journal, and the client has nowhere to move, as in the gone branch below.
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


@app.get("/v1/jobs/{video_id}/restore", dependencies=[Depends(guard)])
async def restore_job(
    video_id: str,
    target: str = "ru",
    authorTrack: bool = True,
    duration: float | None = None,
) -> dict:
    """What's already ready for the video. Starts nothing.

    This is exactly how it differs from /events: that one creates a job, this one only looks.
    Needed for a page reload — the viewer's consent is asked once per video,
    and showing what's already done doesn't mean starting to download audio and occupy the card.

    The read goes directly in the event loop, without a separate thread: track files are
    hundreds of kilobytes, parsing fits in a few milliseconds, and assembling the job
    touches asyncio.Event, which mustn't be touched from a foreign thread.
    """
    job = manager.restore(video_id, target, authorTrack, duration)

    if job is None:
        return {"found": False}

    return {
        "found": True,
        # The stage is needed by the extension to decide whether to start the poll at all.
        # On a ready job there's nothing to wait for, and an extra poll is extra
        # requests to the creating endpoint on every open tab.
        "stage": job.stage.value,
        "cursor": job.journal_end,
        "snapshot": job.snapshot(),
    }


@app.post("/v1/jobs/{video_id}/playhead", dependencies=[Depends(guard)])
async def set_playhead(video_id: str, payload: dict) -> dict:
    """Where the viewer is looking — moves the translation queue priority.

    Body: t — seconds from the start of the video; clientId — an optional client key. With it
    each client has its own position, and translation goes round-robin between them. Without it
    all such requests are counted as one client, as before the key existed.
    """
    jobs = manager.jobs_for(video_id)

    if not jobs:
        raise HTTPException(status_code=404, detail="job not found")

    # The position and client key are parsed once, before the loop: one check for all
    # variants of the video. The absence of t, as before, means the start of the video. The key goes
    # as is: whether it's valid is decided by report_playhead, not by the handler.
    # The extension doesn't send the key and remains the only "legacy" client: for it
    # everything works as with a single playhead.
    position = float((payload or {}).get("t", 0.0))
    client_id = (payload or {}).get("clientId")

    # The position is sent without the track flag, and which of the video's variants the
    # client is watching, the daemon doesn't know. We move all: an extra shift is harmless,
    # while a missed one would leave the translation at the viewer from the start of the video.
    for job in jobs:
        job.report_playhead(client_id, position)

    return {"ok": True}


@app.post("/v1/jobs/{video_id}/drop", dependencies=[Depends(guard)])
async def drop_job(video_id: str, payload: dict | None = Body(default=None)) -> dict:
    """Reset the video's job. The body is optional; in it clientId — the client key.

    With clientId only what this one client needs is reset: its record is removed
    from the video, and the job is cancelled only when no live clients remain. Without clientId
    everything is cancelled, as before keys existed. The extension sends this request with no body and
    no Content-Type (extension/background.js, repeat after refusal), and a mandatory body
    would turn it into a 422. The response carries cancelled: whether at least one job was cancelled.
    """
    # The key goes as is: whether it's valid is decided by Job.forget_client, not by the handler.
    client_id = (payload or {}).get("clientId")

    return {"dropped": video_id, "cancelled": manager.drop(video_id, client_id)}


@app.post("/v1/videos/{video_id}", dependencies=[Depends(guard)])
async def prepare_video(video_id: str) -> dict:
    """Start preparing the mp4. Answers immediately; readiness — a separate GET."""
    if not ytdlp.VIDEO_ID.match(video_id):
        raise HTTPException(status_code=400, detail="invalid video identifier")

    return manager.videos.ensure(
        video_id, cookies=manager.config.youtube.cookies_path
    ).wire()


@app.get("/v1/videos/{video_id}", dependencies=[Depends(guard)])
async def video_status(video_id: str) -> dict:
    if not ytdlp.VIDEO_ID.match(video_id):
        raise HTTPException(status_code=400, detail="invalid video identifier")

    return manager.videos.status(video_id).wire()


@app.api_route(
    "/v1/videos/{video_id}/file", methods=["GET", "HEAD"], dependencies=[Depends(guard)]
)
async def video_file(video_id: str) -> FileResponse:
    """The whole file or a piece: Range is handled by FileResponse, which also sets 206.

    Doesn't ask for a key: <video src> sends no headers, and <video> doesn't send Origin —
    the daemon has one pass, and it isn't there.
    While there's no file — 409 with the state phrase. The page asks for the state first
    and only then sets src, so <video> never gets a non-200.

    HEAD is needed by the page: by Content-Length it decides whether the copy fits on the tablet,
    and shows download percentages. Starlette doesn't send a body on HEAD.
    """
    status = manager.videos.status(video_id)

    if status.state != "ready" or status.path is None:
        raise HTTPException(
            status_code=409, detail=status.message or "the video is still being prepared"
        )

    return FileResponse(status.path, media_type="video/mp4")


LINKS_REFUSAL = "a YouTube link is required"


async def probe_with_account(url: str, cookies: Path | None) -> dict:
    """Parsing a link: anonymously, and on an age restriction — once more with the account.

    The same rule as for downloading a video, and for the same reason: the account
    is substituted only where it's impossible without it. Without this second attempt a link
    to an 18+ video couldn't even be saved, although the daemon can download it.
    """
    try:
        return await asyncio.to_thread(probe_url, url)
    except YtDlpError as exc:
        if not (exc.age_restricted and cookies):
            raise

        return await asyncio.to_thread(probe_url, url, cookies=cookies)



@app.get("/v1/links", dependencies=[Depends(guard)])
async def list_links() -> dict:
    """Links from disk; for a video link — the readiness of the picture and the translation.

    Both flags are computed from the cache, and the id is taken from the address: there's
    no trip to YouTube here, otherwise the list would wait for the network for an icon in each row.
    """
    items = []

    for link in links.load():
        data = link.wire()
        video_id = ytdlp.video_id_in(link.url) if link.kind == "video" else ""

        if video_id:
            data["state"] = manager.videos.status(video_id).state
            data["translated"] = manager.translated(video_id)

        items.append(data)

    return {"links": items}


@app.post("/v1/links", dependencies=[Depends(guard)])
async def add_link(payload: dict) -> dict:
    """Save a link. The title, kind and duration are taken from YouTube, not from the person."""
    url = (payload or {}).get("url", "")

    if not isinstance(url, str) or not links.is_youtube_url(url):
        raise HTTPException(status_code=400, detail=LINKS_REFUSAL)

    try:
        found = await probe_with_account(url, manager.config.youtube.cookies_path)
    except YtDlpError as exc:
        raise HTTPException(status_code=502, detail=f"failed to read the link: {exc}") from exc

    # The duration exists only for a single video: for a playlist one figure would lie
    # about the rest. We save it here so that the list doesn't go to YouTube for
    # the duration in the row.
    videos = found.get("videos") or []
    duration = videos[0].get("duration") if found["kind"] == "video" and videos else None

    try:
        link = links.add(url, found["title"] or url, found["kind"], duration=duration)
    except links.TooManyLinks as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"link": link.wire()}


@app.delete("/v1/links/{link_id}", dependencies=[Depends(guard)])
async def drop_link(link_id: str) -> dict:
    return {"removed": links.remove(link_id)}


@app.get("/v1/links/{link_id}/videos", dependencies=[Depends(guard)])
async def link_videos(link_id: str) -> dict:
    """The link's videos as a flat list. The state of the picture and translation — at once.

    The list answers the question "what can be watched instantly and where not to wait
    for translation": ready — the picture is already in the cache, preparing — it's being downloaded,
    absent — there's nothing; translated — the subtitle track is already made. Asking
    about each video separately would mean a dozen requests per playlist opening.
    """
    link = next((item for item in links.load() if item.id == link_id), None)

    if link is None:
        raise HTTPException(status_code=404, detail="no such link")

    try:
        found = await probe_with_account(link.url, manager.config.youtube.cookies_path)
    except YtDlpError as exc:
        raise HTTPException(status_code=502, detail=f"failed to read the link: {exc}") from exc

    videos = [
        {
            **video,
            "state": (
                manager.videos.status(video["videoId"]).state
                if ytdlp.VIDEO_ID.match(video["videoId"])
                else "absent"
            ),
            "translated": (
                manager.translated(video["videoId"])
                if ytdlp.VIDEO_ID.match(video["videoId"])
                else False
            ),
        }
        for video in found["videos"]
    ]

    return {"title": found["title"], "videos": videos}


@app.websocket("/v1/ws")
async def websocket(socket: WebSocket) -> None:
    """One socket per tab. The client says what it's watching, the daemon sends utterances."""
    origin = socket.headers.get("origin")

    try:
        check_origin(origin, socket.headers.get("host"))
    except HTTPException:
        await socket.close(code=4401)

        return

    await socket.accept()

    # One queue per connection: all watched jobs write to it, the reader
    # waits exactly on it. Waiting on several queues at once loses messages —
    # the `get()` task abandoned after FIRST_COMPLETED has already managed to take an element.
    events: asyncio.Queue = asyncio.Queue()
    watched: set[str] = set()
    pump: asyncio.Task | None = None

    async def forward() -> None:
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=HEARTBEAT)
            except TimeoutError:
                # The ping keeps the service worker alive: socket traffic resets
                # its 30-second idle timer.
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
                        # The refusal goes through the socket queue, not by direct sending: only
                        # one pump writes to the socket, and the refusal takes its place in the common
                        # order with the other events. Sending from the receive loop would be a second
                        # writer. No subscription is made meanwhile.
                        events.put_nowait(
                            refusal_event(video_id, AUTHOR_TRACK_REFUSAL, retryable=False)
                        )

                        continue

                    # get_or_create is called ALWAYS, not only on the first subscription:
                    # it's the only place from which a paused job is resumed. Under the previous
                    # condition a repeat watch on an already watched video was a complete no-op,
                    # and after eviction the job hung in a pause until the client tore down
                    # the whole connection.
                    try:
                        job = manager.get_or_create(
                            video_id, message.get("targetLang", "ru"), author_track
                        )
                    except CapacityError as exc:
                        # Through the same queue and with the same shape as the refusal above, but
                        # retryable: the place will free up by itself, and the viewer needs a retry
                        # button. No subscription — there's nothing to subscribe.
                        events.put_nowait(refusal_event(video_id, str(exc), retryable=True))

                        continue

                    # The socket watches no more than one variant of the video, and a watch
                    # with a different flag means "now I need this one": the queue
                    # moves to its job. The previous one stays as is — it's running
                    # for another client or will be needed on switching back — but events from it
                    # no longer come here. Apart from the snapshot, job events carry neither the video
                    # nor the variant, and a socket subscribed to both would get two streams in one
                    # that the client can't separate.
                    for other in manager.jobs_for(video_id):
                        if other is not job:
                            other.unsubscribe(events)

                    # A subscription sends a catch-up snapshot, so a queue that's already on
                    # this job isn't subscribed again: a repeated watch (which is also
                    # a resumption, see above) would send the whole snapshot every time. We ask
                    # the job itself, not `video_id in watched`: the latter knows the video
                    # but not the variant, and after moving to another variant it would answer
                    # incorrectly.
                    if not job.is_subscribed(events):
                        job.subscribe(events)

                    # Whether to subscribe is decided not by watched but by the job itself. It
                    # remained a list of the socket's videos: unwatch and cleanup on a disconnect
                    # use it to find which jobs to take the queue off.
                    watched.add(video_id)

            elif op == "playhead":
                # Moves the queue priority: the next batch is taken around
                # the current viewing position, not from the start of the video. All
                # variants of the video are shifted, as with POST /playhead: there's no track
                # flag here. The position and client key are parsed the same as in
                # set_playhead, and also before the loop.
                position = float(message.get("t", 0.0))
                client_id = message.get("clientId")

                for job in manager.jobs_for(message.get("videoId", "")):
                    job.report_playhead(client_id, position)

            elif op == "unwatch":
                video_id = message.get("videoId", "")
                watched.discard(video_id)

                # The socket's queue is taken off all variants of the video: which one it
                # ended up on, the socket doesn't remember, and taking it off an extra one changes nothing.
                for job in manager.jobs_for(video_id):
                    job.unsubscribe(events)

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("the socket crashed")
    finally:
        if pump:
            pump.cancel()

            # We also quell ordinary exceptions, not just cancellation. The pump crashes on a
            # dead socket routinely: send_json raises ClientDisconnected, which starlette
            # turns into WebSocketDisconnect — that's an Exception, not CancelledError.
            # Passed outward, it would fly out of finally BEFORE unsubscribing, forever
            # leaving the connection's queue in the job's subscribers.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await pump

        for video_id in watched:
            for job in manager.jobs_for(video_id):
                job.unsubscribe(events)


@app.get("/ca.crt")
async def root_certificate() -> FileResponse:
    """One's own root certificate — so it can be installed on the tablet.

    Public, like the page: it's a certificate, not a key. Served to all and
    before the daemon switches to https: there's nowhere else to install it on the tablet
    from — the page opens over http, while the browser doesn't yet trust it over https.
    """
    if not CA_PATH.is_file():
        raise HTTPException(status_code=404, detail="no certificate: packaging/make-tls.sh")

    return FileResponse(CA_PATH, media_type="application/x-x509-ca-cert")


# Mounted last: the routes above were added earlier and win over the mount,
# so /v1/* remains the API, and everything else is looked up as a file. The page is public —
# there are no secrets in it: the door to the API is guarded by a single Origin.
app.mount("/", StaticFiles(directory=PAGE_DIR, html=True), name="page")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # The card watchdog polls ollama once a second, and httpx writes every poll
    # as an INFO line — 86 thousand lines a day, drowning everything else.
    # Diagnosing a hung translation began with having to filter this noise
    # first to see the single important line.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # HTTPS is needed not for encrypting the home Wi-Fi, but for the tablet: a browser
    # considers only https secure, and without this the page can't be installed as
    # an app — and therefore can't be registered in the Android share dialog.
    tls = manager.config.server.tls_files
    ssl_args = {}

    if tls is not None:
        ssl_args = {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])}
        log.info("listening on https, certificate %s", tls[0])

    uvicorn.run(
        app, host=HOST, port=PORT, log_level="warning", proxy_headers=False, **ssl_args
    )


if __name__ == "__main__":
    main()
