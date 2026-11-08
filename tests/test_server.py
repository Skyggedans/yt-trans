"""Network: the address from the config; the daemon's own page calls it from the same origin."""

import asyncio

import httpx

from conftest import FakeSocket
from helper import app as helper_app
from helper.config import load


def from_peer(address: str) -> httpx.AsyncClient:
    """A client whose address the daemon sees as `address`.

    The peer of a real request is set by the kernel and cannot be forged, so this is
    the one thing a test must choose itself: "on the machine" and "on the network"
    differ in nothing else.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app, client=(address, 51234)),
        base_url="http://127.0.0.1:8756",
    )


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
    # 404, not 403: the request reached the handler and found no job.
    response = await client.get(
        "/v1/jobs/zzzzzzzzzzz", headers={"Origin": "http://test"}
    )

    assert response.status_code == 404


async def test_a_foreign_page_still_cannot(client):
    response = await client.get(
        "/v1/jobs/zzzzzzzzzzz", headers={"Origin": "http://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "origin is not allowed"


async def test_the_socket_still_refuses_a_foreign_page():
    socket = FakeSocket()
    socket.headers = {"origin": "http://evil.example", "host": "127.0.0.1:8756"}

    await helper_app.websocket(socket)

    assert socket.closed == 4401
    assert socket.accepted is False


async def test_the_socket_accepts_the_page_the_daemon_serves(manager):
    socket = FakeSocket()
    socket.headers = {"origin": "http://127.0.0.1:8756", "host": "127.0.0.1:8756"}
    socket.handler = asyncio.create_task(helper_app.websocket(socket))

    try:
        await socket.say({"op": "watch", "videoId": "abcdefghijk"})

        assert socket.accepted is True
        assert (await socket.hear())["type"] == "snapshot"
    finally:
        await socket.disconnect()


async def test_on_the_loop_health_still_diagnoses(client):
    body = (await client.get("/v1/health")).json()

    assert body["ok"] is True
    assert "models" in body
    # There is no token any more — and no hint where it lives, either.
    assert "token_path" not in body


async def test_the_owner_sees_the_models_even_on_all_interfaces(manager, monkeypatch):
    # The tablet in the next room is the reason [server] host is 0.0.0.0. The extension
    # still lives on the machine itself and comes in over loopback — and the bind
    # address says nothing about who is asking. Health used to answer this with
    # a bare "alive", and the settings page printed "undefined" for every model.
    monkeypatch.setattr(helper_app, "HOST", "0.0.0.0")

    async with from_peer("127.0.0.1") as http:
        body = (await http.get("/v1/health")).json()

    assert "models" in body
    assert body["models"]["asr"]["model"]


async def test_in_the_network_health_says_only_that_it_is_alive(manager):
    # Without a key: model names, job state, cache size and the config path with
    # the user's name are a map of someone else's machine.
    async with from_peer("192.168.1.50") as http:
        body = (await http.get("/v1/health")).json()

    assert body == {"ok": True}


async def test_a_foreign_page_on_the_machine_gets_no_map(manager):
    # DNS rebinding brings a page from the internet to loopback: the peer address
    # is the owner's, Origin is not.
    async with from_peer("127.0.0.1") as http:
        body = (await http.get("/v1/health", headers={"Origin": "http://evil.example"})).json()

    assert body == {"ok": True}


async def test_the_daemons_own_page_reads_the_map(manager):
    async with from_peer("127.0.0.1") as http:
        body = (
            await http.get("/v1/health", headers={"Origin": "http://127.0.0.1:8756"})
        ).json()

    assert "models" in body


async def test_the_extension_reads_the_map(manager):
    async with from_peer("127.0.0.1") as http:
        body = (
            await http.get(
                "/v1/health", headers={"Origin": "chrome-extension://abcdefghijklmnop"}
            )
        ).json()

    assert "models" in body
