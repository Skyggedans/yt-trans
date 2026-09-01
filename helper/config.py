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
from dataclasses import dataclass
from pathlib import Path

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
    "translategemma:27b": 17500,
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
    language: str = "sr"
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
    model: str = "qwen3.8:27b"
    vram_mib: int = 0

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
class Config:
    asr: AsrConfig
    translator: TranslatorConfig

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
    translator = TranslatorConfig(**_section(raw, "translator", TranslatorConfig))
    config = Config(asr=asr, translator=translator)

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
