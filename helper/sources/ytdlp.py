"""Всё, что мы берём у YouTube: метаданные, ручные дорожки субтитров, аудио.

Один источник вместо самописного InnerTube-клиента: yt-dlp и так нужен ради аудио,
а его сопровождают люди, которые следят за изменениями YouTube за нас.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import numpy as np

from helper.env import YTDLP_CACHE

SAMPLE_RATE = 16000

# Аудио-only, от самого дешёвого к запасным. Сеть — узкое место, а не декодирование:
# itag 249 (opus ~50k) примерно втрое меньше 140/251 при том же качестве речи.
# `^=` обязателен: на роликах с несколькими дорожками id становятся 249-0, 251-3 и т.д.
AUDIO_FORMAT = (
    "ba[format_id^=249][protocol=https]/"
    "ba[format_id^=250][protocol=https]/"
    "ba[protocol=https]/ba"
)

# Сербский континуум. Всё остальное — не наш язык, даже если дорожка существует.
BCMS_LANG = re.compile(r"^(sr|hr|bs|sh)(-|$)", re.IGNORECASE)

# Идентификатор ролика: ровно 11 символов. Проверять обязательно — идентификатор
# приходит из расширения, а yt-dlp выкусывает первый похожий кусок из чего угодно
# и молча возвращает результат для не того ролика.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Отказы делятся на три рода, и путать их дорого. Разбор идёт по тексту ошибки,
# потому что другого yt-dlp наружу не отдаёт: код возврата у всех сбоев один.
# Формулировки сверены с исходниками yt-dlp 2026.08.19, а не взяты по памяти;
# фразы вроде «Video unavailable» и «Private video» — это текст самого YouTube,
# который yt-dlp пробрасывает как есть.
#
# Ограничение частоты проверяется ПЕРВЫМ. YouTube отдаёт его как
# «Video unavailable. This content isn't available, try again later», и по первым
# словам это неотличимо от удалённого ролика, хотя проходит само за час.
# Немедленно повторять такое нельзя: частые запросы его и вызвали, и каждый
# следующий продлевает запрет. Но и навсегда отказ прилипать не должен —
# поэтому он не временный и не окончательный, а третий случай.
THROTTLED = re.compile(
    r"HTTP Error 429|Too Many Requests|not a bot|rate-limited|try again later|captcha",
    re.IGNORECASE,
)

# Окончательные: ролика нет, он закрыт, платный, с возрастным ограничением,
# недоступен из этой страны или ещё не вышел в эфир. Ничего из этого не пройдёт
# само, сколько ни повторяй.
PERMANENT = re.compile(
    r"Video unavailable|This video is (?:unavailable|not available|private)|Private video"
    r"|has been removed|no longer available|account .{0,80}terminated"
    r"|members-only|members on level|Join this channel"
    r"|confirm your age|age-restricted|inappropriate for some users"
    r"|live event will begin|Premieres? in |Premiere will begin"
    r"|not made this video available in your country|not available from your location"
    r"|Incomplete YouTube ID|Unsupported URL",
    re.IGNORECASE,
)

# Временные: сеть моргнула или googlevideo отказал именно этой ссылке. Такое
# стоит повторить сразу, со свежим извлечением.
#
# 403 здесь сознательно. Без cookies и PO-токена yt-dlp качает только через
# visionos, и googlevideo изредка отвечает 403 на только что выданную ссылку —
# следующая ссылка при этом качается. Сам yt-dlp 4xx не повторяет никогда.
# Ошибки сети yt-dlp называет по-разному в зависимости от места: «Connection
# aborted», «Read timed out», «Failed to resolve», «N bytes read, M more
# expected», а у экстрактора — «Unable to download webpage: … (caused by
# TransportError(…))». Все сетевые сбои запросов к API плеера он превращает
# в предупреждения, и наружу выходит одно «Failed to extract any player response».
#
# Посреди загрузки звука, исчерпав свои повторы, загрузчик пишет причину без
# всякой обёртки: голый текст TLS («[SSL: UNEXPECTED_EOF_WHILE_READING] EOF
# occurred in violation of protocol»), «Connection broken: …» от urllib3,
# а тело, закрытое чисто, но короче обещанного, — «Downloaded N bytes, expected
# M bytes». Совсем пустое тело — «Did not get any data blocks».
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

# Подписанная ссылка на поток. Целиком она в журнал не попадает никогда: пока
# не истекла, по ней скачивается ролик от нашего имени и с нашего адреса.
MEDIA_URL = re.compile(r"https?://[^\s\"']*googlevideo\.com/[^\s\"']*")

# Сколько вывода yt-dlp держать для разбора сбоя. С -v он пишет много, а нужно
# немного: строки ERROR и WARNING и то, что шло прямо перед отказом.
TRACE_LINES = 20
TRACE_TAIL = 12


class YtDlpError(RuntimeError):
    """yt-dlp не смог отдать то, что просили.

    transient — сбой, который стоит повторить сразу же, в том же задании.
    permanent — отказ, который сам не пройдёт, и повторять его бессмысленно
    ни сейчас, ни через минуту. Оба False — ни то ни другое: например,
    ограничение частоты, которое сразу повторять вредно, а позже можно.
    diagnostics — подробности для журнала, не для зрителя.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        permanent: bool = False,
        diagnostics: str = "",
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.permanent = permanent
        self.diagnostics = diagnostics


