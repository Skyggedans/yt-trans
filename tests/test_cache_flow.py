"""Ключ кэша несёт закрепление человека, запись — решение о языке, пустая дорожка не кэшируется."""

import hashlib
import json
from dataclasses import fields

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import cache as cache_module
from helper import jobs
from helper.cache import CacheKey, TrackCache
from helper.config import AsrConfig, Config, TranslatorConfig
from helper.jobs import Stage
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack, YtDlpError

# Ролик тестов конвейера: кэш фикстуры pipeline свой у каждого теста. Тесты
# восстановления пишут в общий на прогон manager.cache, и у каждого свой ролик.
VIDEO = "cacheflow01"

# 3XJLfTSZy6I: сербская речь без метаданных, английский сверху в пяти окнах из восьми,
# доля сербского семейства 0.44 — решил порог BCMS_FLOOR.
FLOOR = LanguageVerdict(
    effective="sr", declared=None, declared_by=None,
    detected="sr", confidence=0.44, reason="floor",
)
# Juske, Q2QcYOBk0Jo: YouTube пометил en, речь сербская.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# Человек закрепил sr в [asr] language.
PINNED = LanguageVerdict(
    effective="sr", declared="sr", declared_by="config",
    detected="sr", confidence=0.97, reason="pin",
)
# Ни одно окно не голосовало, подсказки нет.
SILENT = LanguageVerdict(
    effective="sr", declared=None, declared_by=None,
    detected=None, confidence=None, reason="fallback",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
# Готовые реплики в записи — так их кладёт _finish_translation (JobCue.payload()).
STORED = [
    {
        "i": 0, "start": 0.0, "end": 1.5, "src": "Dobar dan.", "lines": ["Добрый день."],
        "study": "Dobar dan.", "failed": False,
    },
]
# Значение для rewrite: поля в записи нет вовсе.
ABSENT = object()


def answer(verdict, cues=CUES):
    """Ответ воркера по протоколу задачи 5."""
    return {
        "language": verdict.effective,
        "verdict": verdict.wire(),
        "detection": {"bcmsShare": 0.0, "votes": 0, "probed": 0, "windows": []},
        "duration": 2.0,
        "model": "large-v3",
        "cues": cues,
    }


def key_of(stored):
    """Ключ записи по её же полям: тест попадает ровно туда, куда писал конвейер."""
    return CacheKey(
        video_id=stored["video_id"], pin=stored["pin"], asr_model=stored["asr_model"],
        translator_model=stored["translator_model"], study_lang=stored["study_lang"],
        track_fingerprint=stored["track_fingerprint"],
    )


def rewrite(store, key, **changes):
    """Поправить запись на диске; поле со значением ABSENT удаляется."""
    path = store._path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))

    for name, value in changes.items():
        if value is ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── ключ ─────────────────────────────────────────────────────────────────


def test_the_key_is_the_pin_recipe_without_the_target_language():
    key = CacheKey(
        video_id=VIDEO, pin="sr", asr_model="large-v3/float16",
        translator_model="qwen3.8:27b", study_lang="cnr", track_fingerprint="manual:sr",
    )
    raw = "|".join([
        VIDEO, "manual:sr", "sr", "large-v3/float16", "qwen3.8:27b", "cnr",
        str(cache_module.PROMPT_VERSION), str(cache_module.SCHEMA_VERSION),
    ])

    assert [field.name for field in fields(CacheKey)] == [
        "video_id", "pin", "asr_model", "translator_model", "study_lang", "track_fingerprint",
    ]
    assert key.digest() == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert cache_module.SCHEMA_VERSION == 2


def test_the_pipeline_and_restore_build_one_key(manager):
    job = jobs.Job(VIDEO)
    job.source = "manual:sr-Latn"
    expected = CacheKey(
        video_id=VIDEO, pin="", asr_model="large-v3/float16",
        translator_model="qwen3.8:27b", study_lang="cnr", track_fingerprint="manual:sr-Latn",
    )

    assert jobs.track_key(manager.config, VIDEO, "manual:sr-Latn", None) == expected
    assert manager.runner._cache_key(job) == expected
    assert manager._key_for(VIDEO, "manual:sr-Latn") == expected


def test_a_pin_goes_into_both_keys(manager, monkeypatch):
    pinned = Config(asr=AsrConfig(language="sr"), translator=TranslatorConfig())
    monkeypatch.setattr(manager, "config", pinned)
    monkeypatch.setattr(manager.runner, "_config", pinned)

    assert manager.runner._cache_key(jobs.Job(VIDEO)).pin == "sr"
    assert manager._key_for(VIDEO, "asr").pin == "sr"


