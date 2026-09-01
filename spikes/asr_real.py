"""Диагностика ASR на реальных роликах — без эталонов.

FLEURS это начитанная википедийная проза; она ранжирует модели, но польстит любой
и не предсказывает продуктовый опыт. Здесь меряем то, что можно померить без
размеченного эталона, и что для оверлея важнее десятой доли WER:

  * доля кириллицы до и после нормализации — колеблется ли Whisper между письменностями;
  * максимальный подряд идущий повтор n-граммы — ушла ли модель в цикл;
  * реплики на музыкальном интро — галлюцинирует ли на тишине и музыке;
  * скорость относительно реального времени.

Плюс это первый сквозной прогон боевого пути: yt-dlp → ffmpeg → PCM → воркер
в отдельном процессе → нормализация письменности. Читать вывод глазами обязательно.

  python spikes/asr_real.py --ids VIDEOID1 VIDEOID2 --max-seconds 300
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helper.env  # noqa: F401

from helper.asr.worker import SAMPLE_RATE
from helper.script import cyrillic_ratio, normalize_cues, to_latin
from helper.segment import build_cues
from helper.sources.ytdlp import YtDlpError, audio_pcm, probe


def max_ngram_repeat(text: str, n: int = 3) -> int:
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


def run_worker(pcm_bytes: bytes, language: str, model: str, timeout: float) -> dict:
    """Боевой путь: воркер отдельным процессом, PCM через stdin.

    Именно отдельным, а не вызовом функции — так проверяется и протокол, и то,
    что процесс действительно отдаёт VRAM на выходе.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "helper.asr.worker",
            "--language",
            language,
            "--model",
            model,
        ],
        input=pcm_bytes,
        capture_output=True,
        timeout=timeout,
        cwd=Path(__file__).resolve().parent.parent,
    )

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-2000:])

    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--language", default="sr")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--max-seconds", type=float, default=300.0)
    parser.add_argument("--show", type=int, default=8, help="сколько реплик показать")
    parser.add_argument("--out", type=Path, default=Path("spikes/asr_real.json"))
    args = parser.parse_args()

    report = []

    for video_id in args.ids:
        print(f"\n{'=' * 72}\n{video_id}")

        try:
            info = probe(video_id)
            print(f"  {info.title[:66]}")
            print(f"  канал: {info.channel} | длительность: {int(info.duration or 0)} с")
        except YtDlpError as exc:
            print(f"  ОШИБКА метаданных: {exc}")
            continue

        try:
            started = time.monotonic()
            audio = audio_pcm(video_id)
            fetch_seconds = time.monotonic() - started
        except YtDlpError as exc:
            print(f"  ОШИБКА аудио: {exc}")
            continue

        full_seconds = audio.size / SAMPLE_RATE
        audio = audio[: int(args.max_seconds * SAMPLE_RATE)]
        clip_seconds = audio.size / SAMPLE_RATE
        print(f"  аудио: {full_seconds:.0f} с получено за {fetch_seconds:.1f} с, "
              f"беру первые {clip_seconds:.0f} с")

        try:
            started = time.monotonic()
            result = run_worker(audio.tobytes(), args.language, args.model, timeout=1800)
            asr_seconds = time.monotonic() - started
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            print(f"  ОШИБКА ASR: {str(exc)[:400]}")
            continue

        cues = result["cues"]
        raw = " ".join(c["text"] for c in cues)
        latin = to_latin(raw)

        # Боевой порядок целиком: нормализация письменности, затем сборка реплик.
        # Сегменты Whisper — это тридцатисекундные окна по ~300 знаков, а не субтитры;
        # отдавать их дальше по пайплайну нельзя ни для показа, ни для перевода.
        subtitle_cues = build_cues(normalize_cues(cues))

        # Покрытие ловит то, чего не ловит WER: молча выброшенную речь. Модель,
        # пропустившая двадцать секунд рассказа, по WER на коротких отрывках
        # выглядит нормально, а на реальном ролике это дыра в субтитрах.
        spoken = sum(c["end"] - c["start"] for c in cues)

        entry = {
            "video_id": video_id,
            "title": info.title,
            "clip_seconds": clip_seconds,
            "cues": len(cues),
            "coverage": spoken / clip_seconds if clip_seconds else 0.0,
            "chars": sum(len(c["text"]) for c in cues),
            "cyrillic_before": cyrillic_ratio(raw),
            "cyrillic_after": cyrillic_ratio(latin),
            "max_repeat": max_ngram_repeat(latin),
            "asr_seconds": asr_seconds,
            "realtime_factor": clip_seconds / asr_seconds if asr_seconds else 0.0,
            "first_cue_start": cues[0]["start"] if cues else None,
            "subtitle_cues": len(subtitle_cues),
            "median_cue_chars": sorted(len(c.text) for c in subtitle_cues)[
                len(subtitle_cues) // 2
            ]
            if subtitle_cues
            else 0,
            # В файл идут все реплики после сегментации: он служит входом
            # для следующих спайков. Урезание до args.show касается только печати.
            "cues_text": [c.text for c in subtitle_cues],
        }
        report.append(entry)

        print(
            f"  сегментов {entry['cues']} → реплик {entry['subtitle_cues']} "
            f"(медиана {entry['median_cue_chars']} знаков)   "
            f"покрытие {entry['coverage']:.0%}   "
            f"кириллица {entry['cyrillic_before']:.2f} → {entry['cyrillic_after']:.2f}   "
            f"повтор ×{entry['max_repeat']}   "
            f"ASR {asr_seconds:.1f} с (×{entry['realtime_factor']:.0f} рт)"
        )

        if entry["first_cue_start"] is not None:
            print(f"  первая реплика на {entry['first_cue_start']:.1f} с")

        print("  ── первые реплики ──")

        for cue in cues[: args.show]:
            print(f"   {cue['start']:6.1f}  {to_latin(cue['text'])[:64]}")

    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'=' * 72}\nзаписано в {args.out}")

    if report:
        flips = [r for r in report if 0.05 < r["cyrillic_before"] < 0.95]
        loops = [r for r in report if r["max_repeat"] >= 3]

        print(f"\nроликов: {len(report)}")
        print(f"смешанная письменность внутри ролика: {len(flips)}")
        print(f"подозрение на цикл галлюцинаций:      {len(loops)}")

        if flips:
            print("  → initial_prompt на латинице нужен, чтобы прибить письменность")

        if loops:
            print(f"  → проверить: {', '.join(r['video_id'] for r in loops)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