def _failure(message: str, detail: str, diagnostics: str = "") -> YtDlpError:
    """Отказ yt-dlp с родом, определённым по тексту его ошибки."""
    transient, permanent = _classify(detail)

    return YtDlpError(
        message, transient=transient, permanent=permanent, diagnostics=diagnostics
    )


def _classify(detail: str) -> tuple[bool, bool]:
    """(временный, окончательный) по тексту ошибки yt-dlp.

    Разбирать надо только строки ERROR. Предупреждения говорят о другом:
    «This video is age-restricted; some formats may be missing» печатается
    и тогда, когда звук потом прекрасно скачивается.
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


def _ytdlp_cmd(*args: str, warnings: bool = False) -> list[str]:
    YTDLP_CACHE.mkdir(parents=True, exist_ok=True)

    return [
        sys.executable,
        "-m",
        "yt_dlp",
        # Предупреждения нужны только загрузке звука, где по ним разбирают сбой.
        # Метаданным и дорожкам они лишь засоряют текст ошибки, который целиком
        # уходит в сообщение зрителю.
        *([] if warnings else ["--no-warnings"]),
        "--no-playlist",
        # Deno на этой машине нет, а без JS-рантайма клиент `web` молча выпадает
        # из дефолтов и извлечение деградирует до одного visionos.
        "--js-runtimes",
        "node",
        # Держит player JS и решённый nsig между вызовами — без этого каждый
        # запуск платит 1.5–4 с холодного извлечения.
        "--cache-dir",
        str(YTDLP_CACHE),
        *args,
    ]


def _check_id(video_id: str) -> str:
    if not VIDEO_ID.match(video_id):
        raise YtDlpError(f"неправильный идентификатор ролика: {video_id!r}", permanent=True)

    return video_id


def _run(cmd: list[str], timeout: float) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Своим типом, а не TimeoutExpired: конвейер ловит YtDlpError, и дорожка
        # автора, не скачавшаяся за отведённое время, должна уводить в распознавание,
        # а не ронять задание. Процесс subprocess.run к этому моменту уже убил.
        raise YtDlpError(f"yt-dlp не уложился в {timeout} с") from None

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


def probe(video_id: str, timeout: float = 90.0) -> VideoInfo:
    """Метаданные ролика одним вызовом: дорожки, заголовок, описание, теги, главы.

    `skip=translated_subs` существенно: без него yt-dlp материализует ~200 tlang-вариантов
    на каждую дорожку, и разбор становится заметно медленнее (это отмечено прямо
    в исходниках экстрактора).
    """
    out = _run(
        _ytdlp_cmd(
            "-J",
            "--skip-download",
            "--extractor-args",
            "youtube:skip=translated_subs",
            f"https://www.youtube.com/watch?v={_check_id(video_id)}",
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


def usable_manual_track(info: VideoInfo) -> CaptionTrack | None:
    """Ручная дорожка на сербском или соседнем языке, если она есть.

    Guard от чужого языка обязателен. Македонский, болгарский, словацкий, русский
    и албанский — ASR-языки YouTube и акустические соседи сербского, так что ролик
    с ошибочной автодетекцией может отдать правдоподобную дорожку не на том языке.
    Наличие дорожки никогда не равно её пригодности.

    `automatic_captions` сюда не попадают по построению: для BCMS их не существует —
    этих языков нет в списке автосубтитров YouTube.
    """
    for track in info.manual_tracks:
        if BCMS_LANG.match(track.lang):
            return track

    return None


# Условности вещательных субтитров, которые YouTube пропускает в дорожки:
# >> смена говорящего, >>> смена темы, [Music] и подобные пометки. В переводчик
# они попадать не должны — модель начнёт их переводить.
SPEAKER_MARKER = re.compile(r"(^|\s)>{2,}\s*")
SOUND_MARKER = re.compile(r"\[[^\]]{1,30}\]")


def fetch_manual_track(video_id: str, lang: str, timeout: float = 120.0) -> list[dict]:
    """Скачать авторскую дорожку субтитров и разобрать в реплики.

    Качаем через сам yt-dlp, а не прямым запросом по baseUrl: у ссылки со страницы
    стоит POT-гейт, и запрос возвращает HTTP 200 с ПУСТЫМ телом — не 403, не 401.
    Проверка `response.ok` на таком ответе проходит, и наружу уходят пустые субтитры.
    yt-dlp разбирается с клиентами и токенами сам, а мы его и так вызываем ради аудио.
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
            ),
            timeout=timeout,
        )

        files = list(Path(tmp).glob("*.json3")) or list(Path(tmp).glob("*.json"))

        if not files:
            raise YtDlpError(f"дорожка {lang} не скачалась для {video_id}")

        raw = files[0].read_text(encoding="utf-8")

    # Пустое тело — подпись POT-гейта. Проверять обязательно: без этого дальше
    # по конвейеру уходит дорожка из нуля реплик, и выглядит это как «у ролика
    # просто нет субтитров», а не как отказ.
    if not raw.strip():
        raise YtDlpError(f"дорожка {lang} пришла пустой (POT-гейт?)")

    cues = _parse_json3(raw)

    if not cues:
        raise YtDlpError(f"в дорожке {lang} не нашлось ни одной реплики")

    return cues