def test_restore_reads_a_broken_pin_as_no_pin(manager, monkeypatch):
    # Отказ по негодному закреплению — дело конвейера (задача 4). Восстановление только
    # читает: ролики, закэшированные без закрепа, по-прежнему открываются — негодный код
    # читается как «не закреплено»; сделанные при прежнем годном закрепе не находятся,
    # пока код не исправлен.
    manager.cache.put(manager._key_for("brokenpin01", "asr"), STORED, 10.0)
    broken = Config(asr=AsrConfig(language="serbian"), translator=TranslatorConfig())
    # Настройка у демона одна на реестр и бегун: подменяются обе.
    monkeypatch.setattr(manager, "config", broken)
    monkeypatch.setattr(manager.runner, "_config", broken)

    assert manager._key_for(VIDEO, "asr").pin == ""
    assert manager.restore("brokenpin01", "ru", True, 10.0).stage is Stage.DONE


# ── запись ───────────────────────────────────────────────────────────────


def test_put_records_the_verdict_and_the_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    store = TrackCache(root=tmp_path / "tracks")
    config = Config(asr=AsrConfig(), translator=TranslatorConfig())
    asr = jobs.track_key(config, VIDEO, "asr", None)
    track = jobs.track_key(config, VIDEO, "manual:sr", None)

    try:
        store.put(asr, STORED, 10.0, language=FLOOR.wire())
        store.put(track, STORED, 10.0)
        payload = store.get(asr, 10.0)

        assert payload["language"] == FLOOR.wire()
        assert payload["pin"] == ""
        assert payload["schema_version"] == 2
        assert "source_lang" not in payload
        assert "target_lang" not in payload
        assert store.get(track, 10.0)["language"] is None
    finally:
        store._db.close()


async def test_a_second_view_hears_the_verdict_before_the_cues(manager, connection, pipeline):
    pipeline.asr_result = answer(JUSKE)
    await pipeline.run(jobs.Job(VIDEO))
    await connection.say(watch(VIDEO))

    assert (await connection.hear())["language"] is None

    job = manager.get(VIDEO, True)
    await pipeline.run(job)
    heard = await hear_until_done(connection)
    (stored,) = pipeline.cache.variants(VIDEO)

    assert [event["type"] for event in heard] == ["state", "language", "cues", "state"]
    assert heard[1] == {"type": "language", "language": JUSKE.wire()}
    assert heard[-1] == {"type": "state", "stage": "done", "cached": True, "total": 1}
    assert job.snapshot()["language"]["note"] == "YouTube указал en, по речи sr"
    assert len(pipeline.workers) == 1
    assert (stored["pin"], stored["language"]) == ("", JUSKE.wire())


async def test_metadata_does_not_split_the_cache(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    pipeline.info = metadata(audio_language="en")
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 1
    assert again.events[-1] == {"type": "state", "stage": "done", "cached": True, "total": 1}
    assert again.language == FLOOR


async def test_another_target_language_shares_the_cache(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO, "ru"))
    other = jobs.Job(VIDEO, "sr")

    await pipeline.run(other)

    assert len(pipeline.workers) == 1
    assert other.events[-1]["cached"] is True
    assert other.snapshot()["targetLang"] == "sr"


