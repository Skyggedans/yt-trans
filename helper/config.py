"""Конфигурация демона: какие модели использовать и сколько памяти они просят.

Модели настраиваются здесь, а не в расширении. Расход видеопамяти и заявка на
владение моделью — внутренняя механика демона: сторож считает своей ровно ту
модель, которую демон объявил при старте, и если позволить браузеру менять её
на лету, демон увидит собственную модель как чужую работу и вытеснит сам себя.

Расход памяти привязан к модели неразрывно. `ASR_NEED_MIB = 6144` — это про
large-v3 в fp16; у turbo он втрое меньше, у переводчика на 12B — вдвое меньше
17.8 ГБ. Переключив модель и оставив прежнюю цифру, мы получили бы контроль
допуска, который либо ждёт памяти, которая не нужна, либо пускает задачу туда,
где она не помещается. Поэтому цифра идёт из справочника вместе с именем.

Файл читается один раз при старте: смена модели требует перезапуска демона.
Это намеренно — модель резидентна в ollama, и менять её посреди задания значит
переоткрывать всю дисциплину свопа.
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

# Замерено на RTX 4090. Числа с запасом: это то, что просит контроль допуска,
# а не точное потребление. Батчевый режим faster-whisper заметно прожорливее
# одиночного, поэтому fp16 large-v3 стоит 6 ГБ, а не 4.5 из README.
ASR_VRAM_MIB = {
    ("large-v3", "float16"): 6144,
    ("large-v3", "int8_float16"): 3584,
    ("large-v3-turbo", "float16"): 4096,
    ("large-v3-turbo", "int8_float16"): 2560,
    ("distil-large-v3", "float16"): 3584,
}

# Размеры моделей ollama на этой машине (из `ollama list`), плюс запас на KV-кэш.
MT_VRAM_MIB = {
    "qwen3.8:27b": 17800,
    "qwen3-coder:30b": 19100,
    "gemma4:31b": 20400,
    "laguna-xs-2.1:q4_K_M": 20800,
    # Замерено 2026-09-03: ollama просит 17.1 ГиБ при num_ctx 8192.
    "translategemma:27b": 17600,
    "translategemma:12b": 8700,
    "translategemma:4b": 3800,
}


class ConfigError(RuntimeError):
    """Конфигурация не даёт однозначно определить, сколько памяти просить."""


@dataclass(frozen=True)
class AsrConfig:
    model: str = "large-v3"
    # int8 на GPU не быстрее fp16 — замерено 19.6 с против 19.2 на одном клипе.
    # Он экономит около гигабайта памяти и стоит скорости, а не наоборот.
    compute_type: str = "float16"
    # Язык речи. "auto" — решает определитель: он слушает окна по всей длине ролика
    # и работает всегда, а метка языка от YouTube для него лишь подсказка. Код языка
    # закрепляет язык для КАЖДОГО ролика: Whisper декодирует с ним, даже если звучит
    # другое, а определитель только отмечает расхождение. Закреплять стоит, лишь
    # когда все ролики на одном языке: на чужом звуке поведение декодера не
    # определено, и он начинает выбирать между переводом, транслитерацией и записью
    # услышанного чужим алфавитом — заново в каждом тридцатисекундном окне.
    # load() приводит значение к коду Whisper (helper/language.py), auto остаётся auto.
    language: str = "auto"
    vram_mib: int = 0

    def resolved_vram(self) -> int:
        if self.vram_mib:
            return self.vram_mib

        known = ASR_VRAM_MIB.get((self.model, self.compute_type))

        if known:
            return known

        raise ConfigError(
            f"неизвестен расход памяти для ASR {self.model}/{self.compute_type}. "
            f"Укажи vram_mib в [asr] файла {CONFIG_PATH} — без него контроль "
            f"допуска не сможет решить, помещается ли модель."
        )


@dataclass(frozen=True)
class TranslatorConfig:
    # Оба размера translategemma пробовались и отвергнуты. На реальных репликах
    # они СЛИВАЮТ соседние и уводят вывод, причём дрейф накапливается: замерено
    # на 160 репликах, у 12b съехало 11%, 32%, 50% и 81% по четырём батчам, и на
    # пятой позиции субтитр показывал текст десятой. qwen3.8:27b на тех же самых
    # батчах — ноль съехавших из 122 оценённых.
    #
    # Причина, судя по всему, структурная, а не в размере модели: translategemma
    # обучена переводить предложения, а реплики приходят обрывками фраз, и она
    # честно собирает из обрывков предложения — ровно тем разрушая соответствие
    # один к одному, на котором держится весь показ. Схема тут ни при чём:
    # количество ключей грамматика держит, а соответствие ключа исходной
    # реплике не гарантирует никто.
    model: str = "qwen3.8:27b"
    vram_mib: int = 0
    # Вторая строка субтитров для изучения языка. "cnr" — черногорский,
    # пустая строка выключает режим и оставляет только перевод на русский.
    #
    # Обе строки делаются за один проход модели: это вдвое дешевле двух проходов,
    # и русская строка получается глоссой именно к этой черногорской, а не
    # независимым переводом — для сопоставления слово к слову нужно именно так.
    study_language: str = "cnr"

    def resolved_vram(self) -> int:
        if self.vram_mib:
            return self.vram_mib

        known = MT_VRAM_MIB.get(self.model)

        if known:
            return known

        raise ConfigError(
            f"неизвестен расход памяти для переводчика {self.model}. "
            f"Укажи vram_mib в [translator] файла {CONFIG_PATH}; посмотреть "
            f"фактический размер можно через `ollama list`."
        )


@dataclass(frozen=True)
class YoutubeConfig:
    # Файл cookies (формат Netscape) отдельного аккаунта для роликов 18+. Без него
    # такие ролики не переводятся: YouTube отдаёт их только вошедшему. Всё прочее
    # демон качает анонимно и с аккаунтом — никогда. По словам самого yt-dlp,
    # аккаунт, через который он ходит, YouTube может забанить, поэтому свой
    # основной сюда не ставят. Как выгрузить cookies — в config.example.toml.
    cookies: str = ""

    @property
    def cookies_path(self) -> Path | None:
        return Path(self.cookies).expanduser() if self.cookies else None


@dataclass(frozen=True)
class ServerConfig:
    # Адрес, на котором слушает демон. По умолчанию петля: демон запускает
    # запросы к YouTube по команде клиента, и открывать его в сеть «на всякий
    # случай» нельзя. Планшет в другой комнате — это host = "0.0.0.0"
    # и токен из ~/.config/yt-trans/token как единственная защита.
    host: str = "127.0.0.1"
    # Сертификат и ключ для HTTPS. Нужны ради планшета: браузер считает
    # безопасным только https (или петлю), а без этого страницу нельзя поставить
    # как приложение, и значит нельзя зарегистрировать её в share-диалоге
    # Android. Пусто — демон работает по http, как раньше.
    tls_cert: str = ""
    tls_key: str = ""

    @property
    def tls_files(self) -> tuple[Path, Path] | None:
        if not (self.tls_cert or self.tls_key):
            return None

        return Path(self.tls_cert).expanduser(), Path(self.tls_key).expanduser()


@dataclass(frozen=True)
class VideoConfig:
    # Каталог готовых mp4. Пусто — общий кэш проекта (CACHE_DIR/video). Отдельная
    # настройка нужна ради /tmp: это tmpfs, то есть оперативная память, и там
    # потолок кэша меряют уже не диском.
    dir: str = ""
    # Потолок в гибибайтах. Двадцать — примерно пятьдесят часовых роликов в 1080p;
    # на диске это лишь защита от бесконечного роста, в памяти — ещё и от давления
    # на систему, поэтому там его ставят меньше.
    max_gib: float = 20.0

    @property
    def directory(self) -> Path | None:
        return Path(self.dir).expanduser() if self.dir else None

    @property
    def max_bytes(self) -> int:
        # float() до умножения: если в max_gib попало что-то нечисловое, это
        # должна быть быстрая ошибка, а не «строка × гигабайт», то есть
        # многогигабайтная строка в памяти.
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
                # Расширение показывает это в диагностике. На кнопку запуска
                # не годится: она рисуется до связи с демоном, и обещать по
                # этому флагу вторую строку значило бы врать, пока зритель
                # не включит её ещё и в настройках браузера.
                "study_language": self.translator.study_language,
            },
        }


def load(path: Path = CONFIG_PATH) -> Config:
    """Прочитать конфигурацию. Отсутствие файла — не ошибка, берутся значения по умолчанию."""
    raw: dict = {}

    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"не читается {path}: {exc}") from exc

    asr = AsrConfig(**_section(raw, "asr", AsrConfig))

    # Код языка приводится к виду Whisper здесь, один раз: "sh", "SR" и "sr-Latn" — это
    # sr, и дальше по демону ходит уже код. Незнакомое значение — не ошибка запуска, как
    # и cookies: демон поднимается, а каждое новое задание получает отказ той же фразой
    # (JobRunner._pin). str() — на случай числа вместо строки.
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

    # Потолок кэша читается при каждом решении о вытеснении, поэтому мусор здесь
    # всплыл бы только через час работы, посреди просмотра. Пусть падает при старте.
    try:
        if config.video.max_bytes <= 0:
            raise ConfigError(f"[video] max_gib должен быть больше нуля: {video.max_gib!r}")
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[video] max_gib не число: {video.max_gib!r}") from exc

    # Половина пары — не «почти https»: uvicorn поднял бы обычный http, и зритель
    # искал бы причину в планшете. Отсутствие файла тоже лучше увидеть при старте,
    # а не при первом открытии страницы.
    tls = config.server.tls_files

    if tls is not None:
        cert, key = tls

        if not (config.server.tls_cert and config.server.tls_key):
            missing = "tls_key" if config.server.tls_cert else "tls_cert"
            raise ConfigError(f"[server] {missing} не задан: для https нужны оба файла")

        for path, name in ((cert, "tls_cert"), (key, "tls_key")):
            if not path.is_file():
                raise ConfigError(f"[server] {name}: файла {path} нет")

    # Не ошибка запуска: без файла не откроются только ролики 18+, а остальное
    # работает. Зритель такого ролика получит ту же причину в отказе.
    cookies = youtube.cookies_path

    if cookies is not None and not cookies.is_file():
        log.warning("[youtube] cookies: файла %s нет, ролики 18+ открываться не будут", cookies)

    # Разрешаем расход памяти сразу, при старте. Ошибка в конфиге должна падать
    # на запуске демона, а не через минуту работы посреди первого задания.
    config.asr_vram_mib
    config.translator_vram_mib

    return config


def _section(raw: dict, name: str, kind: type) -> dict:
    """Поля секции, отбрасывая незнакомые ключи с предупреждением."""
    section = raw.get(name) or {}
    allowed = {f for f in kind.__dataclass_fields__}
    unknown = set(section) - allowed

    if unknown:
        log.warning("в секции [%s] игнорирую неизвестные ключи: %s", name, ", ".join(sorted(unknown)))

    return {k: v for k, v in section.items() if k in allowed}
