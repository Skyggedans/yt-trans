"""Страница планшета раздаётся самим демоном: с того же источника и без токена."""

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


async def test_the_page_has_the_screen_for_asking_the_token(client):
    text = (await client.get("/")).text

    assert 'id="token-input"' in text
    assert 'id="setup"' in text


async def test_the_root_certificate_is_offered_before_https_starts(client, monkeypatch, tmp_path):
    # Порядок важен: сертификат ставят на планшет, пока демон ещё по http, иначе
    # браузер не откроет страницу и файл взять неоткуда.
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
