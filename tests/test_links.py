"""Ссылки: только YouTube, без дублей, и разбор плейлиста плоским списком."""

import json

import pytest

from helper import links
from helper.sources import ytdlp


def test_the_same_address_is_the_same_link():
    assert links.link_id("https://youtu.be/abcdefghijk") == links.link_id(
        "https://youtu.be/abcdefghijk"
    )
    assert links.link_id("https://youtu.be/abcdefghijk") != links.link_id(
        "https://youtu.be/zzzzzzzzzzz"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://youtube.com/playlist?list=PL123",
        "https://m.youtube.com/watch?v=abcdefghijk",
        "https://youtu.be/abcdefghijk",
    ],
)
def test_youtube_addresses_are_accepted(url):
    assert links.is_youtube_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "не адрес",
        "https://evil.example/watch?v=abcdefghijk",
        "https://youtube.com.evil.example/watch?v=x",
        "file:///etc/passwd",
    ],
)
def test_anything_else_is_refused(url):
    assert not links.is_youtube_url(url)


def test_adding_the_same_address_twice_keeps_one_record(tmp_path):
    path = tmp_path / "links.json"

    first = links.add("https://youtu.be/abcdefghijk", "Ролик", "video", path=path)
    second = links.add("https://youtu.be/abcdefghijk", "Другое имя", "video", path=path)

    assert first.id == second.id
    assert [link.title for link in links.load(path)] == ["Ролик"]


def test_removing_an_unknown_link_says_so(tmp_path):
    assert links.remove("nope", path=tmp_path / "links.json") is False


def test_a_broken_file_does_not_take_the_daemon_down(tmp_path):
    path = tmp_path / "links.json"
    path.write_text("{не json", encoding="utf-8")

    assert links.load(path) == []


def test_the_cap_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(links, "MAX_LINKS", 1)
    path = tmp_path / "links.json"
    links.add("https://youtu.be/abcdefghijk", "Первый", "video", path=path)

    with pytest.raises(links.TooManyLinks):
        links.add("https://youtu.be/zzzzzzzzzzz", "Второй", "video", path=path)


def test_a_playlist_is_read_flat(monkeypatch):
    raw = {
        "_type": "playlist",
        "title": "Сербские песни",
        "entries": [
            {"_type": "url", "id": "abcdefghijk", "title": "Первая", "duration": 120},
            {"_type": "url", "id": None, "title": "битая"},
        ],
    }
    calls = []
    monkeypatch.setattr(ytdlp, "_run", lambda cmd, timeout: calls.append(cmd) or json.dumps(raw))

    found = ytdlp.probe_url("https://youtube.com/playlist?list=PL123")

    assert found == {
        "kind": "playlist",
        "title": "Сербские песни",
        "videos": [{"videoId": "abcdefghijk", "title": "Первая", "duration": 120}],
    }
    assert "--flat-playlist" in calls[0]
    assert "--yes-playlist" in calls[0]
    assert "--no-playlist" not in calls[0]


def test_a_single_video_is_a_list_of_one(monkeypatch):
    raw = {"_type": "video", "id": "abcdefghijk", "title": "Ролик", "duration": 300}
    monkeypatch.setattr(ytdlp, "_run", lambda cmd, timeout: json.dumps(raw))

    found = ytdlp.probe_url("https://youtu.be/abcdefghijk")

    assert found == {
        "kind": "video",
        "title": "Ролик",
        "videos": [{"videoId": "abcdefghijk", "title": "Ролик", "duration": 300}],
    }


@pytest.fixture(autouse=True)
def isolated_links(tmp_path, monkeypatch):
    monkeypatch.setattr(links, "LINKS_PATH", tmp_path / "links.json")


def probe_stub(monkeypatch):
    def probe(url, timeout=60.0, *, cookies=None):
        return {
            "kind": "video",
            "title": "Ролик",
            "videos": [{"videoId": "abcdefghijk", "title": "Ролик", "duration": 300}],
        }

    monkeypatch.setattr("helper.app.probe_url", probe)


