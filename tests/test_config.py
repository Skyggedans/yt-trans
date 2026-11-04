"""[asr] language в config.toml: что написал хозяин и что получает демон."""

import logging
import tomllib
from pathlib import Path

import pytest

from helper import language
from helper.config import ConfigError, load

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    return path


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ('"cnr"', "sr"),
        ('"sh"', "sr"),
        ('"SR"', "sr"),
        ('"sr-Latn"', "sr"),
        ('"hr"', "hr"),
        ('"en-US"', "en"),
        ('"auto"', "auto"),
        ('"AUTO"', "auto"),
        ('""', "auto"),
    ],
)
def test_language_is_normalised_at_load(tmp_path, raw, stored):
    config = load(written(tmp_path, f"[asr]\nlanguage = {raw}\n"))

    assert config.asr.language == stored
    assert config.describe()["asr"]["language"] == stored


def test_missing_file_means_auto(tmp_path):
    assert load(tmp_path / "absent.toml").asr.language == "auto"


@pytest.mark.parametrize(("raw", "kept"), [('"serbian"', "serbian"), ("5", "5")])
def test_unknown_language_is_logged_not_fatal(tmp_path, caplog, raw, kept):
    with caplog.at_level(logging.ERROR, logger="helper.config"):
        config = load(written(tmp_path, f"[asr]\nlanguage = {raw}\n"))

    assert config.asr.language == kept
    assert config.describe()["asr"]["language"] == kept
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        language.refusal(kept)
    ]


def test_example_config_ships_auto():
    assert tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))["asr"]["language"] == "auto"
    assert load(EXAMPLE).asr.language == "auto"


def test_the_video_cache_can_live_where_the_owner_says(tmp_path):
    # /tmp — это tmpfs: там потолок меряют уже не диском, а оперативной памятью,
    # поэтому и потолок задаётся рядом с каталогом, а не зашит в код.
    config = load(written(tmp_path, '[video]\ndir = "/tmp/yt-trans-video"\nmax_gib = 4\n'))

    assert config.video.directory == Path("/tmp/yt-trans-video")
    assert config.video.max_bytes == 4 * 1024**3


def test_without_a_word_the_video_cache_stays_in_the_project_cache(tmp_path):
    config = load(written(tmp_path, ""))

    assert config.video.directory is None
    assert config.video.max_bytes == 20 * 1024**3


def test_a_nonsense_video_ceiling_stops_the_daemon_at_start(tmp_path):
    from helper.config import ConfigError

    with pytest.raises(ConfigError):
        load(written(tmp_path, "[video]\nmax_gib = 0\n"))

    with pytest.raises(ConfigError):
        load(written(tmp_path, '[video]\nmax_gib = "много"\n'))


def test_the_daemon_can_serve_https(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    config = load(written(
        tmp_path, f'[server]\ntls_cert = "{cert}"\ntls_key = "{key}"\n'
    ))

    assert config.server.tls_files == (cert, key)


def test_plain_http_stays_the_default(tmp_path):
    assert load(tmp_path / "absent.toml").server.tls_files is None


def test_half_a_certificate_stops_the_daemon(tmp_path):
    # Один файл без другого — не «почти https»: uvicorn поднял бы обычный http,
    # и зритель искал бы причину в планшете.
    cert = tmp_path / "cert.pem"
    cert.write_text("cert", encoding="utf-8")

    with pytest.raises(ConfigError, match="tls_key"):
        load(written(tmp_path, f'[server]\ntls_cert = "{cert}"\n'))


def test_a_missing_certificate_file_stops_the_daemon(tmp_path):
    with pytest.raises(ConfigError, match="tls_cert"):
        load(written(
            tmp_path,
            f'[server]\ntls_cert = "{tmp_path / "cert.pem"}"\ntls_key = "{tmp_path / "key.pem"}"\n',
        ))
