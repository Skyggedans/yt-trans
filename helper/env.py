"""Пути кэшей проекта. Импортировать ПЕРВЫМ, до всего, что тянет huggingface.

На этой машине `XDG_CACHE_HOME=/mnt/ramdisk` — 8 ГБ tmpfs, и по умолчанию кэш
huggingface уезжает туда. Веса large-v3 это ~3 ГБ, и на ramdisk они не помещаются
(а если бы помещались, то съедали бы оперативную память, которой и так мало).

Глобальную настройку не трогаем — она осознанная. Просто уводим кэши этого проекта
на NVMe явным HF_HOME, который читается до импорта huggingface_hub.
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


# Порядок важен: cublasLt должна загрузиться раньше cublas, которая от неё зависит.
CUDA_LIBS = ("libcublasLt.so.*", "libcublas.so.*", "libcudnn.so.*")
CUDA_MODULES = ("nvidia.cublas.lib", "nvidia.cudnn.lib")


def _cuda_lib_dirs() -> list[str]:
    dirs = []

    for name in CUDA_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue

        # Это namespace-пакеты без __init__.py, поэтому __file__ у них None,
        # а каталог лежит в __path__.
        paths = list(getattr(module, "__path__", None) or [])

        if paths:
            dirs.extend(paths)
        elif module.__file__:
            dirs.append(os.path.dirname(module.__file__))

    return dirs


def preload_cuda() -> list[str]:
    """Подключить cuBLAS и cuDNN из колёс nvidia-*-cu12.

    Системного CUDA-рантайма на машине нет, а CTranslate2 подгружает libcublas.so.12
    и libcudnn.so.9 через dlopen в момент первого прохода энкодера. Без этого всё
    выглядит рабочим — модель грузится, VAD отрабатывает — и падает только когда
    дело доходит до настоящего инференса.

    Нужны оба механизма, потому что ASR идёт отдельным процессом:
      * LD_LIBRARY_PATH наследуется подпроцессами, и там его читает динамический
        загрузчик при старте — для воркера этого достаточно;
      * в уже запущенном процессе менять LD_LIBRARY_PATH поздно, загрузчик его
        больше не перечитывает, поэтому библиотеки подгружаются явно через ctypes.

    Если библиотеки стоят в системе, функция просто ничего не делает.
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
                    # Не фатально: библиотека может быть не нужна этой сборке
                    # CTranslate2 либо уже присутствовать в системе.
                    pass

    return dirs


def setup() -> None:
    """Разложить кэши по NVMe и подключить CUDA. Идемпотентно."""
    for path in (HF_HOME, YTDLP_CACHE, TRACKS_DIR):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HF_HOME", str(HF_HOME))
    preload_cuda()


setup()
