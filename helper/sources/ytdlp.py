"""Everything we take from YouTube: metadata, manual subtitle tracks, audio.

One source instead of a hand-written InnerTube client: yt-dlp is needed for audio anyway,
and it's maintained by people who track YouTube's changes for us.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import numpy as np

from helper.env import YTDLP_CACHE
from helper.language import is_bcms

SAMPLE_RATE = 16000

# Audio-only, from the cheapest to the spares. The network is the bottleneck, not decoding:
# itag 249 (opus ~50k) is about three times smaller than 140/251 at the same speech quality.
# `^=` is mandatory: on videos with several tracks the ids become 249-0, 251-3, etc.
AUDIO_FORMAT = (
    "ba[format_id^=249][protocol=https]/"
    "ba[format_id^=250][protocol=https]/"
    "ba[protocol=https]/ba"
)

# Picture and sound as separate tracks, because YouTube has no pre-muxed 1080p.
# First mp4 with H.264 — it plays everywhere; failing that we take any H.264 (in a webm
# container the merge will still go to mp4 with -c copy); at the end — anything, so the video
# doesn't disappear because of one rare codec.
VIDEO_FORMAT = (
    "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "b[ext=mp4]/b"
)

# Video identifier: exactly 11 characters. Checking is mandatory — the identifier
# comes from the extension, and yt-dlp bites the first similar chunk out of anything
# and silently returns the result for the wrong video.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Refusals fall into three kinds, and confusing them is costly. Parsing goes by the
# error text, because yt-dlp gives out nothing else: the return code is the same for all failures.
# The wordings were checked against the yt-dlp 2026.08.19 sources, not taken from memory;
# phrases like "Video unavailable" and "Private video" are YouTube's own text,
# which yt-dlp passes through as is.
#
# Rate limiting is checked FIRST. YouTube gives it as
# "Video unavailable. This content isn't available, try again later", and by the first
# words it's indistinguishable from a removed video, although it passes by itself within an hour.
# Retrying it immediately must not be done: frequent requests caused it, and every
# next one extends the ban. But the refusal mustn't stick forever either —
# so it's neither transient nor permanent, but a third case.
THROTTLED = re.compile(
    r"HTTP Error 429|Too Many Requests|not a bot|rate-limited|try again later|captcha",
    re.IGNORECASE,
)

# Age restriction. Anonymously it's final, but, unlike the others,
# it's lifted by signing in to an account — hence a separate expression: by it the job
# decides whether to go for the video once more with the account from the settings. "Sign in to
# confirm you're not a bot" doesn't belong here: it's a bot check, it's
# in THROTTLED, and an account mustn't be substituted for it.
AGE_RESTRICTED = re.compile(
    r"confirm your age|age-restricted|inappropriate for some users",
    re.IGNORECASE,
)

# Permanent: the video is gone, closed, paid, age-restricted,
# unavailable from this country or not yet aired. None of this will pass
# by itself, however much you retry. A cookies file that yt-dlp couldn't parse,
# too: until it's replaced, every attempt will fail the same way.
PERMANENT = re.compile(
    r"Video unavailable|This video is (?:unavailable|not available|private)|Private video"
    r"|has been removed|no longer available|account .{0,80}terminated"
    r"|members-only|members on level|Join this channel"
    rf"|{AGE_RESTRICTED.pattern}"
    r"|live event will begin|Premieres? in |Premiere will begin"
    r"|not made this video available in your country|not available from your location"
    r"|Incomplete YouTube ID|Unsupported URL"
    r"|Netscape format cookies file",
    re.IGNORECASE,
)

# Transient: the network blinked or googlevideo refused exactly this link. It's
# worth retrying at once, with a fresh extraction.
#
# 403 here is deliberate. Without cookies and a PO token yt-dlp downloads only via
# visionos, and googlevideo occasionally answers 403 to a just-issued link —
# the next link meanwhile downloads. yt-dlp itself never retries 4xx.
# yt-dlp names network errors differently depending on the place: "Connection
# aborted", "Read timed out", "Failed to resolve", "N bytes read, M more
# expected", and for the extractor — "Unable to download webpage: … (caused by
# TransportError(…))". All network failures of requests to the player API it turns
# into warnings, and what comes out is a single "Failed to extract any player response".
#
# In the middle of downloading audio, having exhausted its retries, the downloader writes the reason without
# any wrapper: bare TLS text ("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF
# occurred in violation of protocol"), "Connection broken: …" from urllib3,
# and a body closed cleanly but shorter than promised — "Downloaded N bytes, expected
# M bytes". A completely empty body — "Did not get any data blocks".
TRANSIENT = re.compile(
    r"HTTP Error (?:403|5\d\d)"
    r"|Connection (?:reset|refused|aborted)|Connection(?:Reset|Refused|Aborted)Error"
    r"|Remote end closed connection|RemoteDisconnected|Connection broken"
    r"|timed out"
    r"|name resolution|Failed to resolve|Name or service not known|getaddrinfo failed"
    r"|Network is unreachable|No route to host|Failed to establish a new connection"
    r"|IncompleteRead|\d+ bytes read, \d+ more expected|content too short"
    r"|Downloaded \d+ bytes, expected \d+ bytes|Did not get any data blocks"
    r"|\[SSL: |EOF occurred in violation of protocol"
    r"|caused by (?:TransportError|SSLError|ProxyError)"
    r"|Failed to extract any player response",
    re.IGNORECASE,
)

# A signed stream link. It never goes into the log whole: while it hasn't
# expired, the video is downloaded through it in our name and from our address.
MEDIA_URL = re.compile(r"https?://[^\s\"']*googlevideo\.com/[^\s\"']*")

# How much of yt-dlp's output to keep for parsing a failure. With -v it writes a lot, and
# little is needed: ERROR and WARNING lines and what went right before the refusal.
TRACE_LINES = 20
TRACE_TAIL = 12


class YtDlpError(RuntimeError):
    """yt-dlp couldn't give what was asked.

    transient — a failure worth retrying right away, in the same job.
    permanent — a refusal that won't pass by itself, and retrying it is pointless
    neither now nor in a minute. Both False — neither: for example
    rate limiting, which is harmful to retry at once but possible later.
    age_restricted — an 18+ video: anonymously final, with an account it may pass.
    diagnostics — details for the log, not for the viewer.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        permanent: bool = False,
        age_restricted: bool = False,
        diagnostics: str = "",
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.permanent = permanent
        self.age_restricted = age_restricted
        self.diagnostics = diagnostics


