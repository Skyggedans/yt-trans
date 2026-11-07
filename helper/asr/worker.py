"""Speech recognition as a short-lived subprocess.

Deliberately a separate process, not a function in the daemon. Two reasons:

1. CTranslate2 doesn't always return VRAM to the driver on `del model`, and on this card
   between the translator (17.3 GB) and the desktop there remain 1–3 GB. Process exit is
   the only guarantee that the memory was freed.
2. Eviction becomes SIGKILL instead of a cooperative protocol: when the watchdog sees
   that the user has started their own work on the GPU, the worker is simply killed.

PCM comes on stdin as f32le 16 kHz mono — exactly what ffmpeg outputs in
sources/ytdlp.py, without intermediate files. JSON with utterances goes to stdout.

  yt-dlp ... | ffmpeg ... -f f32le pipe:1 | python -m helper.asr.worker --language auto --hint en

--language — the code pinned by the owner, or auto; --hint — the language label from
YouTube; the detector listens to the recording in any case, the decision — helper.language.decide.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence

import numpy as np

import helper.env  # noqa: F401  — steers the weights cache off the ramdisk, mandatory before faster_whisper

from helper.asr.prompt import initial_prompt

# Named lang, not language: the transcribe parameters are named language and hint
# and would shadow inside it both the module itself and its function hint.
from helper import language as lang

# The definition is single, in helper.language; jobs.py and spikes/asr_real.py take it from here.
SAMPLE_RATE = lang.SAMPLE_RATE

# large-v3, not large-v3-turbo. Turbo was evaluated only on languages where large-v3
# shows WER ≤20%; Serbian sits at 16–29%, i.e. it almost certainly wasn't part of the
# evaluation, and turbo degrades most strongly precisely on low-resource languages.
# We work in batch, not in real time — there's nothing to save seconds on.
DEFAULT_MODEL = "large-v3"


def _speech_seconds(audio: np.ndarray) -> float:
    """Seconds of speech in the excerpt.

    The same VAD with the same defaults as detect_language(vad_filter=True) — exactly
    what the detector will hear.
    """
    # The import here, not at the top: jobs.py imports this module for SAMPLE_RATE, and
    # the daemon process doesn't need faster_whisper. As a bonus, the substitution of
    # get_speech_timestamps in tests is visible: the attribute is read on every call.
    from faster_whisper.vad import get_speech_timestamps

    chunks = get_speech_timestamps(audio)

    return sum(chunk["end"] - chunk["start"] for chunk in chunks) / SAMPLE_RATE


def _window(model, audio: np.ndarray, speech: float) -> tuple[float, list[tuple[str, float]]]:
    """One record for lang.summarize: seconds of speech and the detector's distribution.

    Speech less than lang.MIN_SPEECH_SECONDS — the distribution is empty, the model isn't
    called on such an excerpt. A crashed detector — also empty: the window simply doesn't vote.
    """
    if speech < lang.MIN_SPEECH_SECONDS:
        return speech, []

    try:
        _, _, probs = model.detect_language(audio=audio, vad_filter=True)
    except Exception:  # noqa: BLE001 — one window must not bring down recognition
        return speech, []

    return speech, list(probs)


def _detect(model, audio: np.ndarray) -> lang.Detection:
    """The speech language by windows laid out along the recording — by the rule of lang.summarize.

    The model arrives already loaded: detection and recognition use the same one, and
    loading it twice means paying extra seconds and extra memory.

    We lay out the windows ourselves (lang.probe_offsets). The standard
    language_detection_segments is "the first N×30 seconds of speech in a row", not N
    probes across the video, and on top of that the 0.5 threshold cuts the search off at
    the very first window. A window with less speech than lang.MIN_SPEECH_SECONDS isn't
    given to the model: on padding it would answer anyway, and silence would vote on par
    with speech. A window where the detector crashed doesn't vote.

    If not a single window voted, and the recording is longer than a window — one pass
    over the first thirty seconds of speech of the whole file: the speech could have
    ended up between the windows. It goes as the last record in windows (on a video
    longer than 37.5 s — the ninth), and the model is called on it only if there's at
    least lang.MIN_SPEECH_SECONDS of speech in the file.
    """
    records = []

    for offset in lang.probe_offsets(audio.size):
        chunk = audio[offset : offset + lang.WINDOW_SAMPLES]
        records.append(_window(model, chunk, _speech_seconds(chunk)))

    detection = lang.summarize(records)

    if detection.votes or audio.size <= lang.WINDOW_SAMPLES:
        return detection

    # detect_language listens to the first thirty seconds of speech: that's how much we record.
    records.append(_window(model, audio, min(30.0, _speech_seconds(audio))))

    return lang.summarize(records)


def _cue(segment) -> dict:
    """A Whisper segment into a response utterance: text, words and metrics by which the daemon judges."""
    return {
        "start": segment.start,
        "end": segment.end,
        "text": segment.text.strip(),
        "words": [
            {"start": w.start, "end": w.end, "word": w.word}
            for w in (segment.words or [])
        ],
        "no_speech_prob": segment.no_speech_prob,
        "avg_logprob": segment.avg_logprob,
        "compression_ratio": segment.compression_ratio,
    }


def transcribe(
    audio: np.ndarray,
    *,
    model_name: str = DEFAULT_MODEL,
    compute_type: str = "float16",
    language: str = "sr",
    hint: str | None = None,
    prompt_names: Sequence[str] | None = None,
    hotwords: str | None = None,
    batch_size: int = 16,
    # Only disabled for measuring memory usage: with the filter, silence is screened out
    # before the encoder, and the peak would come out lowered. In operation it's always
    # on — musical intros and pauses are the main source of hallucinations.
    vad_filter: bool = True,
) -> dict:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    # Before loading the weights: an invalid code — UnknownLanguage, auto — None.
    pin = lang.normalize(language)
    model = WhisperModel(model_name, device="cuda", compute_type=compute_type)
    # The detector always listens, even with a pinned language: then it checks the pin,
    # and the discrepancy goes to the daemon in the decision.
    detection = _detect(model, audio)
    verdict = lang.decide(pin, lang.hint(hint), detection)
    # The seed — by the decided language, not the declared one: Serbian speech under an
    # en label (Juske) needs the Serbian phrase, English speech needs none. None — there
    # was no flag.
    prompt = initial_prompt(verdict.effective, prompt_names) if prompt_names is not None else None
    pipeline = BatchedInferencePipeline(model=model)

    segments, info = pipeline.transcribe(
        audio,
        # The language was decided above and is never empty or None. The library's own
        # detection listens only to the first thirty seconds of speech, and there's the
        # intro; bs/hr/sr are three tokens of one language for it, with arbitrary choice.
        language=verdict.effective,
        # NOT 'translate': on that task Whisper produces only English and would silently
        # substitute the source language for the whole pipeline below.
        task="transcribe",
        beam_size=5,
        # Thresholds are not passed: the batched mode accepts and doesn't apply them. The
        # daemon screens silence as a whole segment by its metrics
        # (helper.asr.prompt.segment_verdict), and in a segment with a "repeat" — only the
        # looping sentences (helper.jobs.asr_cues); every discard is visible in the log.
        word_timestamps=True,
        vad_filter=vad_filter,
        vad_parameters=dict(
            threshold=0.5,
            min_speech_duration_ms=250,
            # The default 2000 ms glue across natural utterance boundaries, and 400 ms of
            # padding smears the starts. For subtitles it needs to be tighter.
            min_silence_duration_ms=500,
            # Serbian is highly inflective: a clipped ending changes the case, and the
            # translator then confidently produces a smooth but incorrect phrase.
            speech_pad_ms=200,
        ),
        initial_prompt=prompt,
        hotwords=hotwords,
        batch_size=batch_size,
    )

    cues = [_cue(segment) for segment in segments]

    return {
        # The language with which recognition was done: the daemon latinizes the utterances by it.
        "language": verdict.effective,
        "verdict": verdict.wire(),
        # Detector windows — for the daemon's log and for tuning thresholds by it.
        "detection": {
            "bcmsShare": round(detection.bcms_share, 2),
            "votes": detection.votes,
            "probed": detection.probed,
            # Seconds of speech — rounded down: a window shown with MIN_SPEECH_SECONDS or more went to the model.
            "windows": [
                [code, round(share, 2), math.floor(speech * 10) / 10]
                for code, share, speech in detection.windows
            ],
        },
        "duration": info.duration,
        "model": model_name,
        "prompt": prompt,
        "cues": [c for c in cues if c["text"]],
    }


def _prompt_names(raw: str) -> list[str]:
    """A JSON list of names from the command line; anything else is a parse error."""
    names = json.loads(raw)

    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise argparse.ArgumentTypeError("a JSON list of strings is required")

    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--compute-type", default="float16")
    # The code pinned by the owner, or auto. helper/measure.py relies on the sr default,
    # which doesn't pass the language.
    parser.add_argument("--language", default="sr")
    parser.add_argument("--hint", default=None, help="language label from YouTube, a hint only")
    parser.add_argument(
        "--prompt-names",
        type=_prompt_names,
        default=None,
        help="JSON list of names from the title and tags; the worker will compose the seed by the decided language",
    )
    parser.add_argument("--hotwords", default=None)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="for memory measurement only: without VAD silence reaches the encoder",
    )
    parser.add_argument("--pcm", help="file with f32le instead of stdin (for debugging)")

    return parser


def main() -> int:
    args = build_parser().parse_args()

    # A refusal costs exactly an argument parse: no sound, no weights. Code 2 and
    # "permanent" — the daemon makes a final refusal out of this (JobRunner._run_asr).
    try:
        lang.normalize(args.language)
    except lang.UnknownLanguage as exc:
        print(json.dumps({"error": lang.refusal(exc.raw), "permanent": True}, ensure_ascii=False))
        return 2

    raw = open(args.pcm, "rb").read() if args.pcm else sys.stdin.buffer.read()

    if not raw:
        print(json.dumps({"error": "no audio came on input"}), file=sys.stdout)
        return 1

    audio = np.frombuffer(raw, dtype="<f4")

    if audio.size < SAMPLE_RATE // 2:
        print(json.dumps({"error": "audio is shorter than half a second"}), file=sys.stdout)
        return 1

    result = transcribe(
        audio,
        model_name=args.model,
        compute_type=args.compute_type,
        language=args.language,
        hint=args.hint,
        prompt_names=args.prompt_names,
        hotwords=args.hotwords,
        batch_size=args.batch_size,
        vad_filter=not args.no_vad,
    )

    json.dump(result, sys.stdout, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
