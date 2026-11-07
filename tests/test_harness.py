import asyncio
import logging
import sys
import threading
from pathlib import Path

import numpy as np

from conftest import metadata
from helper.jobs import Stage

VIDEO = "abcdefghijk"


def test_cache_paths_are_isolated():
    from helper import env

    assert Path.home() not in env.CACHE_DIR.parents
    assert Path.home() not in env.CONFIG_DIR.parents


def test_manager_fixture_builds(manager):
    assert manager.stats()["jobs"] == 0


def test_manager_registers_a_job_without_launching_the_pipeline(manager):
    # A plain def, not async: the real Job.start calls asyncio.create_task,
    # and outside the event loop that is the "no running event loop" crash. All the other
    # registry tests depend on this contract.
    job = manager.get_or_create("abcdefghijk", "ru", True)

    assert job.video_id == "abcdefghijk"
    assert manager.started == ["abcdefghijk"]


def test_manager_records_a_resume_without_launching_it(manager):
    # The same contract for resuming: the real Job.resume also creates a task,
    # and without the stub an async test on a paused job would start the pipeline.
    from helper.jobs import Stage

    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.stage = Stage.PAUSED

    assert manager.get_or_create("abcdefghijk", "ru", True) is job
    # Two calls, not two starts: start on creation and resume on the second
    # touch of the paused job. That is by design — see get_or_create.
    assert manager.started == ["abcdefghijk", "abcdefghijk"]


async def test_client_and_manager_see_one_registry(manager, client):
    # The handlers read the module-level helper.app.manager. If the fixture had not replaced
    # it with the same object it returned to the test, the job created by the request would stay
    # in a foreign registry, and the check through manager would not find it.
    response = await client.post("/v1/jobs", json={"videoId": "abcdefghijk"})

    assert response.status_code == 200
    assert response.json()["videoId"] == "abcdefghijk"
    assert manager.get("abcdefghijk") is not None
    assert manager.started == ["abcdefghijk"]


# ── the pipeline on fakes ────────────────────────────────────────────────


async def test_pipeline_runs_an_asr_video_to_done(manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    # The real _pipeline: the same stages the viewer sees, and not a single network call.
    assert job.stage is Stage.DONE
    assert [event["stage"] for event in job.events if event["type"] == "state"] == [
        "probing", "queued", "fetching", "asr", "translating", "done",
    ]
    assert pipeline.names() == ["probe", "audio", "asr", "translate"]
    # The worker is started by the real _run_asr with the job's audio, and its reply is parsed by the real code.
    worker = pipeline.workers[-1]
    assert worker.args[:3] == [sys.executable, "-m", "helper.asr.worker"]
    assert worker.input == pipeline.audio.tobytes()
    assert [(cue.src, cue.lines, cue.study) for cue in job.cues] == [
        ("Dobar dan.", ["ru Dobar dan."], "cnr Dobar dan."),
    ]
    # Graphics card discipline: the translator is unloaded before the slot for Whisper, and the phases take it in turn.
    assert [call[0] for call in pipeline.lease.calls] == [
        "claim", "unload", "acquire", "release", "acquire", "release",
    ]
    # The track landed in the test's real cache and is read from disk.
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["asr"]
    assert [cue["src"] for cue in pipeline.cache.variants(VIDEO)[0]["cues"]] == ["Dobar dan."]


async def test_pipeline_takes_a_serbian_author_track_without_asr(manager, pipeline):
    pipeline.info = metadata(manual_tracks=["sr"])
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.source == "manual:sr"
    assert pipeline.calls[:2] == [("probe", VIDEO), ("track", VIDEO, "sr")]
    assert pipeline.names() == ["probe", "track", "translate"]
    assert pipeline.workers == []
    # Without recognition the graphics card is needed only by the translator.
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "acquire", "release"]
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["manual:sr"]
    assert [cue.src for cue in job.cues] == ["Zdravo svima."]


async def test_a_resumed_asr_keeps_its_transcript_when_the_paused_audio_stays(
    manager, pipeline, audio_dir, refuse_in_audio_dir, caplog
):
    job = manager.get_or_create(VIDEO, "ru", True)
    # A pause after the audio download: metadata is there, the audio is stashed on disk, there are no cues.
    job.info = metadata()
    job.stage = Stage.PAUSED
    job.stash_audio(pipeline.audio)
    path = job._audio_path
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")

    await pipeline.run(job)

    # The resume took the stashed audio instead of downloading it again.
    assert pipeline.names() == ["asr", "translate"]
    assert pipeline.workers[-1].input == pipeline.audio.tobytes()
    # The audio was not erased, but the recognised text was not thrown away: the job reached the end and the cache.
    assert job.stage is Stage.DONE
    assert [cue.src for cue in job.cues] == ["Dobar dan."]
    assert len(pipeline.cache.puts) == 1
    assert job._audio_path is None
    assert path.exists()
    assert any(
        str(path) in record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    )


async def test_preemption_during_asr_kills_the_worker_and_keeps_the_audio(
    manager, pipeline, audio_dir
):
    pipeline.gate = threading.Event()
    job = manager.get_or_create(VIDEO, "ru", True)
    running = asyncio.create_task(pipeline.run(job))

    # The worker is running and holds the card until it is released; the watchdog sees the user.
    assert await asyncio.to_thread(pipeline.worker_started.wait, 5)
    pipeline.lease.holders[-1].preempt()
    await asyncio.wait_for(running, 5)

    assert pipeline.workers[-1].killed
    assert job.stage is Stage.PAUSED
    assert job.cues == []
    assert job._audio_path.exists()
    assert pipeline.cache.puts == []
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "unload", "acquire", "release"]


# ── the worker on a fake faster_whisper ──────────────────────────────────


def test_whisper_fixture_runs_the_real_worker_transcribe(whisper):
    from helper.asr import worker

    whisper.segments = [whisper.segment(0.0, 1.5, " Dobar dan. ")]

    result = worker.transcribe(np.zeros(5 * worker.SAMPLE_RATE, dtype="<f4"), language="sr")

    # Only the model and the batch recogniser are replaced; everything around them is the real transcribe.
    assert (whisper.model.name, whisper.model.device, whisper.model.compute_type) == (
        "large-v3", "cuda", "float16",
    )
    assert (whisper.kwargs["language"], whisper.kwargs["task"]) == ("sr", "transcribe")
    assert (result["language"], result["model"], result["duration"]) == ("sr", "large-v3", 5.0)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in result["cues"]] == [
        (0.0, 1.5, "Dobar dan."),
    ]