async def test_a_human_pin_is_another_entry(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    pipeline.configure(language="sr")
    pipeline.asr_result = answer(PINNED)
    pinned = jobs.Job(VIDEO)

    await pipeline.run(pinned)

    assert len(pipeline.workers) == 2
    assert pinned.language == PINNED
    assert sorted(stored["pin"] for stored in pipeline.cache.variants(VIDEO)) == ["", "sr"]


async def test_a_result_without_cues_is_not_cached(manager, pipeline):
    pipeline.asr_result = answer(SILENT, cues=[])
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.events[-1] == {"type": "state", "stage": "done", "total": 0, "cached": False}
    assert pipeline.cache.variants(VIDEO) == []
    assert job.snapshot()["language"]["note"] == "речи почти нет, язык принят за sr"


async def test_an_entry_without_cues_is_a_miss_for_the_pipeline_too(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    (stored,) = pipeline.cache.variants(VIDEO)
    key = key_of(stored)
    pipeline.cache.put(key, [], None, language=FLOOR.wire())
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 2
    assert again.events[-1] == {"type": "state", "stage": "done", "total": 1}
    assert len(pipeline.cache.get(key)["cues"]) == 1


async def test_a_cached_entry_without_a_verdict_is_served_with_language_null(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    (stored,) = pipeline.cache.variants(VIDEO)
    rewrite(pipeline.cache, key_of(stored), language=ABSENT)
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 1
    assert again.language is None
    assert again.snapshot()["language"] is None
    assert [event["type"] for event in again.events] == ["state", "cues", "state"]


async def test_a_resumed_job_writes_its_verdict(manager, pipeline, monkeypatch):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = answer(JUSKE)
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED

    await pipeline.run(job)
    (stored,) = pipeline.cache.variants(VIDEO)

    assert job.stage is Stage.DONE
    assert stored["language"] == JUSKE.wire()


async def test_an_author_track_entry_keeps_its_verdict(manager, pipeline):
    pipeline.info = metadata(
        audio_language="en",
        manual_tracks=[CaptionTrack(lang="sr-Latn", url="", ext="json3", name="")],
    )
    pipeline.track = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
    await pipeline.run(jobs.Job(VIDEO))
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert [event["type"] for event in again.events] == ["state", "language", "cues", "state"]
    assert again.source == "manual:sr-Latn"
    assert again.language == LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected=None,
        confidence=None, reason="track", source="manual", track="sr-Latn", verified=None,
    )


async def test_a_failed_author_track_serves_the_cached_asr_verdict_once(manager, pipeline):
    # Сначала в кэш ложится распознавание: без дорожки автора ответ воркера идёт под ключом asr.
    pipeline.asr_result = answer(JUSKE)
    await pipeline.run(jobs.Job(VIDEO))

    pipeline.info = metadata(
        manual_tracks=[CaptionTrack(lang="sr", url="", ext="json3", name="")]
    )
    pipeline.track = YtDlpError("дорожка не скачалась", permanent=True)
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    types = [event["type"] for event in again.events]

    assert again.track_failed is True
    assert again.language == JUSKE
    assert types.count("language") == 1
    assert types.index("language") < types.index("cues")


# ── восстановление ───────────────────────────────────────────────────────


async def test_restore_brings_the_verdict_back_before_the_cues(client, manager):
    video = "restverdict"
    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=JUSKE.wire())

    body = (await client.get(f"/v1/jobs/{video}/restore", params={"duration": "10"})).json()
    restored = manager.get(video, True)

    assert body["found"] is True
    assert body["snapshot"]["language"] == JUSKE.wire()
    assert [event["type"] for event in restored.events] == ["language", "cues", "state"]
    assert restored.events[0] == {"type": "language", "language": JUSKE.wire()}


@pytest.mark.parametrize(
    ("video", "verdict"),
    [("noverdict01", ABSENT), ("noverdict02", None), ("noverdict03", "sr")],
    ids=["absent", "null", "not-an-object"],
)
def test_an_entry_without_a_verdict_restores_with_language_null(manager, video, verdict):
    key = manager._key_for(video, "asr")
    manager.cache.put(key, STORED, 10.0)
    rewrite(manager.cache, key, language=verdict)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored.stage is Stage.DONE
    assert restored.language is None
    assert restored.snapshot()["language"] is None
    assert [event["type"] for event in restored.events] == ["cues", "state"]


def test_restore_follows_the_current_pin(manager, monkeypatch):
    video = "restpinned1"
    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=FLOOR.wire())
    pinned = Config(asr=AsrConfig(language="sr"), translator=TranslatorConfig())
    monkeypatch.setattr(manager, "config", pinned)

    assert manager.restore(video, "ru", True, 10.0) is None

    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=PINNED.wire())

    assert manager.restore(video, "ru", True, 10.0).language == PINNED


def test_restore_never_takes_versions_or_models_from_the_entry(manager, monkeypatch):
    video = "restorphan1"
    other_model = Config(asr=AsrConfig(), translator=TranslatorConfig(model="translategemma:12b"))

    with monkeypatch.context() as patch:
        patch.setattr(cache_module, "PROMPT_VERSION", cache_module.PROMPT_VERSION - 1)
        manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=FLOOR.wire())

    manager.cache.put(
        jobs.track_key(other_model, video, "asr", None), STORED, 10.0, language=FLOOR.wire()
    )

    assert manager.restore(video, "ru", True, 10.0) is None
    # Обе записи на месте: восстановление их не видит, но и не трогает.
    assert len(manager.cache.variants(video)) == 2


def test_restore_prefers_the_newest_author_track(manager):
    video = "restnewest1"
    older = manager._key_for(video, "manual:sr")
    newer = manager._key_for(video, "manual:sr-Latn")
    manager.cache.put(older, [{**STORED[0], "src": "Stara"}], 10.0)
    manager.cache.put(newer, [{**STORED[0], "src": "Nova"}], 10.0)
    rewrite(manager.cache, older, created_at=1000.0)
    rewrite(manager.cache, newer, created_at=2000.0)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored.source == "manual:sr-Latn"
    assert [cue.src for cue in restored.cues] == ["Nova"]


def test_restore_survives_a_string_created_at_on_an_author_track(manager):
    # Правленная руками запись может нести строку вместо времени: max() сравнил бы
    # её с числом и уронил бы TypeError на каждом восстановлении и опросе ролика.
    video = "reststamp01"
    older = manager._key_for(video, "manual:sr")
    newer = manager._key_for(video, "manual:sr-Latn")
    manager.cache.put(older, [{**STORED[0], "src": "Stara"}], 10.0)
    manager.cache.put(newer, [{**STORED[0], "src": "Nova"}], 10.0)
    rewrite(manager.cache, older, created_at="вчера")
    rewrite(manager.cache, newer, created_at=2000.0)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored is not None
    assert restored.source == "manual:sr-Latn"
    assert [cue.src for cue in restored.cues] == ["Nova"]