def _failure(message: str, detail: str, diagnostics: str = "") -> YtDlpError:
    """A yt-dlp refusal with the kind determined by the text of its error."""
    transient, permanent = _classify(detail)

    return YtDlpError(
        message,
        transient=transient,
        permanent=permanent,
        # Among the permanent only: rate limiting is parsed first,
        # and an account mustn't be substituted for it, even if both were found in the text.
        age_restricted=permanent and bool(AGE_RESTRICTED.search(detail)),
        diagnostics=diagnostics,
    )


def _classify(detail: str) -> tuple[bool, bool]:
    """(transient, permanent) by the text of the yt-dlp error.

    Only ERROR lines must be parsed. Warnings say something else:
    "This video is age-restricted; some formats may be missing" is printed
    also when the audio then downloads perfectly.
    """
    if THROTTLED.search(detail):
        return False, False

    if PERMANENT.search(detail):
        return False, True

    return bool(TRANSIENT.search(detail)), False


@dataclass(frozen=True)
class CaptionTrack:
    lang: str
    url: str
    ext: str
    name: str


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    title: str
    description: str
    channel: str
    tags: list[str]
    chapters: list[str]
    duration: float | None
    audio_language: str | None
    live_status: str | None
    manual_tracks: list[CaptionTrack]
    has_automatic_captions: bool

    @property
    def is_live(self) -> bool:
        return self.live_status in ("is_live", "is_upcoming", "post_live")