async def test_a_link_is_saved_and_listed(client, monkeypatch):
    probe_stub(monkeypatch)

    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    listed = await client.get("/v1/links")

    assert added.status_code == 200
    assert added.json()["link"]["kind"] == "video"
    assert [link["title"] for link in listed.json()["links"]] == ["Ролик"]


async def test_a_foreign_address_is_refused_without_asking_yt_dlp(client, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("yt-dlp не должен был запускаться")

    monkeypatch.setattr("helper.app.probe_url", explode)

    response = await client.post("/v1/links", json={"url": "https://evil.example/x"})

    assert response.status_code == 400


async def test_a_link_opens_as_a_playlist(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    (tmp_path / "abcdefghijk.mp4").write_bytes(b"mp4")
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    link_id = added.json()["link"]["id"]

    response = await client.get(f"/v1/links/{link_id}/videos")

    assert response.status_code == 200
    payload = response.json()
    assert [video["videoId"] for video in payload["videos"]] == ["abcdefghijk"]
    # Список сразу говорит, что можно смотреть мгновенно.
    assert payload["videos"][0]["state"] == "ready"


async def test_an_unprepared_episode_says_so(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})

    response = await client.get(f"/v1/links/{added.json()['link']['id']}/videos")

    assert response.json()["videos"][0]["state"] == "absent"


async def test_an_unknown_link_is_a_404(client):
    assert (await client.get("/v1/links/nope/videos")).status_code == 404


async def test_a_link_can_be_removed(client, monkeypatch):
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    link_id = added.json()["link"]["id"]

    assert (await client.delete(f"/v1/links/{link_id}")).json() == {"removed": True}
    assert (await client.get("/v1/links")).json()["links"] == []


async def test_a_restricted_link_is_probed_again_with_the_account(monkeypatch):
    """Ролик 18+ не читается анонимно — значит, вторая попытка с аккаунтом.

    Такое же правило, как у скачивания: анонимно всегда, аккаунт только там, где
    без него никак. Без этой попытки ссылку на ролик 18+ нельзя было даже
    сохранить со страницы планшета.
    """
    from pathlib import Path

    from helper import app as helper_app
    from helper.sources.ytdlp import YtDlpError

    calls = []

    def probe(url, timeout=60.0, *, cookies=None):
        calls.append(cookies)

        if len(calls) == 1:
            raise YtDlpError("ролик 18+", age_restricted=True)

        return {
            "kind": "video",
            "title": "Ролик",
            "videos": [{"videoId": "abcdefghijk", "title": "Ролик", "duration": 300}],
        }

    monkeypatch.setattr(helper_app, "probe_url", probe)
    account = Path("/tmp/youtube-18.txt")

    found = await helper_app.probe_with_account("https://youtu.be/abcdefghijk", account)

    assert found["title"] == "Ролик"
    assert calls == [None, account]


async def test_without_an_account_the_restriction_is_final(monkeypatch):
    from helper import app as helper_app
    from helper.sources.ytdlp import YtDlpError

    calls = []

    def probe(url, timeout=60.0, *, cookies=None):
        calls.append(cookies)
        raise YtDlpError("ролик 18+", age_restricted=True)

    monkeypatch.setattr(helper_app, "probe_url", probe)

    with pytest.raises(YtDlpError):
        await helper_app.probe_with_account("https://youtu.be/abcdefghijk", None)

    assert calls == [None]


async def test_another_failure_is_not_retried_with_the_account(monkeypatch):
    from pathlib import Path

    from helper import app as helper_app
    from helper.sources.ytdlp import YtDlpError

    calls = []

    def probe(url, timeout=60.0, *, cookies=None):
        calls.append(cookies)
        raise YtDlpError("ролик удалён", permanent=True)

    monkeypatch.setattr(helper_app, "probe_url", probe)

    with pytest.raises(YtDlpError):
        await helper_app.probe_with_account(
            "https://youtu.be/abcdefghijk", Path("/tmp/youtube-18.txt")
        )

    assert calls == [None]
