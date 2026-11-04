"""packaging/make-tls.sh: сертификат покрывает то, по чему планшет реально ходит.

Браузер сверяет имя из адресной строки с subjectAltName и не делает поблажек:
имя, подписанное как IP, — не имя, и страница не откроется.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "make-tls.sh"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="нужен openssl")


@pytest.fixture(scope="module")
def alt_names(tmp_path_factory) -> str:
    """Один выпуск на оба теста: ключи RSA — самая долгая часть."""

    work = tmp_path_factory.mktemp("tls")
    env = {**os.environ, "YT_TRANS_CONFIG": str(work)}

    subprocess.run(
        [str(SCRIPT), "yt-trans.local"], env=env, check=True, capture_output=True, text=True
    )

    return subprocess.run(
        ["openssl", "x509", "-in", str(work / "tls" / "server.crt"), "-noout", "-ext", "subjectAltName"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_a_local_name_is_written_as_a_name_not_an_address(alt_names):
    assert "DNS:yt-trans.local" in alt_names
    assert "IP Address:yt-trans.local" not in alt_names


def test_the_machines_own_addresses_stay_in_the_certificate(alt_names):
    # Имя не заменяет адреса: с сертификатом на одно имя отвалился бы и /ca.crt
    # по http с адреса, и всё, что уже настроено на IP.
    assert "IP Address:127.0.0.1" in alt_names
    assert "DNS:localhost" in alt_names