def _ytdlp_cmd(
    *args: str,
    warnings: bool = False,
    cookies: Path | None = None,
    playlist: bool = False,
) -> list[str]:
    """A yt-dlp call. cookies — the account, and only for 18+ videos: everything else anonymously.

    YouTube may ban the account yt-dlp goes through, so it's separate,
    not the viewer's, and is substituted only where it's impossible without it. On exit yt-dlp
    appends the updated cookies to the file — that's how the session lives longer.

    playlist=True — only for the links page, where a person gives the whole playlist address.
    Everything else still takes a single video.
    """
    YTDLP_CACHE.mkdir(parents=True, exist_ok=True)

    return [
        sys.executable,
        "-m",
        "yt_dlp",
        # Warnings are needed only for audio download, where a failure is parsed by them.
        # For metadata and tracks they only clutter the error text, which goes
        # entirely into the viewer's message.
        *([] if warnings else ["--no-warnings"]),
        *(["--yes-playlist"] if playlist else ["--no-playlist"]),
        # Deno isn't on this machine, and without a JS runtime the `web` client silently drops
        # out of the defaults and extraction degrades to visionos alone.
        "--js-runtimes",
        "node",
        # Keeps the player JS and the solved nsig between calls — without this every
        # run pays 1.5–4 s of cold extraction.
        "--cache-dir",
        str(YTDLP_CACHE),
        *(["--cookies", str(cookies)] if cookies else []),
        *args,
    ]


def _check_id(video_id: str) -> str:
    if not VIDEO_ID.match(video_id):
        raise YtDlpError(f"invalid video identifier: {video_id!r}", permanent=True)

    return video_id


def _run(cmd: list[str], timeout: float) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # With our own type, not TimeoutExpired: the pipeline catches YtDlpError, and the author's
        # track that didn't download within the allotted time must lead into recognition,
        # not crash the job. subprocess.run has already killed the process by this point.
        raise YtDlpError(f"yt-dlp didn't finish within {timeout} s") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "yt-dlp failed"

        raise _failure(detail, detail)

    return proc.stdout


def _caption_tracks(raw: dict, key: str) -> list[CaptionTrack]:
    tracks: list[CaptionTrack] = []

    for lang, entries in (raw.get(key) or {}).items():
        for entry in entries:
            tracks.append(
                CaptionTrack(
                    lang=lang,
                    url=entry.get("url", ""),
                    ext=entry.get("ext", ""),
                    name=entry.get("name", ""),
                )
            )

    return tracks


def probe(video_id: str, timeout: float = 90.0, *, cookies: Path | None = None) -> VideoInfo:
    """The video's metadata in one call: tracks, title, description, tags, chapters.

    `skip=translated_subs` is kept as insurance against a yt-dlp version change, not for
    speed: in 2026.08.19 machine translations of tracks are built only when automatic subtitles
    are requested (--write-auto-subs or --list-subs), and land in automatic_captions,
    not in subtitles, so the flag affects neither this call nor fetch_manual_track.
    A translation the author uploaded themselves is an ordinary manual track, and the flag doesn't
    screen it out: only the speech language distinguishes it from the original (see usable_manual_track).
    """
    out = _run(
        _ytdlp_cmd(
            "-J",
            "--skip-download",
            "--extractor-args",
            "youtube:skip=translated_subs",
            f"https://www.youtube.com/watch?v={_check_id(video_id)}",
            cookies=cookies,
        ),
        timeout=timeout,
    )

    raw = json.loads(out)
    chapters = [c.get("title", "") for c in (raw.get("chapters") or [])]

    return VideoInfo(
        video_id=raw.get("id", video_id),
        title=raw.get("title") or "",
        description=raw.get("description") or "",
        channel=raw.get("channel") or raw.get("uploader") or "",
        tags=list(raw.get("tags") or []),
        chapters=[c for c in chapters if c],
        duration=raw.get("duration"),
        audio_language=raw.get("language"),
        live_status=raw.get("live_status"),
        manual_tracks=_caption_tracks(raw, "subtitles"),
        has_automatic_captions=bool(raw.get("automatic_captions")),
    )


