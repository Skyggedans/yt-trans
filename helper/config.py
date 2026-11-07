"""Daemon configuration: which models to use and how much memory they ask for.

Models are configured here, not in the extension. VRAM usage and the claim to
own a model are internal mechanics of the daemon: the watchdog considers as its own
exactly the model the daemon declared at startup, and if the browser were allowed to
change it on the fly, the daemon would see its own model as someone else's work and
evict itself.

Memory usage is tied to the model inseparably. `ASR_NEED_MIB = 6144` is about
large-v3 in fp16; for turbo it's three times less, for the 12B translator it's half
of 17.8 GB. Having switched the model and left the old figure, we would get an
admission check that either waits for memory that isn't needed, or lets a job in
where it doesn't fit. Therefore the figure comes from the reference table together
with the name.

The file is read once at startup: changing the model requires a daemon restart.
This is deliberate — the model is resident in ollama, and changing it in the middle
of a job means reopening the whole swap discipline.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from helper import language
from helper.env import CONFIG_DIR

log = logging.getLogger(__name__)

CONFIG_PATH = CONFIG_DIR / "config.toml"

# Measured on an RTX 4090. The numbers have headroom: this is what the admission
# check asks for, not the exact consumption. faster-whisper's batched mode is
# noticeably hungrier than single, so fp16 large-v3 costs 6 GB, not the 4.5 from the README.
ASR_VRAM_MIB = {
    ("large-v3", "float16"): 6144,
    ("large-v3", "int8_float16"): 3584,
    ("large-v3-turbo", "float16"): 4096,
    ("large-v3-turbo", "int8_float16"): 2560,
    ("distil-large-v3", "float16"): 3584,
}

# Sizes of ollama models on this machine (from `ollama list`), plus headroom for the KV cache.
MT_VRAM_MIB = {
    "qwen3.8:27b": 17800,
    "qwen3-coder:30b": 19100,
    "gemma4:31b": 20400,
    "laguna-xs-2.1:q4_K_M": 20800,
    # Measured 2026-09-03: ollama asks for 17.1 GiB at num_ctx 8192.
    "translategemma:27b": 17600,
    "translategemma:12b": 8700,
    "translategemma:4b": 3800,
}


class ConfigError(RuntimeError):
    """The configuration doesn't allow determining unambiguously how much memory to ask for."""


@dataclass(frozen=True)
class AsrConfig:
    model: str = "large-v3"
    # int8 on the GPU isn't faster than fp16 — measured 19.6 s versus 19.2 on one clip.
    # It saves about a gigabyte of memory and costs speed, not the other way around.
    compute_type: str = "float16"
    # Speech language. "auto" — decided by the detector: it listens to windows along
    # the whole length of the video and always runs, and the language label from YouTube
    # is only a hint for it. A language code pins the language for EVERY video: Whisper
    # decodes with it even if something else is spoken, and the detector only notes the
    # discrepancy. Pinning is worth it only when all videos are in one language: on
    # foreign audio the decoder's behavior is undefined, and it starts choosing between
    # translation, transliteration and writing what was heard in a foreign alphabet —
    # anew in every thirty-second window.
    # load() brings the value to a Whisper code (helper/language.py), auto stays auto.
    language: str = "auto"
    vram_mib: int = 0

    def resolved_vram(self) -> int:
        if self.vram_mib:
            return self.vram_mib

        known = ASR_VRAM_MIB.get((self.model, self.compute_type))

        if known:
            return known

        raise ConfigError(
            f"unknown memory usage for ASR {self.model}/{self.compute_type}. "
            f"Specify vram_mib in [asr] of {CONFIG_PATH} — without it the admission "
            f"check won't be able to decide whether the model fits."
        )


