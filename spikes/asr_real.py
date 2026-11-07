"""ASR diagnostics on real videos — without references.

FLEURS is read-out Wikipedia prose; it ranks models, but flatters any
and does not predict the product experience. Here we measure what can be measured without
a labeled reference, and what matters more for an overlay than a tenth of WER:

  * the share of Cyrillic before and after normalization — whether Whisper wavers between scripts;
  * the longest consecutive n-gram repeat — whether the model fell into a loop;
  * cues over a musical intro — whether it hallucinates on silence and music;
  * speed relative to real time.

Plus this is the first end-to-end run of the production path: yt-dlp → ffmpeg → PCM → worker
in a separate process → script normalization. Reading the output with your eyes is mandatory.

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
    """Production path: worker as a separate process, PCM through stdin.

    Separate, not a function call — this way both the protocol and the fact that
    the process really gives VRAM back on exit are checked.
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
    parser.add_argument("--show", type=int, default=8, help="how many cues to show")
    parser.add_argument("--out", type=Path, default=Path("spikes/asr_real.json"))
    args = parser.parse_args()

    report = []

    for video_id in args.ids:
        print(f"\n{'=' * 72}\n{video_id}")

        try:
            info = probe(video_id)
            print(f"  {info.title[:66]}")
            print(f"  channel: {info.channel} | duration: {int(info.duration or 0)} s")
        except YtDlpError as exc:
            print(f"  METADATA ERROR: {exc}")
            continue

        try:
            started = time.monotonic()
            audio = audio_pcm(video_id)
            fetch_seconds = time.monotonic() - started
        except YtDlpError as exc:
            print(f"  AUDIO ERROR: {exc}")
            continue

        full_seconds = audio.size / SAMPLE_RATE
        audio = audio[: int(args.max_seconds * SAMPLE_RATE)]
        clip_seconds = audio.size / SAMPLE_RATE
        print(f"  audio: {full_seconds:.0f} s received in {fetch_seconds:.1f} s, "
              f"taking the first {clip_seconds:.0f} s")

        try:
            started = time.monotonic()
            result = run_worker(audio.tobytes(), args.language, args.model, timeout=1800)
            asr_seconds = time.monotonic() - started
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            print(f"  ASR ERROR: {str(exc)[:400]}")
            continue

        cues = result["cues"]
        raw = " ".join(c["text"] for c in cues)
        latin = to_latin(raw)

        # The full production order: script normalization, then cue assembly.
        # Whisper segments are thirty-second windows of ~300 characters, not subtitles;
        # they cannot be passed further down the pipeline either for display or for translation.
        subtitle_cues = build_cues(normalize_cues(cues))

        # Coverage catches what WER does not: silently dropped speech. A model
        # that missed twenty seconds of the story looks fine by WER on short excerpts,
        # but on a real video that is a hole in the subtitles.
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
            # All cues after segmentation go into the file: it serves as input
            # for the following spikes. Trimming to args.show affects only printing.
            "cues_text": [c.text for c in subtitle_cues],
        }
        report.append(entry)

        print(
            f"  segments {entry['cues']} → cues {entry['subtitle_cues']} "
            f"(median {entry['median_cue_chars']} chars)   "
            f"coverage {entry['coverage']:.0%}   "
            f"Cyrillic {entry['cyrillic_before']:.2f} → {entry['cyrillic_after']:.2f}   "
            f"repeat ×{entry['max_repeat']}   "
            f"ASR {asr_seconds:.1f} s (×{entry['realtime_factor']:.0f} RT)"
        )

        if entry["first_cue_start"] is not None:
            print(f"  first cue at {entry['first_cue_start']:.1f} s")

        print("  ── first cues ──")

        for cue in cues[: args.show]:
            print(f"   {cue['start']:6.1f}  {to_latin(cue['text'])[:64]}")

    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'=' * 72}\nwritten to {args.out}")

    if report:
        flips = [r for r in report if 0.05 < r["cyrillic_before"] < 0.95]
        loops = [r for r in report if r["max_repeat"] >= 3]

        print(f"\nvideos: {len(report)}")
        print(f"mixed script within a video: {len(flips)}")
        print(f"suspected hallucination loop:      {len(loops)}")

        if flips:
            print("  → an initial_prompt in Latin is needed to pin the script")

        if loops:
            print(f"  → check: {', '.join(r['video_id'] for r in loops)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