def usable_manual_track(info: VideoInfo, pin: str | None = None) -> CaptionTrack | None:
    """A manual track of the Serbian family, if it exists and the owner hasn't cancelled it.

    Only a manual track of the Serbian family is suitable, in any spelling of the label: sr,
    sr-Latn, sr-Cyrl, SR, sh, cnr, hr, bs. The family is checked via is_bcms, not the string:
    an exact comparison recognized neither cnr nor sr_Latn. live_chat (a replay chat)
    and labels not among Whisper codes are never suitable.

    There are deliberately no other languages here: the label must be checked against the speech, and there's
    no detector without the card — only large-v3 and turbo are on disk — and a probe on the card would take
    away the author's track's meaning. Without the check an English track would take a video where
    YouTube called Serbian speech English (Juske).

    pin — the owner's pin ([asr] language). Outside the Serbian family it means
    "the speech isn't Serbian", and a Serbian track on such a video is a translation, not the original.
    YouTube metadata (audio_language) doesn't participate in the choice: it was exactly what once
    lied.

    The previous argument remains: Macedonian, Bulgarian, Slovak and Russian —
    acoustic neighbours of Serbian, and the presence of a track doesn't equal its suitability.

    automatic_captions doesn't get here by construction: probe puts into manual_tracks
    only the subtitles key.
    """
    if pin is not None and not is_bcms(pin):
        return None

    for track in info.manual_tracks:
        if is_bcms(track.lang):
            return track

    return None


# Broadcast subtitle conventions that YouTube lets into tracks:
# >> speaker change, >>> topic change, [Music] and similar marks. They mustn't
# get into the translator — the model will start translating them.
SPEAKER_MARKER = re.compile(r"(^|\s)>{2,}\s*")
SOUND_MARKER = re.compile(r"\[[^\]]{1,30}\]")


def fetch_manual_track(
    video_id: str, lang: str, timeout: float = 120.0, *, cookies: Path | None = None
) -> list[dict]:
    """Download the author's subtitle track and parse it into utterances.

    We download via yt-dlp itself, not by a direct request to baseUrl: the link from the page
    has a POT gate, and the request returns HTTP 200 with an EMPTY body — not 403, not 401.
    The `response.ok` check on such a response passes, and empty subtitles go out.
    yt-dlp deals with clients and tokens itself, and we call it for audio anyway.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _run(
            _ytdlp_cmd(
                "--skip-download",
                "--write-subs",
                "--sub-langs",
                lang,
                "--sub-format",
                "json3",
                "--extractor-args",
                "youtube:skip=translated_subs",
                "-o",
                f"{tmp}/track.%(ext)s",
                f"https://www.youtube.com/watch?v={_check_id(video_id)}",
                cookies=cookies,
            ),
            timeout=timeout,
        )

        files = list(Path(tmp).glob("*.json3")) or list(Path(tmp).glob("*.json"))

        if not files:
            raise YtDlpError(f"track {lang} didn't download for {video_id}")

        raw = files[0].read_text(encoding="utf-8")

    # An empty body — the signature of the POT gate. Checking is mandatory: without it further
    # down the pipeline goes a track of zero utterances, and it looks like "the video
    # simply has no subtitles", not like a refusal.
    if not raw.strip():
        raise YtDlpError(f"track {lang} came back empty (POT gate?)")

    cues = _parse_json3(raw)

    if not cues:
        raise YtDlpError(f"not a single utterance was found in track {lang}")

    return cues


def _parse_json3(raw: str) -> list[dict]:
    """Parse json3 into utterances of the form {start, end, text}."""
    events = json.loads(raw).get("events") or []
    cues: list[dict] = []

    for event in events:
        segments = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segments)
        text = SOUND_MARKER.sub(" ", SPEAKER_MARKER.sub(" ", text))
        text = " ".join(text.split())

        if not text:
            continue

        start = (event.get("tStartMs") or 0) / 1000
        cues.append(
            {
                "start": start,
                "end": start + (event.get("dDurationMs") or 0) / 1000,
                "text": text,
                # There are no word timings on this path: it's been measured that
                # tOffsetMs is absent from the output. The utterance boundaries are the author's,
                # and there's no need to rebuild them.
                "words": [],
            }
        )

    # Automatic subtitle utterances overlap by design — the recognition window
    # slides. With author ones it happens less often, but checking is cheaper than later
    # drawing two utterances simultaneously.
    for previous, current in zip(cues, cues[1:]):
        previous["end"] = min(previous["end"], current["start"])

    return [c for c in cues if c["end"] > c["start"]]


_VIDEO_IN_PATH = {"shorts", "embed", "live", "v"}


def video_id_in(url: str) -> str:
    """The video id from a YouTube address — without a network trip.

    The links list needs picture readiness at once, and it's computed by id:
    for a video link it's already in the address itself, and there's no need to wait for yt-dlp.
    An empty string — the address isn't about one video (playlist, foreign host, garbage).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""

    host = (parts.hostname or "").lower()

    if host == "youtu.be":
        candidate = (parts.path or "").strip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        path = (parts.path or "").rstrip("/")

        if path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            bits = [bit for bit in path.split("/") if bit]
            is_video_path = len(bits) > 1 and bits[0] in _VIDEO_IN_PATH
            candidate = bits[1] if is_video_path else ""
    else:
        return ""

    return candidate if VIDEO_ID.match(candidate) else ""


