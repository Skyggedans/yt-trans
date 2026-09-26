"""Всё, что мы берём у YouTube: метаданные, ручные дорожки субтитров, аудио.

Один источник вместо самописного InnerTube-клиента: yt-dlp и так нужен ради аудио,
а его сопровождают люди, которые следят за изменениями YouTube за нас.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass

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


class YtDlpError(RuntimeError):
    """yt-dlp не смог отдать то, что просили."""


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


def _ytdlp_cmd(*args: str) -> list[str]:
    YTDLP_CACHE.mkdir(parents=True, exist_ok=True)

    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
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
        raise YtDlpError(f"неправильный идентификатор ролика: {video_id!r}")

    return video_id


def _run(cmd: list[str], timeout: float) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    if proc.returncode != 0:
        raise YtDlpError((proc.stderr or proc.stdout).strip() or "yt-dlp failed")

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
            "-q",
            "-o",
            "-",
            f"https://www.youtube.com/watch?v={_check_id(video_id)}",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

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
        raise YtDlpError(f"загрузка аудио {video_id} не уложилась в {timeout} с") from None

    ydl_err = ydl.stderr.read().decode(errors="replace")
    ydl_code = ydl.wait()

    # Код возврата yt-dlp проверять обязательно. При обрыве посреди загрузки
    # (403 на протухшем URL, исчерпанные повторы, потеря сети) он просто закрывает
    # stdout — ffmpeg видит чистый EOF, отдаёт уже раскодированное и выходит с нулём.
    # Без этой проверки обрезанное аудио неотличимо от целого: оно распознаётся,
    # переводится и уходит в кэш под полную длительность ролика.
    if ydl_code != 0 or ff.returncode != 0 or not pcm_bytes:
        detail = ydl_err.strip() or ff_err.decode(errors="replace").strip()
        raise YtDlpError(f"не удалось получить аудио {video_id}: {detail}")

    return np.frombuffer(pcm_bytes, dtype="<f4")
