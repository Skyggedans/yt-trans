"""Spike 3: how often Serbian videos have a manual subtitle track at all.

This is the hit rate of tier T0 and the answer to whether it is worth building. There is no
public statistics on subtitle availability by language — it must be measured on one's own
corpus, not looked up.

It also checks the documentary claim on which the whole plan rests: BCMS videos
never have auto-captions, because Serbian, Croatian and Bosnian are not in YouTube's
recognition language list. If the `auto` column suddenly turns out non-empty — the plan
must be revised, not celebrated.

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
        raise YtDlpError((proc.stderr or "").strip() or f"could not read {url}")

    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", action="append", default=[], help="can be repeated")
    parser.add_argument("--ids", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=25, help="videos from the channel")
    parser.add_argument("--out", type=Path, default=Path("spikes/caption_prevalence.json"))
    args = parser.parse_args()

    video_ids = list(args.ids)

    for channel in args.channel:
        print(f"listing {channel} …")
        try:
            found = channel_video_ids(channel, args.limit)
        except YtDlpError as exc:
            print(f"  error: {exc}")
            continue

        print(f"  {len(found)} videos")
        video_ids += found

    if not video_ids:
        parser.error("need --channel and/or --ids")

    rows, langs, errors = [], Counter(), 0
    print(f"\n{'video':13s} {'BCMS':5s} {'manual':28s} {'auto':5s} {'dur':>6s}  language")

    for video_id in video_ids:
        try:
            info = probe(video_id)
        except YtDlpError as exc:
            errors += 1
            print(f"{video_id:13s} ERROR {str(exc).splitlines()[0][:60]}")
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
            f"{info.video_id:13s} {'YES' if track else '—':5s} {manual[:28]:28s} "
            f"{'YES' if info.has_automatic_captions else '—':5s} "
            f"{int(info.duration or 0):6d}  {info.audio_language or '?'}"
        )

    args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(rows)
    bcms = sum(r["bcms_manual"] for r in rows)
    auto = sum(r["has_automatic"] for r in rows)

    print(f"\n{'—' * 60}")
    print(f"videos queried:            {total}   (errors: {errors})")
    print(f"manual BCMS track (tier T0): {bcms}  = {bcms / total:.1%}" if total else "")
    print(f"has auto-captions:            {auto}  = {auto / total:.1%}" if total else "")

    if auto:
        print("\n  WARNING: auto-captions found. The plan assumes that for BCMS")
        print("  they do not exist. Check languageCode — a wrong automatic detection")
        print("  of the language on YouTube's side is likely (mk/bg/ru — neighbours of Serbian and ASR languages).")

    if langs:
        print(f"\nmanual track languages: {dict(langs.most_common())}")

    print(f"\nwritten to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
