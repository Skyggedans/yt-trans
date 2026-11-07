"""Spike 1: what Serbian ASR actually costs.

There is no public WER number for large-v3 on Serbian. The only primary one —
16–29% on FLEURS across three labels bs/hr/sr, large-v2 era, December 2022. Everything else
in circulation is either from there or from in-domain read speech, which flatters any model.

We measure three things, and WER is not the most important of them:
  * WER — how bad the transcript is;
  * the share of Cyrillic — whether the script is stable (Whisper wavers between the two);
  * the longest n-gram repeat — whether the model fell into a hallucination loop.

Both sides are normalized to Latin BEFORE comparison. Without this, it measures the choice
of script, not accuracy, and the numbers come out meaningless.

  python spikes/asr_wer.py --fleurs 100
  python spikes/asr_wer.py --clips clips/          # your own videos + .txt next to them
"""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cyrtranslit
import jiwer
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helper.env  # noqa: F401  — moves the HF cache off the ramdisk, mandatory before faster_whisper

# (model, language) — each arm answers its own question:
#   large-v3/sr   baseline
#   turbo/sr      the cost of turbo, whose evaluation almost certainly did not include Serbian
#   large-v3/hr   the cost of ekavian versus ijekavian and vocabulary divergence
ARMS = [
    ("large-v3", "sr"),
    ("large-v3-turbo", "sr"),
    ("large-v3", "hr"),
]

CYRILLIC = re.compile(r"[Ѐ-ӿ]")
PUNCT = str.maketrans("", "", string.punctuation + "«»„“”—–…")


def normalize(text: str) -> str:
    """To Latin, without punctuation, lowercase.

    Direction matters: Cyrillic → Latin is an injective mapping, while the reverse
    is ambiguous on the digraphs lj/nj/dž across a morpheme boundary (nadživeti → надживети,
    not наџивети), and rule-based transliterators make mistakes there.
    """
    latin = cyrtranslit.to_latin(text, "sr")

    return " ".join(latin.lower().translate(PUNCT).split())


def cyrillic_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]

    if not letters:
        return 0.0

    return sum(bool(CYRILLIC.match(c)) for c in letters) / len(letters)


def max_ngram_repeat(text: str, n: int = 3) -> int:
    """Length of the longest consecutive repeat of an n-gram — a loop detector."""
    words = text.split()
    best = 0

    for i in range(len(words) - n + 1):
        gram = words[i : i + n]
        run, j = 1, i + n

        while j + n <= len(words) and words[j : j + n] == gram:
            run += 1
            j += n

        best = max(best, run)

    return best


@dataclass
class ArmResult:
    model: str
    language: str
    wer: float
    cyrillic_ratio: float
    max_repeat: int
    seconds: float
    samples: int


def load_fleurs(limit: int) -> list[tuple[np.ndarray, str]]:
    """Only the needed test samples, as a stream.

    Without streaming `datasets` materializes all splits in full (train there is 2944 records
    with audio) even before applying the `test[:100]` slice — several gigabytes for
    a hundred examples.
    """
    import io
    from itertools import islice

    import soundfile as sf
    from datasets import Audio, load_dataset

    from helper.env import CACHE_DIR

    # Streaming caches nothing, and it takes a long time. There will be many runs —
    # we lay the decoded samples next to it so that a restart is instant.
    cache = CACHE_DIR / f"fleurs_sr_{limit}.npz"
    texts_path = cache.with_suffix(".json")

    if cache.exists() and texts_path.exists():
        stored = np.load(cache)
        texts = json.loads(texts_path.read_text(encoding="utf-8"))

        return [(stored[f"a{i}"], text) for i, text in enumerate(texts)]

    ds = load_dataset("google/fleurs", "sr_rs", split="test", streaming=True)

    # decode=False is mandatory: since datasets 5.x audio decoding goes through
    # torchcodec, that is, it pulls the whole torch just to read wav. We can already read
    # the bytes with soundfile, which is installed for its own clips anyway.
    ds = ds.cast_column("audio", Audio(decode=False))
    items = []

    for row in islice(ds, limit):
        audio, rate = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")

        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        if rate != 16000:
            raise SystemExit(f"FLEURS returned {rate} Hz instead of 16000")

        items.append((audio, row["transcription"]))

    np.savez(cache, **{f"a{i}": audio for i, (audio, _) in enumerate(items)})
    texts_path.write_text(
        json.dumps([text for _, text in items], ensure_ascii=False), encoding="utf-8"
    )

    return items


