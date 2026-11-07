"""Links: YouTube only, no duplicates, and playlist parsing as a flat list."""

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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://youtu.be/abcdefghijk", "abcdefghijk"),
        ("https://youtu.be/abcdefghijk?si=track", "abcdefghijk"),
        ("https://www.youtube.com/watch?v=abcdefghijk&t=30", "abcdefghijk"),
        ("https://m.youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
        ("https://music.youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/live/abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/embed/abcdefghijk", "abcdefghijk"),
        ("https://youtube.com/playlist?list=PL123", ""),
        ("https://youtu.be/", ""),
        ("https://youtu.be/короткий", ""),
        ("https://evil.example/watch?v=abcdefghijk", ""),
    ],
)
def test_the_video_id_is_read_from_the_address_without_the_network(url, expected):
    # Picture readiness is computed from the id, and it is already in the address: the link list
    # has no reason to go to YouTube for the badge.
    assert ytdlp.video_id_in(url) == expected


def test_adding_the_same_address_twice_keeps_one_record(tmp_path):
    path = tmp_path / "links.json"

    first = links.add("https://youtu.be/abcdefghijk", "Ролик", "video", path=path)
    second = links.add("https://youtu.be/abcdefghijk", "Другое имя", "video", path=path)

    assert first.id == second.id
    assert [link.title for link in links.load(path)] == ["Ролик"]


def test_a_link_keeps_the_length_it_saw(tmp_path):
    # The length is already known at the moment of adding: storing it is cheaper than going
    # to YouTube on every list display for a figure in a row.
    path = tmp_path / "links.json"

    links.add("https://youtu.be/abcdefghijk", "Ролик", "video", duration=960, path=path)

    assert links.load(path)[0].duration == 960
    assert links.load(path)[0].wire()["duration"] == 960


def test_an_old_record_without_a_length_still_loads(tmp_path):
    # A file written before the length appeared is no reason to lose links.
    path = tmp_path / "links.json"
    path.write_text(
        json.dumps({"links": [{"id": "abc", "url": "https://youtu.be/x", "title": "Ролик",
                               "kind": "video"}]}),
        encoding="utf-8",
    )

    assert links.load(path)[0].duration is None


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


def probe_stub(monkeypatch, video_id="abcdefghijk"):
    def probe(url, timeout=60.0, *, cookies=None):
        return {
            "kind": "video",
            "title": "Ролик",
            "videos": [{"videoId": video_id, "title": "Ролик", "duration": 300}],
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
    # The list says right away what can be watched instantly.
    assert payload["videos"][0]["state"] == "ready"


async def test_an_unprepared_episode_says_so(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})

    response = await client.get(f"/v1/links/{added.json()['link']['id']}/videos")

    assert response.json()["videos"][0]["state"] == "absent"


async def test_a_video_link_says_its_picture_is_ready(client, manager, monkeypatch, tmp_path):
    # The readiness badge is needed by the top row of the list too, not only by the videos
    # inside a playlist: otherwise one has to guess about a downloaded video.
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    (tmp_path / "abcdefghijk.mp4").write_bytes(b"mp4")
    probe_stub(monkeypatch)
    await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["state"] == "ready"


async def test_a_video_link_without_a_picture_says_so(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    probe_stub(monkeypatch)
    await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["state"] == "absent"


async def test_a_video_link_remembers_its_length(client, manager, monkeypatch, tmp_path):
    # The daemon already saw the length while parsing the link — the list should get it
    # without a new trip to YouTube.
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)
    probe_stub(monkeypatch)
    await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["duration"] == 300


