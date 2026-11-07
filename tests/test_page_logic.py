"""A bridge to the page tests: the pure logic runs under node --test, from here — under one pytest."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="no node")
def test_the_page_logic_passes_its_own_tests():
    proc = subprocess.run(
        ["node", "--test", "page/logic.test.mjs"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="no node")
def test_the_cache_module_passes_its_own_tests():
    proc = subprocess.run(
        ["node", "--test", "page/cache.test.mjs"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
