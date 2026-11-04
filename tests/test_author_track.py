"""Дорожка автора: какая метка годится и в какой письменности её текст идёт дальше."""

import pytest

from conftest import metadata
from helper.jobs import Job, Stage
from helper.script import normalize_cues
from helper.sources.ytdlp import CaptionTrack, usable_manual_track

# yt-dlp отдаёт каждую дорожку семью форматами, и probe кладёт по записи на каждый.
FORMATS = ("json3", "srv1", "srv2", "srv3", "ttml", "srt", "vtt")

CYRILLIC = [
    {"start": 1.0, "end": 3.0, "text": "Добар дан, људи.",
     "words": [{"start": 1.0, "end": 1.6, "word": "Добар"}]},
]


def tracks(*labels: str) -> list[CaptionTrack]:
    return [
        CaptionTrack(lang=label, url="", ext=ext, name="")
        for label in labels
        for ext in FORMATS
    ]


@pytest.mark.parametrize("label", ["sr", "sr-Latn", "sr-Cyrl", "SR", "sr_Latn", "sh", "hr", "cnr"])
def test_any_spelling_of_the_serbian_family_is_an_author_track(label):
    track = usable_manual_track(metadata(manual_tracks=tracks(label)))

    assert track is not None
    # В yt-dlp метка уходит как есть: --sub-langs ищет ровно этот ключ.
    assert track.lang == label


@pytest.mark.parametrize("label", ["en", "en-GB", "mk", "live_chat", "und"])
def test_other_languages_and_live_chat_are_never_author_tracks(label):
    assert usable_manual_track(metadata(manual_tracks=tracks(label))) is None


def test_seven_formats_are_one_track_and_the_first_serbian_label_wins():
    info = metadata(manual_tracks=[
        *tracks("en"),
        *tracks("sr-Latn"),
        *tracks("hr"),
        CaptionTrack(lang="live_chat", url="", ext="json", name=""),
    ])

    track = usable_manual_track(info)

    assert (track.lang, track.ext) == ("sr-Latn", "json3")


def test_youtube_metadata_never_refuses_a_serbian_track():
    # Случай Juske: YouTube называет речь английской, а она сербская.
    info = metadata(audio_language="en", manual_tracks=tracks("sr"))

    assert usable_manual_track(info).lang == "sr"


@pytest.mark.parametrize("pin", ["sr", "hr", "bs"])
def test_a_serbian_family_pin_keeps_the_track(pin):
    assert usable_manual_track(metadata(manual_tracks=tracks("sr")), pin).lang == "sr"


@pytest.mark.parametrize("pin", ["en", "ru", "mk"])
def test_a_pin_outside_the_serbian_family_refuses_the_track(pin):
    # Хозяин сказал, что речь не сербская: сербская дорожка у такого ролика — перевод.
    assert usable_manual_track(metadata(manual_tracks=tracks("sr")), pin) is None


@pytest.mark.parametrize("label", ["sr", "sr-Cyrl", "SR", "sr_Latn", "cnr"])
def test_cyrillic_cues_are_latinized_under_any_serbian_label(label):
    (cue,) = normalize_cues(CYRILLIC, label)

    assert cue["text"] == "Dobar dan, ljudi."
    assert cue["words"][0]["word"] == "Dobar"


@pytest.mark.parametrize("language", ["ru", "uk", "mk", "en"])
def test_cues_of_other_languages_keep_their_script(language):
    cues = [{"start": 1.0, "end": 3.0, "text": "Привет, как дела?", "words": []}]

    assert normalize_cues(cues, language) == cues


async def test_cyrillic_author_track_goes_on_in_latin(pipeline):
    pipeline.info = metadata(video_id="cyrtrack001", manual_tracks=tracks("sr-Cyrl"))
    pipeline.track = [{"start": 1.0, "end": 3.0, "text": "Добар дан, људи.", "words": []}]
    job = Job("cyrtrack001")

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.source == "manual:sr-Cyrl"
    assert [c.src for c in job.cues] == ["Dobar dan, ljudi."]
    assert (job.language.effective, job.language.track) == ("sr", "sr-Cyrl")


async def test_a_pin_outside_the_serbian_family_sends_the_video_to_asr(pipeline):
    pipeline.configure(language="en")
    pipeline.info = metadata(video_id="pinnedengl1", manual_tracks=tracks("sr"))
    job = Job("pinnedengl1")

    await pipeline.run(job)

    assert job.source == "asr"
    # Дорожку не брали вовсе, а не взяли и уронили.
    assert job.track_failed is False
    assert len(pipeline.workers) == 1
