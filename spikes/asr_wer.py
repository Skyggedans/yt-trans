"""Спайк 1: сколько на самом деле стоит сербский ASR.

Публичной цифры WER для large-v3 на сербском не существует. Единственная первичная —
16–29% на FLEURS по трём меткам bs/hr/sr, эпоха large-v2, декабрь 2022. Всё остальное
в обороте либо оттуда, либо из in-domain read speech, который польстит любой модели.

Меряем три вещи, и WER из них не самая важная:
  * WER — насколько плох транскрипт;
  * доля кириллицы — стабильна ли письменность (Whisper колеблется между двумя);
  * максимальный повтор n-граммы — ушла ли модель в цикл галлюцинаций.

Обе стороны нормализуются в латиницу ДО сравнения. Без этого меряется выбор
письменности, а не точность, и цифры получаются бессмысленные.

  python spikes/asr_wer.py --fleurs 100
  python spikes/asr_wer.py --clips clips/          # свои ролики + .txt рядом
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

import helper.env  # noqa: F401  — уводит кэш HF с ramdisk, обязателен до faster_whisper

# (модель, язык) — каждое плечо отвечает на свой вопрос:
#   large-v3/sr   базовая линия
#   turbo/sr      цена turbo, чья оценка почти наверняка не включала сербский
#   large-v3/hr   цена екавицы против иекавицы и расхождения лексики
ARMS = [
    ("large-v3", "sr"),
    ("large-v3-turbo", "sr"),
    ("large-v3", "hr"),
]

CYRILLIC = re.compile(r"[Ѐ-ӿ]")
PUNCT = str.maketrans("", "", string.punctuation + "«»„“”—–…")


def normalize(text: str) -> str:
    """В латиницу, без пунктуации, в нижний регистр.

    Направление важно: кириллица → латиница это инъективное отображение, а обратное
    неоднозначно на диграфах lj/nj/dž через границу морфемы (nadživeti → надживети,
    а не наџивети), и правиловые транслитераторы там ошибаются.
    """
    latin = cyrtranslit.to_latin(text, "sr")

    return " ".join(latin.lower().translate(PUNCT).split())


def cyrillic_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]

    if not letters:
        return 0.0

    return sum(bool(CYRILLIC.match(c)) for c in letters) / len(letters)


def max_ngram_repeat(text: str, n: int = 3) -> int:
    """Длина самого длинного подряд идущего повтора n-граммы — детектор цикла."""
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
    """Только нужные сэмплы теста, потоком.

    Без streaming `datasets` материализует все сплиты целиком (train там 2944 записи
    со звуком) ещё до того, как применит срез `test[:100]` — несколько гигабайт ради
    ста примеров.
    """
    import io
    from itertools import islice

    import soundfile as sf
    from datasets import Audio, load_dataset

    from helper.env import CACHE_DIR

    # Streaming ничего не кэширует, а тянется он долго. Прогонов будет много —
    # раскладываем распакованные сэмплы рядом, чтобы перезапуск был мгновенным.
    cache = CACHE_DIR / f"fleurs_sr_{limit}.npz"
    texts_path = cache.with_suffix(".json")

    if cache.exists() and texts_path.exists():
        stored = np.load(cache)
        texts = json.loads(texts_path.read_text(encoding="utf-8"))

        return [(stored[f"a{i}"], text) for i, text in enumerate(texts)]

    ds = load_dataset("google/fleurs", "sr_rs", split="test", streaming=True)

    # decode=False обязателен: начиная с datasets 5.x декодирование аудио идёт через
    # torchcodec, то есть тянет весь torch ради чтения wav. Байты мы и так умеем
    # прочитать soundfile'ом, который уже стоит ради своих клипов.
    ds = ds.cast_column("audio", Audio(decode=False))
    items = []

    for row in islice(ds, limit):
        audio, rate = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")

        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        if rate != 16000:
            raise SystemExit(f"FLEURS отдал {rate} Гц вместо 16000")

        items.append((audio, row["transcription"]))

    np.savez(cache, **{f"a{i}": audio for i, (audio, _) in enumerate(items)})
    texts_path.write_text(
        json.dumps([text for _, text in items], ensure_ascii=False), encoding="utf-8"
    )

    return items


def load_clips(directory: Path) -> list[tuple[np.ndarray, str]]:
    """Свои ролики: <name>.wav рядом с <name>.txt (эталон, выправленный руками)."""
    import soundfile as sf

    items = []

    for wav in sorted(directory.glob("*.wav")):
        ref = wav.with_suffix(".txt")

        if not ref.exists():
            print(f"  пропуск {wav.name}: нет {ref.name}")
            continue

        audio, rate = sf.read(wav, dtype="float32")

        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        if rate != 16000:
            raise SystemExit(f"{wav} должен быть 16 кГц, а не {rate}")

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

    # Пустая гипотеза ломает jiwer и всё равно означает провал — считаем её как есть.
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
    parser.add_argument("--fleurs", type=int, default=0, help="сколько сэмплов FLEURS sr_rs")
    parser.add_argument("--clips", type=Path, help="каталог со своими .wav + .txt")
    parser.add_argument("--out", type=Path, default=Path("spikes/asr_wer.json"))
    args = parser.parse_args()

    data: list[tuple[np.ndarray, str]] = []

    if args.fleurs:
        print(f"загружаю FLEURS sr_rs [:{args.fleurs}] …")
        data += load_fleurs(args.fleurs)

    if args.clips:
        print(f"загружаю свои клипы из {args.clips} …")
        data += load_clips(args.clips)

    if not data:
        parser.error("нужен --fleurs и/или --clips")

    print(f"{len(data)} сэмплов, {len(ARMS)} плеч\n")
    results = []

    for model_name, language in ARMS:
        print(f"→ {model_name} / {language}")
        result = run_arm(model_name, language, data)
        results.append(result)
        print(
            f"   WER {result.wer:.3f}   кириллица {result.cyrillic_ratio:.2f}   "
            f"повтор ×{result.max_repeat}   {result.seconds:.1f} с"
        )

    args.out.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"\nзаписано в {args.out}")

    print(f"\n{'модель':22s} {'яз':3s} {'WER':>7s} {'кир':>6s} {'повтор':>7s} {'сек':>8s}")

    for r in sorted(results, key=lambda x: x.wer):
        print(
            f"{r.model:22s} {r.language:3s} {r.wer:7.3f} {r.cyrillic_ratio:6.2f} "
            f"{r.max_repeat:7d} {r.seconds:8.1f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