def _media_summary(url: str) -> str:
    """What's needed from the signed link for parsing a failure — without the signature itself.

    The client (c=) shows through whom yt-dlp got the link, ip= — from which
    address it was issued, pot= — whether there was a PO token. It's exactly by these three that one
    distinguishes a googlevideo refusal to a specific link from a block of the address or the client.
    """
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    pot = "pot" in query or "/pot/" in parts.path

    def first(name: str) -> str:
        return (query.get(name) or ["?"])[0]

    return (
        f"googlevideo host={parts.hostname} c={first('c')} itag={first('itag')} "
        f"ip={first('ip')} pot={'yes' if pot else 'no'}"
    )


class _Trace:
    """yt-dlp's stderr, read in parallel with the download.

    Reading it in a separate thread and at once is mandatory. With -v yt-dlp writes a lot
    to stderr, and if nobody reads, it hits the pipe buffer and stalls in the middle of the
    download: ffmpeg waits for data from yt-dlp, yt-dlp waits for stderr to free up,
    and we wait for ffmpeg — all nine hundred seconds of the timeout.

    Only a limited amount is kept: ERROR and WARNING lines, the summary by the link
    and a short tail. The tail is cut at the first ERROR: with -v a Python traceback
    follows it, and it would push out of the tail everything that was before
    the refusal — and that's exactly what's needed. If yt-dlp crashed without managing to print
    ERROR, the traceback itself remains in the tail, and that's also what's needed.
    """

    def __init__(self, stream) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.media = ""
        self.tail: deque[str] = deque(maxlen=TRACE_TAIL)
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream) -> None:
        with stream:
            for raw in stream:
                # \r is removed, not treated as a line break: having exhausted its
                # retries, yt-dlp prints "ERROR: \r[download] Got error: …",
                # and a line split in half would lose its ERROR.
                line = raw.decode(errors="replace").replace("\r", "").rstrip()

                if not line:
                    continue

                for url in MEDIA_URL.findall(line):
                    self.media = _media_summary(url)
                    line = line.replace(url, f"<{self.media}>")

                if not self.errors:
                    self.tail.append(line)

                if line.startswith("ERROR:") and len(self.errors) < TRACE_LINES:
                    self.errors.append(line)
                elif (
                    line.startswith("WARNING:")
                    and line not in self.warnings
                    and len(self.warnings) < TRACE_LINES
                ):
                    self.warnings.append(line)

    def join(self, timeout: float = 10.0) -> None:
        # With a limit: yt-dlp's grandchild may hold stderr open, and then
        # EOF won't come while it's alive. Losing the tail is better than hanging.
        self._thread.join(timeout)

    def diagnostics(self) -> str:
        lines = [self.media or "googlevideo: no link was issued", *self.warnings]

        if self.tail:
            lines += ["last lines:", *(f"  {line}" for line in self.tail)]

        return "\n".join(lines)