def _parse_json3(raw: str) -> list[dict]:
    """Разобрать json3 в реплики вида {start, end, text}."""
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
                # Пословных таймингов на этом пути не бывает: замерено, что
                # tOffsetMs в выдаче отсутствует. Границы реплик авторские,
                # и пересобирать их не нужно.
                "words": [],
            }
        )

    # Реплики автосубтитров перекрываются по устройству — окно распознавания
    # скользит. У авторских так бывает реже, но проверять дешевле, чем потом
    # рисовать две реплики одновременно.
    for previous, current in zip(cues, cues[1:]):
        previous["end"] = min(previous["end"], current["start"])

    return [c for c in cues if c["end"] > c["start"]]


def _media_summary(url: str) -> str:
    """Что из подписанной ссылки нужно для разбора отказа — без самой подписи.

    Клиент (c=) показывает, через кого yt-dlp получил ссылку, ip= — с какого
    адреса она выдана, pot= — был ли PO-токен. Именно по этим трём и отличают
    отказ googlevideo конкретной ссылке от блокировки адреса или клиента.
    """
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    pot = "pot" in query or "/pot/" in parts.path

    def first(name: str) -> str:
        return (query.get(name) or ["?"])[0]

    return (
        f"googlevideo host={parts.hostname} c={first('c')} itag={first('itag')} "
        f"ip={first('ip')} pot={'да' if pot else 'нет'}"
    )


