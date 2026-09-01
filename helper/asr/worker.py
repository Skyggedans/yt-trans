"""Распознавание речи как короткоживущий подпроцесс.

Намеренно отдельный процесс, а не функция в демоне. Две причины:

1. CTranslate2 не всегда возвращает VRAM драйверу по `del model`, а на этой карте
   между переводчиком (17.3 ГБ) и десктопом остаётся 1–3 ГБ. Выход процесса —
   единственная гарантия, что память освободилась.
2. Вытеснение становится SIGKILL вместо кооперативного протокола: когда watchdog
   видит, что пользователь начал свою работу на GPU, воркер просто убивается.

PCM приходит на stdin как f32le 16 кГц mono — ровно то, что отдаёт ffmpeg в
sources/ytdlp.py, без промежуточных файлов. JSON с репликами уходит в stdout.

  yt-dlp ... | ffmpeg ... -f f32le pipe:1 | python -m helper.asr.worker --language sr
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

import helper.env  # noqa: F401  — уводит кэш весов с ramdisk, обязателен до faster_whisper

SAMPLE_RATE = 16000

# large-v3, а не large-v3-turbo. Turbo оценивался только на языках, где large-v3
# показывает WER ≤20%; сербский сидит на 16–29%, то есть в оценку почти наверняка
# не входил, а деградирует turbo сильнее всего именно на низкоресурсных языках.
# Мы работаем батчем, а не в реальном времени — секунды экономить не на чем.
DEFAULT_MODEL = "large-v3"


def transcribe(
    audio: np.ndarray,
    *,
    model_name: str = DEFAULT_MODEL,
    compute_type: str = "float16",
    language: str = "sr",
    initial_prompt: str | None = None,
    hotwords: str | None = None,
    batch_size: int = 16,
    # Выключается только для замера расхода памяти: с фильтром тишина отсеивается
    # до энкодера, и пик получился бы заниженным. В работе всегда включён —
    # музыкальные заставки и паузы это главный источник галлюцинаций.
    vad_filter: bool = True,
) -> dict:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    model = WhisperModel(model_name, device="cuda", compute_type=compute_type)
    pipeline = BatchedInferencePipeline(model=model)

    segments, info = pipeline.transcribe(
        audio,
        # Язык фиксируется жёстко. Разделение bs/hr/sr — признанный артефакт импорта
        # меток из VoxLingua107, это один язык с тремя токенами, и автодетект между
        # ними произволен. Плюс он смотрит только первые 30 секунд, а там интро.
        language=language,
        # НЕ 'translate': эта задача у Whisper выдаёт только английский и молча
        # подменила бы язык источника для всего пайплайна ниже.
        task="transcribe",
        beam_size=5,
        # Лестницу оставляем: на языке с WER 16–29% откат по температуре — это то,
        # что спасает схлопнувшиеся сегменты.
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        # Ключевой флаг. Обусловливание на предыдущий текст раскручивает циклы
        # повторов, распространяет флип письменности на весь остаток транскрипта
        # и вытесняет initial_prompt после первого окна.
        condition_on_previous_text=False,
        hallucination_silence_threshold=2.0,
        word_timestamps=True,
        vad_filter=vad_filter,
        vad_parameters=dict(
            threshold=0.5,
            min_speech_duration_ms=250,
            # Дефолтные 2000 мс склеивают через естественные границы реплик,
            # а 400 мс паддинга смазывают начала. Для субтитров нужно плотнее.
            min_silence_duration_ms=500,
            # Сербский сильно флективный: обрезанное окончание меняет падеж,
            # и переводчик потом уверенно выдаёт гладкую, но неверную фразу.
            speech_pad_ms=200,
        ),
        initial_prompt=initial_prompt,
        hotwords=hotwords,
        batch_size=batch_size,
    )

    cues = [
        {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
            "words": [
                {"start": w.start, "end": w.end, "word": w.word}
                for w in (segment.words or [])
            ],
        }
        for segment in segments
    ]

    return {
        "language": info.language,
        "duration": info.duration,
        "model": model_name,
        "cues": [c for c in cues if c["text"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--language", default="sr")
    parser.add_argument("--initial-prompt", default=None)
    parser.add_argument("--hotwords", default=None)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="только для замера памяти: без VAD тишина доходит до энкодера",
    )
    parser.add_argument("--pcm", help="файл с f32le вместо stdin (для отладки)")
    args = parser.parse_args()

    raw = open(args.pcm, "rb").read() if args.pcm else sys.stdin.buffer.read()

    if not raw:
        print(json.dumps({"error": "на вход не пришло аудио"}), file=sys.stdout)
        return 1

    audio = np.frombuffer(raw, dtype="<f4")

    if audio.size < SAMPLE_RATE // 2:
        print(json.dumps({"error": "аудио короче половины секунды"}), file=sys.stdout)
        return 1

    result = transcribe(
        audio,
        model_name=args.model,
        compute_type=args.compute_type,
        language=args.language,
        initial_prompt=args.initial_prompt,
        hotwords=args.hotwords,
        batch_size=args.batch_size,
        vad_filter=not args.no_vad,
    )

    json.dump(result, sys.stdout, ensure_ascii=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
