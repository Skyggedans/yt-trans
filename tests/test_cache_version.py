"""Обновление обесценивает дорожки, сделанные прежними правилами языка."""

from conftest import metadata
from helper import cache
from helper.jobs import Job, Stage


async def test_a_track_made_before_the_update_is_made_again(pipeline, monkeypatch):
    pipeline.info = metadata(video_id="beforebump1")

    # Запись под версией 1 — правила языка до поднятия версии.
    with monkeypatch.context() as old_rules:
        old_rules.setattr(cache, "PROMPT_VERSION", 1)
        await pipeline.run(Job("beforebump1"))

    job = Job("beforebump1")
    await pipeline.run(job)

    assert job.stage is Stage.DONE
    # Распознано заново, а не отдано из кэша.
    assert len(pipeline.workers) == 2
    # Прежняя запись не удалена: удалять её — решение хозяина.
    assert len(pipeline.cache.variants("beforebump1")) == 2
