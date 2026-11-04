"""Сеть: адрес из конфига; страница самого демона ходит к нему с того же источника."""

import asyncio

from conftest import FakeSocket
from helper import app as helper_app
from helper.config import load


def test_the_daemon_listens_on_the_loop_by_default(tmp_path):
    assert load(tmp_path / "config.toml").server.host == "127.0.0.1"


def test_the_host_comes_from_the_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")

    assert load(path).server.host == "0.0.0.0"


def test_the_same_authority_is_another_spelling_of_ourselves():
    assert helper_app.same_origin("http://127.0.0.1:8756", "127.0.0.1:8756")
    assert helper_app.same_origin("https://Tablet.local:8756", "tablet.local:8756")
    assert not helper_app.same_origin("http://127.0.0.1:8756", "127.0.0.1:9000")
    assert not helper_app.same_origin("chrome-extension://abc", "127.0.0.1:8756")
    assert not helper_app.same_origin("http://127.0.0.1:8756", None)


async def test_the_page_served_by_the_daemon_may_call_the_api(client):
    # 404, а не 403: запрос дошёл до обработчика и не нашёл задания.
    response = await client.get(
        "/v1/jobs/zzzzzzzzzzz", headers={"Origin": "http://test"}
    )

    assert response.status_code == 404


async def test_a_foreign_page_still_cannot(client):
    response = await client.get(
        "/v1/jobs/zzzzzzzzzzz", headers={"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "origin не разрешён"


async def test_the_socket_still_refuses_a_foreign_page():
    socket = FakeSocket(helper_app.TOKEN)
    socket.headers = {"origin": "http://evil.example", "host": "127.0.0.1:8756"}

    await helper_app.websocket(socket)

    assert socket.closed == 4401
    assert socket.accepted is False


async def test_the_socket_accepts_the_page_the_daemon_serves(manager):
    socket = FakeSocket(helper_app.TOKEN)
    socket.headers = {"origin": "http://127.0.0.1:8756", "host": "127.0.0.1:8756"}
    socket.handler = asyncio.create_task(helper_app.websocket(socket))

    try:
        await socket.say({"op": "watch", "videoId": "abcdefghijk"})

        assert socket.accepted is True
        assert (await socket.hear())["type"] == "snapshot"
    finally:
        await socket.disconnect()
