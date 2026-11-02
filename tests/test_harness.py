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
    # Именно обычный def, не async: настоящий Job.start зовёт asyncio.create_task,
    # и вне цикла событий это падение «no running event loop». От этого контракта
    # зависят все остальные тесты реестра.
    job = manager.get_or_create("abcdefghijk", "ru", True)

    assert job.video_id == "abcdefghijk"
    assert manager.started == ["abcdefghijk"]


def test_manager_records_a_resume_without_launching_it(manager):
    # Тот же контракт для возобновления: настоящий Job.resume тоже создаёт задачу,
    # и без заглушки async-тест на приостановленном задании запустил бы конвейер.
    from helper.jobs import Stage

    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.stage = Stage.PAUSED

    assert manager.get_or_create("abcdefghijk", "ru", True) is job
    # Два вызова, а не два запуска: start при создании и resume при повторном
    # обращении к приостановленному заданию. Так и задумано — см. get_or_create.
    assert manager.started == ["abcdefghijk", "abcdefghijk"]


async def test_client_and_manager_see_one_registry(manager, client):
    # Обработчики читают модульный helper.app.manager. Если фикстура не подменила
    # его тем же объектом, что вернула тесту, созданное запросом задание осталось
    # бы в чужом реестре, а проверка через manager его не нашла.
    response = await client.post("/v1/jobs", json={"videoId": "abcdefghijk"})

    assert response.status_code == 200
    assert response.json()["videoId"] == "abcdefghijk"
    assert manager.get("abcdefghijk") is not None
    assert manager.started == ["abcdefghijk"]


# ── конвейер на подделках ────────────────────────────────────────────────


async def test_pipeline_runs_an_asr_video_to_done(manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    # Настоящий _pipeline: те же стадии, что видит зритель, и ни одного выхода в сеть.
    assert job.stage is Stage.DONE
    assert [event["stage"] for event in job.events if event["type"] == "state"] == [
        "probing", "queued", "fetching", "asr", "translating", "done",
    ]
    assert pipeline.names() == ["probe", "audio", "asr", "translate"]
    # Воркер запущен настоящим _run_asr со звуком задания, его ответ разобран настоящим кодом.
    worker = pipeline.workers[-1]
    assert worker.args[:3] == [sys.executable, "-m", "helper.asr.worker"]
    assert worker.input == pipeline.audio.tobytes()
    assert [(cue.src, cue.lines, cue.study) for cue in job.cues] == [
        ("Dobar dan.", ["ru Dobar dan."], "cnr Dobar dan."),
    ]
    # Дисциплина карты: переводчик выгружен до пропуска под Whisper, фазы берут его по очереди.
    assert [call[0] for call in pipeline.lease.calls] == [
        "claim", "unload", "acquire", "release", "acquire", "release",
    ]
    # Дорожка легла в настоящий кэш теста и читается с диска.
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
    # Без распознавания карта нужна только переводчику.
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "acquire", "release"]
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["manual:sr"]
    assert [cue.src for cue in job.cues] == ["Zdravo svima."]


async def test_a_resumed_asr_keeps_its_transcript_when_the_paused_audio_stays(
    manager, pipeline, audio_dir, refuse_in_audio_dir, caplog
):
    job = manager.get_or_create(VIDEO, "ru", True)
    # Пауза после загрузки звука: метаданные есть, звук отложен на диск, реплик нет.
    job.info = metadata()
    job.stage = Stage.PAUSED
    job.stash_audio(pipeline.audio)
    path = job._audio_path
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")

    await pipeline.run(job)

    # Возобновление взяло отложенный звук, а не скачало его заново.
    assert pipeline.names() == ["asr", "translate"]
    assert pipeline.workers[-1].input == pipeline.audio.tobytes()
    # Звук не стёрся, но распознанное не выброшено: задание дошло до конца и до кэша.
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

    # Воркер запущен и держит карту, пока его не отпустят; сторож видит пользователя.
    assert await asyncio.to_thread(pipeline.worker_started.wait, 5)
    pipeline.lease.holders[-1].preempt()
    await asyncio.wait_for(running, 5)

    assert pipeline.workers[-1].killed
    assert job.stage is Stage.PAUSED
    assert job.cues == []
    assert job._audio_path.exists()
    assert pipeline.cache.puts == []
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "unload", "acquire", "release"]


# ── воркер на подделке faster_whisper ────────────────────────────────────


def test_whisper_fixture_runs_the_real_worker_transcribe(whisper):
    from helper.asr import worker

    whisper.segments = [whisper.segment(0.0, 1.5, " Dobar dan. ")]

    result = worker.transcribe(np.zeros(5 * worker.SAMPLE_RATE, dtype="<f4"), language="sr")

    # Подменены только модель и пакетный распознаватель; всё вокруг них — настоящий transcribe.
    assert (whisper.model.name, whisper.model.device, whisper.model.compute_type) == (
        "large-v3", "cuda", "float16",
    )
    assert (whisper.kwargs["language"], whisper.kwargs["task"]) == ("sr", "transcribe")
    assert (result["language"], result["model"], result["duration"]) == ("sr", "large-v3", 5.0)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in result["cues"]] == [
        (0.0, 1.5, "Dobar dan."),
    ]
