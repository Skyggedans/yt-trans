"""Project cache paths. Import FIRST, before anything that pulls in huggingface.

On this machine `XDG_CACHE_HOME=/mnt/ramdisk` — an 8 GB tmpfs, and by default the
huggingface cache goes there. large-v3 weights are ~3 GB, and they don't fit on the
ramdisk (and if they did, they'd eat RAM, of which there's already little).

We don't touch the global setting — it's deliberate. We simply steer this project's
caches to the NVMe with an explicit HF_HOME, which is read before huggingface_hub
is imported.
"""

from __future__ import annotations

import ctypes
import glob
import importlib
import os
from pathlib import Path

CACHE_DIR = Path(os.environ.get("YT_TRANS_CACHE", Path.home() / ".cache" / "yt-trans"))
HF_HOME = CACHE_DIR / "hf"
YTDLP_CACHE = CACHE_DIR / "yt-dlp"
TRACKS_DIR = CACHE_DIR / "tracks"

# Settings. They live separately from the cache: the cache can be deleted painlessly,
# but the configuration cannot.
CONFIG_DIR = Path(os.environ.get("YT_TRANS_CONFIG", Path.home() / ".config" / "yt-trans"))


# Order matters: cublasLt must be loaded before cublas, which depends on it.
CUDA_LIBS = ("libcublasLt.so.*", "libcublas.so.*", "libcudnn.so.*")
CUDA_MODULES = ("nvidia.cublas.lib", "nvidia.cudnn.lib")


def _cuda_lib_dirs() -> list[str]:
    dirs = []

    for name in CUDA_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue

        # These are namespace packages without __init__.py, so their __file__ is None,
        # and the directory is in __path__.
        paths = list(getattr(module, "__path__", None) or [])

        if paths:
            dirs.extend(paths)
        elif module.__file__:
            dirs.append(os.path.dirname(module.__file__))

    return dirs


def preload_cuda() -> list[str]:
    """Load cuBLAS and cuDNN from the nvidia-*-cu12 wheels.

    There is no system CUDA runtime on the machine, and CTranslate2 loads libcublas.so.12
    and libcudnn.so.9 via dlopen at the first encoder pass. Without this everything
    looks working — the model loads, VAD runs — and it fails only when it reaches
    actual inference.

    Both mechanisms are needed because ASR runs as a separate process:
      * LD_LIBRARY_PATH is inherited by subprocesses, and the dynamic loader reads it
        there at startup — that's enough for the worker;
      * in an already running process it's too late to change LD_LIBRARY_PATH, the
        loader no longer re-reads it, so the libraries are loaded explicitly via ctypes.

    If the libraries are installed system-wide, the function simply does nothing.
    """
    dirs = _cuda_lib_dirs()

    if not dirs:
        return []

    previous = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([*dirs, previous] if previous else dirs)

    for pattern in CUDA_LIBS:
        for directory in dirs:
            for path in sorted(glob.glob(os.path.join(directory, pattern))):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    # Not fatal: the library may be unneeded by this CTranslate2
                    # build or already present in the system.
                    pass

    return dirs


def setup() -> None:
    """Lay out the caches on the NVMe and load CUDA. Idempotent."""
    for path in (HF_HOME, YTDLP_CACHE, TRACKS_DIR):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HF_HOME", str(HF_HOME))
    preload_cuda()


setup()