def audio_pcm(video_id: str, timeout: float = 900.0, *, cookies: Path | None = None) -> np.ndarray:
    """The video's audio track as float32 mono 16 kHz, without intermediate files.

    The `yt-dlp -o - | ffmpeg` pipe is deliberately chosen over passing a direct URL to ffmpeg:
    yt-dlp itself cuts the download into 10 MiB chunks, and that's its anti-throttling. One long
    GET by a direct YouTube URL gets throttled to playback speed.
    """
    ydl = subprocess.Popen(
        _ytdlp_cmd(
            "-f",
            AUDIO_FORMAT,
            # The sorting must start with lang, otherwise on videos with auto-dubbing
            # the smallest track will be chosen — i.e. a foreign language.
            "-S",
            "lang,+size",
            "--no-part",
            "--no-progress",
            # -v and warnings for parsing refusals: without them, of a googlevideo 403
            # there remains one line and not a word about which client issued the link,
            # from which address and whether it had a PO token. It doesn't hurt the stream —
            # with -o - everything service-related yt-dlp writes to stderr, stdout remains audio.
            "-v",
            # Twice shorter than the default 20 s. The download goes under the pass to the card,
            # one for the whole daemon, and a dead connection that yt-dlp waits for up to ten
            # times by timeout would hold other phases for minutes. A live
            # googlevideo answers in fractions of a second, so the headroom remains large.
            "--socket-timeout",
            "10",
            "-o",
            "-",
            f"https://www.youtube.com/watch?v={_check_id(video_id)}",
            warnings=True,
            cookies=cookies,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    trace = _Trace(ydl.stderr)

    ff = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            "pipe:0",
            "-vn",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        stdin=ydl.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Otherwise ffmpeg won't get EOF when yt-dlp finishes.
    ydl.stdout.close()

    try:
        pcm_bytes, ff_err = ff.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        ydl.kill()
        ff.kill()
        trace.join()

        # Not transient: there's no point retrying fifteen minutes of waiting at once.
        # The job meanwhile remains retryable — the next attempt in a minute.
        raise YtDlpError(
            f"audio download {video_id} didn't finish within {timeout} s",
            diagnostics=trace.diagnostics(),
        ) from None

    ydl_code = ydl.wait()
    trace.join()

    # Checking yt-dlp's return code is mandatory. On a break in the middle of the download
    # (403 on an expired URL, exhausted retries, network loss) it simply closes
    # stdout — ffmpeg sees a clean EOF, outputs what was already decoded and exits with zero.
    # Without this check the truncated audio is indistinguishable from the whole: it's recognized,
    # translated and goes into the cache under the video's full duration.
    if ydl_code != 0 or ff.returncode != 0 or not pcm_bytes:
        # To the viewer — only ERROR lines: the rest of the -v output is hundreds of lines
        # of debugging, and it has no place in the message under the video. It goes to the log.
        detail = (
            "\n".join(trace.errors)
            or ff_err.decode(errors="replace").strip()
            or f"yt-dlp exited with code {ydl_code}, ffmpeg with code {ff.returncode}"
        )

        raise _failure(
            f"failed to get audio {video_id}: {detail}",
            detail,
            trace.diagnostics(),
        )

    return np.frombuffer(pcm_bytes, dtype="<f4")


def fetch_video(
    video_id: str, target: Path, timeout: float = 1800.0, *, cookies: Path | None = None
) -> Path:
    """Download the video and merge into mp4 without re-encoding. Returns the path to the file.

    The temporary directory is created next to the target, not in /tmp: os.replace is atomic
    only within one filesystem. The finished file appears under its name
    whole — there's never a partially downloaded mp4 under the name served to the viewer.
    """
    _check_id(video_id)

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{video_id}-", dir=target.parent) as work:
        _run(
            _ytdlp_cmd(
                "-f",
                VIDEO_FORMAT,
                # First the resolution cap, then the native audio language and only
                # then the size: -S lang must go before +size, otherwise on a video
                # with auto-dubbing the smallest track will be taken — i.e. a foreign one.
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
            raise YtDlpError("yt-dlp left no merged file", permanent=True)

        os.replace(produced, target)

    return target


def probe_url(url: str, timeout: float = 60.0, *, cookies: Path | None = None) -> dict:
    """What lies at the link: a single video or a playlist, as a flat list without entering each.

    --flat-playlist takes only what YouTube gave in the response itself: for a playlist
    it's a pack of ids and titles, one request instead of a hundred.
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
