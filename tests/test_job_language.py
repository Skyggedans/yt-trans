"""Language on the daemon side: the owner's pin, the refusal and the conversation with the worker."""

import pytest
from conftest import metadata

from helper import jobs, language
from helper.asr import worker
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

    assert str(caught.value) == 'unknown language "serbian": [asr] language in config.toml'


async def test_unknown_pin_refuses_the_job_before_youtube(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("serbian")
    assert job.retryable is False
    # Metadata was not requested (job.info is empty), there was no audio or worker: the refusal
    # costs neither YouTube nor the graphics card anything.
    assert job.info is None
    assert pipeline.workers == []


async def test_language_refusal_is_not_recreated_after_cooldown(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)
    await pipeline.run(job)
    # The cooldown of a retryable failure would have elapsed: it does not touch a permanent refusal.
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
    # A video without author tracks and without a language label: the recognition path.
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
    assert job.error == "ASR failed: Traceback (most recent call last):\nValueError: boom"
    assert job.retryable is True


async def test_worker_error_reply_reaches_the_viewer(manager, pipeline):
    # The worker refused by itself, with a phrase in stdout, and stderr is empty: today the viewer saw
    # "ASR failed: " with an empty tail.
    pipeline.asr_result = {"error": "audio is shorter than half a second"}
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b""
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR failed: audio is shorter than half a second"
    assert job.retryable is True


# ── What is declared goes to the worker as a hint ───────────────────────────────


def test_declaration_is_the_pin_and_the_normalised_label(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert runner_with(manager, "SR")._declaration(job) == ("sr", None)

    job.info = metadata(audio_language="en-US")

    assert runner_with(manager, "SR")._declaration(job) == ("sr", "en")
    assert runner_with(manager, "auto")._declaration(job) == (None, "en")

    job.info = metadata(audio_language="xx-YY")

    assert runner_with(manager, "auto")._declaration(job) == (None, None)


@pytest.mark.parametrize(
    ("pin", "audio_language", "sent", "hint"),
    [
        ("auto", "en-US", "auto", "en"),
        ("auto", None, "auto", None),
        ("auto", "xx-YY", "auto", None),
        ("sr", "en", "sr", "en"),
        ("sh", None, "sr", None),
    ],
)
async def test_worker_argv_parses_with_the_worker_parser(manager, pipeline, pin, audio_language, sent, hint):
    pipeline.configure(language=pin)
    job = manager.get_or_create(VIDEO, "ru", True)
    job.info = metadata(audio_language=audio_language)

    await pipeline.run(job)

    args = pipeline.workers[-1].args
    parsed = worker.build_parser().parse_args(args[3:])

    assert args[1:3] == ["-m", "helper.asr.worker"]
    assert (parsed.language, parsed.hint) == (sent, hint)