@dataclass(frozen=True)
class TranslatorConfig:
    # Both translategemma sizes were tried and rejected. On real utterances they
    # MERGE neighbouring ones and drift the output, and the drift accumulates: measured
    # on 160 utterances, for 12b 11%, 32%, 50% and 81% drifted across four batches, and at
    # the fifth position the subtitle showed the text of the tenth. qwen3.8:27b on the
    # very same batches — zero drifted out of 122 evaluated.
    #
    # The reason, apparently, is structural rather than model size: translategemma is
    # trained to translate sentences, and utterances arrive as fragments of phrases, and
    # it honestly assembles sentences from fragments — precisely thereby destroying the
    # one-to-one correspondence the whole display rests on. The schema has nothing to do
    # with it: the grammar keeps the count of keys, but no one guarantees the
    # correspondence of a key to the source utterance.
    model: str = "qwen3.8:27b"
    vram_mib: int = 0
    # The second subtitle line for language study. "cnr" — Montenegrin,
    # an empty string disables the mode and leaves only the translation into Russian.
    #
    # Both lines are produced in one model pass: it's twice as cheap as two passes,
    # and the Russian line comes out as a gloss to this very Montenegrin one, rather
    # than an independent translation — for word-to-word matching that's exactly right.
    study_language: str = "cnr"

    def resolved_vram(self) -> int:
        if self.vram_mib:
            return self.vram_mib

        known = MT_VRAM_MIB.get(self.model)

        if known:
            return known

        raise ConfigError(
            f"unknown memory usage for translator {self.model}. "
            f"Specify vram_mib in [translator] of {CONFIG_PATH}; the "
            f"actual size can be seen via `ollama list`."
        )


@dataclass(frozen=True)
class YoutubeConfig:
    # The cookies file (Netscape format) of a separate account for 18+ videos. Without
    # it such videos aren't translated: YouTube serves them only to a signed-in user.
    # Everything else the daemon downloads anonymously, and with an account — never.
    # According to yt-dlp itself, YouTube may ban the account it goes through, so one's
    # main account isn't put here. How to export cookies — in config.example.toml.
    cookies: str = ""

    @property
    def cookies_path(self) -> Path | None:
        return Path(self.cookies).expanduser() if self.cookies else None


@dataclass(frozen=True)
class ServerConfig:
    # The address the daemon listens on. The default is loopback: the daemon makes
    # requests to YouTube on the client's command, and exposing it to the network
    # "just in case" is not allowed. A tablet in another room is host = "0.0.0.0":
    # then the daemon is open to everyone on the home network, and only Origin guards it.
    host: str = "127.0.0.1"
    # Certificate and key for HTTPS. Needed for the tablet: a browser considers
    # only https (or loopback) secure, and without this the page can't be installed
    # as an app, and therefore can't be registered in the Android share dialog.
    # Empty — the daemon runs over http, as before.
    tls_cert: str = ""
    tls_key: str = ""

    @property
    def tls_files(self) -> tuple[Path, Path] | None:
        if not (self.tls_cert or self.tls_key):
            return None

        return Path(self.tls_cert).expanduser(), Path(self.tls_key).expanduser()


@dataclass(frozen=True)
class VideoConfig:
    # Directory of ready mp4 files. Empty — the project's shared cache (CACHE_DIR/video).
    # A separate setting is needed for /tmp: it's tmpfs, i.e. RAM, and there the cache
    # cap is no longer measured by disk.
    dir: str = ""
    # Cap in gibibytes. Twenty is about fifty hour-long videos in 1080p; on disk it's
    # only protection against endless growth, in memory also against pressure on the
    # system, so there it's set lower.
    max_gib: float = 20.0

    @property
    def directory(self) -> Path | None:
        return Path(self.dir).expanduser() if self.dir else None

    @property
    def max_bytes(self) -> int:
        # float() before multiplication: if something non-numeric got into max_gib, that
        # should be a fast error, not "string × gigabyte", i.e. a multi-gigabyte string
        # in memory.
        return int(float(self.max_gib) * 1024**3)


