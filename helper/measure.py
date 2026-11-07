"""Measuring a model's VRAM usage — for the vram_mib line in the config.

Needed when installing a model that isn't in the reference table of helper/config.py.
The figure there isn't decorative: the admission check uses it to decide whether the
model fits. Underestimate — the daemon will climb onto a card where there's no room,
and risks taking down someone else's session via a runtime-OOM, which in ollama unloads
all models at once. Overestimate — jobs will wait for memory that isn't needed, and go
into a pause on timeout.

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

# Headroom over the measurement. For the translator it's smaller: ollama reports the
# exact allocation size, including the KV cache for the given context. For recognition
# it's larger: the peak depends on the batch content, and the measurement runs on a
# synthetic signal.
TRANSLATOR_MARGIN = 1.05
ASR_MARGIN = 1.15


class Sampler:
    """Records card occupancy in the background while the measurement runs."""

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
            raise SystemExit("NVML is unavailable — measurement is impossible")

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
    """Measurement on a busy card is meaningless: someone else's allocation would enter the peak."""
    import pynvml

    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    free_mib = pynvml.nvmlDeviceGetMemoryInfo(handle).free // MIB

    print(f"in use now {sampler.baseline} MiB, free {free_mib}")

    if free_mib < need_hint:
        print(
            f"\nWARNING: less than {need_hint} MiB free. If the model doesn't fit\n"
            "whole, the measurement will show not its appetite but how much could be allocated.\n"
            "Close the extra and repeat."
        )


def measure_translator(model: str, num_ctx: int) -> None:
    """Usage of an ollama model. It reports the allocation size itself — that's more precise than the peak."""
    with Sampler() as sampler:
        require_free_card(sampler, 4096)
        print(f"\nloading {model} with context {num_ctx} …")

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
            raise SystemExit(f"ollama is not responding or refused: {exc}") from None
        finally:
            # Unload in any case: the measurement must not leave a model hanging
            # on the card that the user needs.
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
        raise SystemExit(f"after loading, {model} did not appear in /api/ps — check the name")

    vram = entry["size_vram"] / MIB
    total = entry["size"] / MIB

    print(f"\nin VRAM {vram:.0f} MiB of {total:.0f} required")
    print(f"peak on the card:  +{sampler.delta} MiB over the baseline {sampler.baseline}")

    if sampler.delta > vram * 1.1:
        print(
            "\nThe peak is noticeably above the steady-state size: at load the model takes\n"
            "temporary buffers beyond the weights. The admission check must be guided\n"
            "by the peak — otherwise the model will pass the check and fail exactly at load time."
        )

    if vram < total * 0.98:
        print(
            "\nTHE MODEL DIDN'T FIT: part went to the CPU.\n"
            "The figure is useless — one must measure what fits whole.\n"
            "That's exactly how gemma4:31b asks for 21.5 GB, holds 17.95 resident\n"
            "and drops from 130 tokens per second to 15."
        )

        return

    # Take the larger of the two. size_vram — the steady-state size, the peak includes
    # the temporary load buffers; admission must survive the worst moment, not the average.
    recommended = max(int(vram * TRANSLATOR_MARGIN), int(sampler.delta * TRANSLATOR_MARGIN))

    recommend(model, recommended, "translator", MT_VRAM_MIB.get(model))


def measure_asr(model: str, compute_type: str, seconds: int, batch_size: int) -> None:
    """Recognition usage. Measured by a real subprocess — the same one the daemon spawns."""
    audio = _probe_audio(seconds)

    with Sampler() as sampler:
        require_free_card(sampler, 4096)
        print(f"\nrunning {model}/{compute_type} on {seconds} s of audio, batch {batch_size} …")

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
                # Without VAD, otherwise silence is screened out before the encoder and
                # the peak comes out lowered.
                "--no-vad",
            ],
            input=audio.tobytes(),
            capture_output=True,
            timeout=1800,
            cwd=Path(__file__).resolve().parent.parent,
        )

    if proc.returncode != 0:
        raise SystemExit(f"the worker crashed:\n{proc.stderr.decode(errors='replace')[-1500:]}")

    print(f"\npeak on the card: +{sampler.delta} MiB over the baseline {sampler.baseline}")
    recommend(
        f"{model}/{compute_type}",
        int(sampler.delta * ASR_MARGIN),
        "asr",
        ASR_VRAM_MIB.get((model, compute_type)),
    )


def _probe_audio(seconds: int) -> np.ndarray:
    """Audio for the run. The peak is determined by the weights and batch size, not by the content."""
    cached = sorted(CACHE_DIR.glob("fleurs_sr_*.npz"))

    if cached:
        stored = np.load(cached[0])
        chunks = [stored[key] for key in stored.files]
        audio = np.concatenate(chunks)[: seconds * 16000]

        if audio.size >= 16000:
            print(f"taking audio from {cached[0].name}")

            return audio.astype(np.float32)

    print("no real audio at hand, taking silence (VAD disabled, the encoder will run)")

    return np.zeros(seconds * 16000, dtype=np.float32)


def recommend(label: str, value: int, section: str, known: int | None) -> None:
    print(f"\n  # {label}")
    print(f"  [{section}]")
    print(f"  vram_mib = {value}")

    if known:
        print(f"\nthe reference table already has the value {known} MiB — the measurement {'matches' if abs(known - value) <= known * 0.2 else 'DISAGREES'}.")
    else:
        print("\nThe reference table helper/config.py doesn't contain this model, so the line above")
        print(f"needs to be added to section [{section}] of ~/.config/yt-trans/config.toml.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", help="model name: an ollama tag or a faster-whisper model")
    parser.add_argument("--asr", action="store_true", help="measure as a recognition model")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--num-ctx", type=int, default=8192, help="translator context")
    parser.add_argument("--seconds", type=int, default=300, help="how much audio to run")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    # An ollama tag always contains a colon, whisper names — never.
    as_asr = args.asr or ":" not in args.model

    started = time.monotonic()

    if as_asr:
        measure_asr(args.model, args.compute_type, args.seconds, args.batch_size)
    else:
        measure_translator(args.model, args.num_ctx)

    print(f"\ntook {time.monotonic() - started:.0f} s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
