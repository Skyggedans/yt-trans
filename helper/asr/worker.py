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


# Окна для определения языка: сколько и в каком диапазоне длительности их брать.
# С начала брать нельзя — там заставка, музыка или реклама спонсора, и язык
# определился бы по ним. Замерено на этом корпусе: у KCR-dkZIhxY первая реплика
# начинается на 21-й секунде, то есть первое тридцатисекундное окно на две трети
# состоит из заставки.
PROBE_WINDOWS = 8
PROBE_SPAN = (0.10, 0.90)
WINDOW_SECONDS = 30

# Языки, между которыми Whisper не различает: bs/hr/sr — это три метки одного языка,
# попавшие в обучение из VoxLingua107 по недосмотру, и выбор между ними произволен.
# Любую из них трактуем как сербский, потому что дальше по конвейеру ждут его.
BCMS = frozenset({"sr", "hr", "bs", "sh", "me", "cnr"})


def _detect(model, audio: np.ndarray) -> tuple[str, float]:
    """Определить язык речи по отрывку из середины записи.

    Модель принимается уже загруженной: определение и распознавание идут одной
    и той же, и грузить её дважды значит платить лишние секунды и лишнюю память.

    Определяем по куску, а не по всей записи — на часовом ролике это разница
    между долями секунды и минутами. Отрывок берётся с 40% длительности:
    начало занято заставкой, музыкой или рекламой спонсора, и язык определился
    бы по ним, а не по речи.
    """
    window = WINDOW_SECONDS * SAMPLE_RATE

    if audio.size <= window:
        language, probability, _ = model.detect_language(audio, vad_filter=True)

        return language, probability

    # Окна раскладываем сами, по всей длине. Штатный language_detection_segments
    # для этого не годится: он означает «первые N×30 секунд подряд от начала»,
    # а не «N проб по ролику», и вдобавок порог 0.5 обрывает перебор на первом же
    # окне — то есть увеличение числа сегментов обычно не меняет вообще ничего.
    low, high = PROBE_SPAN
    span = audio.size * (high - low) - window
    offsets = [
        int(audio.size * low + span * i / max(PROBE_WINDOWS - 1, 1))
        for i in range(PROBE_WINDOWS)
    ]

    totals: dict[str, float] = {}

    for offset in offsets:
        chunk = audio[offset : offset + window]

        if chunk.size < SAMPLE_RATE:
            continue

        try:
            _, _, probs = model.detect_language(chunk, vad_filter=True)
        except Exception:
            # Окно могло целиком оказаться тишиной — VAD вернёт пусто.
            continue

        # Складываем распределения, а не голоса top-1. Жёсткое голосование
        # разорвало бы сербский на три метки bs/hr/sr, и ролик с раскладом
        # 4×hr / 3×sr / 3×bs проиграл бы языку, набравшему меньше суммарно.
        for code, value in probs:
            totals[code] = totals.get(code, 0.0) + value

    if not totals:
        return "", 0.0

    merged = _merge_bcms(totals)
    best = max(merged, key=merged.get)

    return best, merged[best] / max(sum(merged.values()), 1e-9)


def _merge_bcms(totals: dict[str, float]) -> dict[str, float]:
    """Сложить массу bs/hr/sr в одну метку.

    Это три ярлыка одного языка, попавшие в обучение Whisper из VoxLingua107
    по недосмотру: выбор между ними произволен, а разнесённые по трём меткам
    они проигрывают любому конкуренту, набравшему меньше суммарно.
    """
    merged: dict[str, float] = {}

    for code, value in totals.items():
        key = "sr" if code in BCMS else code
        merged[key] = merged.get(key, 0.0) + value

    return merged


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
    detected, confidence = "", 0.0

    if language == "auto":
        detected, confidence = _detect(model, audio)

        # bs/hr/sr — три метки одного языка, и выбор между ними произволен.
        # Приводим к sr: дальше по конвейеру ждут именно его, а разница
        # в тексте сводится к диалектным предпочтениям, а не к языку.
        language = "sr" if detected in BCMS else detected

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
        "language": language,
        # Что показал определитель до приведения bs/hr/sr к одному языку, и
        # насколько уверенно. Нужно выше по конвейеру: по низкой уверенности
        # видно, что ролик, возможно, вообще не про речь.
        "detected": detected or info.language,
        "confidence": confidence,
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
