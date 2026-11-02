"""Язык на стороне демона: закрепление хозяина, отказ и разговор с воркером."""

import pytest
from conftest import metadata

from helper import jobs, language
from helper.config import AsrConfig, Config, TranslatorConfig

VIDEO = "abcdefghijk"


def runner_with(manager, raw: str) -> jobs.JobRunner:
    config = Config(asr=AsrConfig(language=raw), translator=TranslatorConfig())

    return jobs.JobRunner(manager.lease, manager.cache, config)


@pytest.mark.parametrize(
    ("raw", "pin"),
    [
        ("sr", "sr"),
        ("SR", "sr"),
        ("sr-Latn", "sr"),
        ("sh", "sr"),
        ("cnr", "sr"),
        ("hr", "hr"),
        ("auto", None),
        ("AUTO", None),
        ("", None),
    ],
)
def test_pin_is_the_normalised_config_code(manager, raw, pin):
    assert runner_with(manager, raw)._pin() == pin


def test_unknown_pin_is_a_permanent_refusal(manager):
    with pytest.raises(jobs.PermanentJobError) as caught:
        runner_with(manager, "serbian")._pin()

    assert str(caught.value) == "неизвестный язык «serbian»: [asr] language в config.toml"


async def test_unknown_pin_refuses_the_job_before_youtube(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("serbian")
    assert job.retryable is False
    # Метаданных не спрашивали (job.info пуст), звука и воркера не было: отказ
    # не стоит ничего ни YouTube, ни карте.
    assert job.info is None
    assert pipeline.workers == []


async def test_language_refusal_is_not_recreated_after_cooldown(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)
    await pipeline.run(job)
    # Остывание повторяемого отказа прошло бы: окончательный отказ оно не трогает.
    job.failed_at -= jobs.RETRY_COOLDOWN

    assert manager.get_or_create(VIDEO, "ru", True) is job
    assert manager.started == [VIDEO]


@pytest.mark.parametrize(
    ("raw", "sent"),
    [("sh", "sr"), ("SR", "sr"), ("sr-Latn", "sr"), ("cnr", "sr"), ("AUTO", "auto")],
)
async def test_pin_reaches_the_worker_normalised(manager, pipeline, raw, sent):
    pipeline.configure(language=raw)
    job = manager.get_or_create(VIDEO, "ru", True)
    # Ролик без дорожек автора и без метки языка: путь распознавания.
    job.info = metadata()

    await pipeline.run(job)

    args = pipeline.workers[-1].args
    assert args[args.index("--language") + 1] == sent


async def test_worker_refusal_on_stdout_is_permanent(manager, pipeline):
    pipeline.asr_result = {"error": language.refusal("xx"), "permanent": True}
    pipeline.asr_exit = 2
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("xx")
    assert job.retryable is False


async def test_worker_crash_keeps_the_stderr_tail(manager, pipeline):
    pipeline.asr_result = b""
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b"Traceback (most recent call last):\nValueError: boom"
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR не справился: Traceback (most recent call last):\nValueError: boom"
    assert job.retryable is True


async def test_worker_error_reply_reaches_the_viewer(manager, pipeline):
    # Воркер отказал сам, фразой в stdout, а stderr пуст: сегодня зритель видел
    # «ASR не справился: » с пустым хвостом.
    pipeline.asr_result = {"error": "аудио короче половины секунды"}
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b""
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR не справился: аудио короче половины секунды"
    assert job.retryable is True
