"""Скачивание картинки: H.264 не выше 1080p, родной звук, склейка без перекодирования."""

from pathlib import Path

import pytest

from helper.sources import ytdlp

VIDEO = "abcdefghijk"


def fake_download(calls, payload=b"mp4"):
    def run(cmd, timeout):
        calls.append((cmd, timeout))
        template = Path(cmd[cmd.index("-o") + 1])
        template.with_name("video.mp4").write_bytes(payload)

        return ""

    return run


def test_the_video_is_downloaded_as_h264_mp4_with_its_own_sound(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ytdlp, "_run", fake_download(calls))
    target = tmp_path / f"{VIDEO}.mp4"

    ytdlp.fetch_video(VIDEO, target)

    cmd, timeout = calls[0]
    assert cmd[cmd.index("-f") + 1] == ytdlp.VIDEO_FORMAT
    # Сортировка начинается с res, а не с размера файла: 1080p — потолок,
    # ниже можно, выше нельзя. lang,+size — родной звук, а не автодубляж.
    assert cmd[cmd.index("-S") + 1] == "res:1080,lang,+size"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert cmd[cmd.index("--postprocessor-args") + 1] == "Merger+ffmpeg:-movflags +faststart"
    assert cmd[cmd.index("-o") + 1].endswith("video.%(ext)s")
    assert timeout == 1800.0
    assert target.read_bytes() == b"mp4"
    # Временного каталога рядом не осталось: он живёт внутри родителя цели.
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_a_failed_download_leaves_nothing_behind(tmp_path, monkeypatch):
    def run(cmd, timeout):
        raise ytdlp.YtDlpError("HTTP Error 403", transient=True)

    monkeypatch.setattr(ytdlp, "_run", run)
    target = tmp_path / f"{VIDEO}.mp4"

    with pytest.raises(ytdlp.YtDlpError):
        ytdlp.fetch_video(VIDEO, target)

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_bad_id_never_reaches_yt_dlp(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ytdlp, "_run", fake_download(calls))

    with pytest.raises(ytdlp.YtDlpError):
        ytdlp.fetch_video("../../etc/passwd", tmp_path / "x.mp4")

    assert calls == []
