"""The single pass for working with the GPU.

On this card there are 24 GB, of which the desktop holds 4–6 and the translator 17.3.
There remain 1–3 GB free, so two models don't fit at the same time — not "tightly", but
at all. Hence the whole module: any work with the card takes the same semaphore, and
between phases the model is unloaded.

The second purpose is not to be a hostile neighbour. The user works with their own ollama
on the same card, and ollama has no mechanism for reacting to external VRAM pressure:
while a model is resident, free memory isn't re-read at all. Someone else's allocator
gets a hard OOM (system-memory fallback isn't supported on Linux), and on a runtime-OOM
expireRunnersForRuntimeOOM triggers and unloads ALL models at once, killing someone
else's session. Therefore admission control precedes capture, and while the pass is held —
a watchdog that gives up the card at the first signs of someone else's work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("YT_TRANS_OLLAMA", "http://127.0.0.1:11434")

# Headroom beyond the declared need — only for the lag of CUDA reporting
# (ollama acknowledges it in its own comments) and for the growth of the desktop while
# we work: Chrome decodes video exactly then.
#
# Previously 2048 stood here, and it was double counting: the figures in helper/config.py
# already contain their own headroom over the measurement. On a card of 24564 MiB the
# requirement for the translator came out as 17800 + 2048 = 19848 with the desktop at
# 5007 MiB, i.e. more than ever becomes free on this machine. Translation could never
# start: the job waited 120 seconds and went into a pause.
GUARD_MIB = 768

WATCHDOG_INTERVAL = 1.0

# How many clean polls in a row are needed to return to work AFTER eviction.
# Without hysteresis we would fight someone else's keep_alive, capturing and giving up
# the card in a circle. Not applied on a free card: there the pass is issued immediately.
REARM_POLLS = 10

# The admission wait limit. Waiting forever is not allowed: the wait happens under the
# semaphore, so a stuck job would freeze all the rest, and from outside it would look
# like a hung stage without a single event. After the period expires the job goes
# into PAUSED with a clear reason and resumes on the next request.
ADMISSION_TIMEOUT = 120.0

# The threshold separating someone else's compute job from the desktop. NVML counts as
# compute processes Chrome, VSCode, Telegram, gnome-shell and others — they use
# CUDA for compositing and video decoding. Measured on this machine: the most
# voracious of them takes 938 MiB, whereas any ML job takes gigabytes.
# Without this threshold "the card is busy" would be true always.
FOREIGN_MIB = 2048

# ollama runners. They can't be identified by PID: the runner is spawned by the ollama
# service, isn't our descendant and takes all the memory of the model — therefore our own
# loaded model would look like someone else's compute job, and the daemon would evict
# itself immediately after loading the translator (exactly this happened on the first
# end-to-end run). The ownership of ollama models is determined precisely and by name,
# via /api/ps, so they're excluded entirely from the process check.
OLLAMA_RUNNERS = frozenset({"llama-server", "ollama", "ollama_llama_server", "ollama-runner"})


class Preempted(Exception):
    """The card had to be given up: the user started their own work."""


@dataclass
class GpuState:
    free_mib: int
    foreign_pids: list[int] = field(default_factory=list)
    foreign_models: list[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.foreign_pids or self.foreign_models)


def _nvml():
    try:
        import pynvml
    except ImportError:
        return None

    try:
        pynvml.nvmlInit()
    except Exception:
        return None

    return pynvml


class GpuLease:
    """One pass for all work with the card."""

    def __init__(self, ollama_url: str = OLLAMA_URL, guard_mib: int = GUARD_MIB) -> None:
        self._semaphore = asyncio.Semaphore(1)
        self._ollama_url = ollama_url
        self._guard_mib = guard_mib
        self._nvml = _nvml()
        self._own_pid = os.getpid()
        self._own_models: set[str] = set()

    def claim_model(self, name: str) -> None:
        """Mark a model as one's own so the watchdog doesn't mistake it for someone else's."""
        self._own_models.add(name)

    def state(self) -> GpuState:
        """A snapshot of the card: how much is free and whether someone else is working."""
        free_mib = 0
        foreign_pids: list[int] = []

        if self._nvml:
            try:
                handle = self._nvml.nvmlDeviceGetHandleByIndex(0)
                free_mib = self._nvml.nvmlDeviceGetMemoryInfo(handle).free // (1024 * 1024)

                for proc in self._nvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    used_mib = (proc.usedGpuMemory or 0) // (1024 * 1024)

                    # Screen the desktop by volume, our own ASR worker — by lineage,
                    # ollama runners — by name: their ownership is decided more precisely
                    # via /api/ps, where it's visible whose model is loaded exactly.
                    if used_mib < FOREIGN_MIB or _process_name(proc.pid) in OLLAMA_RUNNERS:
                        continue

                    if proc.pid != self._own_pid and not _is_descendant(proc.pid, self._own_pid):
                        foreign_pids.append(proc.pid)
            except Exception as exc:
                # Don't swallow: an empty snapshot is indistinguishable from "the card is
                # free", and by the documented rule a blinded watchdog is obliged to give
                # up the card.
                log.warning("NVML is unavailable: %s", exc)
                raise

        loaded = self._loaded_models()

        return GpuState(
            free_mib=free_mib,
            foreign_pids=foreign_pids,
            foreign_models=[m for m in loaded if m not in self._own_models],
        )

    def _loaded_models(self) -> list[str]:
        """What's loaded in ollama right now.

        The error is deliberately not swallowed. This is the only channel through which
        the user's work is visible: ollama runners are excluded from the process check, so
        treating an unavailable /api/ps as "there are no models" would mean reporting
        "the card is free" exactly when we have gone blind.
        """
        response = httpx.get(f"{self._ollama_url}/api/ps", timeout=3.0)
        response.raise_for_status()

        return [m.get("name", "") for m in response.json().get("models", [])]

    def is_resident(self, model: str) -> bool:
        """Whether this model is already loaded. A poll error counts as "no"."""
        try:
            return model in self._loaded_models()
        except Exception:
            return False

    def unload(self, model: str) -> None:
        """Unload one's own model from ollama immediately.

        keep_alive: 0 — the only way to free VRAM right away. A negative
        value must not be used under any circumstances: ollama's scheduler
        sorts unloading candidates by sessionDuration reduced to uint64,
        so -1 sorts last and makes the model unloadable.
        """
        with contextlib.suppress(Exception):
            httpx.post(
                f"{self._ollama_url}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=30.0,
            )

    @contextlib.asynccontextmanager
    async def acquire(self, need_mib: int, *, model: str | None = None, wait: bool = True):
        """Take a pass for a job that needs need_mib of VRAM.

        Returns an object with the `preempted` flag: long operations must
        check it and wind down when it's raised.
        """
        if not wait and self._semaphore.locked():
            raise Preempted("the card is occupied by another daemon job")

        async with self._semaphore:
            await self._await_admission(need_mib, model)

            holder = _Holder()
            watchdog = asyncio.create_task(self._watch(holder))

            try:
                yield holder
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog

    async def _await_admission(self, need_mib: int, model: str | None = None) -> None:
        """Wait until the card is truly free.

        If our own model is already resident, there's no need to ask for memory for it:
        ollama doesn't reload it. Without this correction the translation phase would
        require 17.8 GB free exactly when 17.3 of them are already occupied by our own
        translator, and admission would never pass.
        """
        needed = 0 if model and self.is_resident(model) else need_mib
        clean = 0
        contended = False
        deadline = time.monotonic() + ADMISSION_TIMEOUT

        while True:
            try:
                state = await asyncio.to_thread(self.state)
                free_enough = state.free_mib >= needed + self._guard_mib
                busy = state.busy
                detail = f"free {state.free_mib} MiB, models {state.foreign_models}"
            except Exception as exc:
                # An unknown card state is "busy", not "free".
                log.warning("GPU state unknown, waiting: %s", exc)
                free_enough, busy, detail = False, True, f"state unknown ({exc})"

            if free_enough and not busy:
                clean += 1

                # On a free card there's nothing to wait for. Hysteresis is needed only
                # so as not to fight someone else's keep_alive after we've already been
                # evicted.
                if not contended or clean >= REARM_POLLS:
                    return
            else:
                if clean:
                    log.info("the card is busy again, the clean poll counter reset")

                contended = True
                clean = 0

            # Waiting forever is not allowed: the wait happens under the semaphore, so one
            # stuck job would freeze all the rest, and the client would see a stage without
            # a single event. We give up the semaphore and report the reason.
            if time.monotonic() >= deadline:
                raise Preempted(
                    f"the card didn't free up for {needed} MiB in "
                    f"{ADMISSION_TIMEOUT:.0f} s: {detail}"
                )

            await asyncio.sleep(WATCHDOG_INTERVAL)

    async def _watch(self, holder: _Holder) -> None:
        """While the pass is held — watch whether the user has appeared nearby.

        Any error inside is treated as a reason to give up the card. The watchdog is the
        safety mechanism itself, and if it goes blind, continuing to work blindly is worse
        than one more time giving up the card: an unknown state here means the risk of
        someone else's OOM, and that in ollama unloads all models at once and kills the
        user's session.
        """
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                state = await asyncio.to_thread(self.state)

                if state.busy:
                    log.info(
                        "giving up the card: processes %s, models %s",
                        state.foreign_pids,
                        state.foreign_models,
                    )
                    holder.preempt()

                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the GPU watchdog crashed — giving up the card just in case")
            holder.preempt()


class _Holder:
    """Handle of a held pass."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def preempted(self) -> bool:
        return self._event.is_set()

    def preempt(self) -> None:
        self._event.set()

    def check(self) -> None:
        """Raise Preempted if it's time to give up the card."""
        if self.preempted:
            raise Preempted("the user started their own work on the GPU")

    async def wait_preempted(self) -> None:
        await self._event.wait()


def _process_name(pid: int) -> str:
    """Short process name from /proc. An empty string if the process has already disappeared."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _is_descendant(pid: int, ancestor: int) -> bool:
    """Whether pid is a descendant of ancestor. Needed so as not to mistake our own ASR worker for someone else's."""
    seen = 0

    while pid > 1 and seen < 20:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                # The fourth field is PPID, but the process name in parentheses may
                # contain spaces, so we cut at the last closing parenthesis.
                fields = handle.read().rsplit(")", 1)[1].split()
                pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            return False

        if pid == ancestor:
            return True

        seen += 1

    return False
