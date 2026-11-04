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
