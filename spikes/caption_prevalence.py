"""Спайк 3: как часто у сербских роликов вообще есть ручная дорожка субтитров.

Это hit-rate тира T0 и ответ на вопрос, стоит ли его строить. Публичной статистики
по доступности субтитров в разрезе языков не существует — её надо мерить на своём
корпусе, а не искать.

Заодно проверяет документальное утверждение, на котором стоит весь план: у BCMS-роликов
не бывает автосубтитров, потому что сербского, хорватского и боснийского нет в списке
языков распознавания YouTube. Если в колонке `auto` вдруг окажется непусто — план
надо пересматривать, а не радоваться.

  python spikes/caption_prevalence.py --channel https://www.youtube.com/@somechannel
  python spikes/caption_prevalence.py --ids dQw4w9WgXcQ jNQXAC9IVRw
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helper.sources.ytdlp import YtDlpError, probe, usable_manual_track, _ytdlp_cmd


def channel_video_ids(url: str, limit: int) -> list[str]:
    if not url.rstrip("/").endswith(("/videos", "/streams", "/shorts")):
        url = url.rstrip("/") + "/videos"

    proc = subprocess.run(
        _ytdlp_cmd("--flat-playlist", "--print", "id", "-I", f":{limit}", url),
        capture_output=True,
        text=True,
        timeout=180,
    )

    if proc.returncode != 0:
        raise YtDlpError((proc.stderr or "").strip() or f"не удалось прочитать {url}")

    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", action="append", default=[], help="можно повторять")
    parser.add_argument("--ids", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=25, help="роликов с канала")
    parser.add_argument("--out", type=Path, default=Path("spikes/caption_prevalence.json"))
    args = parser.parse_args()

    video_ids = list(args.ids)

    for channel in args.channel:
        print(f"перечисляю {channel} …")
        try:
            found = channel_video_ids(channel, args.limit)
        except YtDlpError as exc:
            print(f"  ошибка: {exc}")
            continue

        print(f"  {len(found)} роликов")
        video_ids += found

    if not video_ids:
        parser.error("нужен --channel и/или --ids")

    rows, langs, errors = [], Counter(), 0
    print(f"\n{'video':13s} {'BCMS':5s} {'ручные':28s} {'auto':5s} {'дл':>6s}  язык")

    for video_id in video_ids:
        try:
            info = probe(video_id)
        except YtDlpError as exc:
            errors += 1
            print(f"{video_id:13s} ОШИБКА {str(exc).splitlines()[0][:60]}")
            continue

        track = usable_manual_track(info)
        manual = ",".join(sorted({t.lang for t in info.manual_tracks})) or "—"
        langs.update(t.lang for t in info.manual_tracks)

        rows.append(
            {
                "video_id": info.video_id,
                "title": info.title,
                "bcms_manual": bool(track),
                "manual_langs": sorted({t.lang for t in info.manual_tracks}),
                "has_automatic": info.has_automatic_captions,
                "duration": info.duration,
                "audio_language": info.audio_language,
                "live_status": info.live_status,
            }
        )

        print(
            f"{info.video_id:13s} {'ДА' if track else '—':5s} {manual[:28]:28s} "
            f"{'ДА' if info.has_automatic_captions else '—':5s} "
            f"{int(info.duration or 0):6d}  {info.audio_language or '?'}"
        )

    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(rows)
    bcms = sum(r["bcms_manual"] for r in rows)
    auto = sum(r["has_automatic"] for r in rows)

    print(f"\n{'—' * 60}")
    print(f"роликов опрошено:            {total}   (ошибок: {errors})")
    print(f"ручная BCMS-дорожка (тир T0): {bcms}  = {bcms / total:.1%}" if total else "")
    print(f"есть автосубтитры:            {auto}  = {auto / total:.1%}" if total else "")

    if auto:
        print("\n  ВНИМАНИЕ: автосубтитры найдены. План исходит из того, что для BCMS")
        print("  их не существует. Проверь languageCode — вероятна ошибочная автодетекция")
        print("  языка на стороне YouTube (mk/bg/ru — соседи сербского и ASR-языки).")

    if langs:
        print(f"\nязыки ручных дорожек: {dict(langs.most_common())}")

    print(f"\nзаписано в {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
