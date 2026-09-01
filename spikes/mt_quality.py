"""Спайк 2: качество sr→ru — прямой перевод против пивота, латиница против кириллицы.

Материал — настоящие реплики из роликов пользователя, распознанные спайком asr_real.
Это сознательный выбор: FLORES+ закрыт авторизацией, а Tatoeba и FLORES это формальная
проза, тогда как пайплайн увидит спонтанную речь через ASR со всеми её ошибками.
Ранжировать по chrF++ без эталонов нельзя, но три решения замера закрываются иначе.

Плечи:
  direct-lat  прямой перевод, латиница — боевой путь
  direct-cyr  прямой перевод, тот же текст в кириллице
  pivot-lat   sr→en→ru через английский

direct-cyr против direct-lat проверяет догадку, что модель с русским уклоном читает
сербскую кириллицу как сломанный русский и копирует вместо перевода. pivot-lat проверяет,
стоит ли двойная задержка структурного устранения ложных друзей.

Метрики считаются без эталона:
  протечка   в выводе сербская диакритика, сербская кириллица или латинское слово
             целиком со строчной буквы (марки и номера пишутся с прописной)
  без правки вывод посимвольно совпал с источником
  пусто      модель вернула пустую строку
  ru/sr      отношение длин: русский обычно длиннее на 10–15%, сильное отклонение подозрительно

  python spikes/mt_quality.py --n 120
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cyrtranslit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helper.env  # noqa: F401

from helper.translate import (
    DEFAULT_MODEL,
    Batch,
    TranslateError,
    chat,
    cue_schema,
    looks_untranslated,
    translate_batch,
)

BATCH_SIZE = 40

SYSTEM_SR_EN = """You translate Serbian subtitle cues into English.
The source is automatic speech recognition of spontaneous Serbian speech: it may contain
recognition errors, missing diacritics and no punctuation. Infer intent from context.
Translate EACH cue independently. Never merge, split, reorder or drop cues.
Return a JSON object whose keys are exactly c1..cN. Output only the translation."""

SYSTEM_EN_RU = """Ты переводишь субтитры с английского на русский.
Переводи КАЖДУЮ реплику отдельно. Никогда не объединяй, не разбивай, не переставляй \
и не пропускай реплики.
Верни JSON-объект, ключи которого — ровно c1..cN. Только перевод, без пояснений."""


@dataclass
class ArmResult:
    arm: str
    model: str
    leaked: int
    unchanged: int
    empty: int
    ru_sr_ratio: float
    seconds: float
    samples: int


def load_real_cues(path: Path, limit: int) -> list[str]:
    """Сербские реплики из прогона asr_real — уже нормализованные в латиницу."""
    if not path.exists():
        raise SystemExit(
            f"{path} не найден. Сначала: python spikes/asr_real.py --ids <id> --model large-v3"
        )

    videos = json.loads(path.read_text(encoding="utf-8"))
    cues: list[str] = []

    for video in videos:
        cues += [c.strip() for c in video.get("cues_text", []) if c.strip()]

    if not cues:
        raise SystemExit(f"в {path} нет реплик — перезапусти spikes/asr_real.py")

    return cues[:limit]


def _generic_translate(items: list[str], system: str, model: str) -> list[str]:
    """Батч через ту же схему c1..cN, что и в проде, но с другим системным промптом."""
    numbered = "\n".join(f"c{i}: {t}" for i, t in enumerate(items, 1))
    payload = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": numbered}],
        schema=cue_schema(len(items)),
        model=model,
    )
    data = json.loads(payload["message"]["content"])

    return [data.get(f"c{i}", "") for i in range(1, len(items) + 1)]


def run_arm(arm: str, sources: list[str], model: str) -> tuple[list[str], float]:
    started = time.monotonic()
    hyps: list[str] = []

    for start in range(0, len(sources), BATCH_SIZE):
        chunk = sources[start : start + BATCH_SIZE]

        try:
            if arm == "pivot-lat":
                english = _generic_translate(chunk, SYSTEM_SR_EN, model)
                hyps += _generic_translate(english, SYSTEM_EN_RU, model)
            else:
                hyps += translate_batch(Batch(cues=chunk), model=model)
        except (TranslateError, json.JSONDecodeError) as exc:
            print(f"    батч {start // BATCH_SIZE}: {exc}")
            hyps += [""] * len(chunk)

        print(f"    {min(start + BATCH_SIZE, len(sources))}/{len(sources)}")

    return hyps, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=120, help="сколько реплик взять")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--arms", nargs="*", default=["direct-lat", "direct-cyr", "pivot-lat"])
    parser.add_argument("--cues", type=Path, default=Path("spikes/asr_real_v3.json"))
    parser.add_argument("--out", type=Path, default=Path("spikes/mt_quality.json"))
    args = parser.parse_args()

    latin = load_real_cues(args.cues, args.n)
    cyrillic = [cyrtranslit.to_cyrillic(t, "sr") for t in latin]
    print(f"{len(latin)} реальных сербских реплик, {len(args.arms)} плеч\n")

    results, outputs = [], {}

    for arm in args.arms:
        print(f"→ {arm}")
        sources = cyrillic if arm == "direct-cyr" else latin
        hyps, elapsed = run_arm(arm, sources, args.model)
        outputs[arm] = hyps

        src_chars = sum(len(s) for s in sources) or 1
        result = ArmResult(
            arm=arm,
            model=args.model,
            leaked=sum(bool(h.strip()) and looks_untranslated(h) for h in hyps),
            unchanged=sum(h.strip() == s.strip() for h, s in zip(hyps, sources)),
            empty=sum(not h.strip() for h in hyps),
            ru_sr_ratio=sum(len(h) for h in hyps) / src_chars,
            seconds=elapsed,
            samples=len(hyps),
        )
        results.append(result)
        print(
            f"   протечка {result.leaked}/{result.samples}   без правки {result.unchanged}   "
            f"пусто {result.empty}   ru/sr {result.ru_sr_ratio:.2f}   {result.seconds:.1f} с\n"
        )

    args.out.write_text(
        json.dumps(
            {
                "results": [asdict(r) for r in results],
                "source_latin": latin,
                "outputs": outputs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"{'плечо':12s} {'протечка':>9s} {'без правки':>11s} {'пусто':>6s} {'ru/sr':>6s} {'сек':>7s}")

    for r in sorted(results, key=lambda x: (x.leaked, x.seconds)):
        print(
            f"{r.arm:12s} {r.leaked:9d} {r.unchanged:11d} {r.empty:6d} "
            f"{r.ru_sr_ratio:6.2f} {r.seconds:7.1f}"
        )

    print("\n── первые реплики бок о бок ──")

    for i in range(min(5, len(latin))):
        print(f"\n  sr: {latin[i][:78]}")

        for arm in args.arms:
            print(f"  {arm:11s} {outputs[arm][i][:78]}")

    print(f"\nзаписано в {args.out}")
    print("Цифры отсеивают явный брак. Выбор между плечами — глазами по выводу выше.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
