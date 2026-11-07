"""Video cache: readiness by the file on disk, atomic writes, the ceiling and eviction."""

import asyncio
import os

import pytest

from helper.sources.ytdlp import YtDlpError
from helper.video import VideoStore

VIDEO = "abcdefghijk"


def writer(payload=b"mp4"):
    def fetch(video_id, target, timeout, *, cookies=None):
        target.write_bytes(payload)
        return target

    return fetch


async def test_a_file_on_disk_is_ready_before_anything_is_asked(tmp_path):
    (tmp_path / f"{VIDEO}.mp4").write_bytes(b"mp4")
    store = VideoStore(tmp_path, fetch=writer())

    status = store.status(VIDEO)

    assert (status.state, status.path.name) == ("ready", f"{VIDEO}.mp4")
    assert status.wire() == {"state": "ready", "message": ""}


async def test_the_first_ask_starts_the_download_and_settles_ready(tmp_path):
    calls = []

    def fetch(video_id, target, timeout, *, cookies=None):
        calls.append((video_id, timeout, cookies))
        target.write_bytes(b"mp4")
        return target

    store = VideoStore(tmp_path, fetch=fetch)

    assert store.ensure(VIDEO).state == "preparing"
    assert (await store.settled(VIDEO)).state == "ready"
    assert calls == [(VIDEO, 1800.0, None)]


async def test_a_second_ask_while_preparing_does_not_start_another_download(tmp_path):
    calls = []

    def fetch(video_id, target, timeout, *, cookies=None):
        calls.append(video_id)

    store = VideoStore(tmp_path, fetch=fetch)

    store.ensure(VIDEO)
    store.ensure(VIDEO)
    await store.settled(VIDEO)

    assert calls == [VIDEO]


async def test_a_failure_keeps_the_phrase_and_leaves_no_file(tmp_path):
    def fetch(video_id, target, timeout, *, cookies=None):
        raise YtDlpError("HTTP Error 403")

    store = VideoStore(tmp_path, fetch=fetch)

    store.ensure(VIDEO)
    status = await store.settled(VIDEO)

    assert status.state == "error"
    assert status.message == "HTTP Error 403"
    assert not (tmp_path / f"{VIDEO}.mp4").exists()


async def test_a_retry_after_a_failure_starts_over(tmp_path):
    attempts = []

    def fetch(video_id, target, timeout, *, cookies=None):
        attempts.append(video_id)

        if len(attempts) == 1:
            raise YtDlpError("HTTP Error 403")

        target.write_bytes(b"mp4")

    store = VideoStore(tmp_path, fetch=fetch)

    store.ensure(VIDEO)
    assert (await store.settled(VIDEO)).state == "error"

    assert store.ensure(VIDEO).state == "preparing"
    assert (await store.settled(VIDEO)).state == "ready"
    assert len(attempts) == 2


def age_limited(*, with_account):
    def fetch(video_id, target, timeout, *, cookies=None):
        if cookies is None:
            raise YtDlpError("Sign in to confirm your age", permanent=True, age_restricted=True)

        with_account.append(cookies)
        target.write_bytes(b"mp4")

    return fetch


async def test_an_age_restricted_video_is_retried_with_the_account(tmp_path):
    with_account = []
    store = VideoStore(tmp_path, fetch=age_limited(with_account=with_account))
    account = tmp_path / "cookies.txt"

    store.ensure(VIDEO, cookies=account)
    status = await store.settled(VIDEO)

    assert status.state == "ready"
    assert with_account == [account]


async def test_without_an_account_the_age_limit_is_final(tmp_path):
    store = VideoStore(tmp_path, fetch=age_limited(with_account=[]))

    store.ensure(VIDEO)
    status = await store.settled(VIDEO)

    assert status.state == "error"
    assert "age" in status.message


async def test_the_oldest_video_makes_room(tmp_path):
    for name, stamp in (("old.mp4", 1000), ("new.mp4", 2000)):
        path = tmp_path / name
        path.write_bytes(b"x" * 10)
        os.utime(path, (stamp, stamp))

    store = VideoStore(tmp_path, max_bytes=25, fetch=writer(b"y" * 10))
    store.ensure(VIDEO)
    await store.settled(VIDEO)

    assert not (tmp_path / "old.mp4").exists()
    assert (tmp_path / "new.mp4").exists()
    assert (tmp_path / f"{VIDEO}.mp4").exists()


async def test_a_video_bigger_than_the_whole_cache_is_not_thrown_away(tmp_path):
    store = VideoStore(tmp_path, max_bytes=5, fetch=writer(b"x" * 20))

    store.ensure(VIDEO)
    status = await store.settled(VIDEO)

    assert status.state == "ready"
    assert (tmp_path / f"{VIDEO}.mp4").exists()


def test_the_registry_puts_the_cache_where_the_config_says(tmp_path):
    from helper.config import AsrConfig, Config, TranslatorConfig, VideoConfig
    from helper.jobs import JobManager

    registry = JobManager(
        config=Config(
            asr=AsrConfig(),
            translator=TranslatorConfig(),
            video=VideoConfig(dir=str(tmp_path), max_gib=4),
        )
    )

    try:
        assert registry.videos.directory == tmp_path
        assert registry.videos.max_bytes == 4 * 1024**3
    finally:
        # TrackCache has no close(); without this the garbage collector complains about sqlite.
        registry.cache._db.close()


def test_the_cache_directory_is_closed_to_other_users(tmp_path):
    # In /tmp the directory sits next to other people's: a predictable video name
    # and a world-writable directory would give a neighbouring process room to swap the file.
    target = tmp_path / "video"

    VideoStore(target)

    assert target.stat().st_mode & 0o777 == 0o700