def load_clips(directory: Path) -> list[tuple[np.ndarray, str]]:
    """Your own videos: <name>.wav next to <name>.txt (a reference corrected by hand)."""
    import soundfile as sf

    items = []

    for wav in sorted(directory.glob("*.wav")):
        ref = wav.with_suffix(".txt")

        if not ref.exists():
            print(f"  skipping {wav.name}: no {ref.name}")
            continue

        audio, rate = sf.read(wav, dtype="float32")

        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        if rate != 16000:
            raise SystemExit(f"{wav} must be 16 kHz, not {rate}")

        items.append((audio, ref.read_text(encoding="utf-8")))

    return items


def run_arm(model_name: str, language: str, data: list[tuple[np.ndarray, str]]) -> ArmResult:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cuda", compute_type="float16")
    started = time.monotonic()
    hyps, refs = [], []

    for audio, reference in data:
        segments, _ = model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=5,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            condition_on_previous_text=False,
            word_timestamps=True,
            hallucination_silence_threshold=2.0,
            vad_filter=True,
            vad_parameters=dict(
                threshold=0.5,
                min_speech_duration_ms=250,
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
        )
        hyps.append(" ".join(s.text.strip() for s in segments))
        refs.append(reference)

    elapsed = time.monotonic() - started
    del model

    raw = " ".join(hyps)
    norm_hyps = [normalize(h) for h in hyps]
    norm_refs = [normalize(r) for r in refs]

    # An empty hypothesis breaks jiwer and still means failure — we count it as is.
    pairs = [(r, h) for r, h in zip(norm_refs, norm_hyps) if r]

    return ArmResult(
        model=model_name,
        language=language,
        wer=jiwer.wer([r for r, _ in pairs], [h for _, h in pairs]),
        cyrillic_ratio=cyrillic_ratio(raw),
        max_repeat=max(max_ngram_repeat(h) for h in norm_hyps) if norm_hyps else 0,
        seconds=elapsed,
        samples=len(data),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleurs", type=int, default=0, help="how many FLEURS sr_rs samples")
    parser.add_argument("--clips", type=Path, help="directory with your own .wav + .txt")
    parser.add_argument("--out", type=Path, default=Path("spikes/asr_wer.json"))
    args = parser.parse_args()

    data: list[tuple[np.ndarray, str]] = []

    if args.fleurs:
        print(f"loading FLEURS sr_rs [:{args.fleurs}] …")
        data += load_fleurs(args.fleurs)

    if args.clips:
        print(f"loading own clips from {args.clips} …")
        data += load_clips(args.clips)

    if not data:
        parser.error("need --fleurs and/or --clips")

    print(f"{len(data)} samples, {len(ARMS)} arms\n")
    results = []

    for model_name, language in ARMS:
        print(f"→ {model_name} / {language}")
        result = run_arm(model_name, language, data)
        results.append(result)
        print(
            f"   WER {result.wer:.3f}   Cyrillic {result.cyrillic_ratio:.2f}   "
            f"repeat ×{result.max_repeat}   {result.seconds:.1f} s"
        )

    args.out.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"\nwritten to {args.out}")

    print(f"\n{'model':22s} {'lang':3s} {'WER':>7s} {'cyr':>6s} {'repeat':>7s} {'sec':>8s}")

    for r in sorted(results, key=lambda x: x.wer):
        print(
            f"{r.model:22s} {r.language:3s} {r.wer:7.3f} {r.cyrillic_ratio:6.2f} "
            f"{r.max_repeat:7d} {r.seconds:8.1f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
