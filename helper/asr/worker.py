"""Распознавание речи как короткоживущий подпроцесс.

Намеренно отдельный процесс, а не функция в демоне. Две причины:

1. CTranslate2 не всегда возвращает VRAM драйверу по `del model`, а на этой карте
   между переводчиком (17.3 ГБ) и десктопом остаётся 1–3 ГБ. Выход процесса —
   единственная гарантия, что память освободилась.
2. Вытеснение становится SIGKILL вместо кооперативного протокола: когда watchdog
   видит, что пользователь начал свою работу на GPU, воркер просто убивается.

PCM приходит на stdin как f32le 16 кГц mono — ровно то, что отдаёт ffmpeg в
sources/ytdlp.py, без промежуточных файлов. JSON с репликами уходит в stdout.

  yt-dlp ... | ffmpeg ... -f f32le pipe:1 | python -m helper.asr.worker --language auto --hint en

--language — код, закреплённый хозяином, или auto; --hint — метка языка от YouTube;
определитель слушает запись в любом случае, решение — helper.language.decide.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence

import numpy as np

import helper.env  # noqa: F401  — уводит кэш весов с ramdisk, обязателен до faster_whisper

from helper.asr.prompt import initial_prompt

# Под именем lang, а не language: параметры transcribe называются language и hint
# и затенили бы внутри неё и сам модуль, и его функцию hint.
from helper import language as lang

# Определение одно, в helper.language; jobs.py и spikes/asr_real.py берут его отсюда.
SAMPLE_RATE = lang.SAMPLE_RATE

# large-v3, а не large-v3-turbo. Turbo оценивался только на языках, где large-v3
# показывает WER ≤20%; сербский сидит на 16–29%, то есть в оценку почти наверняка
# не входил, а деградирует turbo сильнее всего именно на низкоресурсных языках.
# Мы работаем батчем, а не в реальном времени — секунды экономить не на чем.
DEFAULT_MODEL = "large-v3"


def _speech_seconds(audio: np.ndarray) -> float:
    """Секунды речи в отрывке.

    Тот же VAD с теми же умолчаниями, что у detect_language(vad_filter=True), — ровно
    столько определитель и услышит.
    """
    # Импорт здесь, а не наверху: jobs.py импортирует этот модуль ради SAMPLE_RATE, и
    # faster_whisper процессу демона не нужен. Заодно подмена get_speech_timestamps
    # в тестах видна: атрибут читается при каждом вызове.
    from faster_whisper.vad import get_speech_timestamps

    chunks = get_speech_timestamps(audio)

    return sum(chunk["end"] - chunk["start"] for chunk in chunks) / SAMPLE_RATE


def _window(model, audio: np.ndarray, speech: float) -> tuple[float, list[tuple[str, float]]]:
    """Одна запись для lang.summarize: секунды речи и распределение определителя.

    Речи меньше lang.MIN_SPEECH_SECONDS — распределение пустое, модель на таком отрывке
    не зовётся. Упавший определитель — тоже пустое: окно просто не голосует.
    """
    if speech < lang.MIN_SPEECH_SECONDS:
        return speech, []

    try:
        _, _, probs = model.detect_language(audio=audio, vad_filter=True)
    except Exception:  # noqa: BLE001 — одно окно не должно ронять распознавание
        return speech, []

    return speech, list(probs)


def _detect(model, audio: np.ndarray) -> lang.Detection:
    """Язык речи по окнам, разложенным по записи, — правилом lang.summarize.

    Модель приходит уже загруженной: определение и распознавание идут одной и той же,
    и грузить её дважды значит платить лишние секунды и лишнюю память.

    Окна раскладываем сами (lang.probe_offsets). Штатный language_detection_segments —
    это «первые N×30 секунд речи подряд», а не N проб по ролику, и вдобавок порог 0.5
    обрывает перебор на первом же окне. Окно, где речи меньше lang.MIN_SPEECH_SECONDS,
    модели не отдаётся: на набивке она всё равно ответила бы, и тишина голосовала бы
    наравне с речью. Окно, где определитель упал, не голосует.

    Не проголосовало ни одно окно, а запись длиннее окна — один проход по первым
    тридцати секундам речи всего файла: речь могла оказаться между окнами. Он идёт
    последней записью в windows (на ролике длиннее 37,5 с — девятой), и модель на нём
    зовут, только если речи в файле не меньше lang.MIN_SPEECH_SECONDS.
    """
    records = []

    for offset in lang.probe_offsets(audio.size):
        chunk = audio[offset : offset + lang.WINDOW_SAMPLES]
        records.append(_window(model, chunk, _speech_seconds(chunk)))

    detection = lang.summarize(records)

    if detection.votes or audio.size <= lang.WINDOW_SAMPLES:
        return detection

    # detect_language слушает первые тридцать секунд речи: столько её и записываем.
    records.append(_window(model, audio, min(30.0, _speech_seconds(audio))))

    return lang.summarize(records)


def _cue(segment) -> dict:
    """Сегмент Whisper в реплику ответа: текст, слова и метрики, по которым судит демон."""
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
    # Выключается только для замера расхода памяти: с фильтром тишина отсеивается
    # до энкодера, и пик получился бы заниженным. В работе всегда включён —
    # музыкальные заставки и паузы это главный источник галлюцинаций.
    vad_filter: bool = True,
) -> dict:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    # До загрузки весов: негодный код — UnknownLanguage, auto — None.
    pin = lang.normalize(language)
    model = WhisperModel(model_name, device="cuda", compute_type=compute_type)
    # Определитель слушает всегда, и при закреплённом языке: тогда он проверяет
    # закрепление, и расхождение уходит демону в решении.
    detection = _detect(model, audio)
    verdict = lang.decide(pin, lang.hint(hint), detection)
    # Затравка — по решённому языку, а не по объявленному: сербской речи под меткой en
    # (Juske) нужна сербская фраза, английской речи никакая. None — флага не было.
    prompt = initial_prompt(verdict.effective, prompt_names) if prompt_names is not None else None
    pipeline = BatchedInferencePipeline(model=model)

    segments, info = pipeline.transcribe(
        audio,
        # Язык решён выше и никогда не пустой и не None. Собственное определение
        # библиотеки слушает только первые тридцать секунд речи, а там интро; bs/hr/sr
        # для неё — три токена одного языка с произвольным выбором.
        language=verdict.effective,
        # НЕ 'translate': эта задача у Whisper выдаёт только английский и молча
        # подменила бы язык источника для всего пайплайна ниже.
        task="transcribe",
        beam_size=5,
        # Пороги не передаются: пакетный режим их принимает и не применяет. Тишину демон
        # отсекает целым сегментом по его метрикам (helper.asr.prompt.segment_verdict),
        # а в сегменте с «повтором» — только зациклившиеся предложения
        # (helper.jobs.asr_cues); каждый выброс виден в журнале.
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
        initial_prompt=prompt,
        hotwords=hotwords,
        batch_size=batch_size,
    )

    cues = [_cue(segment) for segment in segments]

    return {
        # Язык, с которым распознавали: демон латинизирует по нему реплики.
        "language": verdict.effective,
        "verdict": verdict.wire(),
        # Окна определителя — для журнала демона и для подбора порогов по нему.
        "detection": {
            "bcmsShare": round(detection.bcms_share, 2),
            "votes": detection.votes,
            "probed": detection.probed,
            # Секунды речи — вниз: окно, показанное с MIN_SPEECH_SECONDS и больше, ушло в модель.
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
    """JSON-список имён из командной строки; всё прочее — ошибка разбора."""
    names = json.loads(raw)

    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise argparse.ArgumentTypeError("нужен JSON-список строк")

    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--compute-type", default="float16")
    # Код, закреплённый хозяином, или auto. На умолчание sr рассчитывает helper/measure.py,
    # который язык не передаёт.
    parser.add_argument("--language", default="sr")
    parser.add_argument("--hint", default=None, help="метка языка от YouTube, только подсказка")
    parser.add_argument(
        "--prompt-names",
        type=_prompt_names,
        default=None,
        help="JSON-список имён из заголовка и тегов; затравку по решённому языку составит воркер",
    )
    parser.add_argument("--hotwords", default=None)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="только для замера памяти: без VAD тишина доходит до энкодера",
    )
    parser.add_argument("--pcm", help="файл с f32le вместо stdin (для отладки)")

    return parser


def main() -> int:
    args = build_parser().parse_args()

    # Отказ стоит ровно разбора аргументов: ни звука, ни весов. Код 2 и "permanent" —
    # демон делает из этого окончательный отказ (JobRunner._run_asr).
    try:
        lang.normalize(args.language)
    except lang.UnknownLanguage as exc:
        print(json.dumps({"error": lang.refusal(exc.raw), "permanent": True}, ensure_ascii=False))
        return 2

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
