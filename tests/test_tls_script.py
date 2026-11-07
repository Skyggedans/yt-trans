"""packaging/make-tls.sh: the certificate covers what the tablet actually goes by.

The browser compares the name from the address bar with subjectAltName and makes no allowances:
a name signed as an IP is not a name, and the page will not open.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "make-tls.sh"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required")


@pytest.fixture(scope="module")
def alt_names(tmp_path_factory) -> str:
    """One issuance for both tests: RSA keys are the slowest part."""

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
    # The name does not replace the addresses: with a certificate for one name only, /ca.crt
    # over http from the address would break, as would everything already configured for the IP.
    assert "IP Address:127.0.0.1" in alt_names
    assert "DNS:localhost" in alt_names