class _Trace:
    """stderr yt-dlp, прочитанный параллельно с загрузкой.

    Читать его обязательно отдельным потоком и сразу. С -v yt-dlp пишет в stderr
    много, и если никто не читает, он упирается в буфер канала и встаёт посреди
    загрузки: ffmpeg ждёт данных от yt-dlp, yt-dlp ждёт, пока освободится stderr,
    а мы ждём ffmpeg — все девятьсот секунд таймаута.

    Хранится только ограниченное: строки ERROR и WARNING, сводка по ссылке
    и короткий хвост. Хвост обрывается на первой ERROR: с -v за ней идёт
    трассировка Python, и она вытеснила бы из хвоста всё, что было перед
    отказом, — а нужно именно это. Если же yt-dlp упал, не успев напечатать
    ERROR, в хвосте останется сама трассировка, и это тоже то, что нужно.
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
                # \r убирается, а не считается переводом строки: исчерпав свои
                # повторы, yt-dlp печатает «ERROR: \r[download] Got error: …»,
                # и разрезанная пополам строка потеряла бы свой ERROR.
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
        # С ограничением: stderr может держать открытым внук yt-dlp, и тогда
        # EOF не придёт, пока жив он. Потерять хвост лучше, чем повиснуть.
        self._thread.join(timeout)

    def diagnostics(self) -> str:
        lines = [self.media or "googlevideo: ссылка не выдавалась", *self.warnings]

        if self.tail:
            lines += ["последние строки:", *(f"  {line}" for line in self.tail)]

        return "\n".join(lines)


def audio_pcm(video_id: str, timeout: float = 900.0) -> np.ndarray:
    """Аудиодорожка ролика как float32 mono 16 кГц, без промежуточных файлов.

    Пайп `yt-dlp -o - | ffmpeg` намеренно выбран вместо передачи прямого URL в ffmpeg:
    yt-dlp сам режет загрузку на куски по 10 МиБ, и это его антитроттлинг. Один длинный
    GET по прямому URL YouTube придушивает до скорости воспроизведения.
    """
    ydl = subprocess.Popen(
        _ytdlp_cmd(
            "-f",
            AUDIO_FORMAT,
            # Сортировка обязана начинаться с lang, иначе на роликах с автодубляжом
            # выберется самая маленькая дорожка — то есть чужой язык.
            "-S",
            "lang,+size",
            "--no-part",
            "--no-progress",
            # -v и предупреждения ради разбора отказов: без них от 403 googlevideo
            # остаётся одна строка и ни слова о том, какой клиент выдал ссылку,
            # с какого адреса и был ли у неё PO-токен. Потоку это не вредит —
            # при -o - всё служебное yt-dlp пишет в stderr, stdout остаётся звуком.
            "-v",
            # Вдвое короче умолчания в 20 с. Загрузка идёт под пропуском на карту,
            # одним на весь демон, и мёртвое соединение, которое yt-dlp до десяти
            # раз ждёт по таймауту, держало бы чужие фазы минутами. Живой
            # googlevideo отвечает за доли секунды, так что запас остаётся большой.
            "--socket-timeout",
            "10",
            "-o",
            "-",
            f"https://www.youtube.com/watch?v={_check_id(video_id)}",
            warnings=True,
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

    # Иначе ffmpeg не получит EOF, когда yt-dlp закончит.
    ydl.stdout.close()

    try:
        pcm_bytes, ff_err = ff.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        ydl.kill()
        ff.kill()
        trace.join()

        # Не временный: повторять сразу пятнадцать минут ожидания незачем.
        # Задание при этом остаётся повторяемым — следующая попытка через минуту.
        raise YtDlpError(
            f"загрузка аудио {video_id} не уложилась в {timeout} с",
            diagnostics=trace.diagnostics(),
        ) from None

    ydl_code = ydl.wait()
    trace.join()

    # Код возврата yt-dlp проверять обязательно. При обрыве посреди загрузки
    # (403 на протухшем URL, исчерпанные повторы, потеря сети) он просто закрывает
    # stdout — ffmpeg видит чистый EOF, отдаёт уже раскодированное и выходит с нулём.
    # Без этой проверки обрезанное аудио неотличимо от целого: оно распознаётся,
    # переводится и уходит в кэш под полную длительность ролика.
    if ydl_code != 0 or ff.returncode != 0 or not pcm_bytes:
        # Зрителю — только строки ERROR: остальной вывод -v это сотни строк
        # отладки, и в сообщении под роликом ему не место. Он идёт в журнал.
        detail = (
            "\n".join(trace.errors)
            or ff_err.decode(errors="replace").strip()
            or f"yt-dlp вышел с кодом {ydl_code}, ffmpeg с кодом {ff.returncode}"
        )

        raise _failure(
            f"не удалось получить аудио {video_id}: {detail}",
            detail,
            trace.diagnostics(),
        )

    return np.frombuffer(pcm_bytes, dtype="<f4")
