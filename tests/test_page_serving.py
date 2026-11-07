"""The tablet page is served by the daemon itself: from the same origin and without a token."""

import httpx


async def test_the_page_is_served_at_the_root(client):
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>yt-trans" in response.text


async def test_the_page_needs_no_token(manager):
    from helper import app as helper_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=helper_app.app), base_url="http://test"
    ) as open_client:
        assert (await open_client.get("/")).status_code == 200


async def test_the_page_does_not_swallow_the_api(client):
    assert (await client.get("/v1/health")).status_code == 200


async def test_an_unknown_page_is_a_plain_404(client):
    assert (await client.get("/no-such-file")).status_code == 404


async def test_the_page_brings_its_styles_and_scripts(client):
    assert (await client.get("/style.css")).status_code == 200
    assert (await client.get("/app.js")).status_code == 200


async def test_the_page_never_asks_for_a_token(client):
    text = (await client.get("/")).text

    assert 'id="token-input"' not in text
    assert 'id="setup"' not in text
    # The app is visible at once: there is no obstacle screen before it any more.
    assert '<div id="app">' in text


async def test_the_root_certificate_is_offered_before_https_starts(client, monkeypatch, tmp_path):
    # The order matters: the certificate is installed on the tablet while the
    # daemon is still on http, otherwise the browser will not open the page and
    # there is nowhere to get the file.
    from helper import app as helper_app

    ca = tmp_path / "ca.crt"
    ca.write_text("сертификат", encoding="utf-8")
    monkeypatch.setattr(helper_app, "CA_PATH", ca)

    response = await client.get("/ca.crt")

    assert response.status_code == 200
    assert response.text == "сертификат"


async def test_without_a_certificate_there_is_nothing_to_install(client, monkeypatch, tmp_path):
    from helper import app as helper_app

    monkeypatch.setattr(helper_app, "CA_PATH", tmp_path / "absent.crt")

    assert (await client.get("/ca.crt")).status_code == 404


async def test_the_service_worker_is_served_from_the_root(client):
    # The worker lies at the root of the scope, so Service-Worker-Allowed is not
    # needed, and the type must be javascript: otherwise the browser will refuse.
    response = await client.get("/sw.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
