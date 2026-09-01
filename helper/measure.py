"""Замер расхода видеопамяти моделью — для строки vram_mib в конфиге.

Нужен, когда ставишь модель, которой нет в справочнике helper/config.py.
Цифра там не декоративная: по ней контроль допуска решает, помещается ли модель.
Занизишь — демон полезет на карту, где места нет, и рискует уронить чужую сессию
через runtime-OOM, который у ollama выгружает разом все модели. Завысишь — задания
будут ждать памяти, которая не нужна, и уходить в паузу по таймауту.

  python -m helper.measure translategemma:12b
  python -m helper.measure --asr large-v3-turbo --compute-type int8_float16
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np

from helper.config import ASR_VRAM_MIB, MT_VRAM_MIB
from helper.env import CACHE_DIR
from helper.gpulease import OLLAMA_URL

MIB = 1024 * 1024
SAMPLE_INTERVAL = 0.2

# Запас над замером. У переводчика меньше: ollama сообщает точный размер
# аллокации, включая KV-кэш под заданный контекст. У распознавания больше:
# пик зависит от содержимого батча, а замер идёт на синтетическом сигнале.
TRANSLATOR_MARGIN = 1.05
ASR_MARGIN = 1.15


class Sampler:
    """Пишет занятость карты в фоне, пока идёт замер."""

    def __init__(self) -> None:
        self._samples: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._nvml = self._init_nvml()

    @staticmethod
    def _init_nvml():
        try:
            import pynvml

            pynvml.nvmlInit()

            return pynvml
        except Exception:
            return None

    def _used_mib(self) -> int | None:
        if not self._nvml:
            return None

        try:
            handle = self._nvml.nvmlDeviceGetHandleByIndex(0)

            return self._nvml.nvmlDeviceGetMemoryInfo(handle).used // MIB
        except Exception:
            return None

    def _loop(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL):
            value = self._used_mib()

            if value is not None:
                self._samples.append(value)

    def __enter__(self) -> Sampler:
        baseline = self._used_mib()

        if baseline is None:
            raise SystemExit("NVML недоступен — замер невозможен")

        self.baseline = baseline
        self._samples.append(baseline)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=2)

    @property
    def peak(self) -> int:
        return max(self._samples) if self._samples else self.baseline

    @property
    def delta(self) -> int:
        return max(0, self.peak - self.baseline)


def require_free_card(sampler: Sampler, need_hint: int) -> None:
    """Замер на занятой карте бессмыслен: чужая аллокация войдёт в пик."""
    import pynvml

    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    free_mib = pynvml.nvmlDeviceGetMemoryInfo(handle).free // MIB

    print(f"занято сейчас {sampler.baseline} МиБ, свободно {free_mib}")

    if free_mib < need_hint:
        print(
            f"\nВНИМАНИЕ: свободно меньше {need_hint} МиБ. Если модель не поместится\n"
            "целиком, замер покажет не её аппетит, а то, сколько удалось выделить.\n"
            "Закрой лишнее и повтори."
        )


def measure_translator(model: str, num_ctx: int) -> None:
    """Расход модели ollama. Она сама сообщает размер аллокации — это точнее пика."""
    with Sampler() as sampler:
        require_free_card(sampler, 4096)
        print(f"\nзагружаю {model} с контекстом {num_ctx} …")

        try:
            httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": "hi",
                    "stream": False,
                    "options": {"num_ctx": num_ctx},
                    "keep_alive": "2m",
                },
                timeout=900.0,
            ).raise_for_status()

            loaded = httpx.get(f"{OLLAMA_URL}/api/ps", timeout=10.0).json().get("models", [])
        except httpx.HTTPError as exc:
            raise SystemExit(f"ollama не отвечает или отказала: {exc}") from None
        finally:
            # Выгружаем в любом случае: замер не должен оставлять модель висеть
            # на карте, которая нужна пользователю.
            try:
                httpx.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": model, "keep_alive": 0},
                    timeout=60.0,
                )
            except httpx.HTTPError:
                pass

    entry = next((m for m in loaded if m.get("name") == model), None)

    if entry is None:
        raise SystemExit(f"после загрузки {model} не появилась в /api/ps — проверь имя")

    vram = entry["size_vram"] / MIB
    total = entry["size"] / MIB

    print(f"\nв видеопамяти {vram:.0f} МиБ из {total:.0f} требуемых")
    print(f"пик по карте:  +{sampler.delta} МиБ над базой {sampler.baseline}")

    if sampler.delta > vram * 1.1:
        print(
            "\nПик заметно выше установившегося размера: при загрузке модель берёт\n"
            "временные буферы сверх весов. Контроль допуска обязан ориентироваться\n"
            "на пик — иначе модель пройдёт проверку и упадёт именно в момент загрузки."
        )

    if vram < total * 0.98:
        print(
            "\nМОДЕЛЬ НЕ ПОМЕСТИЛАСЬ: часть ушла на процессор.\n"
            "Цифра бесполезна — мерить надо то, что помещается целиком.\n"
            "Именно так gemma4:31b просит 21.5 ГБ, резидентно держит 17.95\n"
            "и падает со 130 токенов в секунду до 15."
        )

        return

    # Берём большее из двух. size_vram — установившийся размер, пик включает
    # временные буферы загрузки; допуск должен пережить худший момент, а не средний.
    recommended = max(int(vram * TRANSLATOR_MARGIN), int(sampler.delta * TRANSLATOR_MARGIN))

    recommend(model, recommended, "translator", MT_VRAM_MIB.get(model))


def measure_asr(model: str, compute_type: str, seconds: int, batch_size: int) -> None:
    """Расход распознавания. Меряется настоящим подпроцессом — тем же, что спавнит демон."""
    audio = _probe_audio(seconds)

    with Sampler() as sampler:
        require_free_card(sampler, 4096)
        print(f"\nпрогоняю {model}/{compute_type} на {seconds} с звука, батч {batch_size} …")

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "helper.asr.worker",
                "--model",
                model,
                "--compute-type",
                compute_type,
                "--batch-size",
                str(batch_size),
                # Без VAD, иначе тишина отсеется до энкодера и пик выйдет заниженным.
                "--no-vad",
            ],
            input=audio.tobytes(),
            capture_output=True,
            timeout=1800,
            cwd=Path(__file__).resolve().parent.parent,
        )

    if proc.returncode != 0:
        raise SystemExit(f"воркер упал:\n{proc.stderr.decode(errors='replace')[-1500:]}")

    print(f"\nпик по карте: +{sampler.delta} МиБ над базой {sampler.baseline}")
    recommend(
        f"{model}/{compute_type}",
        int(sampler.delta * ASR_MARGIN),
        "asr",
        ASR_VRAM_MIB.get((model, compute_type)),
    )


def _probe_audio(seconds: int) -> np.ndarray:
    """Звук для прогона. Пик определяют веса и размер батча, а не содержимое."""
    cached = sorted(CACHE_DIR.glob("fleurs_sr_*.npz"))

    if cached:
        stored = np.load(cached[0])
        chunks = [stored[key] for key in stored.files]
        audio = np.concatenate(chunks)[: seconds * 16000]

        if audio.size >= 16000:
            print(f"беру звук из {cached[0].name}")

            return audio.astype(np.float32)

    print("настоящего звука под рукой нет, беру тишину (VAD отключён, энкодер отработает)")

    return np.zeros(seconds * 16000, dtype=np.float32)


def recommend(label: str, value: int, section: str, known: int | None) -> None:
    print(f"\n  # {label}")
    print(f"  [{section}]")
    print(f"  vram_mib = {value}")

    if known:
        print(f"\nв справочнике уже есть значение {known} МиБ — замер {'сходится' if abs(known - value) <= known * 0.2 else 'РАСХОДИТСЯ'}.")
    else:
        print("\nВ справочнике helper/config.py этой модели нет, поэтому строку выше")
        print(f"нужно добавить в секцию [{section}] файла ~/.config/yt-trans/config.toml.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", help="имя модели: ollama-тег или модель faster-whisper")
    parser.add_argument("--asr", action="store_true", help="мерить как модель распознавания")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--num-ctx", type=int, default=8192, help="контекст переводчика")
    parser.add_argument("--seconds", type=int, default=300, help="сколько звука прогнать")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    # Тег ollama всегда содержит двоеточие, имена whisper — никогда.
    as_asr = args.asr or ":" not in args.model

    started = time.monotonic()

    if as_asr:
        measure_asr(args.model, args.compute_type, args.seconds, args.batch_size)
    else:
        measure_translator(args.model, args.num_ctx)

    print(f"\nзаняло {time.monotonic() - started:.0f} с")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