async def test_a_playlist_has_no_length_of_its_own(client, manager, monkeypatch, tmp_path):
    # A playlist has no single length: the length belongs to each video inside it.
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)

    def probe(url, timeout=60.0, *, cookies=None):
        return {"kind": "playlist", "title": "Список", "videos": []}

    monkeypatch.setattr("helper.app.probe_url", probe)
    await client.post("/v1/links", json={"url": "https://youtube.com/playlist?list=PL123"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["duration"] is None


async def test_a_playlist_has_no_picture_state_of_its_own(client, manager, monkeypatch, tmp_path):
    from helper.video import VideoStore

    manager.videos = VideoStore(tmp_path)

    def probe(url, timeout=60.0, *, cookies=None):
        return {"kind": "playlist", "title": "Список", "videos": []}

    monkeypatch.setattr("helper.app.probe_url", probe)
    await client.post("/v1/links", json={"url": "https://youtube.com/playlist?list=PL123"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert "state" not in listed[0]
    # A playlist has neither its own picture nor its own translation: there is nothing to translate.
    assert "translated" not in listed[0]


# A ready track the way _finish_translation (JobCue.payload()) puts it.
TRANSLATED = [
    {
        "i": 0,
        "start": 0.0,
        "end": 1.5,
        "src": "Dobar dan.",
        "lines": ["Добрый день."],
        "study": "Dobar dan.",
        "failed": False,
    },
]


# Each test has its own video: the track cache is shared for the run, and the ready translation
# of the previous test would otherwise be found here too.
def cache_translation(manager, video_id, cues=TRANSLATED):
    manager.cache.put(manager._key_for(video_id, "asr"), cues, 300.0)


async def test_a_video_link_says_its_translation_is_ready(client, manager, monkeypatch):
    probe_stub(monkeypatch, "tranready01")
    cache_translation(manager, "tranready01")
    await client.post("/v1/links", json={"url": "https://youtu.be/tranready01"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["translated"] is True


async def test_a_video_link_without_a_translation_says_so(client, manager, monkeypatch):
    probe_stub(monkeypatch, "notransla01")
    await client.post("/v1/links", json={"url": "https://youtu.be/notransla01"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["translated"] is False


async def test_an_empty_cached_track_is_not_a_ready_translation(client, manager, monkeypatch):
    # An empty track is a broken record, not "everything is ready": with the check mark the viewer
    # would wait for subtitles that are not there.
    probe_stub(monkeypatch, "emptytrack1")
    cache_translation(manager, "emptytrack1", cues=[])
    await client.post("/v1/links", json={"url": "https://youtu.be/emptytrack1"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["translated"] is False


async def test_a_track_from_other_models_is_not_a_ready_translation(client, manager, monkeypatch):
    # The cache key carries the models and the prompt: what a previous model made is not what
    # the viewer would get now, and the check mark would lie.
    from helper.cache import CacheKey

    probe_stub(monkeypatch, "stalemodel1")
    stale = CacheKey(
        video_id="stalemodel1",
        pin="",
        asr_model="large-v2/float16",
        translator_model="старая",
        track_fingerprint="asr",
    )
    manager.cache.put(stale, TRANSLATED, 300.0)
    await client.post("/v1/links", json={"url": "https://youtu.be/stalemodel1"})

    listed = (await client.get("/v1/links")).json()["links"]

    assert listed[0]["translated"] is False


async def test_a_playlist_episode_says_whether_its_translation_is_ready(
    client, manager, monkeypatch
):
    probe_stub(monkeypatch, "playtrans01")
    cache_translation(manager, "playtrans01")
    added = await client.post("/v1/links", json={"url": "https://youtu.be/playtrans01"})

    response = await client.get(f"/v1/links/{added.json()['link']['id']}/videos")

    assert response.json()["videos"][0]["translated"] is True


async def test_an_unknown_link_is_a_404(client):
    assert (await client.get("/v1/links/nope/videos")).status_code == 404


async def test_a_link_can_be_removed(client, monkeypatch):
    probe_stub(monkeypatch)
    added = await client.post("/v1/links", json={"url": "https://youtu.be/abcdefghijk"})
    link_id = added.json()["link"]["id"]

    assert (await client.delete(f"/v1/links/{link_id}")).json() == {"removed": True}
    assert (await client.get("/v1/links")).json()["links"] == []


async def test_a_restricted_link_is_probed_again_with_the_account(monkeypatch):
    """An 18+ video is not readable anonymously — so a second attempt with the account.

    The same rule as for downloading: anonymous always, the account only where
    there is no way without it. Without this attempt a link to an 18+ video could not even
    be saved from the tablet page.
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
