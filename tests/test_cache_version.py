"""A version bump invalidates tracks made by the previous language rules."""

from conftest import metadata
from helper import cache
from helper.jobs import Job, Stage


async def test_a_track_made_before_the_update_is_made_again(pipeline, monkeypatch):
    pipeline.info = metadata(video_id="beforebump1")

    # A record under version 1 — the language rules before the version bump.
    with monkeypatch.context() as old_rules:
        old_rules.setattr(cache, "PROMPT_VERSION", 1)
        await pipeline.run(Job("beforebump1"))

    job = Job("beforebump1")
    await pipeline.run(job)

    assert job.stage is Stage.DONE
    # Recognised anew rather than served from the cache.
    assert len(pipeline.workers) == 2
    # The previous record is not deleted: deleting it is the owner's decision.
    assert len(pipeline.cache.variants("beforebump1")) == 2
