"""Видео на проводе: подготовка, состояние и файл, который умеет Range."""

import pytest

from helper.video import VideoStore

VIDEO = "abcdefghijk"


def writer(payload=b"0123456789"):
    def fetch(video_id, target, timeout, *, cookies=None):
        target.write_bytes(payload)
        return target

    return fetch


async def test_a_video_is_prepared_and_then_served(client, manager, tmp_path):
    manager.videos = VideoStore(tmp_path, fetch=writer())

    started = await client.post(f"/v1/videos/{VIDEO}")
    await manager.videos.settled(VIDEO)
    ready = await client.get(f"/v1/videos/{VIDEO}")
    served = await client.get(f"/v1/videos/{VIDEO}/file")

    assert started.json()["state"] == "preparing"
    assert ready.json() == {"state": "ready", "message": ""}
    assert served.status_code == 200
    assert served.content == b"0123456789"
    assert served.headers["accept-ranges"] == "bytes"


async def test_a_range_request_gets_206_and_a_slice(client, manager, tmp_path):
    manager.videos = VideoStore(tmp_path, fetch=writer())
    manager.videos.ensure(VIDEO)
    await manager.videos.settled(VIDEO)

    response = await client.get(
        f"/v1/videos/{VIDEO}/file", headers={"Range": "bytes=2-4"}
    )

    assert response.status_code == 206
    assert response.content == b"234"
    assert response.headers["content-range"] == "bytes 2-4/10"


async def test_a_file_that_is_not_ready_is_a_409_with_a_phrase(client, manager, tmp_path):
    manager.videos = VideoStore(tmp_path)

    response = await client.get(f"/v1/videos/{VIDEO}/file")

    assert response.status_code == 409
    assert response.json()["detail"]


async def test_a_bad_video_id_is_a_400_before_any_download(client, manager, tmp_path):
    calls = []
    manager.videos = VideoStore(tmp_path, fetch=lambda *a, **k: calls.append(a))

    response = await client.post("/v1/videos/short")

    assert response.status_code == 400
    assert calls == []


async def test_the_file_is_open_without_a_token(manager, tmp_path):
    # <video src> заголовков не шлёт, а токена больше нет: файл отдаётся тому,
    # кого пустил Origin. Запрос без Origin — это страница самого демона, curl
    # и приложения вне браузера.
    import httpx

    from helper import app as helper_app

    manager.videos = VideoStore(tmp_path, fetch=writer())
    manager.videos.ensure(VIDEO)
    await manager.videos.settled(VIDEO)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app), base_url="http://test"
    ) as open_client:
        assert (await open_client.get(f"/v1/videos/{VIDEO}/file")).status_code == 200


async def test_the_size_is_asked_for_with_head_and_no_body(client, manager, tmp_path):
    # Страница узнаёт размер до скачивания: без HEAD она не проверит свободное
    # место и не покажет проценты, а молча начнёт качать ролик на 400 МБ.
    manager.videos = VideoStore(tmp_path, fetch=writer())
    manager.videos.ensure(VIDEO)
    await manager.videos.settled(VIDEO)

    response = await client.head(f"/v1/videos/{VIDEO}/file")

    assert response.status_code == 200
    assert response.headers["content-length"] == "10"
    assert response.content == b""