@dataclass(frozen=True)
class Config:
    asr: AsrConfig
    translator: TranslatorConfig
    youtube: YoutubeConfig = YoutubeConfig()
    server: ServerConfig = ServerConfig()
    video: VideoConfig = VideoConfig()

    @property
    def asr_vram_mib(self) -> int:
        return self.asr.resolved_vram()

    @property
    def translator_vram_mib(self) -> int:
        return self.translator.resolved_vram()

    def describe(self) -> dict:
        return {
            "asr": {
                "model": self.asr.model,
                "compute_type": self.asr.compute_type,
                "language": self.asr.language,
                "vram_mib": self.asr_vram_mib,
            },
            "translator": {
                "model": self.translator.model,
                "vram_mib": self.translator_vram_mib,
                # The extension shows this in diagnostics. It isn't suitable for the
                # start button: it's drawn before the connection to the daemon, and
                # promising a second line based on this flag would be lying until the
                # viewer also enables it in the browser settings.
                "study_language": self.translator.study_language,
            },
        }


def load(path: Path = CONFIG_PATH) -> Config:
    """Read the configuration. A missing file is not an error, defaults are taken."""
    raw: dict = {}

    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

    asr = AsrConfig(**_section(raw, "asr", AsrConfig))

    # The language code is brought to Whisper's form here, once: "sh", "SR" and
    # "sr-Latn" are sr, and further along the daemon the code already travels. An
    # unfamiliar value is not a startup error, just like cookies: the daemon comes up,
    # and every new job gets a refusal with the same phrase (JobRunner._pin). str() —
    # in case of a number instead of a string.
    try:
        asr = replace(asr, language=language.normalize(str(asr.language)) or "auto")
    except language.UnknownLanguage as exc:
        asr = replace(asr, language=exc.raw)
        log.error(language.refusal(exc.raw))

    translator = TranslatorConfig(**_section(raw, "translator", TranslatorConfig))
    youtube = YoutubeConfig(**_section(raw, "youtube", YoutubeConfig))
    server = ServerConfig(**_section(raw, "server", ServerConfig))
    video = VideoConfig(**_section(raw, "video", VideoConfig))
    config = Config(
        asr=asr, translator=translator, youtube=youtube, server=server, video=video
    )

    # The cache cap is read at every eviction decision, so garbage here would surface
    # only after an hour of work, in the middle of viewing. Let it fail at startup.
    try:
        if config.video.max_bytes <= 0:
            raise ConfigError(f"[video] max_gib must be greater than zero: {video.max_gib!r}")
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[video] max_gib is not a number: {video.max_gib!r}") from exc

    # Half of a pair is not "almost https": uvicorn would bring up plain http, and the
    # viewer would look for the cause on the tablet. A missing file is also better seen
    # at startup, not at the first opening of the page.
    tls = config.server.tls_files

    if tls is not None:
        cert, key = tls

        if not (config.server.tls_cert and config.server.tls_key):
            missing = "tls_key" if config.server.tls_cert else "tls_cert"
            raise ConfigError(f"[server] {missing} is not set: https needs both files")

        for path, name in ((cert, "tls_cert"), (key, "tls_key")):
            if not path.is_file():
                raise ConfigError(f"[server] {name}: file {path} does not exist")

    # Not a startup error: without the file only 18+ videos won't open, and the rest
    # works. The viewer of such a video will get the same reason in the refusal.
    cookies = youtube.cookies_path

    if cookies is not None and not cookies.is_file():
        log.warning("[youtube] cookies: file %s does not exist, 18+ videos will not open", cookies)

    # Resolve the memory usage right away, at startup. An error in the config must fail
    # at daemon startup, not a minute into work in the middle of the first job.
    config.asr_vram_mib
    config.translator_vram_mib

    return config


def _section(raw: dict, name: str, kind: type) -> dict:
    """Fields of a section, discarding unknown keys with a warning."""
    section = raw.get(name) or {}
    allowed = {f for f in kind.__dataclass_fields__}
    unknown = set(section) - allowed

    if unknown:
        log.warning("in section [%s] ignoring unknown keys: %s", name, ", ".join(sorted(unknown)))

    return {k: v for k, v in section.items() if k in allowed}
