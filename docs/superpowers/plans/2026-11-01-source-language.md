# Source language: the detector always runs, codes are validated, the cache remembers the decision — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks 1–11 — one per implementer; task 12 is manual, for the owner: do not hand it to an implementer, stop after task 11 and pass it to the owner.

**Goal:** the daemon correctly determines the speech language of any video and does not hide the decision behind the string `auto` in the cache key: the detector always runs, YouTube metadata is only a hint, language codes are validated, and the decision is visible to clients and lives in the cache together with the track.

**Architecture:** a new pure module `helper/language.py` (standard library only) holds the codes, the detector rule and the language decision; both the recognition worker and the daemon import it. The worker always measures the language over windows with speech, decides by the rule, composes the initial prompt for the decided language itself, and returns the decision together with the segment metrics. The daemon cuts off silence, repeats and prompt echo, and puts the decision on the job, into the snapshot and into the cache record. The cache key carries the language declared by a human, not the string `"auto"`; old tracks stop being found as soon as the key changes (task 7: without `target_lang`, `SCHEMA_VERSION` 2), and `PROMPT_VERSION` 2 (task 11) marks the change of the language rules.

**Tech Stack:** Python 3.14, FastAPI, faster-whisper 1.2.1 (batch mode), yt-dlp 2026.08.19, pytest + pytest-asyncio, httpx ASGITransport.

**Spec:** `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` — sections “4. Languages: any input, always one output”, “Language research: what turned out to be wrong and what actually breaks” (breakages 3–9 and “Decisions”), “What must be fixed right now”, “The second line: the owner's decision”, “Source language: an internal quantity, not a setting”. Breakages 1, 2 and 10 — the “Translation check” plan.

**Basis:** branch `source-language` from the tip of PR #1 (`ba274ea`) with `main` merged in (`98a3ad2`: 18+ videos and README). Line numbers are on `98a3ad2`; every reference also names the function — search by name: tasks change the same files, and the numbers will drift after the first tasks. PR #1 is not merged yet; if it is amended in response to review, the branch must be caught up by merging. The plan is committed to the branch in a separate commit before task 1. If steps are ticked in it, the line ` M docs/superpowers/plans/2026-11-01-source-language.md` in `git status` is not a defect; do not add the plan to task commits.

## Global Constraints

- `extension/` does not change by a single line. The protocol changes only by addition. There are no new `Stage` values. There are no new top-level keys in the snapshot and in `state` events named `source`, `message`, `waiting`, `retry`: `extension/content.js` reads them (lines 250, 364–370) and would rework the badge caption. The language decision lives only under the snapshot key `language` and in the new event `{"type": "language", ...}`; the extension skips unknown event types and snapshot keys.
- On output there are always the same two lines, Russian and Montenegrin; `targetLang` does not change the output.
- Language detection happens inside the same worker run under the same card lease: no second model load, no second lease, no model downloads and no second model in the card's memory.
- The semantics of the 18+ account in `JobRunner._fetch` do not change.
- Tests do not touch the network, the card or real cache paths; the `YT_TRANS_CACHE`/`YT_TRANS_CONFIG` isolation in `tests/conftest.py` stays first: `JobManager.__init__` wipes the paused-audio directory.
- `helper/language.py` imports only the standard library: both the daemon and the worker load it.
- In the worker, `--language` still defaults to `"sr"`: `helper/measure.py` relies on that.
- Russian in comments, log lines and refusals. Phrases that reach the badge are no longer than 60 characters, with the main point first.
- Commits: the message is in Russian, without `Co-Authored-By` lines and other signatures. Never use `--no-verify`. The pre-commit hook runs a diff review with a local model for up to 300 s — the command timeout is at least 360000 ms. Git — via `/usr/bin/git`.
- Tests: `.venv/bin/python -m pytest -q` (on `98a3ad2` — 143 passed).

## Language rules

- **A pinned language decides.** A `[asr] language` value other than `auto` goes to the decoder as is (`hr` and `bs` are not folded into `sr`); the detector still runs, and a mismatch is marked by comparison over the language family. This is what the design demands: a human declaration is “protection against wrong guessing” (“Source language: an internal quantity”), “an English channel is marked once, and after that recognition does not guess” (section 4), and with it the detector works “as a cross-check, not as a fallback” (“Decisions”). The “hint” from “Decisions” here is the YouTube label (`--hint`): if the hint were also a pin, the same window votes it protects against errors of would outweigh it.
- **An invalid pinned language does not bring the daemon down.** On settings load — `log.error` with a human phrase; every new job gets a `PermanentJobError` before the first request to YouTube and without a card lease, while videos cached without a pin still open: restore reads the invalid code as “not pinned”; those made under a previously valid pin are not found until the code is fixed.
- **A low-confidence decision is cached too.** Re-running recognition would cost the card on every view; the remedy is a visible mark on the wire and pinning the language by a human (applies to all videos and to the cache key).

## Decisions that diverge from the letter of the design

1. **The cache key carries the language declared by a human, not the effective one** (design, “Decisions”: “The effective language goes into the cache key… Lookup by the declared one, writing under the effective one”). The cache must answer before any work on the card (`helper/cache.py:4-5`), and the effective language appears only after the worker. When a human has pinned the language, the effective one matches it — the pin decides the decoder token — so “lookup by the declared one, writing under the effective one” holds literally. When nobody declared it, the key holds an empty string, and the detector's decision lies in the record and is visible on the wire. A detector error is fixed by a human pinning the language (a different key — a different record) or by a rule change with a `PROMPT_VERSION` bump. While there is no per-video declaration (“Deferred”), and the pin applies to all videos, a detector error on an unpinned video in a multilingual library lives in the cache until the next `PROMPT_VERSION` (a removed pin finds the record with `pin=""` again); without a declaration `mismatch` does not light up — only `effective`, `reason` and `confidence` are visible.
2. **YouTube metadata does not enter the key and does not outweigh the voting windows:** the Juske channel is labelled `en` while the speech is Serbian. The hint decides only when speech was found in no window.
3. **“A cue matching the prompt”** is understood as “consists only of the prompt's words” (`is_echo`): literal equality never occurred across 66 620 cached cues, and the recorded echo is fragments of the prompt phrase and the list of names. The prompt's words alone are not enough: among them there must be an uncommon word of the phrase or of the names intro (for `sr` — transkript, snimka, pominju) or two different names from the list, not counting the common words of the phrase (the title “OVO JE …” makes OVO a name too). Otherwise one name from the title in a common phrase (“Šta je ovo?” under the title “ŠTA SE KRILO…”, “Ovo je Beograd.”) would go out as echo. “Segments are filtered by fields” is also not to be taken literally: “silence” throws the segment away entirely, while “repeat” only marks it, and inside it only the looping sentences are thrown away — with their own compression ratio (the library's formula) above the threshold or repeating what has already been said in this segment. A batch-mode segment is a piece of speech up to 30 s, and a loop in the middle of it would otherwise carry away the dialogue before it (in the owner's cache — in 20 videos out of 163).
4. **The author's track in any language and the thirty-second probe are deferred** (design, “Decisions”: “The author's track opens for any language… on a single thirty-second probe”). There is no cheap detector without the card on this machine (among the downloaded models there are only large-v3 and turbo), a probe on the card contradicts “releases the card too”, and taking a non-Serbian track without a probe means bringing back the Juske case, where the author's English translation would become the source, that is, a translation of a translation. In this plan track labels are normalised, the Serbian track is taken under any spelling of the label, and a pinned non-Serbian language forbids the Serbian track. The conditions for the next step are in “Deferred”. The reverse case remains, as on 98a3ad2: an English video with a Serbian translation by the author and without a pin takes that translation as the source. That is visible only when YouTube gave a language label: `mismatch` (“YouTube indicated en, author track sr”) and a warning in the log; without a label — not at all. Only a probe can close it (“Deferred”).
5. **The translator prompt does not change.** The working path is `translate_dual` with `montenegrin.SYSTEM_PROMPT`, which does not name the source language; the line “from the Serbian language to Russian” (`translate.py:83`) lives in the dormant single-line path.

## Deferred

- **The author's track in any language with a probe.** Conditions: a non-Serbian track is not selected without a probe verdict; before the probe, all candidate fingerprints and `asr` are looked up in the cache, in restore order; the probe has a cancel handle like `_asr_proc` in recognition; a window without speech gives `verified: false`, not a verdict; the choice of detector (a CPU model, its download and memory) is by timing on this machine.
- **Language declaration per video and per link.** Read only through `JobRunner._declaration(job)`; the slot in the key already exists (`CacheKey.pin`). Consequence for the registry: the job key `(video_id, use_author_track)` will hand out a finished job made under a different declaration — this must be taken into account when the field is introduced. The exceptions are the pin check at the start of `_pipeline` and `JobManager._key_for`: restore has no job, it reads `[asr] language` through `hint` and changes together with `_declaration`.
- **Visible marks “alignment not checked” and “second line not certified”** — a readiness condition for the tablet page. Language-independent protection against drifting cues, compliance with the reviewers' verdict, a typed second line, `montenegrin.check` only for sr/cnr — the “Translation check” plan.
- **The tablet page side** (design, “The second line: the owner's decision”, “Source language: an internal quantity”): the language decision on screen; the original audio track in the mux (the same `-S lang,+size` ordering, readiness — the track language equals `language.effective`); hiding the second line only when its language matches the first, including on a source in Russian or Montenegrin. For them the daemon provides `language.effective`, `mismatch` and `note`; the `lang` of the second line — “Translation check”.
- **Line wrapping at conjunctions of other languages** (`CONJUNCTIONS` in `helper/segment.py`) — cosmetics inside a cue, does not affect the cache.
- **Deleting old cache records** (about 18 MB after the version change) — the owner's decision.
- **`has_automatic_captions`** deliberately remains unread: it is the same YouTube recognition track as `audio_language`, that is, a second signal from the same source.
- **Other places where paused audio is deleted** (`helper/jobs.py`, around lines 922, 545, 770) and **the orphaned yt-dlp on cancel** — unchanged.
- **Extra VAD passes in the detector**: on a file without speech — one extra pass over the whole file (about 13 s on a three-hour one). The speech of a window could be counted once and `detect_language` called with `vad_filter=False`.
- **“Task exception was never retrieved … Preempted” in the log** on every cancel during recognition: `_run_asr_watched` does not await `work` after `CancelledError`. It existed before this plan too; noise, not a breakage.

## Review Focus

- A video with almost no speech (a clip, a quiet intro): no window votes → one extra pass → hint → `"sr"` with reason `fallback`; an empty string never goes to Whisper, the worker does not crash. Test in task 5.
- Audio from 30 to 37.5 s long: window offsets do not go negative; audio under 30 s — one window the same way. Test in task 5.
- A pinned language with a region, script or old code (`sr-Latn`, `SR`, `sh`, `cnr`) is normalised; garbage (`serbian`) refuses the job once, without a recreation loop, while videos cached without a pin still open: restore reads the invalid code as “not pinned”; those made under a previously valid pin are not found until the code is fixed. Tests in task 4 (refusal before YouTube and without recreation) and in task 7 (an invalid pin on restore is read as “not pinned”, the cached video opens).
- A cache record without the `language` field (corrupted or edited by hand) or with `language: null` is served with `language: null` without crashing; a record without cues is not served. Test in task 7.
- Russian, Ukrainian or Macedonian speech whose Serbian-family share has crossed the threshold: the language stays its own (the Slavic-neighbours guard). Test in task 5.

---
### Task 1: Cancellation leaves no zombies

Today `Job.cancel()` first wipes the paused audio and only then kills
recognition and cancels the task. `discard_audio` clears the handle and calls
`path.unlink(missing_ok=True)`, and `missing_ok` swallows only ENOENT: a disk error
(EACCES, EPERM, EROFS, EIO) escapes from `cancel()` before `kill_asr()`. `JobManager._remove`
by that moment has already taken the job out of the registry. A resumed job in the middle of recognition
thus keeps the card, but already without an owner: Whisper runs to the end, the job takes the lease
for the translator again, writes the track to the cache and ends DONE. The loop in `drop` breaks on it and
does not reach the neighbouring variant of the video. `/drop` answers 500. The same error in the audio reset
right after recognition (`_pipeline`, line 928) throws away the finished transcript: the job
fails with a retryable refusal before `job.cues` are assigned, and a minute later recognizes
the video again.

**Files:**
- Modify: `helper/jobs.py:555-565` (`Job.cancel`), `helper/jobs.py:926-928` (`JobRunner._pipeline`: audio reset after recognition), `helper/jobs.py:1781-1784` (comment in `JobManager._evict`)
- Modify: `tests/conftest.py:3-12` (imports), after the `clock` fixture (`:81-100`) — the `audio_dir` and `refuse_in_audio_dir` fixtures
- Modify: `tests/test_limits.py:301-312` (the `audio_dir` fixture moves to conftest), `tests/test_limits.py:472-488` (the `refuse_in_audio_dir` function moves to conftest), `tests/test_limits.py:491-494` and `:510` (`test_disk_errors_in_audio_housekeeping_do_not_fail_a_request` takes the fixture)
- Test: `tests/test_drop.py` (imports `:4-7`; three tests at the end of the file)

**Interfaces:**
- Consumes: the `manager` and `client` fixtures from `tests/conftest.py`.
- Produces:
  - fixture `audio_dir(manager) -> Path`: this is `helper.jobs.PAUSED_AUDIO_DIR`, created by the start of the test and removed afterwards (`shutil.rmtree(..., ignore_errors=True)`). Moved from `tests/test_limits.py`, behaviour the same.
  - fixture `refuse_in_audio_dir(monkeypatch) -> Callable[[str], None]`. A call `refuse_in_audio_dir("unlink")` (or `"stat"`, `"iterdir"`) replaces this method of `pathlib.Path` until the end of the test. For a path that is `PAUSED_AUDIO_DIR` itself or lies directly in it, the method raises `PermissionError(errno.EACCES, "Permission denied", str(path))`, for other paths it works as usual. It used to be the function `refuse_in_audio_dir(monkeypatch, method)` in `tests/test_limits.py`. A factory fixture, because the replacement needs the test's `monkeypatch`; simple conftest helpers (like `metadata` from task 3) are taken by tests with `from conftest import …` — this works in the default import mode, which `pytest.ini` does not change.
  - `Job.cancel(self) -> None` no longer raises `OSError`. The order is: `self._audio_dead = True`, then `self.kill_asr()`, then `self._task.cancel()` (if there is a task), and only then `self.discard_audio()` under `except OSError as exc: log.warning("звук %s не убран при сбросе: %s", self.video_id, exc)`.
  - `JobRunner._pipeline` after successful recognition resets the audio under `except OSError as exc: log.warning("звук %s не убран после распознавания: %s", job.video_id, exc)` and moves on: the cues are assigned, the job goes on to translation.

- [ ] **Step 1: Move `audio_dir` and `refuse_in_audio_dir` into `tests/conftest.py`**

To the imports of `tests/conftest.py` add, keeping alphabetical order, `import errno` and
`from pathlib import Path` (standard library, importing at the top is safe). `helper.*`
is still imported only inside fixtures. Right after the `clock` fixture insert:

```python
@pytest.fixture
def audio_dir(manager):
    """The paused-audio directory: clean at the start of the test, removed at the end.

    After manager, not before: JobManager() wipes this directory when created.
    It has to be removed even for sparse multi-gigabyte files: they take no space, but
    they would remain in the cache directory until the end of the run.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    yield PAUSED_AUDIO_DIR

    shutil.rmtree(PAUSED_AUDIO_DIR, ignore_errors=True)


@pytest.fixture
def refuse_in_audio_dir(monkeypatch):
    """Factory refuse(method): a Path method inside the paused-audio directory fails with permissions.

    The failure is the same as a real directory without permissions: PermissionError with the path name.
    Directory permissions themselves are not good for the test: under root nobody checks them, and the test
    would pass where it checked nothing. Paths outside the directory work as usual, so that
    the replacement touches neither the rest of the daemon nor pytest. The replacement lasts until the end of the test.
    """
    from helper.jobs import PAUSED_AUDIO_DIR

    def refuse(method: str) -> None:
        original = getattr(Path, method)

        def refusing(self, *args, **kwargs):
            if PAUSED_AUDIO_DIR in (self, self.parent):
                raise PermissionError(errno.EACCES, "Permission denied", str(self))

            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, refusing)

    return refuse
```

In `tests/test_limits.py`:
- delete the `audio_dir` fixture (301-312) and the function `refuse_in_audio_dir` (472-488);
- in `test_disk_errors_in_audio_housekeeping_do_not_fail_a_request` (491-494) add the parameter `refuse_in_audio_dir` after `caplog`: `(manager, audio_dir, clock, monkeypatch, caplog, refuse_in_audio_dir, method)`. Replace the call on line 510 `refuse_in_audio_dir(monkeypatch, method)` with `refuse_in_audio_dir(method)`;
- keep the imports `errno`, `shutil`, `Path` and `PAUSED_AUDIO_DIR`: they are used by `test_evict_without_a_paused_audio_directory_is_quiet` and `test_audio_that_cannot_be_removed_still_counts_against_the_budget`.

- [ ] **Step 2: Make sure the move broke nothing**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 143 passed

- [ ] **Step 3: Write the failing tests**

The imports of `tests/test_drop.py` (4-7) become:

```python
import logging

import numpy as np
import pytest

from helper import jobs
from helper.jobs import PLAYHEAD_TTL, Stage
```

At the end of the file:

```python
# ── dropping when the paused audio cannot be erased ──────────────────────


class LiveTask:
    """A job's task that is still running: it remembers cancellation, done() answers after it."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> bool:
        self.cancelled = True

        return True

    def done(self) -> bool:
        return self.cancelled


class RunningWhisper:
    """A recognition process that is still running: poll() is None until it is killed."""

    def __init__(self) -> None:
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True


def resumed_in_asr(job):
    """A resumed job in the middle of recognition: paused audio on disk, the task and Whisper alive.

    Only such a one holds both the card and the audio: on the first pass the audio is in memory, and a pause has
    neither a task nor a process.
    """
    job.stage = Stage.ASR
    job.stash_audio(np.zeros(1000, dtype="<f4"))
    job._task = LiveTask()
    job._asr_proc = RunningWhisper()

    return job._audio_path


def test_cancel_frees_the_card_even_if_the_paused_audio_stays(
    manager, audio_dir, refuse_in_audio_dir, caplog, monkeypatch
):
    job = manager.get_or_create(VIDEO, "ru", True)
    path = resumed_in_asr(job)
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")
    # What held the card at the moment the drop reached the audio.
    seen = []
    discard = job.discard_audio

    def watched():
        seen.append((job._asr_proc.killed, job._task.cancelled))
        discard()

    monkeypatch.setattr(job, "discard_audio", watched)

    job.cancel()

    # The process and the task hold the card, and the drop takes both off before the audio, whatever
    # happens to it: otherwise a job the registry has already forgotten would go on recognizing and translating.
    assert seen == [(True, True)]
    assert job._asr_proc.killed
    assert job._task.cancelled
    # The audio was not erased and stayed without an owner; the disk failure is a log line, not an exception.
    assert job._audio_path is None
    assert path.exists()
    assert any(
        str(path) in record.getMessage() and "Permission denied" in record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    )


def test_drop_with_undeletable_audio_still_stops_every_variant(
    manager, audio_dir, refuse_in_audio_dir
):
    steady = manager.get_or_create(VIDEO, "ru", False)
    steady._task = LiveTask()
    stuck = manager.get_or_create(VIDEO, "ru", True)
    resumed_in_asr(stuck)
    # Touched last: jobs_for hands it to the drop first, and a failure on it must not
    # leave the neighbouring variant uncancelled. Explicitly, not by the clock: two calls in a row
    # could get one reading, and the insertion into the registry would decide the order.
    stuck.touched_at = steady.touched_at + 1.0
    refuse_in_audio_dir("unlink")

    assert manager.jobs_for(VIDEO)[0] is stuck
    assert manager.drop(VIDEO) is True
    assert manager.jobs_for(VIDEO) == []
    assert stuck._asr_proc.killed
    assert stuck._task.cancelled
    assert steady._task.cancelled


async def test_drop_endpoint_answers_even_if_the_paused_audio_cannot_be_removed(
    client, manager, audio_dir, refuse_in_audio_dir
):
    job = manager.get_or_create(VIDEO, "ru", True)
    resumed_in_asr(job)
    refuse_in_audio_dir("unlink")

    response = await client.post(f"/v1/jobs/{VIDEO}/drop")

    # The real server answered 500 to an exception from here; the tests' ASGITransport
    # hands it to the test itself.
    assert response.status_code == 200
    assert response.json() == {"dropped": VIDEO, "cancelled": True}
    assert job._asr_proc.killed
    assert job._task.cancelled
```

- [ ] **Step 4: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: FAIL, 3 failed, 22 passed. All three new tests fail on
`PermissionError: [Errno 13] Permission denied: '/tmp/yt-trans-cache-…/paused-audio/abcdefghijk-….f32'`,
raised in `Job.discard_audio` (`helper/jobs.py:553`) from `Job.cancel`. In the first test it is
thrown by `job.cancel()`, in the second by `manager.drop`, in the third by `client.post`:
`httpx.ASGITransport` by default hands the application's exception to the test, while the real server
would answer 500 (checked with `raise_app_exceptions=False`).

- [ ] **Step 5: Reorder the drop in `Job.cancel` and guard both audio resets**

`helper/jobs.py:555-565`, `Job.cancel` in full:

```python
    def cancel(self) -> None:
        # A paused job has no task of its own — it has already finished
        # with a pause, — and nobody but this place can remove its audio. The flag is set
        # before the handle is cleared, and the writer reads it after the handle has been set:
        # this way a file written after the drop does not remain ownerless either.
        self._audio_dead = True

        # First what holds the card — the recognition process and the task — and only
        # then the audio. The registry has already forgotten the dropped job (JobManager._remove), and
        # a disk error escaping from here earlier left it running without an owner:
        # Whisper on the card, then translation and a cache write, while /drop answered 500.
        self.kill_asr()

        if self._task:
            self._task.cancel()

        # Audio that was not erased stays on disk without an owner: discard_audio clears the handle
        # before deleting. The budget (_trim_paused_audio) will remove it when the audio exceeds
        # PAUSED_AUDIO_MAX_BYTES, or a daemon restart will. The drop does not depend on it.
        try:
            self.discard_audio()
        except OSError as exc:
            log.warning("звук %s не убран при сбросе: %s", self.video_id, exc)
```

`helper/jobs.py:926-928`, `JobRunner._pipeline`: replace the line `job.discard_audio()` after
the comment “Recognized — the paused audio is no longer needed…” with:

```python
            # A disk error here is no reason to throw away the finished transcript: the file
            # stays without an owner, as in cancel, and the job moves on.
            try:
                job.discard_audio()
            except OSError as exc:
                log.warning("звук %s не убран после распознавания: %s", job.video_id, exc)

```

The two-line comment above it stays. This guard is not covered by a test here: line 928
is only reached by the real pipeline, and the harness for it appears in task 3. There it is checked by
`test_a_resumed_asr_keeps_its_transcript_when_the_paused_audio_stays`. On the 98a3ad2 code this
test fails: the job dies on this line, and it never gets to translation.

`helper/jobs.py:1781-1784`, `JobManager._evict`: the last sentence of the comment, “Audio that
was not removed will remain until the next sweep.”, is wrong. `discard_audio` clears the handle before
deleting, and the next sweep will not come back to such a file. Replace the whole paragraph with:

```python
        # A disk failure here does not drop the request. _evict runs at the end of get_or_create (a poll
        # with create=true, watch, POST /v1/jobs) and restore, when the job has already been created and a
        # new one started too, — an exception from here would turn the answer about a running
        # job into a 500. Audio that was not removed stays on disk without an owner: discard_audio
        # clears the handle before deleting, and the next sweep will not come back to it. Such a file
        # will be removed by the budget (_trim_paused_audio) when the audio exceeds
        # PAUSED_AUDIO_MAX_BYTES, or by a daemon restart.
```

Do not touch: `JobManager._remove` (remove from the registry first, then `cancel`), `drop`,
`drop_job` in `helper/app.py:311-324`, `discard_audio` (the handle is still cleared before
deleting). `kill_asr()` does not raise (`Popen.kill` swallows `ProcessLookupError` itself),
`Task.cancel()` does not either. The late drop from `finally` in `JobRunner.run` (770) does nothing
after that: the handle is already gone.

- [ ] **Step 6: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: PASS, 25 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 146 passed

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add helper/jobs.py tests/conftest.py tests/test_limits.py tests/test_drop.py
/usr/bin/git commit -m "Сброс задания сначала снимает распознавание и задачу, а звук паузы, что не стёрся, — строка в журнале, а не 500 и не зомби на карте"
```

Each git command — in a separate Bash call, via `/usr/bin/git`; timeout at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message in Russian, without `Co-Authored-By` lines and other signatures.

---

### Task 2: The path to the paused audio is read once

`stash_audio` and `take_audio` run in a worker thread (`asyncio.to_thread`, `_pipeline`
894 and 919). The audio budget cleanup (`JobManager._trim_paused_audio` → `owner.discard_audio()`
on every `get_or_create` and `restore`) runs in the event loop and can clear `_audio_path`
between two reads. The second read then yields `None.exists()`. That is an `AttributeError`, not
`OSError`, so the job fails in `JobRunner.run` with a retryable refusal (`except Exception`),
instead of taking the audio or downloading it again. `stash_audio` reads the handle twice
(line 507), `take_audio` three times (538 and 542).

**Files:**
- Modify: `helper/jobs.py:497-528` (`Job.stash_audio`, reads on 507), `helper/jobs.py:530-547` (`Job.take_audio`, reads on 538 and 542). Task 1 does not shift these lines: its edits start at 555.
- Test: `tests/test_limits.py` (at the end of the file, after `test_a_handle_cleared_between_two_reads_does_not_fail_a_request`, 564-586). After task 1 `tests/test_limits.py` is shorter: search by name.

**Interfaces:**
- Consumes: the `audio_dir` fixture (task 1); `stash(job, mtime)` and `CHUNK` from `tests/test_limits.py` (315-321 and 297).
- Produces: `Job.stash_audio(self, audio) -> None` and `Job.take_audio(self) -> np.ndarray | None` read `self._audio_path` exactly once, into the local `path`, and work only with it from then on. The `except OSError` branch in `take_audio` still calls `self.discard_audio()`.

- [ ] **Step 1: Write the failing tests**

At the end of `tests/test_limits.py`:

```python
def handle_gone_after_first_read(monkeypatch, path) -> None:
    """The job's handle sees the file only on the first read, after that it is None.

    This is what the budget cleanup looks like (_trim_paused_audio → discard_audio in the event loop)
    when it cleared the handle while take_audio or stash_audio were running in a worker thread. A property on
    the class shadows the instance field, writes into it are swallowed.
    """
    reads = []

    def handle(self):
        reads.append(self)

        return path if len(reads) == 1 else None

    monkeypatch.setattr(
        jobs.Job, "_audio_path", property(handle, lambda self, value: None), raising=False
    )


def test_take_audio_survives_a_handle_cleared_between_two_reads(manager, audio_dir, monkeypatch):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    handle_gone_after_first_read(monkeypatch, path)

    # The handle is read once: the file that was in place at the check is the one read. The second
    # read after the clear would fail with AttributeError, and the resume would fail with a
    # retryable refusal instead of taking the audio or downloading it again.
    assert job.take_audio().size == CHUNK


def test_stash_audio_survives_a_handle_cleared_between_two_reads(
    manager, audio_dir, monkeypatch
):
    job = manager.get_or_create("video00000a", "ru", True)
    path = stash(job, mtime=1000)
    handle_gone_after_first_read(monkeypatch, path)

    job.stash_audio(np.zeros(CHUNK, dtype="<f4"))

    # The file was caught on the first and only read of the handle, and the audio is not written a second time.
    assert list(audio_dir.iterdir()) == [path]
```

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: FAIL, 2 failed, 34 passed:
`test_take_audio_survives_a_handle_cleared_between_two_reads` fails on
`AttributeError: 'NoneType' object has no attribute 'exists'` in `helper/jobs.py:538`,
`test_stash_audio_survives_a_handle_cleared_between_two_reads` — on the same one in
`helper/jobs.py:507`.

- [ ] **Step 3: Read the handle once**

`Job.stash_audio`: replace the check on 507 with

```python
        # Read the handle once: the write runs in a worker thread, and the budget cleanup in the
        # event loop may clear it between the None check and the file check.
        path = self._audio_path

        if path is not None and path.exists():
            return
```

The rest of the body (`mkstemp`, `self._audio_path = Path(name)`, the write, the `_audio_dead` check)
does not change.

`Job.take_audio`: right after `self.touched_at = time.monotonic()` read
`path = self._audio_path` with the comment “Once, for the same reason as in stash_audio:
the read runs in a worker thread.”. Make the check on 538 `if path is None or not path.exists():`,
the read on 542 — `np.fromfile(path, dtype="<f4")`. The `except OSError` branch stays as is.

- [ ] **Step 4: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: PASS, 36 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 148 passed

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add helper/jobs.py tests/test_limits.py
/usr/bin/git commit -m "Ручка отложенного звука читается один раз: уборка бюджета посреди take_audio и stash_audio больше не роняет задание"
```

Each git command — in a separate Bash call, via `/usr/bin/git`; timeout at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message in Russian, without `Co-Authored-By` lines and other signatures.

---

### Task 3: The pipeline harness

Today no test runs `JobRunner.run` / `_pipeline` and `helper.asr.worker.transcribe`,
yet tasks 4–10 change exactly them. The harness appears here: the real pipeline on fakes of
the network, the card and ollama, with its own cache for each test, and the real worker on a fake
faster_whisper. The harness tests prove that the real code works. The chain of `state`
stages is emitted only by `_pipeline` itself. argv for the worker is assembled by the real `_run_asr`, and its reply
is parsed by the real code. The lease is taken in the real order. The track lands in the
real `TrackCache` and is read from disk. On the worker side `transcribe` builds the model with
`device="cuda"`, passes the language to the batch recognizer and cleans the segment text; the detector
windows are checked by task 5 with its own tests. Today's language behaviour is not pinned by the tests:
neither the worker flags, nor the cache key, nor the initial prompt, which the following tasks change.
All their checks stay valid after tasks 4–10, so they will not have to be flipped later.

**Files:**
- Modify: `tests/conftest.py` (to the imports — `contextlib`, `json`, `subprocess`, `threading`, `types`, `numpy as np`; everything else at the end of the file, after the `connection` fixture)
- Test: `tests/test_harness.py` (imports at the top, five tests at the end)

**Interfaces:**
- Consumes: the `manager` fixture; `audio_dir` and `refuse_in_audio_dir` (task 1); the guard on the audio reset after recognition (task 1), checked by the third test.
- Produces. Everything in `tests/conftest.py`; `helper.*` there is still imported only inside functions.
  - **Function `metadata(**overrides) -> VideoInfo`** — a regular module function, not a fixture: tests import it with `from conftest import metadata`. The import works because `pytest.ini` does not set `--import-mode`, and `tests/` has no `__init__.py`: in the default mode (`prepend`) pytest puts `tests/` at the start of `sys.path` and loads conftest under the name `conftest`, and `from conftest import …` gets the same already-loaded module from `sys.modules['conftest']`, not a second instance. Any `VideoInfo` field is overridden by name; `manual_tracks` accepts both strings and ready-made `CaptionTrack`.
  - **Fixture `pipeline(manager, monkeypatch) -> Pipeline`.** In `helper.jobs`, `probe`, `audio_pcm`, `fetch_manual_track`, `translate_dual`, `translate_batch`, `FETCH_BACKOFF = (0.0, 0.0)` and `subprocess` are replaced: a namespace with `Popen`, which `FakeWorker` builds, and the real `PIPE` and `TimeoutExpired`. yt-dlp and asyncio see the real `subprocess`. Left real: `usable_manual_track`, `normalize_cues`, `build_cues`, `validate_dual`, the whole `JobRunner`.
    - Each test has its own cache. The directory `<tmp> = tempfile.mkdtemp(prefix="pipeline-", dir=helper.env.CACHE_DIR)`, `helper.cache.CACHE_DIR` is replaced with it before the constructor, so `index.sqlite` is its own too. The store is the real `TrackCache(root=<tmp>/tracks)`. For the duration of the test `manager.cache` points to the same object, so `manager.restore` reads this cache. After the test the previous cache is returned before the `manager` fixture closes its index; the test's index is closed, `<tmp>` is deleted.
    - The scenario (`info`, `audio`, `track`, `asr_result`, `asr_exit`, `asr_stderr`, `gate`) is set by the test before `run`, the records (`calls`, `names()`, `workers`, `worker_started`, `lease`, `cache`, `runner`, `config`) are read after; names, defaults and records — as in the step 3 code (docstrings of `Pipeline`, `FakeWorker`, `FakeLease`). From task 5 the worker argv is parsed by its own parser: `worker.build_parser().parse_args(pipeline.workers[-1].args[3:])`.
    - Methods:
      - `configure(**asr) -> None` assembles `Config(asr=AsrConfig(**asr), translator=TranslatorConfig())` and a new `JobRunner(lease, cache, config)`. The registry gets the same `Config` and the same runner into `manager.config` and `manager.runner`, so restore computes the key by the same setting. The lease record starts over: right after the call `lease.calls == [("claim", "qwen3.8:27b")]`. The `Pipeline` constructor has already called `configure()` without arguments. The `language` value goes into `AsrConfig` as is, past `config.load()`.
      - `async run(job) -> None` — this is `await runner.run(job)`, the real `JobRunner.run` → `_pipeline`.
    - How to use it. A job is created with `job = manager.get_or_create("abcdefghijk", "ru", True)`: it is in the registry but not started, because `Job.start` of the `manager` fixture only records. One can also use `jobs.Job("abcdefghijk")` outside the registry, then `manager.restore` goes to the cache. Then `await pipeline.run(job)`. Resume after a pause: `job.info = metadata()`, `job.stage = Stage.PAUSED`, `job.stash_audio(pipeline.audio)`, with the `audio_dir` fixture. Preemption in the middle of recognition: `pipeline.gate = threading.Event()`, launch via `asyncio.create_task(pipeline.run(job))`, then `await asyncio.to_thread(pipeline.worker_started.wait, 5)` and `pipeline.lease.holders[-1].preempt()`.
  - **Fixture `whisper(monkeypatch) -> FakeWhisper`.** It first imports `helper.env` (HF_HOME before faster_whisper, as in the worker), then replaces exactly two names: `faster_whisper.WhisperModel` with `whisper.build_model` and `faster_whisper.BatchedInferencePipeline` with `whisper.build_pipeline`. `helper.asr.worker.transcribe` and `_detect` stay real, and so does VAD: the fixture does not touch `faster_whisper.vad.get_speech_timestamps`, and the real Silero runs on the CPU, without network and card (about 0.1 s for five seconds of silence; in zeros it finds no speech). The replacement reaches only code that takes these names from the module at call time, the way `transcribe` takes the classes.
    - The scenario (`segments`), records (`model`, `kwargs`) and the builder `segment(start, end, text, *, ...)` with segment metrics — names, defaults and records as in the step 3 code (docstrings of `FakeWhisper` and `segment`).

- [ ] **Step 1: Write the failing tests**

The beginning of `tests/test_harness.py` (currently it only has `from pathlib import Path`):

```python
import asyncio
import logging
import sys
import threading
from pathlib import Path

import numpy as np

from conftest import metadata
from helper.jobs import Stage

VIDEO = "abcdefghijk"
```

The `Stage` import inside `test_manager_records_a_resume_without_launching_it` can be left alone.
At the end of the file:

```python
# ── the pipeline on fakes ────────────────────────────────────────────────


async def test_pipeline_runs_an_asr_video_to_done(manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    # The real _pipeline: the same stages the viewer sees, and not a single trip to the network.
    assert job.stage is Stage.DONE
    assert [event["stage"] for event in job.events if event["type"] == "state"] == [
        "probing", "queued", "fetching", "asr", "translating", "done",
    ]
    assert pipeline.names() == ["probe", "audio", "asr", "translate"]
    # The worker was started by the real _run_asr with the job's audio, its reply parsed by the real code.
    worker = pipeline.workers[-1]
    assert worker.args[:3] == [sys.executable, "-m", "helper.asr.worker"]
    assert worker.input == pipeline.audio.tobytes()
    assert [(cue.src, cue.lines, cue.study) for cue in job.cues] == [
        ("Dobar dan.", ["ru Dobar dan."], "cnr Dobar dan."),
    ]
    # Card discipline: the translator is unloaded before the lease for Whisper, the phases take it in turn.
    assert [call[0] for call in pipeline.lease.calls] == [
        "claim", "unload", "acquire", "release", "acquire", "release",
    ]
    # The track landed in the test's real cache and is read from disk.
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["asr"]
    assert [cue["src"] for cue in pipeline.cache.variants(VIDEO)[0]["cues"]] == ["Dobar dan."]


async def test_pipeline_takes_a_serbian_author_track_without_asr(manager, pipeline):
    pipeline.info = metadata(manual_tracks=["sr"])
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.source == "manual:sr"
    assert pipeline.calls[:2] == [("probe", VIDEO), ("track", VIDEO, "sr")]
    assert pipeline.names() == ["probe", "track", "translate"]
    assert pipeline.workers == []
    # Without recognition the card is needed only by the translator.
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "acquire", "release"]
    assert [key.track_fingerprint for key in pipeline.cache.puts] == ["manual:sr"]
    assert [cue.src for cue in job.cues] == ["Zdravo svima."]


async def test_a_resumed_asr_keeps_its_transcript_when_the_paused_audio_stays(
    manager, pipeline, audio_dir, refuse_in_audio_dir, caplog
):
    job = manager.get_or_create(VIDEO, "ru", True)
    # A pause after the audio download: metadata present, audio stashed on disk, no cues.
    job.info = metadata()
    job.stage = Stage.PAUSED
    job.stash_audio(pipeline.audio)
    path = job._audio_path
    refuse_in_audio_dir("unlink")
    caplog.set_level(logging.WARNING, logger="helper.jobs")

    await pipeline.run(job)

    # The resume took the paused audio instead of downloading it again.
    assert pipeline.names() == ["asr", "translate"]
    assert pipeline.workers[-1].input == pipeline.audio.tobytes()
    # The audio was not erased, but what was recognized was not thrown away: the job reached the end and the cache.
    assert job.stage is Stage.DONE
    assert [cue.src for cue in job.cues] == ["Dobar dan."]
    assert len(pipeline.cache.puts) == 1
    assert job._audio_path is None
    assert path.exists()
    assert any(
        str(path) in record.getMessage()
        for record in caplog.records
        if record.name == "helper.jobs" and record.levelno == logging.WARNING
    )


async def test_preemption_during_asr_kills_the_worker_and_keeps_the_audio(
    manager, pipeline, audio_dir
):
    pipeline.gate = threading.Event()
    job = manager.get_or_create(VIDEO, "ru", True)
    running = asyncio.create_task(pipeline.run(job))

    # The worker is running and holding the card until it is released; the watcher sees the user.
    assert await asyncio.to_thread(pipeline.worker_started.wait, 5)
    pipeline.lease.holders[-1].preempt()
    await asyncio.wait_for(running, 5)

    assert pipeline.workers[-1].killed
    assert job.stage is Stage.PAUSED
    assert job.cues == []
    assert job._audio_path.exists()
    assert pipeline.cache.puts == []
    assert [call[0] for call in pipeline.lease.calls] == ["claim", "unload", "acquire", "release"]


# ── the worker on a fake faster_whisper ──────────────────────────────────


def test_whisper_fixture_runs_the_real_worker_transcribe(whisper):
    from helper.asr import worker

    whisper.segments = [whisper.segment(0.0, 1.5, " Dobar dan. ")]

    result = worker.transcribe(np.zeros(5 * worker.SAMPLE_RATE, dtype="<f4"), language="sr")

    # Only the model and the batch recognizer are replaced; everything around them is the real transcribe.
    assert (whisper.model.name, whisper.model.device, whisper.model.compute_type) == (
        "large-v3", "cuda", "float16",
    )
    assert (whisper.kwargs["language"], whisper.kwargs["task"]) == ("sr", "transcribe")
    assert (result["language"], result["model"], result["duration"]) == ("sr", "large-v3", 5.0)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in result["cues"]] == [
        (0.0, 1.5, "Dobar dan."),
    ]
```

Why these checks will not flip in tasks 4–10. The stage chain, the lease order
(`claim, unload, acquire, release, acquire, release`) and `names()` are not changed by any task:
the plan introduces no new stages, and the detector runs inside the same worker run under the same
lease. The `language` event from task 6 has a type other than `state`. The default reply without
`verdict` takes the fallback branch of `_accept_asr`: the decision is built from `result["language"]`.
After task 5 `transcribe(..., language="sr")` always detects the language, but the pinned code decides.
In five seconds of zeros the real VAD finds no speech, and the model's `detect_language` is not called at all.
The worker test does not read `result["detected"]` and `result["confidence"]`: task 5 moves them under
`result["verdict"]`. The new cue fields and the task 8 flags are not read by the tests.

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: FAIL — a collection error in `tests/test_harness.py`, not a single test run:
`ImportError: cannot import name 'metadata' from 'conftest'` and
`Interrupted: 1 error during collection`.

- [ ] **Step 3: Set up the harness**

To the imports of `tests/conftest.py` add, keeping the order: `import contextlib`, `import json`,
`import subprocess`, `import threading`, `import types` (to the standard ones) and `import numpy as np`
(after `import httpx`). At the end of the file, after the `connection` fixture, append the code below. It is
the contract on which tasks 4–10 write their tests, so transfer the names, defaults and records exactly.

```python
# ── the pipeline on fakes ────────────────────────────────────────────────


def metadata(**overrides):
    """VideoInfo of the video abcdefghijk: all fields at their defaults, overrides — fields by name.

    A regular function, not a fixture: tests take it with `from conftest import metadata`.
    manual_tracks also accepts strings: "sr" is CaptionTrack(lang="sr", url="",
    ext="json3", name=""); a ready-made CaptionTrack goes as is.
    """
    from helper.sources.ytdlp import CaptionTrack, VideoInfo

    tracks = [
        track
        if isinstance(track, CaptionTrack)
        else CaptionTrack(lang=track, url="", ext="json3", name="")
        for track in overrides.pop("manual_tracks", [])
    ]
    fields = {
        "video_id": "abcdefghijk",
        "title": "Naslov",
        "description": "",
        "channel": "Kanal",
        "tags": [],
        "chapters": [],
        "duration": 60.0,
        "audio_language": None,
        "live_status": None,
        "has_automatic_captions": False,
    }

    return VideoInfo(**{**fields, **overrides}, manual_tracks=tracks)


class FakeLease:
    """A card lease without NVML and ollama: records calls and yields the real _Holder.

    calls in order: ("claim", model), ("unload", model), ("acquire", MiB, model),
    ("release", MiB). holders — the handed-out handles; holders[-1].preempt() — what the
    watcher would have done on seeing a user on the card.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.holders: list = []

    def claim_model(self, name: str) -> None:
        self.calls.append(("claim", name))

    def unload(self, model: str) -> None:
        self.calls.append(("unload", model))

    @contextlib.asynccontextmanager
    async def acquire(self, need_mib: int, *, model: str | None = None, wait: bool = True):
        from helper.gpulease import _Holder

        holder = _Holder()
        self.calls.append(("acquire", need_mib, model))
        self.holders.append(holder)

        try:
            yield holder
        finally:
            self.calls.append(("release", need_mib))


class RecordingCache:
    """The real TrackCache that remembers keys: with which one it looked up (gets) and wrote (puts).

    It writes for real, into the test directory, and variants() reads what was written from disk. Everything
    except get and put goes to it as is.
    """

    def __init__(self, store) -> None:
        self.store = store
        self.gets: list = []
        self.puts: list = []

    def get(self, key, length_seconds=None):
        self.gets.append(key)

        return self.store.get(key, length_seconds)

    def put(self, key, cues, length_seconds, **kwargs) -> None:
        self.puts.append(key)
        self.store.put(key, cues, length_seconds, **kwargs)

    def __getattr__(self, name):
        return getattr(self.store, name)


class FakeWorker:
    """subprocess.Popen for `python -m helper.asr.worker`: remembers the call, answers by scenario.

    args — the whole argv as JobRunner._run_asr assembled it; options — the flags after
    `-m helper.asr.worker` in pairs; input — the audio bytes from communicate; killed — kill() was called.
    The reply is taken from the pipeline at communicate time: stdout — asr_result (a dict goes out
    as JSON, bytes — as is), exit code — asr_exit, stderr — asr_stderr. A killed one
    answers with code -9 and empty output, as after SIGKILL.

    gate holds communicate, like a long Whisper, and kill() releases it. communicate
    runs in a worker thread (asyncio.to_thread), so gate is a threading.Event, not asyncio.
    """

    def __init__(self, rig, args, **kwargs) -> None:
        self.rig = rig
        self.args = list(args)
        self.input: bytes | None = None
        self.returncode: int | None = None
        self.killed = False
        rig.workers.append(self)
        rig.calls.append(("asr", self.options))
        rig.worker_started.set()

    @property
    def options(self) -> dict[str, str]:
        flags = self.args[3:]

        return dict(zip(flags[::2], flags[1::2]))

    def communicate(self, input=None, timeout=None):
        self.input = input

        if self.rig.gate is not None:
            self.rig.gate.wait(5)

        if self.killed:
            self.returncode = -9

            return b"", b""

        self.returncode = self.rig.asr_exit
        out = self.rig.asr_result

        if isinstance(out, dict):
            out = json.dumps(out, ensure_ascii=False).encode()

        return out, self.rig.asr_stderr

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True

        if self.rig.gate is not None:
            self.rig.gate.set()


class Pipeline:
    """The real JobRunner on fakes: no network, card or ollama, the pipeline its own.

    Scenario (the test sets it before run):
      info        — the VideoInfo that probe returns; metadata() by default;
      audio       — the audio from audio_pcm: two seconds of zeros, float32, 16 kHz;
      track       — the author track cues from fetch_manual_track, or the exception it
                    will raise (YtDlpError sends the pipeline to recognition);
      asr_result  — the worker's stdout: a dict goes out as JSON, bytes — as is;
      asr_exit    — the worker's exit code (0); asr_stderr — its stderr (b"");
      gate        — threading.Event or None: holds the worker until set() or kill().
    Records (the test reads them after):
      calls          — outward calls in order: ("probe", id), ("track", id, language),
                       ("audio", id), ("asr", worker flags), ("translate", cues);
      workers        — the started FakeWorker instances; worker_started — Popen has been called;
      lease          — FakeLease: calls and holders;
      cache          — RecordingCache: gets and puts (CacheKey), variants(id) from disk;
      runner, config — the runner and its setting; configure() reassembles them.
    """

    def __init__(self, manager, cache) -> None:
        self.manager = manager
        self.cache = cache
        self.lease = FakeLease()
        self.calls: list[tuple] = []
        self.workers: list[FakeWorker] = []
        self.worker_started = threading.Event()
        self.gate: threading.Event | None = None
        self.info = metadata()
        self.audio = np.zeros(2 * 16000, dtype="<f4")
        self.track = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima.", "words": []}]
        self.asr_result: dict | bytes = {
            "language": "sr",
            "duration": 2.0,
            "model": "large-v3",
            "cues": [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}],
        }
        self.asr_exit = 0
        self.asr_stderr = b""
        self.configure()

    def configure(self, **asr) -> None:
        """A runner on Config(asr=AsrConfig(**asr)); the registry gets the same Config and runner.

        The lease record starts over: right after the call lease.calls contains only
        ("claim", translator model) of the new runner.
        """
        from helper import jobs
        from helper.config import AsrConfig, Config, TranslatorConfig

        self.config = Config(asr=AsrConfig(**asr), translator=TranslatorConfig())
        self.lease.calls.clear()
        self.runner = jobs.JobRunner(self.lease, self.cache, self.config)
        # restore computes the key by manager.config: the same setting as the runner's.
        self.manager.config = self.config
        self.manager.runner = self.runner

    def names(self) -> list[str]:
        """Outward call names in order: ["probe", "audio", "asr", "translate"]."""
        return [call[0] for call in self.calls]

    async def run(self, job) -> None:
        """The real JobRunner.run(job): _pipeline with all its branches and refusals."""
        await self.runner.run(job)


@pytest.fixture
def pipeline(manager, monkeypatch):
    """JobRunner on fakes: the real pipeline without network, card or ollama. See Pipeline."""
    from helper import cache as track_cache
    from helper import env, jobs

    # Each test has its own cache. The shared run cache would fill with finished tracks, and
    # restore in other tests would find them. CACHE_DIR is replaced before TrackCache(): the
    # index.sqlite index is opened in the constructor.
    base = Path(tempfile.mkdtemp(prefix="pipeline-", dir=env.CACHE_DIR))
    monkeypatch.setattr(track_cache, "CACHE_DIR", base)
    rig = Pipeline(manager, RecordingCache(track_cache.TrackCache(root=base / "tracks")))
    # By plain assignment, not monkeypatch: its rollback would come after
    # manager has closed the index of its cache, and it would then close this one.
    shared = manager.cache
    manager.cache = rig.cache

    def probe(video_id, *, cookies=None):
        rig.calls.append(("probe", video_id))

        return rig.info

    def audio_pcm(video_id, *, cookies=None):
        rig.calls.append(("audio", video_id))

        return rig.audio

    def fetch_manual_track(video_id, lang, *, cookies=None):
        rig.calls.append(("track", video_id, lang))

        if isinstance(rig.track, BaseException):
            raise rig.track

        return rig.track

    def translate_dual(batch, model, **kwargs):
        rig.calls.append(("translate", list(batch.cues)))

        return [(f"cnr {cue}", f"ru {cue}") for cue in batch.cues]

    def translate_batch(batch, model, **kwargs):
        rig.calls.append(("translate", list(batch.cues)))

        return [f"ru {cue}" for cue in batch.cues]

    monkeypatch.setattr(jobs, "probe", probe)
    monkeypatch.setattr(jobs, "audio_pcm", audio_pcm)
    monkeypatch.setattr(jobs, "fetch_manual_track", fetch_manual_track)
    monkeypatch.setattr(jobs, "translate_dual", translate_dual)
    monkeypatch.setattr(jobs, "translate_batch", translate_batch)
    monkeypatch.setattr(jobs, "FETCH_BACKOFF", (0.0, 0.0))
    # Only the subprocess of the helper.jobs module is replaced: yt-dlp and asyncio see the real one.
    monkeypatch.setattr(
        jobs,
        "subprocess",
        types.SimpleNamespace(
            Popen=lambda args, **kwargs: FakeWorker(rig, args, **kwargs),
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    yield rig

    manager.cache = shared
    rig.cache.store._db.close()
    shutil.rmtree(base, ignore_errors=True)


# ── the worker on a fake faster_whisper ──────────────────────────────────


class FakeWhisperModel:
    """WhisperModel without the card: remembers what it was built with, and always hears Serbian."""

    def __init__(self, name, device, compute_type) -> None:
        self.name = name
        self.device = device
        self.compute_type = compute_type

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        # From task 5 the detector runs on every transcribe. The reply is like the real one's:
        # the best language, its share and the whole distribution. Windows and votes are checked by task 5
        # with its own tests, on its own model stub.
        return "sr", 1.0, [("sr", 1.0)]


class FakeBatchedPipeline:
    """BatchedInferencePipeline without the card: remembers the arguments, yields the scenario segments."""

    def __init__(self, whisper, model) -> None:
        self.whisper = whisper
        self.model = model

    def transcribe(self, audio, **kwargs):
        from faster_whisper.transcribe import TranscriptionInfo

        self.whisper.kwargs = kwargs
        seconds = len(audio) / 16000
        info = TranscriptionInfo(
            language=kwargs.get("language"),
            language_probability=1.0,
            duration=seconds,
            duration_after_vad=seconds,
            all_language_probs=None,
            transcription_options=None,
            vad_options=None,
        )

        return iter(self.whisper.segments), info


class FakeWhisper:
    """faster_whisper without the card for worker tests: transcribe and _detect are real.

    Scenario (the test sets it):
      segments — what BatchedInferencePipeline.transcribe returns; build it via segment().
    Records (the test reads them):
      model  — the last model the worker built: name, device, compute_type;
      kwargs — the keyword arguments of the last BatchedInferencePipeline.transcribe.
    VAD is not replaced: faster_whisper.vad.get_speech_timestamps — the real Silero, on
    the CPU and without network; in zeros of speech it finds nothing.
    """

    def __init__(self) -> None:
        self.segments: list = []
        self.model: FakeWhisperModel | None = None
        self.kwargs: dict | None = None

    def build_model(self, model_size_or_path, device="auto", compute_type="default", **kwargs):
        self.model = FakeWhisperModel(model_size_or_path, device, compute_type)

        return self.model

    def build_pipeline(self, model):
        return FakeBatchedPipeline(self, model)

    @staticmethod
    def segment(
        start,
        end,
        text,
        *,
        no_speech_prob=0.1,
        avg_logprob=-0.3,
        compression_ratio=1.5,
        words=(),
    ):
        """The real faster_whisper.transcribe.Segment: the field names are checked by the library itself.

        words — faster_whisper.transcribe.Word, as returned by word_timestamps=True.
        """
        from faster_whisper.transcribe import Segment

        return Segment(
            id=0,
            seek=0,
            start=start,
            end=end,
            text=text,
            tokens=[],
            avg_logprob=avg_logprob,
            compression_ratio=compression_ratio,
            no_speech_prob=no_speech_prob,
            words=list(words),
            temperature=0.0,
        )


@pytest.fixture
def whisper(monkeypatch):
    """A fake faster_whisper for worker tests, see FakeWhisper. The card is not needed."""
    # First helper.env, as in the worker itself: it sets HF_HOME to a temporary cache before
    # faster_whisper pulls in huggingface_hub.
    import helper.env  # noqa: F401

    import faster_whisper

    fake = FakeWhisper()
    # transcribe imports both classes inside itself, on every call: a replacement in the
    # faster_whisper module reaches it. VAD stays real.
    monkeypatch.setattr(faster_whisper, "WhisperModel", fake.build_model)
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", fake.build_pipeline)

    return fake
```

- [ ] **Step 4: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: PASS, 10 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 153 passed

`python -X dev -m pytest` after a green run fails on interpreter exit: this is
a mismatch of allocators in the faster_whisper C extension. A normal run is not affected;
do not investigate.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add tests/conftest.py tests/test_harness.py
/usr/bin/git commit -m "Оснастка конвейера: настоящие JobRunner и воркер в тестах, на подделках сети, карты, ollama и faster_whisper"
```

Each git command — in a separate Bash call, via `/usr/bin/git`; timeout at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message in Russian, without `Co-Authored-By` lines and other signatures.

---
### Task 4: Language codes: validation, reduction and refusal before YouTube

**Files:** line numbers are on `98a3ad2`, previous tasks shifted them: search by function name.
- Create: `helper/language.py` — the codes part (task 5 will append the detector part)
- Modify: `helper/config.py:23-26` (imports), `helper/config.py:66-72` (comment on `AsrConfig.language`), `helper/config.py:179-206` (`load`: right after `AsrConfig` is assembled on line 189)
- Modify: `config.example.toml:21-24`
- Modify: `helper/jobs.py:30-33` (imports); `helper/jobs.py:772-786` (`JobRunner._pipeline`: between the resume, 775-778, and the metadata request, 784-786); `helper/jobs.py:1195-1209` (`JobRunner._run_asr`: non-zero exit code, between lines 1207 and 1209); `helper/jobs.py:1218-1242` (`JobRunner._source_language`: config branch 1229-1232; the new method `JobRunner._pin` — right before it)
- Test: `tests/test_language.py`, `tests/test_config.py`, `tests/test_job_language.py` — all three new

**Interfaces:**
- Consumes: the `manager` fixture (`tests/conftest.py`). From task 3: the builder `metadata(**overrides) -> VideoInfo` (tests import it with `from conftest import metadata`; without arguments — a video without author tracks and without `audio_language`) and the `pipeline` fixture: `pipeline.configure(**asr)` assembles the runner on `AsrConfig(**asr)`, `await pipeline.run(job)` drives the real `JobRunner.run`, `pipeline.workers` — the fake worker processes in order, each with `.args` — the full argv, as in `subprocess.Popen.args`: `[sys.executable, "-m", "helper.asr.worker", ...]`; the fake worker's reply is set by `pipeline.asr_result` (stdout: a dict goes out as JSON, bytes — as is), `pipeline.asr_exit` (exit code) and `pipeline.asr_stderr`.
- Produces:
  - `helper/language.py`, standard library only: `WHISPER_CODES: frozenset[str]`, `SUBSTITUTES: dict[str, str]`, `NO_LANGUAGE: frozenset[str]`, `BCMS: frozenset[str]`, `class UnknownLanguage(ValueError)` with a `raw: str` attribute, `normalize(raw: str | None) -> str | None`, `hint(raw: str | None) -> str | None`, `family(code: str | None) -> str | None`, `is_bcms(code: str | None) -> bool`, `refusal(raw: str) -> str`.
  - `JobRunner._pin(self) -> str | None` — the Whisper code from `[asr] language` or `None` for auto; an invalid code → `PermanentJobError(language.refusal(raw))`.
  - `helper.config.load()`: `asr.language` — a Whisper code, `"auto"`, or, if the code is invalid, the original string (plus `log.error`). `Config.describe()["asr"]["language"]` stays a string: it is printed by `extension/options.js:125`.
  - `JobRunner._run_asr`, non-zero worker exit not by signal: stdout is parsed before the stderr tail. A dict with `"error"` and `"permanent": true` → `PermanentJobError(error)`; a dict with `"error"` without that flag → `RuntimeError(f"ASR не справился: {error}")`; everything else — the previous `RuntimeError` with the stderr tail.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_language.py
"""helper/language.py: language codes, the detector rule and the language decision."""

import ast
import sys
from pathlib import Path

import pytest

from helper import language


def test_whisper_codes_are_the_installed_faster_whisper_list():
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    assert language.WHISPER_CODES == frozenset(_LANGUAGE_CODES)
    assert len(language.WHISPER_CODES) == 100


SUBSTITUTE_ROWS = [
    ("sh", "sr"),
    ("me", "sr"),
    ("cnr", "sr"),
    ("iw", "he"),
    ("jv", "jw"),
    ("fil", "tl"),
    ("nb", "no"),
    ("in", "id"),
]


def test_every_substitute_has_its_own_row():
    assert dict(SUBSTITUTE_ROWS) == language.SUBSTITUTES


@pytest.mark.parametrize(("raw", "code"), SUBSTITUTE_ROWS)
def test_substitute_lands_on_a_whisper_code(raw, code):
    assert raw not in language.WHISPER_CODES
    assert code in language.WHISPER_CODES
    assert language.normalize(raw) == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sr", "sr"),
        ("SR", "sr"),
        (" sr ", "sr"),
        ("sr-Latn", "sr"),
        ("sr-Cyrl", "sr"),
        ("sr_RS", "sr"),
        ("cnr-ME", "sr"),
        ("hr", "hr"),
        ("bs", "bs"),
        ("en-US", "en"),
        ("EN-us", "en"),
        ("en-orig", "en"),
        ("en-desc", "en"),
        ("zh-Hans", "zh"),
        ("yue", "yue"),
        ("en+en-US", "en"),
    ],
)
def test_normalize_keeps_the_primary_subtag(raw, code):
    assert language.normalize(raw) == code


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "auto", "AUTO", " Auto ", "und", "zxx", "mul", "mis"]
)
def test_normalize_reads_no_language_as_none(raw):
    assert language.normalize(raw) is None


@pytest.mark.parametrize("raw", ["serbian", "xx", "srb", "en+sr", "live_chat"])
def test_normalize_refuses_an_unknown_code(raw):
    with pytest.raises(language.UnknownLanguage) as caught:
        language.normalize(raw)

    assert caught.value.raw == raw
    assert isinstance(caught.value, ValueError)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("en-orig", "en"),
        ("SR", "sr"),
        ("sh", "sr"),
        ("serbian", None),
        ("live_chat", None),
        ("en+sr", None),
        ("und", None),
        (None, None),
    ],
)
def test_hint_never_raises(raw, code):
    assert language.hint(raw) == code


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sr", "sr"),
        ("hr", "sr"),
        ("bs", "sr"),
        ("sh", "sr"),
        ("cnr", "sr"),
        ("sr-Latn", "sr"),
        ("mk", "mk"),
        ("en-US", "en"),
        ("serbian", None),
        (None, None),
    ],
)
def test_family_folds_the_three_bcms_tokens(raw, code):
    assert language.family(raw) == code


@pytest.mark.parametrize("raw", ["sr", "hr", "bs", "sh", "me", "cnr", "sr-Latn", "sr-Cyrl", "SR"])
def test_bcms_spellings_are_bcms(raw):
    assert language.is_bcms(raw) is True


@pytest.mark.parametrize("raw", ["mk", "sl", "ru", "en", None, "", "live_chat", "serbian"])
def test_other_codes_are_not_bcms(raw):
    assert language.is_bcms(raw) is False


def test_bcms_is_three_whisper_tokens():
    assert language.BCMS == {"sr", "hr", "bs"}
    assert language.BCMS <= language.WHISPER_CODES


def test_refusal_names_the_value_and_where_to_fix_it():
    phrase = language.refusal("serbian")

    assert phrase == "неизвестный язык «serbian»: [asr] language в config.toml"
    assert len(phrase) <= 60


def test_language_module_imports_only_the_standard_library():
    tree = ast.parse(Path(language.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])

    assert roots <= sys.stdlib_module_names
```

```python
# tests/test_config.py
"""[asr] language in config.toml: what the owner wrote and what the daemon gets."""

import logging
import tomllib
from pathlib import Path

import pytest

from helper import language
from helper.config import load

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
```

```python
# tests/test_job_language.py
"""Language on the daemon side: the owner's pinning, refusal and the conversation with the worker."""

import pytest
from conftest import metadata

from helper import jobs, language
from helper.config import AsrConfig, Config, TranslatorConfig

VIDEO = "abcdefghijk"


def runner_with(manager, raw: str) -> jobs.JobRunner:
    config = Config(asr=AsrConfig(language=raw), translator=TranslatorConfig())

    return jobs.JobRunner(manager.lease, manager.cache, config)


@pytest.mark.parametrize(
    ("raw", "pin"),
    [
        ("sr", "sr"),
        ("SR", "sr"),
        ("sr-Latn", "sr"),
        ("sh", "sr"),
        ("cnr", "sr"),
        ("hr", "hr"),
        ("auto", None),
        ("AUTO", None),
        ("", None),
    ],
)
def test_pin_is_the_normalised_config_code(manager, raw, pin):
    assert runner_with(manager, raw)._pin() == pin


def test_unknown_pin_is_a_permanent_refusal(manager):
    with pytest.raises(jobs.PermanentJobError) as caught:
        runner_with(manager, "serbian")._pin()

    assert str(caught.value) == "неизвестный язык «serbian»: [asr] language в config.toml"


async def test_unknown_pin_refuses_the_job_before_youtube(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("serbian")
    assert job.retryable is False
    # Metadata was not requested (job.info is empty), there was no audio and no worker: the refusal
    # costs nothing to YouTube or to the card.
    assert job.info is None
    assert pipeline.workers == []


async def test_language_refusal_is_not_recreated_after_cooldown(manager, pipeline):
    pipeline.configure(language="serbian")
    job = manager.get_or_create(VIDEO, "ru", True)
    await pipeline.run(job)
    # The cooldown of a retryable refusal would have passed: it does not touch a final refusal.
    job.failed_at -= jobs.RETRY_COOLDOWN

    assert manager.get_or_create(VIDEO, "ru", True) is job
    assert manager.started == [VIDEO]


@pytest.mark.parametrize(
    ("raw", "sent"),
    [("sh", "sr"), ("SR", "sr"), ("sr-Latn", "sr"), ("cnr", "sr"), ("AUTO", "auto")],
)
async def test_pin_reaches_the_worker_normalised(manager, pipeline, raw, sent):
    pipeline.configure(language=raw)
    job = manager.get_or_create(VIDEO, "ru", True)
    # A video without author tracks and without a language label: the recognition path.
    job.info = metadata()

    await pipeline.run(job)

    args = pipeline.workers[-1].args
    assert args[args.index("--language") + 1] == sent


async def test_worker_refusal_on_stdout_is_permanent(manager, pipeline):
    pipeline.asr_result = {"error": language.refusal("xx"), "permanent": True}
    pipeline.asr_exit = 2
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == language.refusal("xx")
    assert job.retryable is False


async def test_worker_crash_keeps_the_stderr_tail(manager, pipeline):
    pipeline.asr_result = b""
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b"Traceback (most recent call last):\nValueError: boom"
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR не справился: Traceback (most recent call last):\nValueError: boom"
    assert job.retryable is True


async def test_worker_error_reply_reaches_the_viewer(manager, pipeline):
    # The worker refused itself, with a phrase in stdout, and stderr is empty: today the viewer saw
    # «ASR не справился: » with an empty tail.
    pipeline.asr_result = {"error": "аудио короче половины секунды"}
    pipeline.asr_exit = 1
    pipeline.asr_stderr = b""
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    assert job.stage is jobs.Stage.ERROR
    assert job.error == "ASR не справился: аудио короче половины секунды"
    assert job.retryable is True
```

All three worker-reply tests go through the real pipeline, not by calling `_run_asr` directly: `job.info` is set for them by the fake `probe`, and from task 8 `_run_asr` reads the title and tags from it. `test_worker_crash_keeps_the_stderr_tail` passes before the edit too: it guards that a worker crash without a JSON reply stays the previous retryable `RuntimeError` with the stderr tail, and does not become final.

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_language.py tests/test_config.py tests/test_job_language.py -q`
Expected: FAIL — `3 errors during collection`, in all three `ImportError: cannot import name 'language' from 'helper'`

- [ ] **Step 3: The codes module `helper/language.py`**

A new file. Module docstring: language codes shared by the daemon and the recognition worker; **standard library only** — the module is loaded by both the daemon and the worker, while `faster_whisper` pulls in ctranslate2, av and tokenizers, which have no business in the daemon process (guarded by `test_language_module_imports_only_the_standard_library`). First line of code — `from __future__ import annotations`.

The tables are exactly these (comments in Russian, as below):

```python
# Exactly one hundred codes of faster-whisper 1.2.1 (faster_whisper.tokenizer._LANGUAGE_CODES).
# A copy, not an import: the name is private, and an import would drag all of faster_whisper into the daemon.
# If it diverges from the installed version, a test fails.
WHISPER_CODES: frozenset[str] = frozenset({
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs",
    "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr", "gl", "gu",
    "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "jw", "ka",
    "kk", "km", "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml",
    "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt",
    "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi", "yo",
    "yue", "zh",
})

# Spellings Whisper does not know, but which mean its code. sh, me and cnr —
# Serbian under other names; the rest — old or alternative ISO codes
# used by YouTube and language tables.
SUBSTITUTES: dict[str, str] = {
    "sh": "sr", "me": "sr", "cnr": "sr",
    "iw": "he", "jv": "jw", "fil": "tl", "nb": "no", "in": "id",
}

# “No language”: empty and auto in config.toml, und, zxx, mul and mis from ISO 639.
NO_LANGUAGE: frozenset[str] = frozenset({"", "auto", "und", "zxx", "mul", "mis"})

# Three Whisper tokens for one language. bs, hr and sr got into its training from VoxLingua107
# by oversight, and the choice between them is arbitrary; family() folds them into sr.
BCMS: frozenset[str] = frozenset({"sr", "hr", "bs"})


class UnknownLanguage(ValueError):
    """A code Whisper does not know and which does not reduce to any of its codes."""

    def __init__(self, raw: str) -> None:
        super().__init__(raw)
        self.raw = raw
```

Functions (behaviour pinned by the step 1 tests):

- `normalize(raw: str | None) -> str | None`, in order: `None` → `None`; `code = raw.strip().lower().replace("_", "-")`; `code in NO_LANGUAGE` → `None`; the value is split at `+` (that is how yt-dlp joins track languages), and each part is reduced separately: the primary subtag up to the first `-`, then `SUBSTITUTES.get(...)`, then a check `in WHISPER_CODES`, otherwise `raise UnknownLanguage(raw)` — with the **original** string; the parts agreed on one code — that is returned, they diverged (`en+sr`) — `UnknownLanguage(raw)`. `hr` and `bs` stay themselves: they are real Whisper codes, only `family` folds them.
- `hint(raw)` — `normalize`, but `UnknownLanguage` → `None`: for YouTube labels, where an unfamiliar one is simply “we don't know”.
- `family(code)` — `hint(code)`, then any code from `BCMS` → `"sr"`. For comparing languages, not for the decoder.
- `is_bcms(code)` — `family(code) == "sr"`.
- `refusal(raw)` — exactly `f"неизвестный язык «{raw}»: [asr] language в config.toml"` (56 characters for `serbian`, fits on the badge); a value longer than 11 characters pushes the phrase past 60 (`serbo-croatian` — 63): the badge will cut the tail with an ellipsis, the full phrase will remain in the tooltip, and, most importantly, “unknown language «…»” comes first; the template is pinned — do not shorten it.

The three previous BCMS definitions (`helper/asr/worker.py:48`, `helper/script.py:25`, `helper/sources/ytdlp.py:35`) are not touched in this task: they are replaced by the tasks that change those files (the worker — task 5).

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_language.py -q`
Expected: PASS, 79 passed

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_job_language.py -q`
Expected: FAIL — 29 failed, 4 passed. The `"hr"` and `"auto"` rows in `test_language_is_normalised_at_load`, `test_missing_file_means_auto` and the guard `test_worker_crash_keeps_the_stderr_tail` pass. Failing: the config stores the value as is (`assert 'cnr' == 'sr'`; a number from TOML stays a number — `assert 5 == '5'`), the log has no error, the example yields `'sr'`, `AttributeError: 'JobRunner' object has no attribute '_pin'`, a job with `serbian` reaches the worker and `done`, `--language sh` goes to the worker, and the worker's refusal and its error phrase reach the job with an empty stderr tail (`assert 'ASR не справился: ' == …`).

- [ ] **Step 5: Config and the example config**

`helper/config.py`, imports (lines 23-26): `from dataclasses import dataclass, replace` and `from helper import language` before `from helper.env import CONFIG_DIR`.

`helper/config.py`, `load` (179-206): right after `asr = AsrConfig(**_section(raw, "asr", AsrConfig))` (line 189):

```python
    # The language code is reduced to Whisper's form here, once: "sh", "SR" and "sr-Latn" are
    # sr, and from here on the code itself travels through the daemon. An unfamiliar value is not a startup error, as with
    # cookies: the daemon comes up, and every new job gets a refusal with the same phrase
    # (JobRunner._pin). str() — in case of a number instead of a string.
    try:
        asr = replace(asr, language=language.normalize(str(asr.language)) or "auto")
    except language.UnknownLanguage as exc:
        asr = replace(asr, language=exc.raw)
        log.error(language.refusal(exc.raw))
```

`describe()` does not change: `language` in it is a string anyway.

`helper/config.py:66-71` — replace the comment above `language: str = "auto"` entirely (the previous one says “the language is taken from metadata”, and metadata is now only a hint):

```python
    # Speech language. "auto" — the detector decides: it listens to windows over the whole video
    # length and always runs, and the YouTube language label is only a hint for it. A language
    # code pins the language for EVERY video: Whisper decodes with it even if something else is
    # spoken, and the detector only marks the mismatch. Pin it only when all videos are in one
    # language: on foreign speech the decoder's behaviour is not defined, and it starts choosing
    # between translation, transliteration and writing down what was heard in a foreign alphabet
    # — anew in every thirty-second window.
    # load() reduces the value to a Whisper code (helper/language.py), auto stays auto.
```

`config.example.toml:21-24` — replace the comment “The language is fixed hard, autodetect is not used…” and the line `language = "sr"` with:

```toml
# Speech language. "auto" — the daemon itself determines the language: the detector listens
# to windows over the whole video length and always runs, and the language label from YouTube
# is only a hint for it (YouTube sometimes labels Serbian channels in English).
#
# A language code ("sr", "en", "ru"…) pins the language for EVERY video: Whisper
# recognizes with it even if another language is spoken, and the detector only
# marks the mismatch. Set it only if all videos are in one language.
# sh, me and cnr are also fine and mean sr; region and script are dropped
# ("sr-Latn" is sr). A code Whisper does not know is an error in the log
# at startup and a refusal for every new video: «unknown language …».
language = "auto"
```

The phrases “the detector always runs” and “marks the mismatch” become true with tasks 5 and 6 of this same branch. Both comments are written in their final form right away, so as not to edit them in every task: the branch is merged as a whole.

- [ ] **Step 6: Daemon: pinning, early refusal, the worker's reply**

`helper/jobs.py`, imports: `from helper import language` before `from helper.asr.worker import SAMPLE_RATE` (line 33). There are no other names `language` in the module — no collisions until task 8 (the `asr_cues` parameter).

New method `JobRunner._pin(self) -> str | None` — right before `_source_language` (1218):

```python
    def _pin(self) -> str | None:
        """The language pinned by the owner in [asr] language: a Whisper code or None (“auto”).

        load() reduces the value to a code, and an invalid one only writes to the log: the daemon must
        come up. A job with an invalid code, though, gets a refusal in a human phrase,
        a final one: a retry with the same config would give the same thing.
        """
        try:
            return language.normalize(self._config.asr.language)
        except language.UnknownLanguage as exc:
            raise PermanentJobError(language.refusal(exc.raw)) from None
```

`JobRunner._pipeline`: the call `self._pin()` with its own comment — right after the `return` of the resume branch (`if job.cues: … return`, 778), before the comment “Metadata is already there…” (780), which stays above `if job.info is None:`. The comment on the call: an invalid code in `[asr] language` is a refusal before the first request to YouTube and to the card, with no metadata, no audio and no lease; the refusal is final, otherwise the cooldown in `get_or_create` would recreate the job once a minute, as it did with the Whisper traceback; the resume above does not ask about the language — recognition is already behind. `JobRunner.run` already turns `PermanentJobError` into `retryable=False` (752-754), no need to change it.

`JobRunner._source_language`, the config branch (1229-1232): instead of `configured = self._config.asr.language` / `if configured and configured != "auto": return configured` — `pin = self._pin()` / `if pin: return pin`. This is a transitional change: the already-reduced code goes to the worker (`sh` → `sr`, `AUTO` → the metadata branch and `auto`), and task 5 will delete the method itself. Do not touch the metadata branch (1234-1242).

`JobRunner._run_asr`, non-zero exit code: both signal branches (1198-1207) stay first; between them and `raise RuntimeError(f"ASR не справился: …")` (1209) insert:

```python
            # The worker refused itself — a phrase in stdout, not a traceback in stderr. A refusal with
            # "permanent" will not pass on a retry either (an invalid language code), so it is
            # final: the cooldown does not recreate such a job. A phrase without the flag
            # (“audio shorter than half a second”) is a retryable refusal, but the viewer sees it,
            # not an empty stderr tail.
            try:
                reply = json.loads(out)
            except ValueError:
                reply = None

            if isinstance(reply, dict) and reply.get("error"):
                if reply.get("permanent") is True:
                    raise PermanentJobError(str(reply["error"]))

                raise RuntimeError(f"ASR не справился: {reply['error']}")
```

`ValueError` catches both `json.JSONDecodeError` (empty stdout) and `UnicodeDecodeError`. Non-JSON and JSON without `"error"` take the previous path, to `RuntimeError` with the stderr tail. Do not touch the `"error" in result` branch after parsing on a zero code (1211-1214).

`restore()`, `_key_for` and `_cached_variant` do not call `_pin()` and must not: a restore after F5 does not run the pipeline and is not obliged to refuse.

- [ ] **Step 7: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_language.py tests/test_config.py tests/test_job_language.py -q`
Expected: PASS, 112 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 265 passed

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add helper/language.py helper/config.py helper/jobs.py config.example.toml tests/test_language.py tests/test_config.py tests/test_job_language.py
/usr/bin/git commit -m "Коды языков проверяются: sh, me, cnr и sr-Latn — это sr, негодный код — окончательный отказ до обращения к YouTube"
```

Each git command — in a separate Bash call, via `/usr/bin/git`; timeout at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message in Russian, without `Co-Authored-By` lines and other signatures.

---

### Task 5: The detector always runs, the YouTube label is only a hint

**Files:** line numbers are from `98a3ad2`, the previous tasks have shifted them: search by function name.
- Modify: `helper/language.py` — append the detector part to the end of the file; imports `from collections.abc import Sequence` and `from dataclasses import dataclass`
- Modify: `helper/asr/worker.py:1-15` (the module docstring, the usage example on line 14), `:17-27` (imports, `SAMPLE_RATE`), `:36-122` (`PROBE_WINDOWS`, `PROBE_SPAN`, `WINDOW_SECONDS`, `BCMS`, `_detect`, `_merge_bcms` — go away; in their place `_speech_seconds`, `_detect`, `_window`), `:125-215` (`transcribe`), `:218-259` (`main` → `build_parser` + `main`)
- Modify: `helper/jobs.py:1151-1167` (`JobRunner._run_asr`, argv); `JobRunner._source_language` is deleted entirely, the `JobRunner._pin` method right above it (Task 4) stays, and `JobRunner._declaration` takes the place of `_source_language`, under `_pin`
- Test: `tests/test_language.py` (append), `tests/test_worker.py` (new), `tests/test_job_language.py` (append)

**Interfaces:**
- Consumes: Task 4 — the codes in `helper.language`, `JobRunner._pin`. Task 3 — the `whisper` fixture (replaces `faster_whisper.WhisperModel` and `BatchedInferencePipeline`; `whisper.kwargs` — the keyword arguments of the last `BatchedInferencePipeline.transcribe`), the `pipeline` fixture and `metadata`, as in Task 4.
- Produces:
  - `helper/language.py`: `MIN_SPEECH_SECONDS = 3.0`, `BCMS_FLOOR = 0.2`, `SLAVIC_NEIGHBOURS = frozenset({"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"})`, `SAMPLE_RATE = 16000`, `WINDOW_SAMPLES = 30 * SAMPLE_RATE`; `probe_offsets(n_samples: int, *, windows: int = 8, span: tuple[float, float] = (0.10, 0.90), window: int = WINDOW_SAMPLES) -> list[int]`; `@dataclass(frozen=True) class Detection(language: str | None, confidence: float | None, bcms_share: float, floor: bool, votes: int, probed: int, windows: tuple[tuple[str | None, float, float], ...])`; `summarize(windows: Sequence[tuple[float, Sequence[tuple[str, float]]]]) -> Detection`; `@dataclass(frozen=True) class LanguageVerdict(effective: str, declared: str | None, declared_by: str | None, detected: str | None, confidence: float | None, reason: str, source: str = "asr", track: str | None = None, verified: bool | None = None)` with `mismatch` (a property), `note() -> str | None`, `wire() -> dict`, `from_wire(cls, data: object) -> LanguageVerdict | None`; `decide(pin: str | None, hint: str | None, detection: Detection, *, fallback: str = "sr") -> LanguageVerdict`.
  - `helper/asr/worker.py` (the language module is imported as `lang`, see Step 7): `_detect(model, audio: np.ndarray) -> lang.Detection`; `transcribe(audio, *, model_name=DEFAULT_MODEL, compute_type="float16", language: str = "sr", hint: str | None = None, initial_prompt=None, hotwords=None, batch_size=16, vad_filter=True) -> dict` (`initial_prompt` until Task 8); `build_parser() -> argparse.ArgumentParser` with the new `--hint CODE`; the answer `{"language": verdict.effective, "verdict": verdict.wire(), "detection": {"bcmsShare", "votes", "probed", "windows"}, "duration", "model", "cues"}`; an invalid `--language` → stdout `{"error": refusal, "permanent": true}`, exit code 2.
  - `JobRunner._declaration(self, job: Job) -> tuple[str | None, str | None]` — `(self._pin(), language.hint(job.info.audio_language) if job.info else None)`; the worker receives `--language <pin or auto>` and `--hint <code>` when the label exists.

- [ ] **Step 1: Write failing tests for the rule**

Append to the end of `tests/test_language.py` (the imports are the same):

```python
# ── Detector: windows, rule, verdict ─────────────────────────────────────────────


def test_detector_constants_are_pinned():
    # A change of any of them changes recognition of cached videos: fix here —
    # raise helper.cache.PROMPT_VERSION.
    assert language.MIN_SPEECH_SECONDS == 3.0
    assert language.BCMS_FLOOR == 0.2
    assert language.SLAVIC_NEIGHBOURS == {"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"}
    assert language.SAMPLE_RATE == 16000
    assert language.WINDOW_SAMPLES == 30 * 16000


@pytest.mark.parametrize(
    ("seconds", "offsets"),
    [
        (5, [0]),
        (30, [0]),
        (31.25, [0, 4286, 15714, 20000]),
        (37.5, [60000]),
        (20 * 60, [1920000, 4045714, 6171429, 8297143, 10422857, 12548571, 14674286, 16800000]),
    ],
)
def test_probe_offsets(seconds, offsets):
    n_samples = int(seconds * language.SAMPLE_RATE)

    assert language.probe_offsets(n_samples) == offsets
    assert all(0 <= start <= max(n_samples - language.WINDOW_SAMPLES, 0) for start in offsets)


def window(*shares: tuple[str, float], speech: float = 25.0):
    return (speech, list(shares))


# 3XJLfTSZy6I: Serbian speech without metadata. en on top in five windows out of eight,
# on average en 0.55 against 0.44 for the Serbian family.
IZA_MRAKA = [window(("en", 0.80), ("sr", 0.10), ("hr", 0.06), ("bs", 0.03), ("de", 0.01))] * 5 + [
    window(("sr", 0.45), ("hr", 0.27), ("bs", 0.15), ("en", 0.10), ("de", 0.03)),
    window(("hr", 0.40), ("sr", 0.35), ("en", 0.15), ("bs", 0.10)),
    window(("sr", 0.50), ("bs", 0.20), ("hr", 0.15), ("en", 0.15)),
]
# Q2QcYOBk0Jo (Juske): YouTube label en, the speech is Serbian.
JUSKE = [window(("sr", 0.60), ("hr", 0.25), ("bs", 0.12), ("en", 0.03))] * 8
# 3UbtKc9MlXg: English, zero Serbian family in all eight windows.
ENGLISH = [window(("en", 0.95), ("de", 0.03), ("nl", 0.02))] * 8
# Russian speech whose Serbian family is 0.3: above the threshold, but the language is its own.
RUSSIAN = [window(("ru", 0.60), ("sr", 0.20), ("hr", 0.10), ("uk", 0.10))] * 8
# A clip with almost no speech: no window votes.
SILENCE = [window(speech=0.0)] * 8


def test_floor_turns_the_english_bias_into_serbian():
    detection = language.summarize(IZA_MRAKA)

    assert [w[0] for w in detection.windows].count("en") == 5
    assert detection.language == "sr"
    assert detection.floor is True
    assert detection.bcms_share == pytest.approx(0.44)
    assert detection.confidence == pytest.approx(0.44)
    assert (detection.votes, detection.probed) == (8, 8)


def test_serbian_speech_under_an_english_label():
    detection = language.summarize(JUSKE)

    assert (detection.language, detection.floor) == ("sr", False)
    assert detection.confidence == pytest.approx(0.97)

    verdict = language.decide(None, "en", detection)

    assert verdict.effective == "sr"
    assert (verdict.declared, verdict.declared_by) == ("en", "metadata")
    assert verdict.reason == "detector"
    assert verdict.mismatch is True
    assert verdict.note() == "YouTube указал en, по речи sr"


def test_english_without_bcms_stays_english():
    detection = language.summarize(ENGLISH)

    assert (detection.language, detection.floor) == ("en", False)
    assert detection.bcms_share == 0.0
    assert detection.confidence == pytest.approx(0.95)


def test_slavic_neighbour_keeps_its_language_over_the_floor():
    detection = language.summarize(RUSSIAN)

    assert (detection.language, detection.floor) == ("ru", False)
    assert detection.bcms_share == pytest.approx(0.30)
    assert detection.confidence == pytest.approx(0.60)


@pytest.mark.parametrize(
    ("shares", "winner", "floor"),
    [
        ([("en", 0.80), ("sr", 0.20)], "sr", True),
        ([("en", 0.81), ("sr", 0.19)], "en", False),
        ([("mk", 0.50), ("sr", 0.30), ("bg", 0.20)], "mk", False),
    ],
)
def test_floor_boundary(shares, winner, floor):
    detection = language.summarize([window(*shares)])

    assert (detection.language, detection.floor) == (winner, floor)


def test_windows_without_speech_or_answer_do_not_vote():
    detection = language.summarize(
        [
            window(("en", 0.99), speech=2.9),
            window(speech=0.0),
            # There is speech, but the detector failed on this window: no answer.
            window(speech=25.0),
        ]
    )

    assert detection == language.Detection(
        language=None,
        confidence=None,
        bcms_share=0.0,
        floor=False,
        votes=0,
        probed=3,
        windows=((None, 0.0, 2.9), (None, 0.0, 0.0), (None, 0.0, 25.0)),
    )


def test_three_seconds_of_speech_vote_and_a_window_reports_its_family():
    detection = language.summarize([window(("hr", 0.40), ("sr", 0.35), ("en", 0.25), speech=3.0)])

    assert detection.votes == 1
    assert detection.windows == (("sr", pytest.approx(0.75), 3.0),)


@pytest.mark.parametrize(
    ("pin", "hint", "windows", "effective", "reason", "declared_by"),
    [
        ("sr", None, ENGLISH, "sr", "pin", "config"),
        ("hr", None, JUSKE, "hr", "pin", "config"),
        ("en", "sr", SILENCE, "en", "pin", "config"),
        (None, "en", JUSKE, "sr", "detector", "metadata"),
        (None, None, IZA_MRAKA, "sr", "floor", None),
        (None, "en", SILENCE, "en", "hint", "metadata"),
        (None, None, SILENCE, "sr", "fallback", None),
    ],
)
def test_decide_precedence(pin, hint, windows, effective, reason, declared_by):
    detection = language.summarize(windows)
    verdict = language.decide(pin, hint, detection)

    assert (verdict.effective, verdict.reason, verdict.declared_by) == (effective, reason, declared_by)
    assert verdict.declared == (pin or hint)
    assert (verdict.detected, verdict.confidence) == (detection.language, detection.confidence)
    assert (verdict.source, verdict.track, verdict.verified) == ("asr", None, None)


def manual(effective: str, declared: str | None, declared_by: str | None) -> language.LanguageVerdict:
    return language.LanguageVerdict(
        effective=effective,
        declared=declared,
        declared_by=declared_by,
        detected=None,
        confidence=None,
        reason="track",
        source="manual",
        track="sr-Latn",
    )


@pytest.mark.parametrize(
    ("pin", "hint", "windows", "mismatch", "note"),
    [
        ("sr", None, ENGLISH, True, "в настройках sr, по речи en"),
        ("hr", None, JUSKE, False, None),
        (None, "en", JUSKE, True, "YouTube указал en, по речи sr"),
        (None, None, IZA_MRAKA, False, None),
        (None, "en", SILENCE, False, "речи почти нет, язык взят у YouTube: en"),
        (None, None, SILENCE, False, "речи почти нет, язык принят за sr"),
    ],
)
def test_mismatch_and_note_from_speech(pin, hint, windows, mismatch, note):
    verdict = language.decide(pin, hint, language.summarize(windows))

    assert verdict.mismatch is mismatch
    assert verdict.note() == note
    assert note is None or len(note) <= 60


@pytest.mark.parametrize(
    ("declared", "declared_by", "mismatch", "note"),
    [
        ("en", "metadata", True, "YouTube указал en, дорожка автора sr"),
        ("ru", "config", True, "в настройках ru, дорожка автора sr"),
        ("hr", "config", False, None),
        (None, None, False, None),
    ],
)
def test_mismatch_and_note_for_an_author_track(declared, declared_by, mismatch, note):
    verdict = manual("sr", declared, declared_by)

    assert verdict.mismatch is mismatch
    assert verdict.note() == note
    assert note is None or len(note) <= 60


def test_wire_carries_exactly_the_agreed_keys():
    verdict = language.decide(None, "en", language.summarize(JUSKE))

    assert verdict.wire() == {
        "effective": "sr",
        "declared": "en",
        "declaredBy": "metadata",
        "detected": "sr",
        "confidence": 0.97,
        "reason": "detector",
        "source": "asr",
        "track": None,
        "verified": None,
        "mismatch": True,
        "note": "YouTube указал en, по речи sr",
    }


def test_wire_rounds_confidence_to_two_places():
    verdict = language.LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.4361, reason="floor"
    )

    assert verdict.wire()["confidence"] == 0.44


def test_from_wire_round_trips():
    asr = language.LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected="sr", confidence=0.97, reason="detector"
    )
    track = manual("sr", None, None)

    assert language.LanguageVerdict.from_wire(asr.wire()) == asr
    assert language.LanguageVerdict.from_wire(track.wire()) == track


@pytest.mark.parametrize(
    "data", [None, "sr", [], {}, {"effective": ""}, {"effective": 5}, {"effective": "xx"}, {"effective": "SR"}]
)
def test_from_wire_refuses_what_is_not_a_verdict(data):
    assert language.LanguageVerdict.from_wire(data) is None


def test_from_wire_ignores_derived_and_broken_fields():
    verdict = language.LanguageVerdict.from_wire(
        {"effective": "en", "mismatch": True, "note": "чужое", "confidence": "high", "declaredBy": 7, "verified": "yes"}
    )

    assert verdict == language.LanguageVerdict(
        effective="en", declared=None, declared_by=None, detected=None, confidence=None, reason="detector"
    )
    assert verdict.mismatch is False
```

The window numbers are chosen so that the averages match the field records: for `IZA_MRAKA` en totals 4.40 of 8 (0.55), the family 3.52 of 8 (0.44); for `JUSKE` the family is 0.97 in every window.

- [ ] **Step 2: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_language.py -q`
Expected: FAIL — 44 failed, 79 passed: the new tests fail on `AttributeError: module 'helper.language' has no attribute …` (`MIN_SPEECH_SECONDS`, `SAMPLE_RATE`, `summarize`, `decide`, `LanguageVerdict`), the code tests from Task 4 pass

- [ ] **Step 3: The detector rule in `helper/language.py`**

Append to the end of the file. The constants — with a comment above the block: any of them changes recognition of already cached videos, so an edit requires raising `helper.cache.PROMPT_VERSION`:

```python
# A window with less speech does not vote: silence, music and an intro give
# a distribution based on padding, and such a window would vote on par with speech.
MIN_SPEECH_SECONDS = 3.0

# The Serbian-family share at which the detector decides “Serbian”, even if
# another language is on top. On Serbian speech Whisper leans towards English: 3XJLfTSZy6I
# gave en 0.55 against 0.44 for the family; on an English video the family is zero in every window.
BCMS_FLOOR = 0.2

# Neighbours that often have a lot of the Serbian family by kinship: the threshold does not
# override them, otherwise Macedonian or Russian speech would be recognised as Serbian.
SLAVIC_NEIGHBOURS = frozenset({"mk", "bg", "sl", "ru", "uk", "be", "cs", "sk", "pl"})

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 30 * SAMPLE_RATE
```

`probe_offsets` — the windows lie inside a `span` of the length, as in the worker today (the first starts at 10%, the last ends at 90%); the starts are rounded with `round` (not `int`: the test values, e.g. 4286 and 6171429, depend on it) and clamped to `[0, n_samples - window]`, so on a recording of 30 to 37.5 s the windows are whole and overlap, and do not go negative:

```python
    if n_samples <= window:
        return [0]

    low, high = span
    first = n_samples * low
    last = n_samples * high - window
    latest = n_samples - window
    starts = {
        min(max(round(first + (last - first) * i / max(windows - 1, 1)), 0), latest)
        for i in range(windows)
    }

    return sorted(starts)
```

In the `probe_offsets` docstring, move the reasoning from `helper/asr/worker.py:36-40`: one cannot take from the start — that is where the intro, music or a sponsor ad is; in KCR-dkZIhxY the first cue is at the 21st second.

`Detection` — `@dataclass(frozen=True)` with the fields in the order from Interfaces; a comment at `windows`: per window — the top family or `None` (the window did not vote), its share, the seconds of speech.

`summarize(windows)` — a pure function, in order:
1. For each window `(speech, distribution)` the distribution is reduced by family: `family(code)` for each code, the shares of bs/hr/sr are summed under `"sr"`; a code whose `family` is `None` is skipped.
2. A window votes if `speech >= MIN_SPEECH_SECONDS` **and** the reduced distribution is not empty (empty — the detector failed on the window or was not called). Does not vote — the record `(None, 0.0, speech)`; votes — `(the top family of the window, its share, speech)`.
3. No votes at all — `Detection(None, None, 0.0, False, 0, len(windows), records)`.
4. Otherwise the shares of each family are averaged over the voting windows; `bcms_share` — the average of `"sr"` (0.0 if absent); the winner — the largest average.
5. The winner is not `"sr"`, `bcms_share >= BCMS_FLOOR` and the winner is not in `SLAVIC_NEIGHBOURS` → the winner is `"sr"`, `floor=True`. `confidence` — the winner's average (at the threshold this is `bcms_share`).

`LanguageVerdict` — `@dataclass(frozen=True)`, the fields in the order from Interfaces.
- `mismatch` (property): the compared language is `detected` when `source == "asr"`, `effective` when `source == "manual"`; `True` only when both `declared` and the compared one are known and their `family()` differ (`hr` vs `sr` is not a mismatch).
- `note()` — exactly these templates, in order: on `mismatch` and `declared_by == "metadata"`: for asr `f"YouTube указал {declared}, по речи {detected}"`, for manual `f"YouTube указал {declared}, дорожка автора {effective}"`; on `mismatch` and `declared_by == "config"`: `f"в настройках {declared}, по речи {detected}"` / `f"в настройках {declared}, дорожка автора {effective}"`; otherwise `reason == "hint"` → `f"речи почти нет, язык взят у YouTube: {effective}"`; `reason == "fallback"` → `f"речи почти нет, язык принят за {effective}"`; otherwise `None`.
- `wire()` — exactly the keys `effective, declared, declaredBy, detected, confidence, reason, source, track, verified, mismatch, note`; `confidence` — `round(…, 2)` or `None`; `mismatch` and `note` — computed.
- `from_wire(data)` (`@classmethod`) — never raises: not a dict → `None`; `effective` not a string or not from `WHISPER_CODES` (without coercion: `"SR"` and `"sh"` — `None`) → `None`. The rest is lenient: `declared`, `declaredBy`, `detected`, `track` — a non-empty string or `None`; `confidence` — `int`/`float`, but not `bool`, otherwise `None`, stored as `float`; `reason` — a string or `"detector"`; `source` — `"manual"`, only if that is what is recorded, otherwise `"asr"`; `verified` — `bool` or `None`; `mismatch` and `note` are not read.

`decide(pin, hint, detection, *, fallback="sr")` — `pin` and `hint` are already Whisper codes (the callers reduce them: `normalize` and `hint`). `effective`: `pin`, if present; otherwise `detection.language`, if `detection.votes`; otherwise `hint`; otherwise `fallback` — never `""` and never `None`. `reason`: `"pin"` | (`"floor"` if `detection.floor` else `"detector"`) | `"hint"` | `"fallback"`. `declared = pin or hint`; `declared_by = "config" if pin else ("metadata" if hint else None)`; `detected`, `confidence` — from `detection`; `source` and the rest — defaults. The hint does not outweigh voting windows (the Juske case).

- [ ] **Step 4: Run them**

Run: `.venv/bin/python -m pytest tests/test_language.py -q`
Expected: PASS, 123 passed

- [ ] **Step 5: Write failing tests for the worker and the daemon**

```python
# tests/test_worker.py
"""Recognition worker without a GPU: detector windows, the language verdict, argument parsing."""

import json
import sys

import numpy as np
import pytest

from helper import language
from helper.asr import worker

SR = language.SAMPLE_RATE
WINDOW = language.WINDOW_SAMPLES


class Model:
    """WhisperModel for _detect: answers with the distributions in turn and remembers what it heard."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        # The lengths of the excerpts given to detect_language, in order.
        self.heard: list[int] = []

    def detect_language(self, audio=None, features=None, vad_filter=False, **kwargs):
        assert vad_filter is True
        self.heard.append(audio.size)
        answer = self.answers.pop(0)

        if isinstance(answer, Exception):
            raise answer

        return answer[0][0], answer[0][1], answer


@pytest.fixture
def vad(monkeypatch):
    """VAD without silero: speech is the non-zero samples of the excerpt."""
    import faster_whisper.vad

    def speech(audio, vad_options=None, sampling_rate=16000, **kwargs):
        voiced = int(np.count_nonzero(audio))

        return [{"start": 0, "end": voiced}] if voiced else []

    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", speech)


def recording(seconds: float, speech: list[float]) -> np.ndarray:
    """Silence of length seconds; at the start of each window — as many seconds of “speech” as in speech."""
    audio = np.zeros(int(seconds * SR), dtype="<f4")

    for offset, voiced in zip(language.probe_offsets(audio.size), speech):
        audio[offset : offset + int(voiced * SR)] = 0.1

    return audio


def test_detect_hears_only_windows_with_speech(vad):
    model = Model(*[[("sr", 0.9), ("en", 0.1)]] * 6)

    detection = worker._detect(model, recording(20 * 60, [30, 30, 30, 2, 0, 30, 30, 30]))

    assert model.heard == [WINDOW] * 6
    assert (detection.votes, detection.probed) == (6, 8)
    assert detection.language == "sr"
    assert [w[2] for w in detection.windows] == [30.0, 30.0, 30.0, 2.0, 0.0, 30.0, 30.0, 30.0]


def test_window_where_the_detector_fails_does_not_vote(vad):
    # Without a Serbian-family share: the outcome does not depend on BCMS_FLOOR and its calibration (Task 12).
    model = Model(RuntimeError("CUDA"), *[[("en", 0.95), ("de", 0.05)]] * 7)

    detection = worker._detect(model, recording(20 * 60, [30] * 8))

    assert (detection.votes, detection.probed) == (7, 8)
    assert detection.windows[0] == (None, 0.0, 30.0)
    assert detection.language == "en"


def test_short_audio_is_one_window_on_the_same_path(vad):
    audio = np.full(20 * SR, 0.1, dtype="<f4")
    model = Model([("hr", 0.45), ("sr", 0.40), ("en", 0.15)])

    detection = worker._detect(model, audio)

    assert model.heard == [audio.size]
    assert detection.probed == 1
    # The family, not the top label: today this path gave hr 0.45.
    assert detection.language == "sr"
    assert detection.confidence == pytest.approx(0.85)


def test_audio_between_30_and_37_5_seconds_gives_whole_windows(vad):
    model = Model(*[[("sr", 0.9), ("en", 0.1)]] * 4)

    detection = worker._detect(model, np.full(int(31.25 * SR), 0.1, dtype="<f4"))

    assert model.heard == [WINDOW] * 4
    assert detection.probed == 4


def test_no_votes_means_one_pass_over_the_whole_file(vad):
    audio = recording(20 * 60, [2] * 8)
    # Ten seconds of speech in the intro, outside the windows: the windows start at the 120th second.
    audio[: 10 * SR] = 0.1
    model = Model([("sr", 0.8), ("en", 0.2)])

    detection = worker._detect(model, audio)

    assert model.heard == [audio.size]
    assert (detection.votes, detection.probed) == (1, 9)
    assert detection.windows[-1] == ("sr", pytest.approx(0.8), 26.0)
    assert detection.language == "sr"


def test_silent_file_gets_no_vote_and_no_model_call(vad):
    model = Model()

    detection = worker._detect(model, np.zeros(20 * 60 * SR, dtype="<f4"))

    assert model.heard == []
    assert (detection.language, detection.votes, detection.probed) == (None, 0, 9)
    assert detection.windows[-1] == (None, 0.0, 0.0)


def test_short_silent_audio_makes_no_extra_pass(vad):
    model = Model()

    detection = worker._detect(model, np.zeros(10 * SR, dtype="<f4"))

    assert model.heard == []
    assert (detection.votes, detection.probed) == (0, 1)


SERBIAN = language.Detection(
    language="sr", confidence=0.97, bcms_share=0.97, floor=False, votes=8, probed=8,
    windows=(("sr", 0.97, 25.0),) * 8,
)
ENGLISH = language.Detection(
    language="en", confidence=0.95, bcms_share=0.05, floor=False, votes=8, probed=8,
    windows=(("en", 0.95, 25.0),) * 8,
)
SILENCE = language.Detection(
    language=None, confidence=None, bcms_share=0.0, floor=False, votes=0, probed=9,
    windows=((None, 0.0, 0.0),) * 9,
)
AUDIO = np.zeros(5 * SR, dtype="<f4")


def detector_says(monkeypatch, detection) -> list[int]:
    """_detect answers with a ready Detection; the list — the lengths of recordings it was called on."""
    heard: list[int] = []

    def detect(model, audio):
        heard.append(audio.size)

        return detection

    monkeypatch.setattr(worker, "_detect", detect)

    return heard


def test_detector_runs_even_with_a_pin(whisper, monkeypatch):
    heard = detector_says(monkeypatch, ENGLISH)

    result = worker.transcribe(AUDIO, language="sr")

    assert heard == [AUDIO.size]
    assert whisper.kwargs["language"] == "sr"
    assert result["language"] == "sr"
    assert result["verdict"]["reason"] == "pin"
    assert result["verdict"]["detected"] == "en"
    assert result["verdict"]["note"] == "в настройках sr, по речи en"


def test_speech_outweighs_the_youtube_label(whisper, monkeypatch):
    detector_says(monkeypatch, SERBIAN)

    result = worker.transcribe(AUDIO, language="auto", hint="en-US")

    assert whisper.kwargs["language"] == "sr"
    assert (result["verdict"]["declared"], result["verdict"]["declaredBy"]) == ("en", "metadata")
    assert result["verdict"]["mismatch"] is True


@pytest.mark.parametrize(
    ("hint", "decoded", "reason"),
    [(None, "sr", "fallback"), ("en-US", "en", "hint"), ("xx-YY", "sr", "fallback")],
)
def test_without_speech_whisper_never_gets_an_empty_language(whisper, monkeypatch, hint, decoded, reason):
    detector_says(monkeypatch, SILENCE)

    result = worker.transcribe(AUDIO, language="auto", hint=hint)

    assert whisper.kwargs["language"] == decoded
    assert result["language"] == decoded
    assert result["verdict"]["reason"] == reason
    assert result["verdict"]["detected"] is None


def test_result_carries_the_verdict_and_the_windows(whisper, monkeypatch):
    detector_says(monkeypatch, SERBIAN)

    result = worker.transcribe(AUDIO, language="auto")

    assert set(result) == {"language", "verdict", "detection", "duration", "model", "cues"}
    assert result["detection"] == {
        "bcmsShare": 0.97,
        "votes": 8,
        "probed": 8,
        "windows": [["sr", 0.97, 25.0]] * 8,
    }
    json.dumps(result)


def test_unknown_pin_fails_before_the_model_loads(monkeypatch):
    import faster_whisper

    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda *a, **k: pytest.fail("модель грузить рано"))

    with pytest.raises(language.UnknownLanguage):
        worker.transcribe(AUDIO, language="serbian")


def test_parser_keeps_the_cli_contract():
    args = worker.build_parser().parse_args([])

    # helper/measure.py does not pass --language and relies on sr.
    assert args.language == "sr"
    assert args.hint is None
    assert (args.model, args.compute_type, args.batch_size) == (worker.DEFAULT_MODEL, "float16", 16)
    assert (args.no_vad, args.initial_prompt, args.hotwords, args.pcm) == (False, None, None, None)


def test_main_refuses_an_unknown_pin_before_reading_audio(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["helper.asr.worker", "--language", "serbian"])
    monkeypatch.setattr(worker, "transcribe", lambda *a, **k: pytest.fail("до распознавания доходить нельзя"))

    assert worker.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "неизвестный язык «serbian»: [asr] language в config.toml",
        "permanent": True,
    }
```

The last test also checks the order: under pytest's output capture reading stdin raises `OSError`, so the refusal must happen before reading the audio.

In `tests/test_job_language.py`, add the import `from helper.asr import worker` after `from helper import jobs, language` and append to the end:

```python
# ── The declaration goes to the worker as a hint ─────────────────────────────────


def test_declaration_is_the_pin_and_the_normalised_label(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert runner_with(manager, "SR")._declaration(job) == ("sr", None)

    job.info = metadata(audio_language="en-US")

    assert runner_with(manager, "SR")._declaration(job) == ("sr", "en")
    assert runner_with(manager, "auto")._declaration(job) == (None, "en")

    job.info = metadata(audio_language="xx-YY")

    assert runner_with(manager, "auto")._declaration(job) == (None, None)


@pytest.mark.parametrize(
    ("pin", "audio_language", "sent", "hint"),
    [
        ("auto", "en-US", "auto", "en"),
        ("auto", None, "auto", None),
        ("auto", "xx-YY", "auto", None),
        ("sr", "en", "sr", "en"),
        ("sh", None, "sr", None),
    ],
)
async def test_worker_argv_parses_with_the_worker_parser(manager, pipeline, pin, audio_language, sent, hint):
    pipeline.configure(language=pin)
    job = manager.get_or_create(VIDEO, "ru", True)
    job.info = metadata(audio_language=audio_language)

    await pipeline.run(job)

    args = pipeline.workers[-1].args
    parsed = worker.build_parser().parse_args(args[3:])

    assert args[1:3] == ["-m", "helper.asr.worker"]
    assert (parsed.language, parsed.hint) == (sent, hint)
```

Parsing argv with the worker's real parser is the daemon–worker contract: any flag the daemon sends and the worker does not know will fail the test with argparse's `SystemExit`.

- [ ] **Step 6: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_worker.py tests/test_job_language.py -q`
Expected: FAIL — 22 failed, 20 passed (the Task 4 tests pass). In `test_worker.py` all 16: the old `_detect` calls the model on every window and returns a tuple (`assert [480000, …] == […]`, `AttributeError: 'tuple' object has no attribute 'votes'` (at 20 s — `… no attribute 'probed'`), at 31.25 s — shortened windows `[450000, …]`, on silence — `IndexError: pop from empty list`: the stub has nothing to answer), under a pin the old `transcribe` does not call the detector (`assert [] == [80000]`), and under `auto` it unpacks its answer (`TypeError: cannot unpack non-iterable Detection object`), `TypeError: transcribe() got an unexpected keyword argument 'hint'`, `Failed: модель грузить рано`, `AttributeError: module 'helper.asr.worker' has no attribute 'build_parser'`, `OSError: pytest: reading from stdin while output is captured!`. In `test_job_language.py` 6: `AttributeError: 'JobRunner' object has no attribute '_declaration'` and the same `build_parser`

- [ ] **Step 7: The worker**

`helper/asr/worker.py`:

- Imports: `import math` — to the standard ones, between `json` and `sys` (window seconds in the answer, see below); after `import helper.env` (line 25) — `from helper import language as lang` with a comment on why not `language`: the `transcribe` parameters are called `language` and `hint` and would shadow the module and its function. `SAMPLE_RATE = lang.SAMPLE_RATE` instead of `16000` (line 27): one definition; `jobs.py:33` and `spikes/asr_real.py:31` import it from here and keep working.
- Delete lines 36-122: `PROBE_WINDOWS`, `PROBE_SPAN`, `WINDOW_SECONDS` (the layout is now `lang.probe_offsets`), `BCMS` (now `lang.family`), the old `_detect` and `_merge_bcms`. In their place three functions (the behaviour is pinned by the seven `_detect` tests of Step 5):
  - `_speech_seconds(audio: np.ndarray) -> float` — the seconds of speech in the excerpt: the sum of `end - start` over `get_speech_timestamps(audio)` from `faster_whisper.vad`, divided by `SAMPLE_RATE`. The docstring: the same VAD with the same defaults as `detect_language(vad_filter=True)` — exactly as much as the detector will hear.
  - `_window(model, audio: np.ndarray, speech: float) -> tuple[float, list[tuple[str, float]]]` — one record for `summarize`. Less speech than `lang.MIN_SPEECH_SECONDS` — `(speech, [])`, the model is not called on such an excerpt. Otherwise the distribution is the third element of `model.detect_language(audio=audio, vad_filter=True)`'s answer, the record `(speech, list(probs))`. An exception from `detect_language` — also `(speech, [])`: `except Exception` marked `# noqa: BLE001 — one window must not bring recognition down`.
  - `_detect(model, audio: np.ndarray) -> lang.Detection` — windows by `lang.probe_offsets(audio.size)`, each `audio[offset : offset + lang.WINDOW_SAMPLES]` with its own speech by `_speech_seconds`, and `lang.summarize` over all the records. If no window voted and the recording is longer than a window (`audio.size > lang.WINDOW_SAMPLES`), the last record is a pass over the whole file, `_window(model, audio, min(30.0, _speech_seconds(audio)))`, and `summarize` is computed again.

  The `get_speech_timestamps` import from `faster_whisper.vad` — inside `_speech_seconds`, not at module level: `jobs.py` imports this module for `SAMPLE_RATE`, and faster_whisper is not needed in the daemon's process; the test patches `faster_whisper.vad.get_speech_timestamps`, and the patch is visible only when the attribute is read during the call. The `_detect` docstring: the model arrives already loaded (one for detection and recognition); we lay out the windows ourselves, because the stock `language_detection_segments` is “the first N×30 seconds of speech in a row”, and its 0.5 threshold stops the scan at the first window; a window with less speech than `MIN_SPEECH_SECONDS` is not given to the model — on padding it would answer anyway, and silence would vote on par with speech; a failed window does not vote; no window voted and the recording is longer than a window — one pass over the first thirty seconds of speech of the whole file, as the last record in `windows` (the ninth on a video longer than 37.5 s; the speech might have fallen between the windows), and the model is called on it only if the speech in the file is at least `MIN_SPEECH_SECONDS`. The old docstring (“The excerpt is taken from 40% of the duration”) and the comment “The window could have been entirely silence — VAD will return empty” are wrong and go away together with the function.

- `transcribe` (125-215): the new parameter `hint: str | None = None` right after `language`; the rest of the signature is the previous one (`initial_prompt` stays until Task 8). The order inside: the faster_whisper import as before; `pin = lang.normalize(language)` **before** `WhisperModel(...)` — an invalid code raises `UnknownLanguage` without loading the weights (`"auto"` is part of `NO_LANGUAGE`, so a separate check `language == "auto"` is not needed); `WhisperModel(...)`; `detection = _detect(model, audio)` — **always**, even with a pinned language; `verdict = lang.decide(pin, lang.hint(hint), detection)`; in `pipeline.transcribe` — `language=verdict.effective`. The comment above this argument (156-158, “The language is pinned hard… looks only at the first 30 seconds”) is replaced: the language is decided above and is never empty and never `None`; the library's own detection listens only to the first thirty seconds of speech, and that is where the intro is; bs/hr/sr are three tokens of one language to it, with an arbitrary choice. The block `detected, confidence = "", 0.0` / `if language == "auto": …` (142-150) is deleted. The answer (205-215):

```python
    return {
        # The language recognition ran with: the daemon transliterates the cues by it.
        "language": verdict.effective,
        "verdict": verdict.wire(),
        # Detector windows — for the daemon's journal and for tuning the thresholds against it.
        "detection": {
            "bcmsShare": round(detection.bcms_share, 2),
            "votes": detection.votes,
            "probed": detection.probed,
            # Speech seconds — rounded down: a window shown with MIN_SPEECH_SECONDS or more went into the model.
            "windows": [
                [code, round(share, 2), math.floor(speech * 10) / 10]
                for code, share, speech in detection.windows
            ],
        },
        "duration": info.duration,
        "model": model_name,
        "cues": [c for c in cues if c["text"]],
    }
```

The top-level `detected` and `confidence` keys are gone: they live in `verdict`. The daemon reads them only in the dead journal line `jobs.py:932-934`, which `_accept_asr` replaces in Task 6; until then the line stays dead, as it was.

- `build_parser() -> argparse.ArgumentParser` — move out of `main` (219-231) all the flags as they are, plus `parser.add_argument("--hint", default=None, help="метка языка от YouTube, только подсказка")` right after `--language`. `--language` keeps the default `"sr"`, the comment: the code pinned by the owner, or auto; `helper/measure.py`, which does not pass a language, relies on the `sr` default.
- `main()`: `args = build_parser().parse_args()`; **before** reading the audio (234) — the pin check:

```python
    try:
        lang.normalize(args.language)
    except lang.UnknownLanguage as exc:
        print(json.dumps({"error": lang.refusal(exc.raw), "permanent": True}, ensure_ascii=False))
        return 2
```

  with the comment: the refusal costs exactly the argument parsing; code 2 and `"permanent"` — the daemon turns this into a final refusal (`_run_asr`, Task 4). Add `hint=args.hint` to the `transcribe` call. Do not change the other JSON errors (`на вход не пришло аудио`, `аудио короче половины секунды`) and their code 1.
- The module docstring, the example on line 14: `python -m helper.asr.worker --language auto --hint en` and the line under it: `--language` — the code pinned by the owner, or auto; `--hint` — the language label from YouTube; the detector listens to the recording in any case, the verdict — `helper.language.decide`.

`helper/measure.py` does not change: without `--language` the worker takes the pin `sr`, and the detector will now run up to eight windows through one encoder before the measured recognition; peak memory is still given by batched recognition. The harness test from Task 3 that calls the real `transcribe` (`test_whisper_fixture_runs_the_real_worker_transcribe`) now runs the real Silero VAD after this task — the `whisper` fixture does not replace it: on the CPU, without network and a GPU, about 0.1 s per 5 s of silence. There is no speech in zeros, and the fake `detect_language` is not even called. The `transcribe` tests of this task patch `worker._detect` (`detector_says`), while the direct `_detect` tests run on their own `Model` stub and the `vad` fixture.

- [ ] **Step 8: Daemon: the declaration is a hint**

`helper/jobs.py`: delete the `_source_language` method entirely (after Task 4 its config branch calls `self._pin()`); the `_pin` method standing right above it stays; `_declaration` — in place of `_source_language`, under `_pin`:

```python
    def _declaration(self, job: Job) -> tuple[str | None, str | None]:
        return self._pin(), (language.hint(job.info.audio_language) if job.info else None)
```

The docstring: what is said about the language before the audio — (the owner's pin, the YouTube label); it is not this that decides, but the worker (`helper.language.decide`), the detector always listens; the YouTube label is only a hint: yt-dlp derives it from YouTube's ASR track, and that one labels Serbian channels as English (Juske), while Serbian videos usually have none at all (60 out of 60 in the corpus — the measurement from the old docstring); an unfamiliar label is simply “we don't know”. The declaration for the video and for the link will arrive here later (“Deferred”): the declaration for the verdict, the cache key and the track choice is read only here; the exceptions are the pin check at the start of `_pipeline` (`_pin`) and restore (`JobManager._key_for`: there is no job there, it reads the config through `hint` and changes together with this function). The journal line “язык %s взят из метаданных, распознаватель определять не будет” goes away together with the early return.

`JobRunner._run_asr` (1153-1167): `pin, hint = self._declaration(job)` next to `prompt = _initial_prompt(job.info)`; in argv instead of `self._source_language(job)` — `pin or "auto"`; after the list, before `--initial-prompt`:

```python
        if hint:
            args += ["--hint", hint]
```

Do not touch the result block in `_pipeline` (929-941): `_accept_asr` replaces it in Task 6. Do not touch `--initial-prompt` and `_initial_prompt`: that is Task 8.

- [ ] **Step 9: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_worker.py tests/test_job_language.py -q`
Expected: PASS, 42 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 331 passed

- [ ] **Step 10: Commit**

```bash
/usr/bin/git add helper/language.py helper/asr/worker.py helper/jobs.py tests/test_language.py tests/test_worker.py tests/test_job_language.py
/usr/bin/git commit -m "Определитель языка слушает всегда: голосуют только окна с речью, порог сербского семейства, метка YouTube — лишь подсказка"
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---
### Task 6: The language verdict is visible to clients

**Files:** line numbers are from `98a3ad2`, the previous tasks have shifted them: search by function name.
- Modify: `helper/jobs.py` — imports (line 37, next to `from helper.gpulease import GpuLease, Preempted`); `Job.__init__` (239-337, next to `self.source`, 269-271); `Job.snapshot` (427-451, after `"generation"`, 440); the new `Job._set_language` between `Job._set_stage` (453-457) and `Job._fail` (459-463); the new module-level `_log_language` between `refusal_snapshot` (688-701) and `class JobRunner` (704); `JobRunner._pipeline` (772-943) — the author-track `else:` branch (853-860) and the block after `_run_asr_watched` (926-941); the new `JobRunner._accept_asr` right after `JobRunner._run_asr_watched` (1070-1096)
- Modify: `tests/test_limits.py:243-276` (`test_events_with_create_answers_200_with_an_error_snapshot`)
- Modify: `tests/conftest.py` — next to `metadata` (Task 3), three ordinary functions: `watch`, `hear_until_done`, `preempt_after_asr`
- Test: `tests/test_language_wire.py` (new)

**Interfaces:**
- Consumes:
  - `helper.language.LanguageVerdict` (Task 5): fields in order `effective, declared, declared_by, detected, confidence, reason, source="asr", track=None, verified=None`; the `mismatch` property; `note() -> str | None`; `wire() -> dict` with the keys `effective, declared, declaredBy, detected, confidence, reason, source, track, verified, mismatch, note`; `LanguageVerdict.from_wire(data: object) -> LanguageVerdict | None` (None, not a dict, missing or invalid `effective` → `None`).
  - `language.normalize`, `language.UnknownLanguage`, `language.hint` (Task 4); the module is already imported in `helper/jobs.py` under the name `language` (Task 4).
  - `language.MIN_SPEECH_SECONDS` (Task 5, 3.0) — from that many seconds of speech a window goes to the detector; the window seconds in the worker's answer are rounded down to tenths (Task 5), so a window shown with this number or more went into the model. The warning threshold in `_log_language`.
  - `JobRunner._declaration(self, job) -> tuple[str | None, str | None]` — `(pin, metadata hint)` (Task 5).
  - The worker's answer (Task 5): `{"language": <effective code>, "verdict": LanguageVerdict.wire(), "detection": {"bcmsShare", "votes", "probed", "windows": [[code or null, share, speech seconds], ...]}, "duration", "model", "cues"}`.
  - The harness (Task 3): the builder `metadata(**overrides) -> VideoInfo` from `tests/conftest.py`; the `pipeline` fixture — `run(job)`, `workers`, `cache`, `configure(**asr)` and the inputs `pipeline.asr_result` (the JSON the fake worker answers with), `pipeline.info` (what `probe` returns; by default a video without author tracks and without `audio_language`), `pipeline.track` (what `fetch_manual_track` returns); its permit for the GPU is provided by a real `helper.gpulease._Holder`. The fixtures `manager`, `client`, `connection` already exist.
- Produces:
  - `Job.language: LanguageVerdict | None` — `None` until there is a verdict; it does not enter the registry key `(video_id, use_author_track)`.
  - `Job._set_language(self, verdict: LanguageVerdict) -> None` — sets `Job.language` and writes `{"type": "language", "language": verdict.wire()}` to the journal.
  - `Job.snapshot()["language"]` — `self.language.wire()` or `None`; in `refusal_snapshot` — `None`.
  - `JobRunner._accept_asr(self, job: Job, result: dict) -> None` — the language verdict, the cues and `segmented` from the worker's answer; Task 8 will replace its cue assembly with `asr_cues(...)`. An answer without a valid `verdict`, whose `language` does not reduce to a Whisper code — `PermanentJobError(f"распознаватель вернул неизвестный язык «{code}»")`: final, without a retry on the GPU.
  - `_log_language(job: Job, verdict: LanguageVerdict, windows: list) -> None` (module-level, private): INFO `язык <ролик>: <effective> (<reason>), объявлен <declared> (<declared_by>), по речи <detected>, уверенность <0.00>, окна: <код> <0.00>/<N>с, ...`, where every missing value (the declaration as a whole, the decided language, the confidence, a window code without a vote, the list of windows) is written with the character `—`; WARNING `язык <ролик>: <note()>` on `mismatch`; WARNING `язык <ролик>: определитель не ответил, окон с речью без ответа: <N>` when at least one window has the code `None` with speech seconds not less than `language.MIN_SPEECH_SECONDS`.
  - Event order: `language` goes before `segmented` on the recognition and author-track paths (cache and restore — Task 7).
  - In `tests/conftest.py`, next to `metadata`, — ordinary functions, the tests take them `from conftest import …`: `watch(video_id, **fields) -> dict` — the tab's `watch` message; `async hear_until_done(connection) -> list[dict]` — everything the socket heard until the `state` event with the `done` or `error` stage; `preempt_after_asr(pipeline, monkeypatch) -> None` — the GPU is taken away on the first preemption check after the worker (patching `gpulease._Holder.check`). Task 7 takes all three.

- [ ] **Step 1: Write failing tests**

In `tests/conftest.py`, right after the `metadata` function (Task 3), — three ordinary functions. This task and Task 7 use them; `helper.*` is still imported inside the function:

```python
def watch(video_id, **fields):
    """The watch message from the tab: a subscription to the video's job."""
    return {"op": "watch", "videoId": video_id, **fields}


async def hear_until_done(connection):
    """Everything the socket heard until the job ends: until the state event with the done or error stage."""
    heard = []

    while True:
        event = await connection.hear()
        heard.append(event)

        if event["type"] == "state" and event["stage"] in ("done", "error"):
            return heard


def preempt_after_asr(pipeline, monkeypatch):
    """The GPU is taken right after recognition: the first preemption check after the worker."""
    from helper import gpulease

    real_check = gpulease._Holder.check
    taken = []

    def check(holder):
        if pipeline.workers and not taken:
            taken.append(holder)

            raise gpulease.Preempted("пользователь начал свою работу на GPU")

        real_check(holder)

    monkeypatch.setattr(gpulease._Holder, "check", check)
```

```python
# tests/test_language_wire.py
"""The language verdict on the wire: the language field in the snapshot and the language event before the cues."""

import logging

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import jobs
from helper.jobs import Stage, refusal_snapshot
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack

# Eleven characters, like a real video's.
VIDEO = "langwire001"

# Serbian speech on a channel YouTube labelled English (Juske, Q2QcYOBk0Jo):
# the detector is sure of Serbian, the declaration diverges from the speech.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# An English video without metadata: there is nothing to diverge from, nothing to say.
ENGLISH = LanguageVerdict(
    effective="en", declared=None, declared_by=None,
    detected="en", confidence=0.99, reason="detector",
)
# No window voted, and the language is taken from YouTube.
HINTED = LanguageVerdict(
    effective="en", declared="en", declared_by="metadata",
    detected=None, confidence=None, reason="hint",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
TRACK = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
# The snapshot keys before this plan. The new field is one, and the extension does not read its name.
OLD_SNAPSHOT_KEYS = {
    "type", "videoId", "authorTrack", "targetLang", "generation", "stage", "error",
    "retryable", "waiting", "title", "total", "translated", "cues",
}
# Event types the extension reacts to with an action (content.js, background.js).
EXTENSION_TYPES = {"state", "snapshot", "cues", "helper", "restored", "gone"}


def author_track(lang):
    return CaptionTrack(lang=lang, url="", ext="json3", name="")


def answer(verdict, windows=(), cues=CUES):
    """The worker's answer according to the Task 5 protocol: the verdict, the detector windows, the cues."""
    return {
        "language": verdict.effective,
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.0,
            "votes": sum(code is not None for code, _, _ in windows),
            "probed": len(windows),
            "windows": [list(window) for window in windows],
        },
        "duration": 2.0,
        "model": "large-v3",
        "cues": cues,
    }


def language_lines(caplog):
    """The journal lines about this video's language verdict: level and text."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "helper.jobs" and record.getMessage().startswith(f"язык {VIDEO}:")
    ]


def test_snapshot_has_no_language_until_the_daemon_decides(manager):
    job = manager.get_or_create(VIDEO, "ru", True)

    assert job.language is None
    assert job.snapshot()["language"] is None
    assert refusal_snapshot(VIDEO, "ru", True, "отказ", True)["language"] is None


def test_the_language_event_lands_in_the_journal_and_the_snapshot(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_stage(Stage.QUEUED, waiting="gpu")

    job._set_language(JUSKE)

    assert job.language is JUSKE
    assert job.events[-1] == {"type": "language", "language": JUSKE.wire()}
    assert job.snapshot()["language"] == JUSKE.wire()
    # Not via _set_stage: the stage and the GPU wait stay, the badge label is the same.
    assert job.stage is Stage.QUEUED
    assert job.waiting == "gpu"


def test_the_language_reuses_no_name_the_extension_reads(manager):
    job = manager.get_or_create(VIDEO, "ru", True)
    job._set_language(JUSKE)
    event = job.events[-1]
    snapshot = job.snapshot()

    assert event.keys() == {"type", "language"}
    assert event["type"] not in EXTENSION_TYPES
    assert snapshot.keys() - OLD_SNAPSHOT_KEYS == {"language"}
    assert snapshot.keys() >= OLD_SNAPSHOT_KEYS


async def test_the_socket_hears_the_language_before_segmented_after_asr(
    manager, connection, pipeline
):
    pipeline.asr_result = answer(JUSKE)
    await connection.say(watch(VIDEO))
    caught_up = await connection.hear()

    assert (caught_up["type"], caught_up["language"]) == ("snapshot", None)

    await pipeline.run(manager.get(VIDEO, True))
    heard = await hear_until_done(connection)
    types = [event["type"] for event in heard]
    at = types.index("language")

    assert at < types.index("segmented")
    # The verdict arrives right after recognition: the worker makes it.
    assert heard[at - 1]["stage"] == "asr"
    assert heard[at] == {"type": "language", "language": JUSKE.wire()}
    assert heard[at]["language"]["mismatch"] is True
    assert heard[at]["language"]["note"] == "YouTube указал en, по речи sr"
    assert heard[-1]["stage"] == "done"


async def test_the_job_endpoint_and_the_poll_carry_the_language(client, manager, pipeline):
    job = manager.get_or_create(VIDEO, "ru", True)
    before = job.journal_end
    pipeline.asr_result = answer(JUSKE)

    await pipeline.run(job)

    snapshot = (await client.get(f"/v1/jobs/{VIDEO}")).json()
    stale = (await client.get(f"/v1/jobs/{VIDEO}/events", params={"cursor": "-1"})).json()
    polled = (
        await client.get(f"/v1/jobs/{VIDEO}/events", params={"cursor": str(before)})
    ).json()
    types = [event["type"] for event in polled["events"]]

    assert snapshot["language"] == JUSKE.wire()
    assert stale["snapshot"]["language"] == JUSKE.wire()
    assert types.index("language") < types.index("segmented")


async def test_the_socket_hears_the_language_before_segmented_on_the_author_track(
    manager, connection, pipeline, caplog
):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.info = metadata(audio_language="en", manual_tracks=[author_track("sr")])
    pipeline.track = TRACK
    await connection.say(watch(VIDEO))
    await connection.hear()

    await pipeline.run(manager.get(VIDEO, True))
    heard = await hear_until_done(connection)
    types = [event["type"] for event in heard]
    expected = LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected=None,
        confidence=None, reason="track", source="manual", track="sr", verified=None,
    )

    assert types.index("language") < types.index("segmented")
    assert heard[types.index("language")] == {"type": "language", "language": expected.wire()}
    assert expected.wire()["note"] == "YouTube указал en, дорожка автора sr"
    assert not pipeline.workers
    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: sr (track), объявлен en (metadata), по речи —, "
            "уверенность —, окна: —",
        ),
        (logging.WARNING, f"язык {VIDEO}: YouTube указал en, дорожка автора sr"),
    ]


@pytest.mark.parametrize(("label", "effective"), [("sr-Latn", "sr"), ("sh", "sr"), ("hr", "hr")])
async def test_the_author_track_label_names_the_language(manager, pipeline, label, effective):
    pipeline.info = metadata(manual_tracks=[author_track(label)])
    pipeline.track = TRACK
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.language == LanguageVerdict(
        effective=effective, declared=None, declared_by=None, detected=None,
        confidence=None, reason="track", source="manual", track=label, verified=None,
    )


async def test_a_pin_is_the_declaration_of_an_author_track(manager, pipeline):
    pipeline.configure(language="hr")
    pipeline.info = metadata(audio_language="en", manual_tracks=[author_track("sr")])
    pipeline.track = TRACK
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.language == LanguageVerdict(
        effective="sr", declared="hr", declared_by="config", detected=None,
        confidence=None, reason="track", source="manual", track="sr", verified=None,
    )
    # hr and sr are one language with three tokens: no mismatch.
    assert job.language.mismatch is False


async def test_a_job_preempted_after_recognition_resumes_with_its_verdict(
    manager, pipeline, monkeypatch
):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = answer(JUSKE)
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED
    assert job.language == JUSKE
    assert [cue.src for cue in job.cues] == ["Dobar dan."]

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert len(pipeline.workers) == 1
    assert job.snapshot()["language"] == JUSKE.wire()
    assert [event["type"] for event in job.events].count("language") == 1


async def test_an_answer_without_a_verdict_falls_back_to_its_language(manager, pipeline):
    pipeline.asr_result = {"language": "sr", "duration": 2.0, "model": "large-v3", "cues": CUES}
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.language == LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected=None,
        confidence=None, reason="detector",
    )


async def test_an_answer_in_an_unknown_language_fails_the_job(manager, pipeline):
    pipeline.asr_result = {"language": "xx", "duration": 2.0, "model": "large-v3", "cues": CUES}
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.ERROR
    # Final: a retry would take the GPU for recognition again for the same answer.
    assert job.retryable is False
    assert job.error == "распознаватель вернул неизвестный язык «xx»"
    assert job.language is None
    assert job.cues == []


async def test_the_decision_is_one_info_line_with_the_windows(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(ENGLISH, windows=[("en", 0.99, 30.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: en (detector), объявлен —, по речи en, "
            "уверенность 0.99, окна: en 0.99/30с",
        ),
    ]


async def test_a_mismatch_is_a_warning_in_words(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    pipeline.asr_result = answer(JUSKE, windows=[("sr", 0.97, 28.0), (None, 0.0, 1.2)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: sr (detector), объявлен en (metadata), по речи sr, "
            "уверенность 0.97, окна: sr 0.97/28с, — 0.00/1с",
        ),
        (logging.WARNING, f"язык {VIDEO}: YouTube указал en, по речи sr"),
    ]
    # The dead line “язык речи … определитель дал” is gone: the two above took its place.
    assert not [r for r in caplog.records if "определитель дал" in r.getMessage()]


async def test_a_detector_that_never_answered_is_a_warning(manager, pipeline, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    # There is speech in the window, but no detector answer: zero votes, the YouTube label
    # decided — the same path by which Serbian speech under the en label would go into the English decoder.
    pipeline.asr_result = answer(HINTED, windows=[(None, 0.0, 25.0)])

    await pipeline.run(jobs.Job(VIDEO))

    assert language_lines(caplog) == [
        (
            logging.INFO,
            f"язык {VIDEO}: en (hint), объявлен en (metadata), по речи —, "
            "уверенность —, окна: — 0.00/25с",
        ),
        (logging.WARNING, f"язык {VIDEO}: определитель не ответил, окон с речью без ответа: 1"),
    ]
```

And one line in the existing test that lists the refusal snapshot fields one by one:

```python
# tests/test_limits.py, test_events_with_create_answers_200_with_an_error_snapshot,
# right after assert snapshot["waiting"] is None:
    assert snapshot["language"] is None
```

The comparison `snapshot.keys() == full[0].snapshot().keys()` in the same test stays true without edits: `refusal_snapshot` assembles the snapshot from a real `Job`. `refusal_event` (`helper/app.py:67-79`) is not touched — the tests that compare it for exact equality (`tests/test_limits.py:221-227`, `tests/test_create_paths.py`) stay green.

- [ ] **Step 2: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_language_wire.py tests/test_limits.py -q`
Expected: FAIL, 17 failed (16 new and `test_events_with_create_answers_200_with_an_error_snapshot`): `AttributeError: 'Job' object has no attribute 'language'` and `... '_set_language'`; `KeyError: 'language'` in the snapshots; `ValueError: list.index(x): x not in list` — there is no `language` event in the stream; the unknown-language test — `assert <Stage.DONE: 'done'> is <Stage.ERROR: 'error'>` (today any code from the worker's answer goes into `normalize_cues` without a check); the journal tests — `assert [] == [(20, 'язык langwire001: ...')]`.

- [ ] **Step 3: The field, the event and the snapshot**

In the imports of `helper/jobs.py`: `from helper.language import LanguageVerdict`.

`Job.__init__`, next to `self.source`: `self.language: LanguageVerdict | None = None`. The comment: which language the daemon decided and why; `None` until there is a verdict; written only through `_set_language`; it does not enter the registry key — a video variant is `(video_id, use_author_track)`, while the language is a property of the material.

`Job._set_language(self, verdict: LanguageVerdict) -> None` between `_set_stage` and `_fail`: `self.language = verdict`, then `self._emit({"type": "language", "language": verdict.wire()})`. The comment — why not via `_set_stage`: the latter clears `waiting` and sends `state`, on which `extension/content.js` (`updateChip`) redraws the badge, and `background.js` (`lastStage`) decides whether to keep polling. The extension skips an event of the new type: `trackStage`, `mergeCues` and `updateChip` act only on `state`, `snapshot` and `cues`. Checked by running the unchanged `content.js` and `background.js` in Node: a snapshot with a nested `language` and a `language` event leave the badge “перевожу… 2/290” as it was, and `lastStage` on a single `language` event gives `undefined` — polling continues.

`Job.snapshot()`: after `"generation"` — `"language": self.language.wire() if self.language else None`. The comment: the verdict whole, as one nested object; the top-level keys `source`, `message`, `waiting`, `retry` are read by `content.js` (lines 250, 364-370), and `source: "asr"` at the top would turn the badge into “качаю asr…”. `refusal_snapshot` does not change: it is assembled from a real `Job` and gets `"language": null` itself.

- [ ] **Step 4: `_accept_asr` — the worker's answer applied to the job**

In `_pipeline` (the recognition branch, 929-940 on `98a3ad2`), replace the lines from `segments = result["cues"]` through `job._emit({"type": "segmented", ...})` inclusive with two elements: a rewritten comment (the former 936–938, “Assign the cues BEFORE the preemption check…”) — “The language verdict and the cues before the preemption check: …” with the same reasoning: a job paused after recognition resumes at translation, and the language verdict must lie on it together with the cues, — and below it the call `self._accept_asr(job, result)`; do not move the comment into `_accept_asr` itself. The deletion of the deferred audio before them (guarded by Task 1) and `held.check()` after the call stay in place. The dead line `log.info("язык речи: %s (определитель дал %s, уверенность %.2f)", ...)` (932-934) is deleted: in batched mode `detected` always equals the passed one, and it never fired; `_log_language` takes its place.

`JobRunner._accept_asr` — a method right after `_run_asr_watched`. The order matters — the `language` event before `segmented`, and an unknown language aborts the job before anything appears on it:

```python
    def _accept_asr(self, job: Job, result: dict) -> None:
        """The worker's answer applied to the job: the language verdict first, then the cues and segmented."""
        verdict = LanguageVerdict.from_wire(result.get("verdict"))

        if verdict is None:
            # There is no verdict in the answer or it is invalid: the language is taken from the language
            # field by the same rule as the pin. An unknown code is a final refusal: a retry
            # a minute later would drive recognition on the GPU again and get the same answer.
            raw = result.get("language")

            try:
                effective = language.normalize(raw)
            except language.UnknownLanguage:
                effective = None

            if effective is None:
                raise PermanentJobError(f"распознаватель вернул неизвестный язык «{raw}»")

            verdict = LanguageVerdict(
                effective=effective, declared=None, declared_by=None,
                detected=None, confidence=None, reason="detector",
            )

        job._set_language(verdict)
        _log_language(job, verdict, (result.get("detection") or {}).get("windows") or [])
        job.cues = self._to_job_cues(
            build_cues(normalize_cues(result["cues"], verdict.effective))
        )
        job._emit({"type": "segmented", "total": len(job.cues)})
```

`_log_language(job: Job, verdict: LanguageVerdict, windows: list) -> None` — a module-level function between `refusal_snapshot` and `class JobRunner`. The line formats are in Interfaces and in the tests `test_the_decision_is_one_info_line_with_the_windows`, `test_a_mismatch_is_a_warning_in_words`, `test_a_detector_that_never_answered_is_a_warning` and in the socket test on the author track. In order:
- one INFO line with the verdict and the windows; a window — `<code or —> <share:.2f>/<int(seconds)>с`, windows comma-separated, an empty list — `—`; the seconds are whole and rounded down, as on the wire: a window shown with three seconds or more went into the model (`:.0f` would show a window of 2.6–2.9 s that never reached the model as “3с” — without the warning below);
- WARNING `note()` on `mismatch`;
- WARNING “определитель не ответил, окон с речью без ответа: N” when N ≥ 1 windows have the code `None` with speech seconds not less than `language.MIN_SPEECH_SECONDS`. The comment: without votes the verdict silently goes to the YouTube label or to `sr`, and a broken detector (failing on every window, say, after a faster-whisper update) would look like a video without speech; a window shorter than the threshold never went into the model, and staying silent about it is right.

The docstring: the language verdict goes to the journal, an INFO line and warnings in words; the extension will not show the verdict, and there is no tablet page yet: until it exists, the journal and the snapshot (`GET /v1/jobs/{id}`) are the only places where the verdict is visible.

- [ ] **Step 5: The verdict on the author-track path**

In `_pipeline`, the `else:` branch after a successful `fetch_manual_track` (853-860), before `job.cues = ...` and `segmented`:

```python
                pin, hint = self._declaration(job)
                verdict = LanguageVerdict(
                    effective=language.hint(track.lang) or "sr",
                    declared=pin or hint,
                    declared_by="config" if pin else ("metadata" if hint else None),
                    detected=None,
                    confidence=None,
                    reason="track",
                    source="manual",
                    track=track.lang,
                    verified=None,
                )
                job._set_language(verdict)
                _log_language(job, verdict, [])
```

The declaration is the same as in `decide`: the human's pin overrides the metadata. `verified=None` — the thirty-second probe is deferred (see “Deferred”). `normalize_cues(segments, track.lang)` remains as is for now: Task 10 moves it to `verdict.effective`. In the track-failure branch (`except YtDlpError`, 841-852) nothing is declared: the verdict goes out only after a successful download, and there is nothing to revoke.

Resuming needs to do nothing: a paused job holds `Job.language` the same way as `job.cues`, and `_pipeline` with ready cues goes straight to `_finish_translation`, without a second event. One recreated after a cooled-down refusal is a new `Job` with `language=None` and its own declaration.

- [ ] **Step 6: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_language_wire.py tests/test_limits.py -q`
Expected: PASS — 16 new tests green, no failure in `tests/test_limits.py`.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 347 passed

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add helper/jobs.py tests/conftest.py tests/test_language_wire.py tests/test_limits.py
/usr/bin/git commit -m "Решение о языке видно клиентам: поле language в снимке, событие раньше реплик, расхождение — предупреждением в журнале"
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---

### Task 7: The cache key and the recorded verdict

**Files:** line numbers are from `98a3ad2`, the previous tasks have shifted them: search by function name.
- Modify: `helper/cache.py` — the module docstring (7-9); the shared versions comment (24-25) and `SCHEMA_VERSION` (27); `CacheKey` (32-61, fields 34-44 and `digest` 46-61); `TrackCache.put` (137-185, the write 146-162)
- Modify: `helper/jobs.py` — the new module-level `track_key` next to `_log_language` (Task 6), before `class JobRunner` (704); `JobRunner._cache_key` (720-731); `JobRunner._finish_translation` (1098-1149, the cache write 1143-1148); `JobRunner._serve_cached` (1244-1274); `JobManager._key_for` (1652-1661); `JobManager.restore` (1663-1726: the call at 1696, the new job at 1704-1705); `JobManager._cached_variant` (1728-1765)
- Modify: `tests/test_job_identity.py:335` (`test_restore_from_cache_registers_the_variant_under_its_own_key`)
- Test: `tests/test_cache_flow.py` (new; `watch`, `hear_until_done` and `preempt_after_asr` are taken from `tests/conftest.py`, Task 6 — it does not create its own copies)

**Interfaces:**
- Consumes:
  - Task 6: `Job.language`, `Job._set_language(verdict)`, the `language` event.
  - Task 5: `LanguageVerdict.wire()`, `LanguageVerdict.from_wire(data) -> LanguageVerdict | None`; `JobRunner._declaration(job) -> tuple[str | None, str | None]` — `(pin, metadata hint)`, where the pin is `JobRunner._pin()` (Task 4), which `_pipeline` has already checked before `probe`.
  - Task 4: `language.hint(raw) -> str | None` (never raises).
  - The harness (Task 3): `metadata`, `pipeline` — `run`, `workers`, `cache` (a real `TrackCache`, its own per test), `configure(**asr)` (the runner is rebuilt, the cache is the same), the inputs `asr_result`, `info`, `track`; a pass is provided by a real `helper.gpulease._Holder`. Fixtures `manager`, `client`, `connection`. From Task 6 — the `tests/conftest.py` functions: `watch`, `hear_until_done`, `preempt_after_asr(pipeline, monkeypatch)`; the tests take them `from conftest import …`.
- Produces:
  - `helper.cache.CacheKey(video_id: str, pin: str, asr_model: str, translator_model: str, study_lang: str = "", track_fingerprint: str = "asr")` — no `target_lang`, `source_lang` renamed to `pin` (`""` — the human has not pinned a language). `digest()` — sha256 of `video_id|track_fingerprint|pin|asr_model|translator_model|study_lang|PROMPT_VERSION|SCHEMA_VERSION`. `SCHEMA_VERSION = 2`; `PROMPT_VERSION` stays 1 (Task 11 raises it).
  - `TrackCache.put(self, key: CacheKey, cues: list[dict], length_seconds: float | None, *, language: dict | None = None) -> None`; the write: `video_id, pin, asr_model, translator_model, study_lang, track_fingerprint, prompt_version, schema_version, length_seconds, language, created_at, cues` — without `source_lang` and `target_lang`. Readers take the verdict via `.get("language")`.
  - `helper.jobs.track_key(config: Config, video_id: str, fingerprint: str, pin: str | None) -> CacheKey` — the only recipe for the key.
  - `JobRunner._cache_key(job)` = `track_key(self._config, job.video_id, job.source, self._declaration(job)[0])`.
  - `JobManager._key_for(self, video_id: str, fingerprint: str) -> CacheKey` = `track_key(self.config, video_id, fingerprint, language.hint(self.config.asr.language))`.
  - `JobManager._cached_variant(self, video_id: str, use_author_track: bool, duration: float | None = None) -> dict | None` — without `target_lang`.
  - Behaviour: a result without cues — `{"type": "state", "stage": "done", "total": 0, "cached": false}` and it does not go into the cache; an entry without cues — a miss for `_serve_cached` too; a cache hit and a restore send `language` before `cues`; `Job.target_lang` and `targetLang` in the snapshot stay.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cache_flow.py
"""The cache key carries the human's pin, the entry — the language verdict, an empty track is not cached."""

import hashlib
import json
from dataclasses import fields

import pytest

from conftest import hear_until_done, metadata, preempt_after_asr, watch
from helper import cache as cache_module
from helper import jobs
from helper.cache import CacheKey, TrackCache
from helper.config import AsrConfig, Config, TranslatorConfig
from helper.jobs import Stage
from helper.language import LanguageVerdict
from helper.sources.ytdlp import CaptionTrack

# The video of the pipeline tests: the pipeline fixture cache is its own per test. The
# restore tests write into the manager.cache shared for the run, and each has its own video.
VIDEO = "cacheflow01"

# 3XJLfTSZy6I: Serbian speech without metadata, English on top in five windows out of eight,
# the Serbian-family share 0.44 — BCMS_FLOOR decided it.
FLOOR = LanguageVerdict(
    effective="sr", declared=None, declared_by=None,
    detected="sr", confidence=0.44, reason="floor",
)
# Juske, Q2QcYOBk0Jo: YouTube labelled it en, the speech is Serbian.
JUSKE = LanguageVerdict(
    effective="sr", declared="en", declared_by="metadata",
    detected="sr", confidence=0.97, reason="detector",
)
# The human pinned sr in [asr] language.
PINNED = LanguageVerdict(
    effective="sr", declared="sr", declared_by="config",
    detected="sr", confidence=0.97, reason="pin",
)
# No window voted, there is no hint.
SILENT = LanguageVerdict(
    effective="sr", declared=None, declared_by=None,
    detected=None, confidence=None, reason="fallback",
)
CUES = [{"start": 0.0, "end": 1.5, "text": "Dobar dan.", "words": []}]
# Ready cues in the entry — that is how _finish_translation (JobCue.payload()) stores them.
STORED = [
    {
        "i": 0, "start": 0.0, "end": 1.5, "src": "Dobar dan.", "lines": ["Добрый день."],
        "study": "Dobar dan.", "failed": False,
    },
]
# The value for rewrite: the field is absent from the entry altogether.
ABSENT = object()


def answer(verdict, cues=CUES):
    """The worker's answer according to the Task 5 protocol."""
    return {
        "language": verdict.effective,
        "verdict": verdict.wire(),
        "detection": {"bcmsShare": 0.0, "votes": 0, "probed": 0, "windows": []},
        "duration": 2.0,
        "model": "large-v3",
        "cues": cues,
    }


def key_of(stored):
    """The key of an entry from its own fields: the test hits exactly where the pipeline wrote."""
    return CacheKey(
        video_id=stored["video_id"], pin=stored["pin"], asr_model=stored["asr_model"],
        translator_model=stored["translator_model"], study_lang=stored["study_lang"],
        track_fingerprint=stored["track_fingerprint"],
    )


def rewrite(store, key, **changes):
    """Fix an entry on disk; a field with the ABSENT value is removed."""
    path = store._path(key)
    payload = json.loads(path.read_text(encoding="utf-8"))

    for name, value in changes.items():
        if value is ABSENT:
            payload.pop(name, None)
        else:
            payload[name] = value

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── key ─────────────────────────────────────────────────────────────────


def test_the_key_is_the_pin_recipe_without_the_target_language():
    key = CacheKey(
        video_id=VIDEO, pin="sr", asr_model="large-v3/float16",
        translator_model="qwen3.8:27b", study_lang="cnr", track_fingerprint="manual:sr",
    )
    raw = "|".join([
        VIDEO, "manual:sr", "sr", "large-v3/float16", "qwen3.8:27b", "cnr",
        str(cache_module.PROMPT_VERSION), str(cache_module.SCHEMA_VERSION),
    ])

    assert [field.name for field in fields(CacheKey)] == [
        "video_id", "pin", "asr_model", "translator_model", "study_lang", "track_fingerprint",
    ]
    assert key.digest() == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert cache_module.SCHEMA_VERSION == 2


def test_the_pipeline_and_restore_build_one_key(manager):
    job = jobs.Job(VIDEO)
    job.source = "manual:sr-Latn"
    expected = CacheKey(
        video_id=VIDEO, pin="", asr_model="large-v3/float16",
        translator_model="qwen3.8:27b", study_lang="cnr", track_fingerprint="manual:sr-Latn",
    )

    assert jobs.track_key(manager.config, VIDEO, "manual:sr-Latn", None) == expected
    assert manager.runner._cache_key(job) == expected
    assert manager._key_for(VIDEO, "manual:sr-Latn") == expected


def test_a_pin_goes_into_both_keys(manager, monkeypatch):
    pinned = Config(asr=AsrConfig(language="sr"), translator=TranslatorConfig())
    monkeypatch.setattr(manager, "config", pinned)
    monkeypatch.setattr(manager.runner, "_config", pinned)

    assert manager.runner._cache_key(jobs.Job(VIDEO)).pin == "sr"
    assert manager._key_for(VIDEO, "asr").pin == "sr"


def test_restore_reads_a_broken_pin_as_no_pin(manager, monkeypatch):
    # Refusing an invalid pin is the pipeline's business (Task 4). Restore only
    # reads: videos cached without a pin still open — an invalid code is
    # read as “not pinned”; ones made under a previously valid pin are not found
    # until the code is fixed.
    manager.cache.put(manager._key_for("brokenpin01", "asr"), STORED, 10.0)
    broken = Config(asr=AsrConfig(language="serbian"), translator=TranslatorConfig())
    # The daemon has one config for the registry and the runner: both are patched.
    monkeypatch.setattr(manager, "config", broken)
    monkeypatch.setattr(manager.runner, "_config", broken)

    assert manager._key_for(VIDEO, "asr").pin == ""
    assert manager.restore("brokenpin01", "ru", True, 10.0).stage is Stage.DONE


# ── entry ───────────────────────────────────────────────────────────────


def test_put_records_the_verdict_and_the_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    store = TrackCache(root=tmp_path / "tracks")
    config = Config(asr=AsrConfig(), translator=TranslatorConfig())
    asr = jobs.track_key(config, VIDEO, "asr", None)
    track = jobs.track_key(config, VIDEO, "manual:sr", None)

    try:
        store.put(asr, STORED, 10.0, language=FLOOR.wire())
        store.put(track, STORED, 10.0)
        payload = store.get(asr, 10.0)

        assert payload["language"] == FLOOR.wire()
        assert payload["pin"] == ""
        assert payload["schema_version"] == 2
        assert "source_lang" not in payload
        assert "target_lang" not in payload
        assert store.get(track, 10.0)["language"] is None
    finally:
        store._db.close()


async def test_a_second_view_hears_the_verdict_before_the_cues(manager, connection, pipeline):
    pipeline.asr_result = answer(JUSKE)
    await pipeline.run(jobs.Job(VIDEO))
    await connection.say(watch(VIDEO))

    assert (await connection.hear())["language"] is None

    job = manager.get(VIDEO, True)
    await pipeline.run(job)
    heard = await hear_until_done(connection)
    (stored,) = pipeline.cache.variants(VIDEO)

    assert [event["type"] for event in heard] == ["state", "language", "cues", "state"]
    assert heard[1] == {"type": "language", "language": JUSKE.wire()}
    assert heard[-1] == {"type": "state", "stage": "done", "cached": True, "total": 1}
    assert job.snapshot()["language"]["note"] == "YouTube указал en, по речи sr"
    assert len(pipeline.workers) == 1
    assert (stored["pin"], stored["language"]) == ("", JUSKE.wire())


async def test_metadata_does_not_split_the_cache(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    pipeline.info = metadata(audio_language="en")
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 1
    assert again.events[-1] == {"type": "state", "stage": "done", "cached": True, "total": 1}
    assert again.language == FLOOR


async def test_another_target_language_shares_the_cache(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO, "ru"))
    other = jobs.Job(VIDEO, "sr")

    await pipeline.run(other)

    assert len(pipeline.workers) == 1
    assert other.events[-1]["cached"] is True
    assert other.snapshot()["targetLang"] == "sr"


async def test_a_human_pin_is_another_entry(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    pipeline.configure(language="sr")
    pipeline.asr_result = answer(PINNED)
    pinned = jobs.Job(VIDEO)

    await pipeline.run(pinned)

    assert len(pipeline.workers) == 2
    assert pinned.language == PINNED
    assert sorted(stored["pin"] for stored in pipeline.cache.variants(VIDEO)) == ["", "sr"]


async def test_a_result_without_cues_is_not_cached(manager, pipeline):
    pipeline.asr_result = answer(SILENT, cues=[])
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.events[-1] == {"type": "state", "stage": "done", "total": 0, "cached": False}
    assert pipeline.cache.variants(VIDEO) == []
    assert job.snapshot()["language"]["note"] == "речи почти нет, язык принят за sr"


async def test_an_entry_without_cues_is_a_miss_for_the_pipeline_too(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    (stored,) = pipeline.cache.variants(VIDEO)
    key = key_of(stored)
    pipeline.cache.put(key, [], None, language=FLOOR.wire())
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 2
    assert again.events[-1] == {"type": "state", "stage": "done", "total": 1}
    assert len(pipeline.cache.get(key)["cues"]) == 1


async def test_a_cached_entry_without_a_verdict_is_served_with_language_null(manager, pipeline):
    pipeline.asr_result = answer(FLOOR)
    await pipeline.run(jobs.Job(VIDEO))
    (stored,) = pipeline.cache.variants(VIDEO)
    rewrite(pipeline.cache, key_of(stored), language=ABSENT)
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert len(pipeline.workers) == 1
    assert again.language is None
    assert again.snapshot()["language"] is None
    assert [event["type"] for event in again.events] == ["state", "cues", "state"]


async def test_a_resumed_job_writes_its_verdict(manager, pipeline, monkeypatch):
    preempt_after_asr(pipeline, monkeypatch)
    pipeline.asr_result = answer(JUSKE)
    job = jobs.Job(VIDEO)

    await pipeline.run(job)

    assert job.stage is Stage.PAUSED

    await pipeline.run(job)
    (stored,) = pipeline.cache.variants(VIDEO)

    assert job.stage is Stage.DONE
    assert stored["language"] == JUSKE.wire()


async def test_an_author_track_entry_keeps_its_verdict(manager, pipeline):
    pipeline.info = metadata(
        audio_language="en",
        manual_tracks=[CaptionTrack(lang="sr-Latn", url="", ext="json3", name="")],
    )
    pipeline.track = [{"start": 1.0, "end": 3.0, "text": "Zdravo svima."}]
    await pipeline.run(jobs.Job(VIDEO))
    again = jobs.Job(VIDEO)

    await pipeline.run(again)

    assert [event["type"] for event in again.events] == ["state", "language", "cues", "state"]
    assert again.source == "manual:sr-Latn"
    assert again.language == LanguageVerdict(
        effective="sr", declared="en", declared_by="metadata", detected=None,
        confidence=None, reason="track", source="manual", track="sr-Latn", verified=None,
    )


# ── restore ─────────────────────────────────────────────────────────────


async def test_restore_brings_the_verdict_back_before_the_cues(client, manager):
    video = "restverdict"
    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=JUSKE.wire())

    body = (await client.get(f"/v1/jobs/{video}/restore", params={"duration": "10"})).json()
    restored = manager.get(video, True)

    assert body["found"] is True
    assert body["snapshot"]["language"] == JUSKE.wire()
    assert [event["type"] for event in restored.events] == ["language", "cues", "state"]
    assert restored.events[0] == {"type": "language", "language": JUSKE.wire()}


@pytest.mark.parametrize(
    ("video", "verdict"),
    [("noverdict01", ABSENT), ("noverdict02", None), ("noverdict03", "sr")],
    ids=["absent", "null", "not-an-object"],
)
def test_an_entry_without_a_verdict_restores_with_language_null(manager, video, verdict):
    key = manager._key_for(video, "asr")
    manager.cache.put(key, STORED, 10.0)
    rewrite(manager.cache, key, language=verdict)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored.stage is Stage.DONE
    assert restored.language is None
    assert restored.snapshot()["language"] is None
    assert [event["type"] for event in restored.events] == ["cues", "state"]


def test_restore_follows_the_current_pin(manager, monkeypatch):
    video = "restpinned1"
    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=FLOOR.wire())
    pinned = Config(asr=AsrConfig(language="sr"), translator=TranslatorConfig())
    monkeypatch.setattr(manager, "config", pinned)

    assert manager.restore(video, "ru", True, 10.0) is None

    manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=PINNED.wire())

    assert manager.restore(video, "ru", True, 10.0).language == PINNED


def test_restore_never_takes_versions_or_models_from_the_entry(manager, monkeypatch):
    video = "restorphan1"
    other_model = Config(asr=AsrConfig(), translator=TranslatorConfig(model="translategemma:12b"))

    with monkeypatch.context() as patch:
        patch.setattr(cache_module, "PROMPT_VERSION", cache_module.PROMPT_VERSION - 1)
        manager.cache.put(manager._key_for(video, "asr"), STORED, 10.0, language=FLOOR.wire())

    manager.cache.put(
        jobs.track_key(other_model, video, "asr", None), STORED, 10.0, language=FLOOR.wire()
    )

    assert manager.restore(video, "ru", True, 10.0) is None
    # Both entries are in place: restore does not see them, but does not touch them either.
    assert len(manager.cache.variants(video)) == 2


def test_restore_prefers_the_newest_author_track(manager):
    video = "restnewest1"
    older = manager._key_for(video, "manual:sr")
    newer = manager._key_for(video, "manual:sr-Latn")
    manager.cache.put(older, [{**STORED[0], "src": "Stara"}], 10.0)
    manager.cache.put(newer, [{**STORED[0], "src": "Nova"}], 10.0)
    rewrite(manager.cache, older, created_at=1000.0)
    rewrite(manager.cache, newer, created_at=2000.0)

    restored = manager.restore(video, "ru", True, 10.0)

    assert restored.source == "manual:sr-Latn"
    assert [cue.src for cue in restored.cues] == ["Nova"]
```

The videos of the restore tests (`brokenpin01`, `restverdict`, `noverdict01`–`noverdict03`, `restpinned1`, `restorphan1`, `restnewest1`) are written into the `manager.cache` shared for the run, so each test has its own; `abcdefghijk` does not get in there — `test_restore_looks_up_the_variant_the_viewer_asked_for` relies on it being empty. In the last test the entry older by `created_at` sits first in the index: the order of index rows and the order by freshness diverge, and the test tells one from the other.

And the new `_key_for` signature in the existing test:

```python
# tests/test_job_identity.py:335, test_restore_from_cache_registers_the_variant_under_its_own_key
    manager.cache.put(manager._key_for(video, "asr"), cues, 10.0)
```

- [ ] **Step 2: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_cache_flow.py tests/test_job_identity.py -q`
Expected: FAIL, 22 failed (21 new and `test_restore_from_cache_registers_the_variant_under_its_own_key`): `TypeError: CacheKey.__init__() got an unexpected keyword argument 'pin'`; `AttributeError: 'CacheKey' object has no attribute 'pin'`; `AttributeError: module 'helper.jobs' has no attribute 'track_key'`; `TypeError: JobManager._key_for() missing 1 required positional argument: 'fingerprint'` in all the restore tests; in the pipeline tests a cache hit yields `['state', 'cues', 'state']` without `language`, `assert None == LanguageVerdict(...)`, a different `targetLang` — `assert 2 == 1` (today it is in the key, and recognition runs a second time), `KeyError: 'pin'` and `KeyError: 'language'` in the entry, and the `done` of an empty result has no `cached: False`.

- [ ] **Step 3: The key and the entry in `helper/cache.py`**

`CacheKey`: instead of `source_lang` and `target_lang` — one field `pin: str` in place of `source_lang`. The comment: the language pinned by a human (`[asr] language`), normalised; `""` — not pinned; neither `"auto"` nor YouTube metadata ever appears here; the effective language and the detector's verdict are in the entry, the `language` field. In `digest()`, `target_lang` leaves the list of parts, `source_lang` is replaced by `pin`; the other parts and their order stay the same: `video_id, track_fingerprint, pin, asr_model, translator_model, study_lang, PROMPT_VERSION, SCHEMA_VERSION`.

Versions (24-27). The shared comment above both (“Raise on any change of the prompt or of the entry format…”) narrows down to `PROMPT_VERSION`, and `SCHEMA_VERSION = 2` gets its own full comment right above it:

```python
# Raise on any change of the prompt — it is part of the key, so a version bump
# automatically invalidates old entries.
PROMPT_VERSION = 1

# The on-disk entry format: raise when what put writes changes.
# 2 — the entry carries language and pin instead of source_lang and target_lang.
SCHEMA_VERSION = 2
```

Do not touch the value of `PROMPT_VERSION`: Task 11 raises it once and rewrites the comment above it in full at the same time.

`TrackCache.put(self, key, cues, length_seconds, *, language: dict | None = None)`. In the entry, replace `"source_lang"` and `"target_lang"` with `"pin": key.pin` — the key field is written into the file for the same reason as `study_lang` (the comment next to it: without it the entry, read by eye, does not explain why the key does not match); after `"length_seconds"` add `"language": language`. In the `put` docstring: `language` — `LanguageVerdict.wire()` or `None`, readers take it via `.get("language")`, and an entry without the field is served with the language `null`.

The module docstring, lines 7-9: replace “…the old entry simply stops being found, and then goes away by LRU” with: “…the old entry simply stops being found. It does not leave the disk by itself: eviction by read age happens only inside put and only when the entries have outgrown the budget, so orphans lie there as long as it lasts. Deleting them is the owner's decision.”

- [ ] **Step 4: One key recipe in `helper/jobs.py`**

The module-level `track_key(config: Config, video_id: str, fingerprint: str, pin: str | None) -> CacheKey` before `class JobRunner`: `CacheKey(video_id=video_id, pin=pin or "", asr_model=f"{config.asr.model}/{config.asr.compute_type}", translator_model=config.translator.model, study_lang=config.translator.study_language, track_fingerprint=fingerprint)`. The docstring — the decision and the departure from the design (the design: “the effective language goes into the cache key… lookup by the declared one, the write under the effective one”): the language in the key is the human's pin, not the effective language, because the cache must answer before any work on the GPU (`helper/cache.py:4-5`), while the effective language appears only after the worker; when the human has pinned a language, the effective one matches it, and the verdict for an unpinned video lies in the entry and is visible on the wire. YouTube metadata does not enter the key: the Juske channel is labelled `en` while the speech is Serbian, and the language of such a video is decided by the detector. `targetLang` does not enter either: the output is always a Russian and a Montenegrin line, and a client with a different `targetLang` gets the same track. Move here as well the comment from `_cache_key` about both models entering the key.

`JobRunner._cache_key(job)` = `track_key(self._config, job.video_id, job.source, self._declaration(job)[0])`. The declaration — via `_declaration` (`_pin()` for now), not `hint`; `_pipeline` has already refused an invalid pin via `_pin()`; the link declaration will arrive here too, see “Deferred”. A resume after a pause writes under the same key the job looked up by: the config does not change until the daemon restarts.

`JobManager._key_for(self, video_id: str, fingerprint: str) -> CacheKey` = `track_key(self.config, video_id, fingerprint, language.hint(self.config.asr.language))`. The comment: the same recipe as the pipeline's, otherwise a restore would silently fail to find what was made; the pin via `hint`, because a restore only reads and does not refuse — an invalid pin makes the job fail in the pipeline (Task 4), while videos cached without a pin still open: a restore reads an invalid code as “not pinned”; ones made under a previously valid pin are not found until the code is fixed.

- [ ] **Step 5: Writing and reading the verdict**

`_finish_translation`: before the cache write, after the `pending` and `failed` branches — `if not job.cues: job._set_stage(Stage.DONE, total=0, cached=False); return`. The comment: an empty track does not go into the cache — once written, it would be served as ready forever (the daemon's cache holds three such entries, one for a 41-minute video), and a restore treats it as a corrupted entry; the price — a video without a single cue is not cached: a new job for it — after 12 other finished ones or a restart — downloads the audio and recognises it under the permit again. In `self._cache.put` add `language=job.language.wire() if job.language else None` (`asyncio.to_thread` passes a named argument as is).

`_serve_cached`: `if not cached or not cached.get("cues"): return False` — an entry without cues is a miss, as in `restore`. Then, BEFORE `job.cues = [...]`: `verdict = LanguageVerdict.from_wire(cached.get("language"))`, and if it is not `None` — `job._set_language(verdict)`. The comment: the verdict before the cues, as on a fresh recognition, otherwise the divergence flag would disappear on every repeat view; an entry without the field or with an invalid one — the language is `null` and there is no `language` event, without crashing.

`restore`: the call `self._cached_variant(video_id, use_author_track, duration)`; after `job.source = ...` and before `job.cues = [...]` — the same copying of the verdict via `from_wire`. Do not touch the “empty track is a corrupted entry” check (1698-1702). `target_lang` stays in the `restore` signature: it is used to create the `Job` and goes into the snapshot as `targetLang`.

- [ ] **Step 6: Restore: newest first, the key by the current config**

`_cached_variant(self, video_id, use_author_track, duration=None)` — without `target_lang`. Only the fingerprint is taken from the entry (`track_fingerprint`, `asr` by default); the pin is the current one, the models and versions are the current code's; for each fingerprint one `get` by `self._key_for(video_id, fingerprint)` with `duration` (it has a single key, and `get` writes `used_at`); of what was found the newest by `created_at` comes first — of two author tracks (`manual:sr` and `manual:sr-Latn`) the last made is served, not the first by index row order. With `use_author_track` the freshest author track (`manual:…`) is served, and if there is none — the recognition entry (`asr`); without it — `asr` only; nothing found — `None`. Keep the comments about the duration from the player (1746-1749) and about the author-track priority (1758-1760) in their places.

In the `_cached_variant` docstring, add: the pin in the recomputed key is the current one, from the config, not the one written in the file; therefore an entry made under a different pin, with a different model or version is not found, but is not deleted either.

- [ ] **Step 7: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_cache_flow.py tests/test_job_identity.py -q`
Expected: PASS — 21 new tests green, no failure in `tests/test_job_identity.py`.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 368 passed

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add helper/cache.py helper/jobs.py tests/test_cache_flow.py tests/test_job_identity.py
/usr/bin/git commit -m "Ключ кэша несёт закрепление человека вместо строки auto и без targetLang; запись хранит решение о языке, пустая дорожка не кэшируется"
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---
### Task 8: The prompt depends on the language, recognition garbage is filtered out

Breakage 3 of the design. Today the daemon assembles the prompt before the worker and without a language (`_initial_prompt`, `helper/jobs.py:1443-1458`): the Serbian phrase goes into an English video too, and names from the title, description and tags of any language go through Serbian transliteration (`_proper_nouns`, `helper/jobs.py:1461-1478`). The batched mode of faster-whisper 1.2.1 feeds the prompt to every chunk anew and applies none of the guards the worker sets, so prompt echo, silence and repetition loops reach the translator and the screen.

What is decided. After Task 5 the language is decided in the worker, so the worker also composes the prompt — by the decided language (`verdict.effective`), not the declared one; the daemon sends it only the names. The worker returns all segments together with their metrics, and the daemon filters: the worker's stderr is discarded on success (`_run_asr`), so a drop decided in the worker would not be visible in the journal; a separate worker run still shows the raw output. Echo is filtered per sentence inside `build_cues`, and so is the repetition loop: a batched-mode segment is a piece of speech up to 30 s, and “повтор” only marks it — the looping sentences leave, while the dialogue before the loop stays; only “тишина” is dropped whole. “A cue matching the prompt” is understood as “consisting only of words from the prompt, with at least one uncommon word of the phrase or of the names lead-in among them (for `sr` — transkript, snimka, pominju) or two different names that are not among its common words”. Both interpretations are record 3 in “Decisions that diverge from the letter of the design”. The filters do not touch the author track: it has neither a prompt nor metrics. Do not raise `PROMPT_VERSION` here — Task 11 raises it once for everything that changes a cached track.

Line numbers are from `98a3ad2`. By this task, `helper/jobs.py` and `helper/asr/worker.py` have already been changed by Tasks 4–7: search by function name.

**Files:**
- Create: `helper/asr/prompt.py`
- Modify: `helper/segment.py:16-19` (imports), `helper/segment.py:177-218` (`build_cues`, the loop over sentences — line 195)
- Modify: `helper/asr/worker.py` — imports (17-25); `transcribe` (125-215: the `initial_prompt` parameter — 131, the empty guards with their comments — 164-174, `initial_prompt=initial_prompt` — 187, cue assembly — 192-203, the answer — 205-215); `build_parser` from Task 5 (on `98a3ad2` argument parsing is inside `main`, 219-232, the `--initial-prompt` flag — 223); the `transcribe` call in `main` (246-255, `initial_prompt=args.initial_prompt` — 251)
- Modify: `helper/jobs.py` — imports (16-28: `import re`; 33-39); `JobRunner._accept_asr` from Task 6 (on `98a3ad2` its cue line is 939 in `_pipeline`); `JobRunner._run_asr` (1151-1167: the prompt — 1153, the flag — 1166-1167); delete `_initial_prompt` (1443-1458) and `_proper_nouns` (1461-1478), in their place — the module-level `asr_cues`
- Modify: `tests/test_worker.py` (Task 5) — `test_result_carries_the_verdict_and_the_windows` and `test_parser_keeps_the_cli_contract` (Step 11)
- Test: `tests/test_prompt.py`, `tests/test_segment.py`, `tests/test_asr_filters.py` (new)

**Interfaces:**
- Consumes:
  - `helper.language.family(code: str | None) -> str | None` (Task 4).
  - `helper.language.Detection`, `LanguageVerdict` (`wire()`), `decide(pin, hint, detection, *, fallback="sr")`; in the worker `_detect(model, audio) -> Detection`, `transcribe(..., language: str = "sr", hint: str | None = None, ...)`, `build_parser() -> argparse.ArgumentParser`, the `--hint` flag (Task 5).
  - `JobRunner._accept_asr(self, job: Job, result: dict) -> None`, `held.check()` after it in `_pipeline` (Task 6).
  - Fixtures: `manager`; `pipeline` — `pipeline.info`, `pipeline.run(job)`, `pipeline.workers[-1].args`; `whisper` — `whisper.segments`, `whisper.kwargs` and the builder `whisper.segment(start, end, text, *, no_speech_prob=0.1, avg_logprob=-0.3, compression_ratio=1.5, words=())` (Task 3).
  - `tests/test_worker.py` (Task 5): `test_result_carries_the_verdict_and_the_windows` checks the set of answer keys, `test_parser_keeps_the_cli_contract` — the parser defaults, including `args.initial_prompt`.
- Produces:
  - `helper/asr/prompt.py`: `NO_SPEECH_THRESHOLD = 0.6`, `LOG_PROB_THRESHOLD = -1.0`, `COMPRESSION_RATIO_THRESHOLD = 2.4`, `MAX_NAMES = 12`, `PHRASES = {"sr": "Ovo je transkript video snimka na srpskom jeziku."}`, `NAMES_LEAD = "Pominju se:"`, `ECHO_COMMON = frozenset({"ovo", "je", "video", "na", "se", "srpskom", "jeziku"})`;
    `def harvest_names(title: str, tags: Sequence[str]) -> list[str]`;
    `def initial_prompt(language: str | None, names: Sequence[str]) -> str | None`;
    `def is_echo(sentence: str, prompt: str | None) -> bool`;
    `def compression_ratio(text: str) -> float` — the faster-whisper formula (`get_compression_ratio`): UTF-8 bytes over bytes after `zlib`;
    `def segment_verdict(segment: Mapping[str, object]) -> str | None` — `"тишина"`, `"повтор"` or `None`.
  - `helper.segment.build_cues(segments: list[dict], drop: Callable[[str, dict], bool] | None = None) -> list[Cue]` — `drop(sentence, segment)`, where segment is the very dict the assembly walks, with its metrics.
  - Worker: `def _cue(segment) -> dict`; `transcribe(audio, *, model_name=DEFAULT_MODEL, compute_type="float16", language: str = "sr", hint: str | None = None, prompt_names: Sequence[str] | None = None, hotwords: str | None = None, batch_size: int = 16, vad_filter: bool = True) -> dict`. The answer additionally carries `"prompt": str | None`, every cue — `"no_speech_prob"`, `"avg_logprob"`, `"compression_ratio"`. The `--prompt-names JSON` flag instead of `--initial-prompt`.
  - `helper.jobs.asr_cues(segments: list[dict], language: str, prompt: str | None) -> tuple[list[Cue], list[tuple[str, str]]]`; drop reasons: `"тишина"` (a whole segment), `"повтор"` (a looping sentence of a marked segment), `"эхо затравки"` (a sentence).
  - Daemon → worker protocol: `--prompt-names '<JSON list>'` on every run, even `[]`; `--initial-prompt` is gone. The worker's answer changes only by added keys.

- [ ] **Step 1: Write failing tests for the pure functions**

The echo and speech lines are taken from the local track cache (66 620 cues) and copied as literals: the tests do not read the user's cache.

```python
# tests/test_prompt.py
"""The recognition prompt and the filtering of its garbage: pure functions of helper.asr.prompt."""

import pytest

from helper.asr.prompt import (
    MAX_NAMES,
    compression_ratio,
    harvest_names,
    initial_prompt,
    is_echo,
    segment_verdict,
)

PHRASE = "Ovo je transkript video snimka na srpskom jeziku."


def test_names_come_from_title_and_tags_in_their_own_script():
    names = harvest_names(
        "Мамини синови - Епизода 5 (ОФИЦИЈЕЛНИ спот)", ["Andrija i Anđelka", "Beograd"]
    )

    assert names == ["Мамини", "Епизода", "Andrija", "Anđelka", "Beograd"]


def test_shouted_title_words_are_not_names_but_short_acronyms_are():
    assert harvest_names("UDARIO SAM TEK OFARBANOG BMW", ["NATO", "MOTOR"]) == [
        "SAM", "TEK", "BMW", "NATO",
    ]


def test_names_are_deduplicated_in_order_and_capped():
    title = "Ana Bora Cana Dara Ema Fila Goca Hana Ivana Jana Kaja Lana Maja Nada"

    assert harvest_names("Beograd i Beograd", ["Beograd", "Novi Sad"]) == [
        "Beograd", "Novi", "Sad",
    ]
    assert harvest_names(title, []) == title.split()[:MAX_NAMES]


def test_serbian_gets_the_latin_phrase_and_transliterated_names():
    assert initial_prompt("sr", ["Мамини", "Beograd"]) == f"{PHRASE} Pominju se: Mamini, Beograd."


@pytest.mark.parametrize("language", ["hr", "bs", "sh", "me", "cnr", "sr-Latn", "SR"])
def test_every_label_of_the_serbian_family_gets_the_serbian_phrase(language):
    assert initial_prompt(language, ["Zagreb"]) == f"{PHRASE} Pominju se: Zagreb."


@pytest.mark.parametrize("language", ["en", "ru", "uk", "mk"])
def test_a_language_without_a_phrase_gets_no_prompt_not_even_names(language):
    assert initial_prompt(language, ["Clarkson"]) is None


@pytest.mark.parametrize("language", [None, "", "auto", "xx"])
def test_an_undecided_or_unknown_language_gets_no_prompt(language):
    assert initial_prompt(language, ["Clarkson"]) is None


def test_without_names_the_phrase_stands_alone():
    assert initial_prompt("sr", []) == PHRASE


def test_names_still_cyrillic_after_serbian_rules_are_dropped():
    assert initial_prompt("sr", ["Для", "Patreon"]) == f"{PHRASE} Pominju se: Patreon."
    assert initial_prompt("sr", ["Для"]) == PHRASE


def test_a_name_written_in_both_scripts_appears_once():
    assert initial_prompt("sr", ["Београд", "Beograd"]) == f"{PHRASE} Pominju se: Beograd."


def test_the_prompt_never_carries_more_than_twelve_names():
    names = [f"Ime{letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]

    assert initial_prompt("sr", names) == f"{PHRASE} Pominju se: {', '.join(names[:MAX_NAMES])}."


NAMED = initial_prompt("sr", ["Mamini", "Epizoda"])
FILM = initial_prompt("sr", [
    "Terminus", "Full", "Movie", "Johnny", "Hallyday", "Karen",
    "Allen", "Jürgen", "Prochnow", "However", "Genres", "Action",
])
SERIES = initial_prompt("sr", ["AndrijaiAnđelka", "Label", "Zabranjeno", "Andrija"])


@pytest.mark.parametrize("prompt, sentence", [
    (PHRASE, "Ovo je transkript video snimka na srpskom jeziku."),
    (NAMED, "Pominju se na srpskom jeziku."),
    (NAMED, "Pominju se."),
    (NAMED, "Pominju se na srpskom"),
    (NAMED, "Mamini, Epizoda."),
    (FILM, "Pominju se, Terminus, Full, Movie, Johnny, Hallyday, Karen, Allen, Jürgen, "
           "Prochnow, However, Genres, Action."),
    (FILM, "Karen, Allen, Jürgen, Prochnow, However, Genres, Action."),
    (SERIES, "AndrijaiAndjelka, Label, Zabranjeno,"),
])
def test_field_echoes_of_the_prompt_are_recognised(prompt, sentence):
    assert is_echo(sentence, prompt)


@pytest.mark.parametrize("prompt, sentence", [
    (NAMED, "Međutim, zadacima pominju se i ubistva."),
    (NAMED, "Evo, gospodine, uputstvo na srpskom jeziku."),
    (NAMED, "Ovo je video."),
    (NAMED, "Na srpskom jeziku."),
    (NAMED, "Pominju se, da je Rusija mogla organizirati diversiju"),
    (NAMED, "Pominju se 30 sekundi."),
    (PHRASE, "Pominju se."),
    (SERIES, "Ćao, Andrija."),
    (SERIES, "Andrija, Andrija."),
    (None, "Ovo je transkript video snimka na srpskom jeziku."),
    # One title name in an everyday phrase is speech: in the cache “Šta je ovo?” occurs in 17
    # of 160 videos, and the title “ŠTA SE KRILO U TITOVOM PLAVOM VOZU?” yields the name ŠTA.
    (initial_prompt("sr", ["ŠTA", "VOZU", "Prava", "Istina"]), "Šta je ovo?"),
    (initial_prompt("sr", ["Beograd"]), "Ovo je Beograd."),
    # A common word of the phrase that became a name does not give a second name: the title
    # “OVO JE NAJGORI AUTO NA SVETU” yields the names OVO and AUTO.
    (initial_prompt("sr", ["OVO", "AUTO"]), "Ovo je auto."),
])
def test_speech_that_shares_words_with_the_prompt_is_kept(prompt, sentence):
    assert not is_echo(sentence, prompt)


@pytest.mark.parametrize("metrics, verdict", [
    ({"no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 1.0}, "тишина"),
    ({"no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 3.0}, "тишина"),
    ({"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 2.5}, "повтор"),
    ({"no_speech_prob": 0.9, "avg_logprob": -0.3, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.1, "avg_logprob": -1.5, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.6, "avg_logprob": -1.5, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.9, "avg_logprob": -1.0, "compression_ratio": 1.0}, None),
    ({"no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 2.4}, None),
    ({"no_speech_prob": 0.9, "compression_ratio": 1.0}, None),
    ({}, None),
])
def test_segment_verdict_drops_only_silence_and_repetition(metrics, verdict):
    assert segment_verdict({"text": "Dobar dan.", **metrics}) == verdict


def test_compression_ratio_is_the_one_faster_whisper_uses():
    import helper.env  # noqa: F401 — HF_HOME to a temporary cache before faster_whisper, as in the worker

    from faster_whisper.transcribe import get_compression_ratio

    loop = "Ideš, slušaj me, prvo uzduš, ovako, pa poprično, pa što sitnije, " + "tak, " * 70 + "tak"

    for text in (PHRASE, "Dobar dan.", ", ".join(["ne"] * 12), loop, "Šta je ovo?"):
        assert compression_ratio(text) == get_compression_ratio(text)
```

- [ ] **Step 2: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_prompt.py -v`
Expected: FAIL — a collection error `ModuleNotFoundError: No module named 'helper.asr.prompt'`

- [ ] **Step 3: Create `helper/asr/prompt.py`**

Imports: `re`, `unicodedata`, `zlib`, `from collections.abc import Mapping, Sequence`, `from helper.language import family`, `from helper.script import CYRILLIC, to_latin`. `family` specifically, not the `helper.language` module as a whole: the `language` parameter of `initial_prompt` would shadow the module. `CYRILLIC` is the existing pattern `[Ѐ-ӿ]` (`helper/script.py:18`); do not create a second one. Neither `faster_whisper` nor `helper.jobs`: both the worker and the daemon load the module.

The constants are as in Interfaces. `PHRASES["sr"]` and `NAMES_LEAD` are verbatim the ones `_initial_prompt` assembled (`helper/jobs.py:1456`, `1458`): with the same names the Serbian prompt stays byte for byte the same, only the set of names and who gets it change.

The module docstring, briefly and in Russian — what was checked in faster-whisper 1.2.1 and why it is built this way:
- the prompt is needed for exactly one thing — to pin Latin script for the Serbian family (`helper/script.py:4-6`), so the phrase exists only for `sr`; where there is no phrase, there is no prompt either, and the names do not go in either;
- batched mode feeds the same prompt to every chunk up to 30 s (`generate_segment_batched` copies it to every chunk of the batch), and on chunks without speech it surfaces as echo;
- it takes the last 223 tokens of the prompt (`get_prompt`: `max_length // 2 - 1`): an overflow would eat the phrase, not the names;
- batched mode accepts the silence and repetition thresholds and does not apply them (“Unused Arguments” in the `BatchedInferencePipeline.transcribe` docstring), so filtering is on the daemon's side: a segment is judged by the metrics it carries anyway (`segment_verdict`), and the sentences of a segment marked “повтор” — by their own `compression_ratio` and by a repeat of what was already said (`helper.jobs.asr_cues`).

`harvest_names(title, tags)`: words `\b\w{3,}` from the title, then from the tags, in order. A word whose first letter is uppercase in any script is taken (`word[0].isalpha() and word[0].isupper()`); an all-caps word longer than four letters (`word.isupper() and len(word) > 4`) is skipped; duplicates are removed preserving order; no more than `MAX_NAMES`. No transliteration. The description is absent even from the parameters. In the docstring: the field echo came from the template text of descriptions (Label, Copyright, Patreon, Instagram, sponsors); all-caps longer than four letters is emphasis in a title (UDARIO, OFARBANOG, NAJGORIH), while short BMW and NATO are names.

`initial_prompt(language, names)`: `PHRASES.get(family(language))`; no phrase → `None`. Otherwise the names: `to_latin` of each; drop duplicates that appeared after transliteration (a Cyrillic title and Latin tags yield one name twice); drop names where Cyrillic remains after the Serbian rules (`CYRILLIC.search`: “Для” → “Dlя”); keep no more than `MAX_NAMES` here too — the worker takes the list from the command line, and an overflow would eat the phrase. No names left → the phrase; otherwise `f"{phrase} {NAMES_LEAD} {', '.join(names)}."`. There are no other `to_latin` calls on names: the BCMS test here is structural — the phrase exists only for the `sr` family.

`is_echo(sentence, prompt)`: `prompt is None` → `False`. The words of both strings: `casefold()`, then `"đ"` → `"dj"` (đ has no decomposition in NFKD), then `unicodedata.normalize("NFKD", …)` without combining marks (`unicodedata.combining`), then the set `re.findall(r"\w+", …)`. The prompt is split at `NAMES_LEAD` (`prompt.partition(NAMES_LEAD)`): before the lead-in — the phrase, after it — the names. It is echo when the sentence has at least two different words, all of them present in the prompt, at least one not from `ECHO_COMMON`, and at the same time among them there is a word of the phrase or of the lead-in itself that is not in `ECHO_COMMON` (for `sr` — transkript, snimka, pominju) or at least two different words from the names, not counting `ECHO_COMMON` words: the title “OVO JE NAJGORI AUTO NA SVETU” yields the names OVO and AUTO, while “Ovo je auto.” is speech, as is “Šta je ovo?” under “ŠTA JE OVO?! …”. In the docstring — the departure from the letter of the design: a literal match with the prompt occurs in none of the 66 620 cached cues, echo is fragments and permutations of its tail (“Pominju se na srpskom jeziku.”) and lists of names; the common words of the phrase do not make echo (“Ovo je video.”, “Na srpskom jeziku.”), nor does one word (“Andrija, Andrija.”), nor does one title name in an everyday phrase: the title “ŠTA SE KRILO U TITOVOM PLAVOM VOZU?” yields the name ŠTA, and without this condition “Šta je ovo?” would go out as echo.

`compression_ratio(text)` — the compression ratio by the faster-whisper formula (`faster_whisper.transcribe.get_compression_ratio`): `len(UTF-8 bytes) / len(zlib.compress(UTF-8 bytes))`, standard library only. Agreement with the library is pinned by `test_compression_ratio_is_the_one_faster_whisper_uses`. The daemon needs it for sentences inside a segment: the library computes the segment metric itself, and only we compute it for sentences.

`segment_verdict(segment)`: `"тишина"` when `no_speech_prob > NO_SPEECH_THRESHOLD` and `avg_logprob < LOG_PROB_THRESHOLD`; otherwise `"повтор"` when `compression_ratio > COMPRESSION_RATIO_THRESHOLD`; otherwise `None`. No field or a `None` field — do not drop on it. In the docstring: “тишина” is the rule by which Whisper itself skips a window in sequential mode, and such a segment is dropped whole; “повтор” replaces the temperature fallback, which batched mode does not have (Whisper would re-decode the chunk), but a batched-mode segment is a whole piece of speech up to 30 s, and the loop often starts in the middle of it, after real dialogue. That is why “повтор” only marks the segment: only the looping sentences are dropped inside it (`asr_cues`, Step 12), while the real speech before the loop stays. Every drop goes to the journal (Step 12).

- [ ] **Step 4: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_prompt.py -v`
Expected: PASS, 55 passed

- [ ] **Step 5: Write failing tests for cue assembly with filtering**

```python
# tests/test_segment.py
"""Cue assembly from recognition segments: helper.segment."""

from helper.segment import build_cues

FILM_ECHO = (
    "Pominju se, Terminus, Full, Movie, Johnny, Hallyday, Karen, Allen, Jürgen, "
    "Prochnow, However, Genres, Action."
)


def word(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "word": text}


GREETING = {
    "start": 0.0,
    "end": 5.0,
    "text": "Dobar dan. Pominju se na srpskom jeziku. Kako ste?",
    "words": [
        word(0.0, 0.4, " Dobar"), word(0.5, 0.9, " dan."),
        word(1.0, 1.4, " Pominju"), word(1.5, 1.6, " se"), word(1.7, 1.8, " na"),
        word(1.9, 2.2, " srpskom"), word(2.3, 2.6, " jeziku."),
        word(4.0, 4.4, " Kako"), word(4.5, 5.0, " ste?"),
    ],
}


def timeline(cues) -> list[tuple[str, float, float]]:
    return [(c.text, round(c.start, 2), round(c.end, 2)) for c in cues]


def test_a_dropped_sentence_leaves_no_cue_and_its_neighbours_keep_their_words():
    cues = build_cues([GREETING], drop=lambda sentence, segment: sentence.startswith("Pominju"))

    assert timeline(cues) == [("Dobar dan.", 0.0, 0.9), ("Kako ste?", 4.0, 5.0)]


def test_a_long_sentence_is_dropped_whole_not_piece_by_piece():
    segment = {"start": 0.0, "end": 6.0, "text": FILM_ECHO, "words": []}

    assert len(build_cues([segment])) == 2
    assert build_cues([segment], drop=lambda sentence, segment: sentence == FILM_ECHO) == []


def test_a_predicate_that_drops_nothing_changes_nothing():
    assert build_cues([GREETING], drop=lambda sentence, segment: False) == build_cues([GREETING])
```

The first test catches filtering after word matching: then “Kako ste?” would land on the words “Pominju se” and get 1.0–1.6 s (checked: the result is `[('Dobar dan. Kako ste?', 0.0, 1.6)]`). The second — filtering by pieces: 108 characters are cut by `_split_balanced` into two cues, and neither alone equals the sentence.

- [ ] **Step 6: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: FAIL — 3 failed, `TypeError: build_cues() got an unexpected keyword argument 'drop'` (the first check of the second test, two cues without filtering, already passes)

- [ ] **Step 7: Filter sentences in `build_cues`**

`helper/segment.py`: the import `from collections.abc import Callable`; the signature `build_cues(segments: list[dict], drop: Callable[[str, dict], bool] | None = None) -> list[Cue]`. As the first line of the loop body `for sentence, (start, end) in zip(sentences, spans):` (line 195) — `if drop is not None and drop(sentence, segment): continue`; `segment` is the dict the assembly walks, with all its keys: `normalize_cues` copies them (`{**cue, ...}`), so the segment's metrics reach `drop`. The comment above it: intervals are matched to words over all sentences of the segment, before filtering — drop a sentence earlier and every following one would land on someone else's words; filtering is per whole sentence, before `_split_balanced` and `_enforce_min_duration`: a long echo would otherwise be cut into pieces that do not look like echo separately, and a short one would be glued to the neighbouring real cue (“Da. Pominju se na srpskom jeziku.”). In the `build_cues` docstring — one phrase about `drop`: called as `drop(sentence, segment)`; `True` — the sentence does not go into the cues. Without `drop` the behaviour is the previous one: `spikes/asr_real.py:134` relies on it.

- [ ] **Step 8: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: PASS, 3 passed

- [ ] **Step 9: Write failing tests for the worker and the daemon**

`_detect` is patched right in the worker module: the tests of this task are about the prompt and the metrics, not about windows, VAD and the model. faster-whisper segments are built by `whisper.segment(...)` of the Task 3 fixture — a real `Segment`, and the library itself checks the metric field names. Metadata — `dataclasses.replace(pipeline.info, …)`.

```python
# tests/test_asr_filters.py
"""A prompt for the decided language and filtering of recognition garbage: the worker and the daemon."""

import logging
from dataclasses import replace

import numpy as np
import pytest

from helper import jobs
from helper.asr import worker
from helper.asr.prompt import initial_prompt
from helper.jobs import Stage
from helper.language import Detection, LanguageVerdict

VIDEO = "abcdefghijk"
PHRASE = "Ovo je transkript video snimka na srpskom jeziku."
NAMED = initial_prompt("sr", ["Mamini", "Epizoda"])
LOOP = ", ".join(["ne"] * 12)
# jWrDvxtofLk, 201.8–229.6 s: six real sentences, then a “tak” loop in the same chunk.
DIALOGUE = (
    "Pa nije to dobro. Kako nije? Šta nije dobro? Pa očajno je, pogledaj ti koliko je ovo "
    "krupno. Šta radiš sa ovim? Da ga jedem."
)
TAK = "Ideš, slušaj me, prvo uzduš, ovako, pa poprično, pa što sitnije, " + "tak, " * 70 + "tak"
MINUTE = np.zeros(60 * worker.SAMPLE_RATE, dtype="<f4")

# Eight windows with speech, the detector decides. Serbian speech — like the Juske channel,
# which YouTube labels en.
SERBIAN_SPEECH = Detection(
    language="sr", confidence=0.97, bcms_share=0.97, floor=False, votes=8, probed=8,
    windows=(("sr", 0.97, 25.0),) * 8,
)
ENGLISH_SPEECH = Detection(
    language="en", confidence=0.99, bcms_share=0.0, floor=False, votes=8, probed=8,
    windows=(("en", 0.99, 28.0),) * 8,
)

# Arguments that the batched mode of faster-whisper 1.2.1 accepts and does not apply.
DEAD_GUARDS = {
    "temperature",
    "compression_ratio_threshold",
    "log_prob_threshold",
    "no_speech_threshold",
    "condition_on_previous_text",
    "hallucination_silence_threshold",
}


def heard(monkeypatch, detection: Detection) -> None:
    """The detector “heard” the given thing: windows, VAD and the model are irrelevant here."""
    monkeypatch.setattr(worker, "_detect", lambda model, audio: detection)


def segment(start, end, text, *, no_speech=0.05, logprob=-0.2, ratio=1.1) -> dict:
    """A segment as the worker JSON delivers it."""
    return {
        "start": start, "end": end, "text": text, "words": [],
        "no_speech_prob": no_speech, "avg_logprob": logprob, "compression_ratio": ratio,
    }


MIXED = [
    segment(0.0, 5.0, "Dobar dan. Pominju se na srpskom jeziku. Kako ste?"),
    segment(6.0, 9.0, "Hvala na gledanju.", no_speech=0.92, logprob=-1.4),
    segment(10.0, 14.0, LOOP, ratio=3.1),
]


def test_cue_carries_the_three_metrics_of_its_segment(whisper):
    from faster_whisper.transcribe import Word

    whole = whisper.segment(
        1.0, 2.5, " Zdravo svima.", no_speech_prob=0.12, avg_logprob=-0.31, compression_ratio=1.07,
        words=[
            Word(start=1.0, end=1.4, word=" Zdravo", probability=0.9),
            Word(start=1.5, end=2.5, word=" svima.", probability=0.8),
        ],
    )

    assert worker._cue(whole) == {
        "start": 1.0,
        "end": 2.5,
        "text": "Zdravo svima.",
        "words": [
            {"start": 1.0, "end": 1.4, "word": " Zdravo"},
            {"start": 1.5, "end": 2.5, "word": " svima."},
        ],
        "no_speech_prob": 0.12,
        "avg_logprob": -0.31,
        "compression_ratio": 1.07,
    }


def test_prompt_follows_the_decided_language_not_the_metadata(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)
    whisper.segments = [whisper.segment(0.0, 2.0, " Dobar dan.")]

    result = worker.transcribe(
        MINUTE, language="auto", hint="en", prompt_names=["Juske", "Беочин"]
    )

    assert result["language"] == "sr"
    assert result["prompt"] == f"{PHRASE} Pominju se: Juske, Beočin."
    assert whisper.kwargs["initial_prompt"] == result["prompt"]


def test_english_speech_is_decoded_without_any_prompt(whisper, monkeypatch):
    heard(monkeypatch, ENGLISH_SPEECH)
    whisper.segments = [whisper.segment(0.0, 2.0, " Hello there.")]

    result = worker.transcribe(MINUTE, language="auto", prompt_names=["Copyright", "Subscribe"])

    assert result["language"] == "en"
    assert result["prompt"] is None
    assert whisper.kwargs["initial_prompt"] is None


def test_a_pinned_croatian_keeps_its_token_and_gets_the_serbian_phrase(whisper, monkeypatch):
    heard(monkeypatch, ENGLISH_SPEECH)

    result = worker.transcribe(MINUTE, language="hr", prompt_names=["Zagreb"])

    assert whisper.kwargs["language"] == "hr"
    assert result["prompt"] == f"{PHRASE} Pominju se: Zagreb."


def test_without_prompt_names_there_is_no_prompt(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)

    result = worker.transcribe(MINUTE, language="sr")

    assert result["prompt"] is None
    assert whisper.kwargs["initial_prompt"] is None


def test_worker_returns_every_segment_with_its_metrics(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)
    whisper.segments = [
        whisper.segment(0.0, 2.0, " Dobar dan.", no_speech_prob=0.05, avg_logprob=-0.2),
        whisper.segment(2.0, 30.0, " Hvala na gledanju.", no_speech_prob=0.92, avg_logprob=-1.4),
    ]

    result = worker.transcribe(MINUTE, language="sr", prompt_names=[])

    assert [(c["text"], c["no_speech_prob"], c["avg_logprob"]) for c in result["cues"]] == [
        ("Dobar dan.", 0.05, -0.2),
        ("Hvala na gledanju.", 0.92, -1.4),
    ]
    assert result["prompt"] == PHRASE


def test_guards_that_batched_mode_ignores_are_not_passed(whisper, monkeypatch):
    heard(monkeypatch, SERBIAN_SPEECH)

    worker.transcribe(MINUTE, language="sr", prompt_names=[])

    assert DEAD_GUARDS.isdisjoint(whisper.kwargs)


def test_worker_takes_names_as_a_json_list_and_no_ready_prompt():
    parser = worker.build_parser()

    assert parser.parse_args(["--prompt-names", '["Мамини", "Beograd"]']).prompt_names == [
        "Мамини", "Beograd",
    ]
    assert parser.parse_args([]).prompt_names is None

    refused = (
        ["--initial-prompt", PHRASE],
        ["--prompt-names", '"Beograd"'],
        ["--prompt-names", "Beograd"],
    )

    for argv in refused:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_silence_repetition_and_echo_are_dropped_with_a_reason():
    cues, dropped = jobs.asr_cues(MIXED, "sr", NAMED)

    assert [c.text for c in cues] == ["Dobar dan.", "Kako ste?"]
    assert sorted(dropped) == sorted([
        ("тишина", "Hvala na gledanju."),
        ("повтор", LOOP),
        ("эхо затравки", "Pominju se na srpskom jeziku."),
    ])


def test_echo_is_judged_after_cyrillic_becomes_latin():
    cues, dropped = jobs.asr_cues([segment(0.0, 3.0, "Помињу се на српском језику.")], "sr", NAMED)

    assert cues == []
    assert dropped == [("эхо затравки", "Pominju se na srpskom jeziku.")]


def test_without_a_prompt_the_phrase_is_just_speech():
    cues, dropped = jobs.asr_cues([segment(0.0, 4.0, PHRASE)], "en", None)

    assert [c.text for c in cues] == [PHRASE]
    assert dropped == []


def test_a_loop_takes_only_its_own_sentence_not_the_dialogue_before_it():
    # The whole chunk compresses 3.45 times, while each real sentence in it — no
    # more than 0.89: dropping the chunk would carry the dialogue away with the loop.
    cues, dropped = jobs.asr_cues(
        [segment(201.8, 229.6, f"{DIALOGUE} {TAK}", ratio=3.45)], "sr", NAMED
    )

    assert [c.text for c in cues] == [
        "Pa nije to dobro.", "Kako nije?", "Šta nije dobro?",
        "Pa očajno je, pogledaj ti koliko je ovo krupno.", "Šta radiš sa ovim?", "Da ga jedem.",
    ]
    assert dropped == [("повтор", TAK)]


def test_a_two_sentence_loop_keeps_its_first_turn():
    looped = "Dobro jutro. Kako si? " + "Idemo dalje. Vidimo se. " * 6

    cues, dropped = jobs.asr_cues([segment(0.0, 30.0, looped, ratio=3.0)], "sr", NAMED)

    # Every sentence of the loop compresses poorly; only a repeat of what was said catches it.
    assert [c.text for c in cues] == ["Dobro jutro.", "Kako si?", "Idemo dalje.", "Vidimo se."]
    assert dropped == [("повтор", "Idemo dalje."), ("повтор", "Vidimo se.")] * 5


def test_repeats_in_an_unmarked_segment_are_speech():
    cues, dropped = jobs.asr_cues([segment(0.0, 4.0, "Da. Ne. Da. Ne znam. Da.")], "sr", NAMED)

    assert " ".join(c.text for c in cues) == "Da. Ne. Da. Ne znam. Da."
    assert dropped == []


def test_dropped_lines_never_become_job_cues_and_each_is_logged(manager, caplog):
    caplog.set_level(logging.INFO, logger="helper.jobs")
    job = manager.get_or_create(VIDEO, "ru", True)
    verdict = LanguageVerdict(
        effective="sr", declared=None, declared_by=None, detected="sr", confidence=0.97,
        reason="detector",
    )
    result = {
        "language": "sr",
        "verdict": verdict.wire(),
        "detection": {
            "bcmsShare": 0.97, "votes": 8, "probed": 8, "windows": [["sr", 0.97, 25.0]] * 8,
        },
        "duration": 14.0,
        "model": "large-v3",
        "prompt": NAMED,
        "cues": MIXED,
    }

    manager.runner._accept_asr(job, result)

    assert [(c.index, c.src) for c in job.cues] == [(0, "Dobar dan."), (1, "Kako ste?")]

    logged = [r.getMessage() for r in caplog.records if r.name == "helper.jobs"]

    assert f"затравка {VIDEO}: {NAMED}" in logged
    assert f"выброшено {VIDEO} (тишина): Hvala na gledanju." in logged
    assert f"выброшено {VIDEO} (повтор): {LOOP}" in logged
    assert f"выброшено {VIDEO} (эхо затравки): Pominju se na srpskom jeziku." in logged


async def test_daemon_sends_title_and_tag_names_not_a_ready_prompt(pipeline, manager):
    pipeline.info = replace(
        pipeline.info,
        title="Мамини синови - Епизода 5",
        tags=["Beograd"],
        description="Copyright Label Instagram",
    )
    job = manager.get_or_create(VIDEO, "ru", True)

    await pipeline.run(job)

    argv = pipeline.workers[-1].args
    parsed = worker.build_parser().parse_args(argv[3:])

    assert job.stage is Stage.DONE
    assert "--initial-prompt" not in argv
    assert argv[argv.index("--prompt-names") + 1] == '["Мамини", "Епизода", "Beograd"]'
    assert parsed.prompt_names == ["Мамини", "Епизода", "Beograd"]
```

- [ ] **Step 10: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_asr_filters.py -v`
Expected: FAIL — 16 failed:
- `test_cue_carries_the_three_metrics_of_its_segment` — `AttributeError: module 'helper.asr.worker' has no attribute '_cue'`;
- the five tests that call `transcribe(..., prompt_names=...)` — `TypeError: transcribe() got an unexpected keyword argument 'prompt_names'`;
- `test_without_prompt_names_there_is_no_prompt` — `KeyError: 'prompt'`;
- `test_worker_takes_names_as_a_json_list_and_no_ready_prompt` — `SystemExit: 2` (the parser does not know `--prompt-names`);
- the six `asr_cues` tests — `AttributeError: module 'helper.jobs' has no attribute 'asr_cues'`;
- `test_dropped_lines_never_become_job_cues_and_each_is_logged` — `job.cues` has five cues: the echo, the silence and the loop among them;
- `test_daemon_sends_title_and_tag_names_not_a_ready_prompt` — `AssertionError: assert '--initial-prompt' not in [...]`.

- [ ] **Step 11: Worker — the prompt after the language decision, metrics in the answer, names instead of the prompt**

`helper/asr/worker.py`:

1. Imports: `from collections.abc import Sequence`; after `import helper.env` — `from helper.asr.prompt import initial_prompt`.
2. `_cue(segment) -> dict` — a module-level function instead of the list expression at 192-203. Keys: `start`, `end`, `text` (`segment.text.strip()`), `words` (`start`, `end`, `word` of each word from `segment.words or []`; `probability` is not taken, as today), plus `no_speech_prob`, `avg_logprob`, `compression_ratio` as they are. The empty filter `"cues": [c for c in cues if c["text"]]` stays.
3. `transcribe`: replace the `initial_prompt` parameter with `prompt_names: Sequence[str] | None = None` — after `hint`, the order as in Interfaces. Right after the language decision (Task 5: `verdict = …decide(…)`) — `prompt = initial_prompt(verdict.effective, prompt_names) if prompt_names is not None else None`; in `pipeline.transcribe` — `initial_prompt=prompt`; in the answer — `"prompt": prompt` next to `"model"`. `None` means “there was no flag”: that is how `helper/measure.py` and `spikes/asr_real.py` call the worker, and they get no prompt, as today; `[]` — the phrase without names. The comment at the prompt: it follows the decided language, not the declared one — Serbian speech under an en label (Juske) needs the Serbian phrase, English speech needs none.
4. From the arguments of `pipeline.transcribe`, remove `temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]`, `compression_ratio_threshold=2.4`, `log_prob_threshold=-1.0`, `no_speech_threshold=0.6`, `condition_on_previous_text=False`, `hallucination_silence_threshold=2.0` together with comments 164-165 and 170-172 — both are wrong for batched mode. On 1.2.1 the behaviour does not change: batched mode only stores the thresholds in the options (`faster_whisper/transcribe.py:525-527`) and reads them nowhere, cuts the temperature ladder to the first value (528-532; the default is the same, 0.0), and sets `hallucination_silence_threshold=None` and `condition_on_previous_text=False` itself (546-547). Instead of them, one comment: the thresholds are not passed because batched mode does not apply them; the daemon filters silence as a whole segment by its metrics (`helper.asr.prompt.segment_verdict`), and in a segment marked “повтор” — only the looping sentences (`helper.jobs.asr_cues`); every drop is visible in the journal. `task`, `beam_size`, `word_timestamps`, `vad_filter`, `vad_parameters`, `hotwords`, `batch_size` — unchanged.
5. `build_parser` (Task 5): remove `--initial-prompt`; add `--prompt-names` with `type=_prompt_names`, `default=None` and `help="JSON-список имён из заголовка и тегов; затравку по решённому языку составит воркер"`. `_prompt_names(raw: str) -> list[str]` — `json.loads(raw)`; not a list of strings → `argparse.ArgumentTypeError("нужен JSON-список строк")`; argparse turns broken JSON into a parse error anyway, exit 2. In `main` — `prompt_names=args.prompt_names` instead of `initial_prompt=args.initial_prompt`.
6. `tests/test_worker.py` (Task 5) — following the answer and the parser: in `test_result_carries_the_verdict_and_the_windows` the expected key set becomes `{"language", "verdict", "detection", "duration", "model", "prompt", "cues"}`; in `test_parser_keeps_the_cli_contract` replace `args.initial_prompt` in the defaults tuple with `args.prompt_names` — without the flag it is still `None`.

- [ ] **Step 12: Daemon — names instead of the prompt, filtering before the cues**

`helper/jobs.py`:

1. Imports: `import re` — to the standard ones (between `logging` and `secrets`); `from helper.asr.prompt import COMPRESSION_RATIO_THRESHOLD, compression_ratio, harvest_names, is_echo, segment_verdict` — before `from helper.asr.worker import SAMPLE_RATE`.
2. Delete `_initial_prompt` (1443-1458) and `_proper_nouns` (1461-1478) entirely — together with the docstring, which is wrong for batched mode (conditioning on the previous text, “half of the 224-token context”). In their place — the module-level `asr_cues`:

```python
def asr_cues(
    segments: list[dict], language: str, prompt: str | None
) -> tuple[list[Cue], list[tuple[str, str]]]:
    """Cues from recognition segments and what was dropped along the way: (reason, text)."""
    dropped: list[tuple[str, str]] = []
    kept = []

    for segment in segments:
        if segment_verdict(segment) == "тишина":
            dropped.append(("тишина", segment["text"]))
        else:
            kept.append(segment)

    # What was already said in the segment: its own per segment, a loop lives inside one chunk.
    heard: dict[int, set[tuple[str, ...]]] = {}

    def drop(sentence: str, segment: dict) -> bool:
        if segment_verdict(segment) == "повтор":
            # Words without case and punctuation: “Idemo dalje” without a period is the same repeat.
            words = tuple(re.findall(r"\w+", sentence.casefold()))
            seen = heard.setdefault(id(segment), set())
            looped = words in seen or compression_ratio(sentence) > COMPRESSION_RATIO_THRESHOLD
            seen.add(words)

            if looped:
                dropped.append(("повтор", sentence))

                return True

        # Echo is checked after normalize_cues: Cyrillic output is compared
        # with the Latin prompt already in Latin.
        if is_echo(sentence, prompt):
            dropped.append(("эхо затравки", sentence))

            return True

        return False

    return build_cues(normalize_cues(kept, language), drop=drop), dropped
```

The filtering rule. “тишина” drops the segment whole, before `normalize_cues`. In a segment marked “повтор”, `drop` drops a sentence as `("повтор", sentence)` when its own compression ratio is above `COMPRESSION_RATIO_THRESHOLD` or it repeats what was already said in this same segment; the words are compared without case (`re.findall(r"\w+", sentence.casefold())`), so a lost period does not hide a repeat. The memory is its own per segment. A loop of two sentences (A. B. A. B. …) leaves the first A and B. In segments without the mark, repeats are never dropped: “Da. Ne. Da.” is ordinary speech. Echo is checked after the repeat, as before.

Two more things in the docstring: the filters are for recognition only — the author-track branch in `_pipeline` does not change; the drops are returned rather than written here because the job keeps the journal.

The `language` parameter (the name is fixed in Interfaces) shadows the module inside `asr_cues`; the module is not needed there, do not call `language.*` in it.

3. `JobRunner._accept_asr` (Task 6): replace the line that builds the cues (`job.cues = self._to_job_cues(build_cues(normalize_cues(result["cues"], verdict.effective)))`) with:
   - `prompt = result.get("prompt")`;
   - `log.info("затравка %s: %s", job.video_id, prompt or "нет")`;
   - `cues, dropped = asr_cues(result["cues"], verdict.effective, prompt)`;
   - for each drop, `log.info("выброшено %s (%s): %s", job.video_id, reason, text)`;
   - `job.cues = self._to_job_cues(cues)`.

   The order is the previous one: language, cues, `segmented` — all before `held.check()` in `_pipeline`. The prompt is written because the worker composes it now, and the daemon discards its stderr on success: without this line the echo on the screen has nothing to be matched against. A line per drop — because a dropped “тишина” is up to 30 s of audio, while a “повтор” is a sentence, and there is no way to check the thresholds on this corpus: the cache has no metrics. The journal is the only check in the field. `JobCue` indices stay contiguous: filtering happens before `_to_job_cues`, and `_next_batch` and `_context_for` rely on `index`.
4. `JobRunner._run_asr`: remove line 1153 (`prompt = _initial_prompt(job.info)`) and 1166-1167 (`--initial-prompt`). Right after the `if hint: args += ["--hint", hint]` block from Task 5 — unconditionally, on every run, even with the list `[]`: `args += ["--prompt-names", json.dumps(harvest_names(job.info.title, job.info.tags), ensure_ascii=False)]`. `ensure_ascii=False` — the names are readable in `ps` and in the journal. Comment: names, not a ready prompt — the worker decides the language after the detector, and only it knows whether a phrase exists for that language; the description is not passed (see `harvest_names`).

- [ ] **Step 13: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_asr_filters.py tests/test_prompt.py tests/test_segment.py -v`
Expected: PASS, 74 passed

Run: `grep -rn --include="*.py" -e "_initial_prompt" -e "_proper_nouns" -e "--initial-prompt" helper tests`
Expected: two lines, both in `tests/test_asr_filters.py`: `["--initial-prompt", PHRASE],` and `assert "--initial-prompt" not in argv` (on `98a3ad2` the same command finds six lines in `helper/jobs.py` and `helper/asr/worker.py`)

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 442 passed

- [ ] **Step 14: Commit**

```bash
/usr/bin/git add helper/asr/prompt.py helper/asr/worker.py helper/segment.py helper/jobs.py tests/test_prompt.py tests/test_segment.py tests/test_asr_filters.py tests/test_worker.py
/usr/bin/git commit -m "Затравка распознавания по решённому языку; тишина, петли повторов и эхо затравки не доходят до реплик"
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---

### Task 9: Cutting does not tear abbreviations

`SENTENCE_END` (`helper/segment.py:32`) ends a sentence at any period before a space. “Today we test the U.S. market, e.g. tech stocks.” is three parts today, `['Today we test the U.S.', 'market, e.g.', 'tech stocks.']`, “Rođen je 5. maja 1980. godine.” is three as well, and the fragments go to the translator as independent phrases. In the local cache that is about 0.63% of recognition cue boundaries, almost all of them on Serbian dates and years (`21.` | `januara`, `1948.` | `godine`), and a handful in English. The rule does not depend on the language: a part that starts with a lowercase letter does not start a sentence. There are no per-language abbreviation lists, and the effective language is not needed.

**Files:**
- Modify: `helper/segment.py:62-72` (`split_sentences`)
- Test: `tests/test_segment.py` (from Task 8, append)

**Interfaces:**
- Consumes: `tests/test_segment.py` with the `word` and `timeline` helpers (Task 8).
- Produces: `split_sentences(text: str) -> list[str]` — the signature is unchanged; a part whose first character is a lowercase letter is glued to the previous one with a single space.

- [ ] **Step 1: Write failing tests**

In `tests/test_segment.py` replace the import with `from helper.segment import build_cues, split_sentences` and append at the end:

```python
def test_abbreviations_do_not_end_a_sentence():
    assert split_sentences("Today we test the U.S. market, e.g. tech stocks.") == [
        "Today we test the U.S. market, e.g. tech stocks."
    ]


def test_ordinals_and_years_do_not_end_a_sentence():
    assert split_sentences("Rođen je 5. maja 1980. godine.") == ["Rođen je 5. maja 1980. godine."]


def test_ordinary_sentences_still_split():
    assert split_sentences("Prva rečenica. Druga rečenica! Treća?") == [
        "Prva rečenica.", "Druga rečenica!", "Treća?",
    ]
    assert split_sentences("Bilo je 1948. Tada je počelo.") == ["Bilo je 1948.", "Tada je počelo."]


def test_a_date_reaches_the_translator_as_one_cue():
    segment = {
        "start": 0.0,
        "end": 5.0,
        "text": "Rođen je 5. maja 1980. godine. Živio je u Beogradu.",
        "words": [
            word(0.0, 0.4, " Rođen"), word(0.5, 0.6, " je"), word(0.7, 0.9, " 5."),
            word(1.0, 1.3, " maja"), word(1.4, 1.9, " 1980."), word(2.0, 2.6, " godine."),
            word(3.5, 3.9, " Živio"), word(4.0, 4.1, " je"), word(4.2, 4.3, " u"),
            word(4.4, 5.0, " Beogradu."),
        ],
    }

    assert timeline(build_cues([segment])) == [
        ("Rođen je 5. maja 1980. godine.", 0.0, 2.6),
        ("Živio je u Beogradu.", 3.5, 5.0),
    ]
```

`test_ordinary_sentences_still_split` passes already — it is a guard: the joining must not eat real boundaries, including a year at the end of a sentence before a capital letter.

- [ ] **Step 2: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: FAIL — 3 failed, 4 passed:
- `test_abbreviations_do_not_end_a_sentence` — got `['Today we test the U.S.', 'market, e.g.', 'tech stocks.']`;
- `test_ordinals_and_years_do_not_end_a_sentence` — got `['Rođen je 5.', 'maja 1980.', 'godine.']`;
- `test_a_date_reaches_the_translator_as_one_cue` — got `[('Rođen je 5.', 0.0, 0.9), ('maja 1980. godine.', 1.0, 2.6), ('Živio je u Beogradu.', 3.5, 5.0)]`.

- [ ] **Step 3: Glue a lowercase part to the previous one**

`split_sentences` (`helper/segment.py:62-72`): after splitting on `SENTENCE_END`, walk the parts in order; a part with `part[0].islower()` is appended to the previous one with a single space; the first part and parts starting with a capital letter, a digit, a quote or a bracket stay separate. Empty text — as now, `[]`.

Into the docstring — the rule and its limits. A period before a lowercase letter does not end a sentence: ordinal numbers and years (“5. maja”, “1980. godine”), abbreviations (“U.S. market”, “e.g. tech”, “npr.”), an ellipsis before a continuation (“ali... ali, nekako”). What stays as it was: a continuation with a capital (“Mr. Smith”) is still cut; scripts without case are unaffected (`islower()` is false for them); a boundary that coincides with the edge of a VAD chunk lies between segments and does not get here.

Do not touch: `SENTENCE_END`, `CONJUNCTIONS` (the break by other languages' conjunctions is deferred, see “Deferred”), `_split_balanced`. Do not raise `PROMPT_VERSION` — that is Task 11.

- [ ] **Step 4: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_segment.py -v`
Expected: PASS, 7 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 446 passed

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add helper/segment.py tests/test_segment.py
/usr/bin/git commit -m "Нарезка не рвёт предложение на сокращениях, порядковых числительных и годах"
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---
### Task 10: The author track: labels are normalised

Right now the author track is chosen by the regexp `BCMS_LANG = ^(sr|hr|bs|sh)(-|$)`, while the
script of its text is decided by the exact comparison `language not in BCMS` in `helper/script.py:76`,
into which the pipeline passes the raw label `track.lang`. Checked on 98a3ad2: `normalize_cues` with
the label `sr-Cyrl`, `SR` or `sr_Latn` leaves “Добар дан, људи.” in Cyrillic, and it goes to the
translator; the regexp does not recognise the labels `cnr` and `sr_Latn` at all; the owner's pin takes
no part in the choice, so with `[asr] language = "en"` the Serbian track (that is, the translation) is
taken as the original. The task reduces all three places to one language family from
`helper/language.py`. Tracks in other languages are not selected in this plan (see “Deferred”): there
is no way to check a label against speech without a GPU.

`helper/script.py` is not assigned to any task in the plan, and by the general rule its `BCMS` is
replaced by whichever task touches the file. The `normalize_cues` gate is the very bug this task
fixes, hence the replacement here.

**Files:**
- Modify: `helper/sources/ytdlp.py:34-35` (the comment and `BCMS_LANG` — delete), `helper/sources/ytdlp.py:321-336` (`usable_manual_track`), the import next to `helper/sources/ytdlp.py:21`
- Modify: `helper/script.py:24-25` (`BCMS` — delete), `helper/script.py:60-86` (`normalize_cues`, the check on line 76), the import next to `helper/script.py:16`
- Modify: `helper/jobs.py:813-817` (`JobRunner._pipeline`, track selection), `helper/jobs.py:854-856` (`JobRunner._pipeline`, the track branch after a successful download: `normalize_cues(segments, track.lang)`)
- Test: `tests/test_author_track.py` (new)

**Interfaces:**
- Consumes: `helper.language.is_bcms(code: str | None) -> bool` (Task 4); `JobRunner._declaration(self, job) -> tuple[str | None, str | None]` — `(pin, metadata hint)` (Task 5); the track decision `verdict` — `LanguageVerdict(effective=language.hint(track.lang) or "sr", …, reason="track", source="manual", track=track.lang)`, which Task 6 builds in the `_pipeline` track branch, and `Job.language` (Task 6); the `pipeline` fixture and the `metadata(**overrides) -> VideoInfo` builder from `tests/conftest.py` (Task 3).
- Produces: `usable_manual_track(info: VideoInfo, pin: str | None = None) -> CaptionTrack | None`; `normalize_cues(cues: list[dict], language: str = "sr") -> list[dict]` latinises when `is_bcms(language)`; the track branch in `_pipeline` normalises the text by `verdict.effective`. `helper.sources.ytdlp.BCMS_LANG` and `helper.script.BCMS` no longer exist.

- [ ] **Step 1: Write failing tests**

The fixture inputs are as Task 3 named them: `pipeline.info` — what the patched `probe` returns,
`pipeline.track` — what the patched `fetch_manual_track` returns. If `tests/conftest.py` names them
differently, take those names.

```python
# tests/test_author_track.py
"""The author track: which label is suitable and in which script its text goes on."""

import pytest

from conftest import metadata
from helper.jobs import Job, Stage
from helper.script import normalize_cues
from helper.sources.ytdlp import CaptionTrack, usable_manual_track

# yt-dlp serves each track in seven formats, and probe puts one record per format.
FORMATS = ("json3", "srv1", "srv2", "srv3", "ttml", "srt", "vtt")

CYRILLIC = [
    {"start": 1.0, "end": 3.0, "text": "Добар дан, људи.",
     "words": [{"start": 1.0, "end": 1.6, "word": "Добар"}]},
]


def tracks(*labels: str) -> list[CaptionTrack]:
    return [
        CaptionTrack(lang=label, url="", ext=ext, name="")
        for label in labels
        for ext in FORMATS
    ]


@pytest.mark.parametrize("label", ["sr", "sr-Latn", "sr-Cyrl", "SR", "sr_Latn", "sh", "hr", "cnr"])
def test_any_spelling_of_the_serbian_family_is_an_author_track(label):
    track = usable_manual_track(metadata(manual_tracks=tracks(label)))

    assert track is not None
    # In yt-dlp the label goes as is: --sub-langs looks for exactly this key.
    assert track.lang == label


@pytest.mark.parametrize("label", ["en", "en-GB", "mk", "live_chat", "und"])
def test_other_languages_and_live_chat_are_never_author_tracks(label):
    assert usable_manual_track(metadata(manual_tracks=tracks(label))) is None


def test_seven_formats_are_one_track_and_the_first_serbian_label_wins():
    info = metadata(manual_tracks=[
        *tracks("en"),
        *tracks("sr-Latn"),
        *tracks("hr"),
        CaptionTrack(lang="live_chat", url="", ext="json", name=""),
    ])

    track = usable_manual_track(info)

    assert (track.lang, track.ext) == ("sr-Latn", "json3")


def test_youtube_metadata_never_refuses_a_serbian_track():
    # The Juske case: YouTube calls the speech English, but it is Serbian.
    info = metadata(audio_language="en", manual_tracks=tracks("sr"))

    assert usable_manual_track(info).lang == "sr"


@pytest.mark.parametrize("pin", ["sr", "hr", "bs"])
def test_a_serbian_family_pin_keeps_the_track(pin):
    assert usable_manual_track(metadata(manual_tracks=tracks("sr")), pin).lang == "sr"


@pytest.mark.parametrize("pin", ["en", "ru", "mk"])
def test_a_pin_outside_the_serbian_family_refuses_the_track(pin):
    # The owner said the speech is not Serbian: the Serbian track of such a video is a translation.
    assert usable_manual_track(metadata(manual_tracks=tracks("sr")), pin) is None


@pytest.mark.parametrize("label", ["sr", "sr-Cyrl", "SR", "sr_Latn", "cnr"])
def test_cyrillic_cues_are_latinized_under_any_serbian_label(label):
    (cue,) = normalize_cues(CYRILLIC, label)

    assert cue["text"] == "Dobar dan, ljudi."
    assert cue["words"][0]["word"] == "Dobar"


@pytest.mark.parametrize("language", ["ru", "uk", "mk", "en"])
def test_cues_of_other_languages_keep_their_script(language):
    cues = [{"start": 1.0, "end": 3.0, "text": "Привет, как дела?", "words": []}]

    assert normalize_cues(cues, language) == cues


async def test_cyrillic_author_track_goes_on_in_latin(pipeline):
    pipeline.info = metadata(video_id="cyrtrack001", manual_tracks=tracks("sr-Cyrl"))
    pipeline.track = [{"start": 1.0, "end": 3.0, "text": "Добар дан, људи.", "words": []}]
    job = Job("cyrtrack001")

    await pipeline.run(job)

    assert job.stage is Stage.DONE
    assert job.source == "manual:sr-Cyrl"
    assert [c.src for c in job.cues] == ["Dobar dan, ljudi."]
    assert (job.language.effective, job.language.track) == ("sr", "sr-Cyrl")


async def test_a_pin_outside_the_serbian_family_sends_the_video_to_asr(pipeline):
    pipeline.configure(language="en")
    pipeline.info = metadata(video_id="pinnedengl1", manual_tracks=tracks("sr"))
    job = Job("pinnedengl1")

    await pipeline.run(job)

    assert job.source == "asr"
    # The track was not taken at all, rather than taken and then dropped.
    assert job.track_failed is False
    assert len(pipeline.workers) == 1
```

- [ ] **Step 2: Run them and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_author_track.py -v`
Expected: FAIL — 13 failed, 19 passed. The six pin tests — `TypeError: usable_manual_track() takes 1 positional argument but 2 were given`; the labels `sr_Latn` and `cnr` — `assert None is not None` (the regexp does not know them); Latin under `sr-Cyrl`, `SR`, `sr_Latn` — `AssertionError: assert 'Добар дан, људи.' == 'Dobar dan, ljudi.'`; the first pipeline test — Cyrillic in `job.cues` (Task 6 left `normalize_cues(segments, track.lang)` in the track branch); the second — `assert 'manual:sr' == 'asr'`.

- [ ] **Step 3: `usable_manual_track` — by language family**

`helper/sources/ytdlp.py`:

- Delete `BCMS_LANG` together with the comment above it (`:34-35`). `import re` stays: `VIDEO_ID` and the failure classifiers rely on it.
- The import `from helper.language import is_bcms` next to `from helper.env import YTDLP_CACHE` (`:21`). `helper/language.py` pulls only the standard library, so there is no cycle.
- `def usable_manual_track(info: VideoInfo, pin: str | None = None) -> CaptionTrack | None` (`:321-336`). If a pin is given and `not is_bcms(pin)` — `None`, without looking at the tracks. Otherwise — the first record of `info.manual_tracks` whose `is_bcms(track.lang)`, or `None`. No separate deduplication is needed: the seven formats of one label go in a row, and only the first record of the label can be returned — that is “one track per label”. Among several labels of the family (`sr-Latn`, `hr`) the first in metadata order is taken; a pin inside the family (`hr`, `bs`) does not change the choice. The label is returned as is: `fetch_manual_track` looks up the key by it (`--sub-langs`, an exact match), and the `manual:<label>` fingerprint is built from it; do not put the normalised code there.
- The docstring is replaced and says:
  1. only a manual track of the Serbian family is suitable, in any spelling of the label (`sr`, `sr-Latn`, `sr-Cyrl`, `SR`, `sh`, `cnr`, `hr`, `bs`): the family is compared through `is_bcms`, not the string; `live_chat` (the live-replay chat) and labels that are not among the Whisper codes are never suitable;
  2. why not other languages: the label must be checked against the speech, and there is no detector without a GPU (only large-v3 and turbo are on disk), while a probe on the GPU would take away the point of the author track — do without the GPU; without a check, an English track would take a video where YouTube called Serbian speech English (Juske);
  3. `pin` is the owner's pin (`[asr] language`); outside the Serbian family it means “the speech is not Serbian”, and the Serbian track of such a video is a translation, not the original. YouTube metadata (`audio_language`) takes no part in the choice — it is exactly what lied once;
  4. the previous argument stands: Macedonian, Bulgarian, Slovak, Russian are acoustic neighbours of Serbian, and the presence of a track does not equal its suitability;
  5. `automatic_captions` do not get here by construction: `probe` puts only the `subtitles` key into `manual_tracks`.

- [ ] **Step 4: `normalize_cues` — by language family**

`helper/script.py`:

- Delete `BCMS` with the comment (`:24-25`). It has no readers outside the file: `grep -rn --include='*.py' -e "script import.*BCMS" -e "script\.BCMS" helper tests spikes` — empty; inside the file only line 76 reads it.
- The import `from helper.language import is_bcms` — as a function, not a module: the `language` parameter of `normalize_cues` would shadow the module name.
- `:76`: `if language not in BCMS:` → `if not is_bcms(language):`. Do not touch the default `language: str = "sr"` — `spikes/asr_real.py:134` relies on it.
- Into the `normalize_cues` docstring, one sentence: the language is compared by family (`is_bcms`), not by string — an author track label can be `sr-Cyrl` or `SR`, and the exact comparison let such Cyrillic through to the translator.

- [ ] **Step 5: The pipeline passes the pin and the effective language**

`helper/jobs.py`, `JobRunner._pipeline`:

- Track selection (98a3ad2: `:813-817`): before it `pin, _ = self._declaration(job)`, then `usable_manual_track(job.info, pin)`. `_declaration` does not raise here: `_pipeline` has already called `_pin()` before `probe` (Task 4), and a bad pin would have refused the job there. The declaration goes through `_declaration`, as with the cache key (Task 7): the link declaration will arrive here too, see “Deferred”. To the comment above the selection (`:804-812`) add: a pin outside the Serbian family does not take the track, see `usable_manual_track`.
- The track branch after a successful download (98a3ad2: `:854-856`): `normalize_cues(segments, track.lang)` → `normalize_cues(segments, verdict.effective)`, where `verdict` is the track decision Task 6 builds right before this. Comment: the script follows the effective language of the decision, not the raw label, otherwise Cyrillic under `sr-Cyrl` or `SR` would go to the translator. `fetch_manual_track`, the `source` stage label and `job.source` still get the raw label.

- [ ] **Step 6: Run them and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_author_track.py -v`
Expected: PASS, 32 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 478 passed

Run: `grep -rn --include='*.py' -e "BCMS_LANG" -e '"sh", "me", "cnr"' helper spikes`
Expected: empty

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add helper/sources/ytdlp.py helper/script.py helper/jobs.py tests/test_author_track.py
/usr/bin/git commit -m "Дорожка автора: метка сверяется по семье языка, кириллица латинизируется при любой записи метки" -m "usable_manual_track берёт ручную дорожку сербского семейства в любой записи метки (sr-Latn, sr-Cyrl, SR, sh, cnr), live_chat — никогда; закреп вне семейства дорожку не берёт: сербская дорожка у несербской речи — перевод. normalize_cues сверяет семью через is_bcms, а ветка дорожки нормализует текст по действующему языку решения, а не по сырой метке: кириллица под sr-Cyrl или SR больше не уходит в переводчик. BCMS_LANG и script.BCMS удалены — семья языка теперь в одном месте, helper/language.py."
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---

### Task 11: Cache version and documentation

This task closes the work: it raises `PROMPT_VERSION` — the version is part of the key and marks a
change of the language rules; once raised, it devalues entries made under the previous rules — and
records what was built: in `docs/running.md` for the owner and with “How it was built” notes in the
spec. It changes no behaviour: apart from the version, only comments and docstrings are edited. The
task does not touch `helper/asr/worker.py`: its comments and dead parameters are edited by Tasks 5
and 8, and here they are only checked (Step 6).

**Files:**
- Modify: `helper/cache.py`: `PROMPT_VERSION` and the comment above it
- Modify: `helper/sources/ytdlp.py:285-290` (the `probe` docstring)
- Modify: `helper/jobs.py:201-203` (`JobCue.failed`), `:1134` (`_finish_translation`), `:1265-1266` (`_serve_cached`), `:1343-1344` (`_translate`) — comments only
- Modify: `docs/running.md` (the heading on the first line and a new section at the end)
- Modify: `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` (notes after lines 290, 799, 909, 956, 965, 1178)
- Test: `tests/test_cache_version.py` (new)

**Interfaces:**
- Consumes: the `pipeline` fixture (`run`, `workers`, `cache`, the `pipeline.info` input) and the `metadata` builder (Task 3); `Job`, `Stage` from `helper.jobs`. For the texts — what Tasks 4–10 built: the `helper/language.py` constants, the `LanguageVerdict.wire()` keys, the cache key with the pin and `SCHEMA_VERSION = 2` (Task 7), `helper/asr/prompt.py` (Task 8), the `split_sentences` joining rule (Task 9), `usable_manual_track` (Task 10).
- Produces: `PROMPT_VERSION = 2`; `docs/running.md` with the heading “Daemon: startup and diagnostics” and the section “Which language the daemon decided”; “How it was built” notes in the language sections of the spec.

- [ ] **Step 1: Write a failing test**

```python
# tests/test_cache_version.py
"""An update devalues tracks made under the previous language rules."""

from conftest import metadata
from helper import cache
from helper.jobs import Job, Stage


async def test_a_track_made_before_the_update_is_made_again(pipeline, monkeypatch):
    pipeline.info = metadata(video_id="beforebump1")

    # An entry under version 1 — the language rules before the version was raised.
    with monkeypatch.context() as old_rules:
        old_rules.setattr(cache, "PROMPT_VERSION", 1)
        await pipeline.run(Job("beforebump1"))

    job = Job("beforebump1")
    await pipeline.run(job)

    assert job.stage is Stage.DONE
    # Recognised again, not served from the cache.
    assert len(pipeline.workers) == 2
    # The previous entry is not deleted: deleting it is the owner's decision.
    assert len(pipeline.cache.variants("beforebump1")) == 2
```

- [ ] **Step 2: Run it and make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_cache_version.py -v`
Expected: FAIL — `assert 1 == 2` on `len(pipeline.workers)`: the version is still 1, and the second run is served from the cache by the entry under version 1.

- [ ] **Step 3: Raise the version**

`helper/cache.py`, `PROMPT_VERSION`: the value is 2, and the comment above it (after Task 7 it speaks only about `PROMPT_VERSION`) is replaced entirely so that it says the version covers everything that makes a track:

```python
# The version of everything that makes a track out of audio or out of an author
# track, except the models themselves (they are in the key separately): the rules
# for choosing the recognition language together with the helper/language.py
# constants, the recognition prompts and filters, the cutting into cues and the
# translator prompt. Raise it on any edit of those: the version is part of the
# key, and previous entries stop being found. They do not leave the disk, though:
# eviction runs only when the cache outgrows its budget.
# 2 — “Source language”: the detector always, a prompt by language, the silence,
# repetition and echo filters, abbreviations in the cutting.
PROMPT_VERSION = 2
```

Do not touch `SCHEMA_VERSION` and its comment (Task 7).

- [ ] **Step 4: Run it and make sure it passes**

Run: `.venv/bin/python -m pytest tests/test_cache_version.py -v`
Expected: PASS, 1 passed

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 479 passed

- [ ] **Step 5: The `probe` docstring**

`helper/sources/ytdlp.py:285-290`, `probe`. The promise “without it yt-dlp materialises ~200 tlang variants” is wrong for yt-dlp 2026.08.19: the extractor builds machine translations only when auto-subtitles are requested (`get_translated_subs` requires `writeautomaticsub` or `listsubtitles`, `yt_dlp/extractor/youtube/_video.py`), and puts them in `automatic_captions`, not in `subtitles`. The new docstring:

```python
    """The video's metadata in one call: tracks, title, description, tags, chapters.

    `skip=translated_subs` is kept as insurance against a yt-dlp version change, not
    for speed: in 2026.08.19 machine translations of tracks are built only when
    auto-subtitles are requested (--write-auto-subs or --list-subs), and they land in
    automatic_captions, not in subtitles, so the flag affects neither this call nor
    fetch_manual_track. A translation the author uploaded themselves is an ordinary
    manual track, and the flag does not cut it out: only the spoken language tells it
    from the original (see usable_manual_track).
    """
```

Do not touch the flag itself in `probe` and `fetch_manual_track`.

- [ ] **Step 6: Outdated comments**

Comments in `helper/jobs.py` (line numbers from 98a3ad2; search by text, the numbers have shifted):

| Where | What is wrong | What to do |
|---|---|---|
| `jobs.py:201-203` (`JobCue.failed`), `:1134` (`_finish_translation`), `:1265-1266` (`_serve_cached`), `:1343-1344` (`_translate`) | an untranslated cue is called Serbian, but the source is now any language | “исходный сербский” → “исходный текст реплики, на языке ролика”; “исходным сербским” → “исходным текстом”; “с непереведённым сербским” → “с непереведённым исходным текстом”; “Показывать сербский вместо русского” → “Показывать исходный текст вместо русского”. |

Then check that nothing is left of the previous rules:

Run: `grep -rn --include='*.py' -e "уедет по LRU" -e "язык берётся из метаданных" -e "def _source_language" -e "def _initial_prompt" -e "def _proper_nouns" -e "40% длительности" -e "VAD вернёт пусто" -e "смотрит только первые 30 секунд" -e "Лестницу оставляем" -e "вытесняет initial_prompt" -e "до приведения bs/hr/sr" -e "initial-prompt" -e "tlang-вариантов" -e "BCMS_LANG" -e '"sh", "me", "cnr"' -e "PROBE_WINDOWS" -e "_merge_bcms" -e "исходным сербским" -e "исходный сербский" -e "непереведённым сербским" -e "Показывать сербский вместо" helper`
Expected: empty

Run: `grep -n "автодетект не используется" config.example.toml`
Expected: empty

Of these patterns, this task itself removes only `tlang-вариантов` (Step 5) and the four “Serbian” ones (the table above). The rest are the work of other tasks: “уедет по LRU” — Task 7; “язык берётся из метаданных” and `config.example.toml` — Task 4; `def _source_language`, “40% длительности”, “VAD вернёт пусто”, “смотрит только первые 30 секунд”, “до приведения bs/hr/sr”, `PROBE_WINDOWS`, `_merge_bcms` — Task 5; `def _initial_prompt`, `def _proper_nouns`, “Лестницу оставляем”, “вытесняет initial_prompt”, `initial-prompt` — Task 8; `BCMS_LANG` and the six-code `BCMS` — Tasks 5 and 10. If any of these is found, the owning task has a defect: stop and report, do not fix it here. `helper/asr/worker.py` is not edited in this task.

- [ ] **Step 7: A section in `docs/running.md`**

Replace the first-line heading `# Daemon without a graphical session` with `# Daemon: startup and
diagnostics`: the file is no longer only about the session. Do not touch the sections below it.

At the end of the file (after the “Decision” section), add:

````markdown
## Which language the daemon decided

The daemon decides the speech language of every video itself. The Whisper detector
listens to up to eight windows of 30 seconds spread over 10–90 % of the video's length,
and the windows vote; a window where there is less than three seconds of speech does not
vote. The language label from YouTube is only a hint: it decides only when no window
voted, because YouTube sometimes calls Serbian channels English. The code in
`[asr] language` of `~/.config/yt-trans/config.toml` is a pin: recognition runs with it,
and the detector runs with it too, so the mismatch is visible. The author track is taken
only from the Serbian family (sr, hr, bs in any spelling of the label), and its language
is the track's label: the speech is not listened to in that case.

The decision lies in the job snapshot, in the `language` field (`null` until accepted):

```
TOKEN=$(cat ~/.config/yt-trans/token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8756/v1/jobs/<id> | jq .language
```

`<id>` — the video id from the address (`watch?v=…`). Jobs live in memory until the
daemon restarts, and no more than twelve finished ones are kept; a video that is already
in the cache is viewed through `/restore` — it starts nothing:

```
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8756/v1/jobs/<id>/restore | jq .snapshot.language
```

A video with no cues at all is not cached: a new job for it — after 12 other finished
ones or a restart — downloads the audio and recognises it under a GPU lease again.

An example — a video where YouTube called Serbian speech English:

```json
{"effective": "sr", "declared": "en", "declaredBy": "metadata", "detected": "sr",
 "confidence": 0.97, "reason": "detector", "source": "asr", "track": null,
 "verified": null, "mismatch": true, "note": "YouTube указал en, по речи sr"}
```

| Field | What it means |
|---|---|
| `effective` | the language it was recognised with; for an author track — the language of its label |
| `declared`, `declaredBy` | what was declared and by whom: `config` — a pin, `metadata` — YouTube; `null` — nobody |
| `detected`, `confidence` | what the detector heard and that language's share in the window votes (0–1); `null` — no speech was found or it was not listened to |
| `reason` | where `effective` comes from: `pin` — a pin; `detector` — the detector; `floor` — a language won that is not a Slavic neighbour of Serbian (mk, bg, sl, ru, uk, be, cs, sk, pl), while the Serbian share is not below 0.2: that is how Whisper on Serbian speech tends to say English; `hint` — no speech was found, the YouTube label was taken; `fallback` — no speech was found and there is no label, Serbian was accepted; `track` — the author track |
| `source`, `track` | `asr` — recognition, `manual` — the author track; `track` — its label |
| `verified` | always `null` for now: there is no check of the author track against the speech |
| `mismatch`, `note` | what was declared diverged from what was heard (or from the track label) — and the phrase about it |

`mismatch: true` is a reason to look into the video. Either YouTube got the label wrong
again, and then everything is right: `effective` is taken from the speech. Or the
detector got it wrong — then there is one remedy: a pin in `[asr] language`. It applies
to all videos at once and works only when the language is the same for all; declaring a
language for a video or a link is not possible yet. The pin is read at startup — after
editing it, restart the daemon. While it is set, tracks made without it are not found:
every opened video is recognised again. A removed pin brings back the previous entry
together with the previous mistake — fixing one video for good is not possible yet. In
the journal, every recognised video has a line with the decision and the window votes,
while a mismatch and windows with speech where the detector did not answer come out as
warnings:

```
journalctl --user -u yt-trans --since today | grep <id>
```

### After the update

Right after merging into the working copy from which the daemon runs
(`~/Work/yt-trans`) — `systemctl --user restart yt-trans`: the daemon holds the code from
startup, while the recognition worker takes it from disk; the previous process sends the
new worker `--initial-prompt`, which it does not know, and every recognition fails with
the refusal “ASR не справился: usage: …”.

An update with this work (a new cache key: `SCHEMA_VERSION` and `PROMPT_VERSION` 2 in
`helper/cache.py`) devalues the whole track cache: every already translated video is
recognised and translated again on the next view — once, and after that it is in the
cache again. Before that, F5 on such a video shows the start button, not ready subtitles.

Previous entries stay on disk — about 18 MB (169 entries as of 2026-11-01) — and do not
go away on their own: eviction runs only when the cache outgrows 2 GiB. Whether to delete
them is the owner's decision. Having decided to delete, stop the daemon first: it keeps
`index.sqlite` open, and at startup it creates empty index and directory anew. Only these
two paths are deleted: next to them in `~/.cache/yt-trans` lie the model weights (`hf/`),
which must not be deleted. Along with the previous ones, entries made after the update
also go — such videos are recognised one more time.

```
systemctl --user stop yt-trans
rm -r ~/.cache/yt-trans/tracks ~/.cache/yt-trans/index.sqlite
systemctl --user start yt-trans
```
````

Before inserting, check every number and name against the code: the windows, 3 s, 0.2 and the
neighbour list — `helper/language.py`; the keys and `reason` — `LanguageVerdict`; the token path —
`TOKEN_PATH` in `helper/app.py`. Where what was built diverges from this text, write what was built.

- [ ] **Step 8: “How it was built” notes in the spec**

`docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md`, six insertions, each a separate paragraph
after the given line (numbers from 98a3ad2, the place is recognised by the quote). The date in the notes
is the commit day (`date +%F`). Where what was built diverges from the text below, write what was built.

After line 290 (“`targetLang` and `generation`; the other three are not there yet.”):

````markdown
**How it was built** (branch `source-language`, YYYY-MM-DD): the source of the cues and the language
live in the `language` object (`source` — `asr` or `manual`, `track` — the author track's label), and
not in a `source` field at the top level of the snapshot: the extension would show that as the label
“качаю asr…” (`extension/content.js`, the `message.source` branch). The fields are in the “Decisions”
section below.
````

After line 799 (“is filled in by the research and will go below.”):

````markdown
**How it was built** (branch `source-language`, YYYY-MM-DD). The daemon has no saved links yet, and a
person can declare the language in one way: with the code in `[asr] language` of the `config.toml`
file, for all videos at once (a pin); `auto` — do not declare. The detector runs on every recognised
video, with a pin as well: the pin sets the decoder token, while a mismatch with what was heard goes
onto the wire (see “Decisions”). Declaring a language for a link or a video is deferred together with
its consequence: the language is not part of the job registry key, but with a per-link declaration it
would have to be. A cheap path for English videos is not open: the author track is still taken only
from the Serbian family.
````

After line 909 (the end of item 10, “the line by the `failed` attribute.”):

````markdown
**How it was built** (branch `source-language`, YYYY-MM-DD). Item 3 is closed: the prompt exists only
for the Serbian family, names come from the title and tags, segments are filtered by `no_speech_prob`,
`avg_logprob` and `compression_ratio` (for “повтор” — only the looping sentences), the prompt echo does
not reach the cues, and the guards that are empty in batched mode are removed from the call. Item 4 is
closed: there is no early return on metadata, the mismatch is written to the journal as a warning and
goes onto the wire. Item 5 is closed in part and differently from what was proposed — see “Decisions”.
Item 6 is closed: the codes are checked, `""` does not reach Whisper. Item 7 is half closed:
abbreviations (“U.S. market, e.g.”, “5. maja”) no longer tear a cue apart, and `CONJUNCTIONS` are
untouched — the line break does not reach the wire (`_to_job_cues` takes only the text). Item 8 is half
closed: Serbian-family labels are accepted in any spelling, other languages are deferred. Item 9 is
closed: `targetLang` left the cache key and stayed in the snapshot. Items 1, 2 and 10 are the
“Translation check” work.
````

After line 956 (the end of the “Decisions” list, “…having evicted the daemon's own job.”):

````markdown
**How it was built** (branch `source-language`, YYYY-MM-DD), by decision:

- **The detector always.** The worker listens to up to eight windows of 30 seconds spread over
  10–90 % of the length (a recording no longer than a window — one window). A window where VAD hears
  less than three seconds of speech does not go to the model and does not vote; nor does a window
  where the model failed. The bs/hr/sr shares fold into one family, and the winner is the largest
  average share over the voting windows. If the winner is not Serbian and not a Slavic neighbour (mk,
  bg, sl, ru, uk, be, cs, sk, pl), while the Serbian share is at least 0.2, the decision is Serbian
  (`reason: "floor"`): that is how Whisper lost Serbian speech to English (3XJLfTSZy6I: en 0.55
  against 0.44). If no window voted — one pass over the first 30 seconds of speech of the whole video;
  and if that is empty too — the YouTube label, and without it Serbian (`fallback`, `detected: null`).
  Metadata decides only then; with window votes it only flags the mismatch (Juske: YouTube en, speech
  sr → sr, “YouTube указал en, по речи sr”). The pin sets the decoder token as given (hr and bs are
  kept), and the detector runs with it too. All of it in the same worker run and under the same GPU
  lease: neither a second model nor a second load. The constants are in `helper/language.py`, and any
  edit of them requires raising `PROMPT_VERSION`.
- **On the wire** — the `language` object in the snapshot and the `{"type": "language"}` event; it
  arrives before `segmented` and `cues` on every path: recognition, author track, cache, restore. The
  fields: `effective`, `declared`, `declaredBy`, `detected`, `confidence` (two decimals), `reason`,
  `source`, `track`, `verified`, `mismatch`, `note`. `note` is a ready-made phrase no longer than 60
  characters: “YouTube указал en, по речи sr”, “в настройках sr, по речи en”, “речи почти нет, язык
  принят за sr”. A job resumed after a pause keeps the decision. The extension does not show it — it
  is frozen; the decision will reach the screen with the tablet page, and meanwhile it is visible in
  the snapshot and the journal (`docs/running.md`, “Which language the daemon decided”).
- **The cache key — a departure from “look up by the declared, write under the effective”.** The key
  holds the human's declaration (the pin; `""` if there is none), not the effective language: the
  cache must answer before any GPU work, while the effective language appears only inside the worker.
  Where the human declared a language, the effective one matches the declared one, and the formula
  holds; where not, the decision lies in the entry and arrives on the wire on every view from the
  cache. `targetLang` left the key (`Job.target_lang` and `targetLang` in the snapshot stayed).
  `PROMPT_VERSION` and `SCHEMA_VERSION` are raised to 2: previous entries are not found and not
  deleted from the disk (about 18 MB as of 2026-11-01; whether to delete them is the owner's call). A
  result with no cues is not written to the cache, and such an old entry is served by neither the
  pipeline nor restore. Restore walks entries from newest to oldest and recomputes the key from the
  saved fingerprint and the current pin; it trusts neither the versions nor the models from the file.
  A low-confidence decision is cached: a repeat would cost the GPU on every view, and the remedy for a
  mistake is the flag on the wire and the pin.
- **Codes.** The hundred faster-whisper 1.2.1 codes are written into `helper/language.py` and checked
  against the installed package by a test. Substitutions: sh, me, cnr → sr; iw → he, jv → jw, fil →
  tl, nb → no, in → id. A wrong pin does not bring the daemon down: at startup — `log.error`, and
  every job — a refusal before the YouTube request with the phrase “неизвестный язык «…»: [asr]
  language в config.toml”, without a retry. A YouTube label that is not in the list simply does not
  hint.
- **The prompt** is a function of the effective language: the phrase exists only for the Serbian
  family, the rest get no prompt at all. Names come from the title and tags, not from the description
  (the field echo came from its template lines), in Latin; a name that stays Cyrillic is dropped.
  Segments are filtered: “тишина” (`no_speech_prob` > 0.6 with `avg_logprob` < −1.0) drops the segment
  whole, while “повтор” (`compression_ratio` > 2.4) only marks it: a batched-mode segment is a piece
  of speech up to 30 s, and only the looping sentences leave it — ones with their own compression
  ratio above 2.4 or repeating what was already said in this segment; the dialogue before the loop
  stays. Every drop is written to the journal. Echo is a departure from “matching the prompt”: no
  exact match occurs in any of the 66 620 cues in the cache, so a sentence counts as echo when it has
  at least two different words, all of them present in the prompt, at least one not a service word
  (ovo, je, video, na, se, srpskom, jeziku), and among them there is an uncommon word of the phrase or
  of the names lead-in (transkript, snimka, pominju) or two different names that are not service
  words: one title name in an everyday phrase (“Šta je ovo?”) is speech.
- **The author track in any language — deferred.** There is no way to check a label against speech
  without a GPU: no detector smaller than large-v3 and turbo is on disk, a probe on the GPU would take
  away the point of the author-track path, and taking a foreign track without checking means bringing
  back the Juske case (label en on Serbian speech). The conditions for coming back are in the plan,
  the “Deferred” section. The lesser thing is done: Serbian-family labels are accepted in any spelling
  (sr-Latn, sr-Cyrl, SR, sh, cnr) and are latinised, `live_chat` is never accepted, and a pin outside
  the family does not take the track. The track decision: `source: "manual"`, `reason: "track"`,
  `detected` and `verified` — `null`.
- **Not done here** (the “Translation check” work): the typed second line, the checkers' verdict, the
  language-independent protection against cue drift and the visible notes “выравнивание не
  проверено” and “вторая строка не удостоверена”. “One model for all directions” is correct without
  edits.
````

After line 965 (“This is one line.”):

````markdown
**How it was built** (branch `source-language`, YYYY-MM-DD): `config.example.toml` has
`language = "auto"` and an explanation that the detector always runs, while the code pins the language
of every video and works only when the language is the same for all; sh, me and cnr are accepted as
sr.
````

After line 1178 (“— not a silent choice of one of the two, but a note on the screen.”):

````markdown
**How it was built** (branch `source-language`, YYYY-MM-DD). The first is done, see “Decisions”.
There is no probe for the author track: it is deferred together with tracks in other languages (in the
same place). While tracks are taken only from the Serbian family, the decision on them honestly says
the speech was not listened to: `detected` and `verified` are `null`, and the mismatch is counted
between the declaration and the track label (“YouTube указал en, дорожка автора sr”).
````

- [ ] **Step 9: Verify**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 479 passed

Run: `/usr/bin/git status --short`
Expected: only `helper/cache.py`, `helper/sources/ytdlp.py`, `helper/jobs.py`, `docs/running.md`, the spec and the new `tests/test_cache_version.py`; `helper/asr/worker.py` and `extension/` untouched (and the plan file itself, if steps were ticked in it — do not add it in this commit).

- [ ] **Step 10: Commit**

```bash
/usr/bin/git add helper/cache.py helper/sources/ytdlp.py helper/jobs.py docs/running.md docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md tests/test_cache_version.py
/usr/bin/git commit -m "PROMPT_VERSION 2: дорожки прежних правил языка делаются заново; как читать решение о языке" -m "Версия покрывает всё, что делает дорожку, кроме моделей: правила языка и константы helper/language.py, затравку и фильтры распознавания, нарезку, промпт переводчика. После обновления каждый ролик распознаётся и переводится заново один раз; прежние записи (около 18 МБ) остаются на диске, удалять их — решение хозяина. docs/running.md объясняет, как прочесть поле language снимка; пометки «Как построено» в спецификации записывают построенное и отступления (ключ кэша по закрепу, правило эха, дорожки на других языках отложены). Поправлены устаревшие докстроки и комментарии: skip=translated_subs и «сербский» у непереведённых реплик. docs/running.md теперь называется «Демон: запуск и диагностика»."
```

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---

### Task 12: Calibration on the GPU

A manual step for the owner: it needs a GPU and YouTube, and there is no automated test and cannot be one.
Do it after Task 11 and before merging the branch. The detector constants (`MIN_SPEECH_SECONDS = 3.0`,
`BCMS_FLOOR = 0.2`, `SLAVIC_NEIGHBOURS`) were derived from field numbers collected when windows without
speech still voted; after Task 5 they do not vote, and the shares will shift. The step checks that the
decision on the same videos is still correct, and touches nothing if it is. Along the way it runs the
worker's answer through the daemon's `asr_cues` and looks at what Task 8's filters drop: their thresholds
have not been checked on this corpus, and the cache has no metrics.

The fourth video has Russian speech: `2zcvNJdxPHE` (525 s), it is in the owner's cache, and its cues
contain the Russian letters ы, э, ъ, ё and not a single Serbian one. The fallback is `tXuNg8BxieA`
(Ukrainian speech, 1126 s, from the same place). The fifth is `jWrDvxtofLk`: in the cache, at
201,8–229,6 s it has six dialogue cues, and after them in the same chunk Whisper fell into a “tak,
tak…” loop; the video checks that “повтор” (repetition) carries away the loop, not the dialogue. The
sixth is a Serbian song with an intro without words; the owner picks the id and writes it into the
`song` variable at the start of Step 2. The song checks the path the tests do not have: how Silero
hears singing, the pass over the whole file, the fallback language, and what the filters do to the
song's lines.

**Files:**
- Modify (only if acceptance fails): `helper/language.py` — `BCMS_FLOOR`, `SLAVIC_NEIGHBOURS` (Task 5); with them `tests/test_language.py` — the literal in `test_detector_constants_are_pinned`, and when `BCMS_FLOOR` changes, the first two lines of `test_floor_boundary`; `docs/running.md` — the threshold and the neighbour list (the `reason` table, the `floor` row)
- Modify (only if a constant changed and the branch has already been deployed): `helper/cache.py` — `PROMPT_VERSION` 3 and a history line above it; with it the number in `docs/running.md` (“After the update”)
- Modify: `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` — the calibration record at the end of the “How it was built” note after the “Decisions” list (Task 11 added it)
- Test: a manual check on the GPU

**Interfaces:**
- Consumes: the Task 5 worker CLI — `--language auto`, `--hint CODE`, `--pcm FILE` — and its answer: `verdict` (`LanguageVerdict.wire()`), `detection` (`bcmsShare`, `votes`, `probed`, `windows`, where a window is `[family or null, its share in the window, seconds of speech]`); the `helper/language.py` constants (Task 5). From Task 8: the `--prompt-names JSON` flag, `harvest_names(title, tags)` from `helper/asr/prompt.py`, and in the answer — `prompt` and, for every cue, `no_speech_prob`, `avg_logprob`, `compression_ratio`; `helper.jobs.asr_cues(segments, language, prompt)` — cues and drops `(reason, text)`, exactly what `_accept_asr` does with the answer. `helper.sources.ytdlp.probe` and `audio_pcm`; `helper.language.hint` (Task 4); `PROMPT_VERSION = 2` (Task 11).
- Produces: a calibration record in the spec; if Step 7 acceptance fails, corrected detector constants (Step 8). The filter thresholds are not changed here.

- [ ] **Step 1: Free the GPU**

```bash
systemctl --user stop yt-trans
nvidia-smi --query-gpu=memory.free --format=csv,noheader
```

Expected: at least 6144 MiB free — that is what large-v3/float16 asks for (`ASR_VRAM_MIB` in `helper/config.py`). If less, the translator is still on the GPU: `ollama ps`, then `ollama stop qwen3.8:27b` (it leaves on its own a minute after the last request); and if it is still low, the desktop took the memory — close the heavy windows. Stop the daemon, do not leave it idle: a job started by a tab in the middle of calibration would sit waiting for the GPU, and the watchdog would take the worker for someone else's work.

- [ ] **Step 2: Download the audio, the language label and the names**

From the root of the working copy of the `source-language` branch (otherwise `helper` does not import). Put the song id into `song=` instead of `ID_ПЕСНИ`. The video list lands in `$dir/ids`, and the following steps take it from there — each block is self-contained and works in a new shell:

```bash
dir=/var/tmp/yt-trans-calibration
song=ID_ПЕСНИ
mkdir -p "$dir"
echo Q2QcYOBk0Jo 3XJLfTSZy6I 3UbtKc9MlXg 2zcvNJdxPHE jWrDvxtofLk "$song" > "$dir/ids"
for id in $(cat "$dir/ids"); do
  .venv/bin/python -c '
import json
import sys
from pathlib import Path
from helper.asr.prompt import harvest_names
from helper.language import hint
from helper.sources.ytdlp import audio_pcm, probe
video, folder = sys.argv[1], Path(sys.argv[2])
info = probe(video)
(folder / f"{video}.hint").write_text(hint(info.audio_language) or "")
names = harvest_names(info.title, info.tags)
(folder / f"{video}.names").write_text(json.dumps(names, ensure_ascii=False))
audio_pcm(video).tofile(folder / f"{video}.f32")
print(video, "метка YouTube:", info.audio_language, "длина, с:", info.duration)
' "$id" "$dir"
done
```

Expected: six lines; `Q2QcYOBk0Jo` most likely has the label `en`, and `3XJLfTSZy6I` has `None`. In `$dir/$id.names` — the same JSON list of names from the title and tags that the daemon would pass to the worker in `--prompt-names` (`JobRunner._run_asr`, Task 8). In `$dir/$id.hint` — the label normalised by `language.hint`, as in `JobRunner._declaration`: `en-US` → `en`, an unfamiliar one — empty, and then Step 3 does not pass `--hint`; the output line shows the raw YouTube label. The audio is taken by the same `audio_pcm` as in the daemon: the same `-S lang,+size` sorting, that is, the original track, not the auto-dub. `/var/tmp` is on disk (`/tmp` on Fedora is memory), and the file is 64 KB per second of video. A 403 refusal from googlevideo is sometimes one-off — rerun the command for that video.

- [ ] **Step 3: Run the worker**

```bash
dir=/var/tmp/yt-trans-calibration
for id in $(cat "$dir/ids"); do
  hint=$(cat "$dir/$id.hint")
  names=$(cat "$dir/$id.names")
  if [ -n "$hint" ]; then
    .venv/bin/python -m helper.asr.worker --language auto --hint "$hint" --prompt-names "$names" --pcm "$dir/$id.f32" > "$dir/$id.json"
  else
    .venv/bin/python -m helper.asr.worker --language auto --prompt-names "$names" --pcm "$dir/$id.f32" > "$dir/$id.json"
  fi
  echo "$id: код выхода $?"
done
```

Expected: `exit code 0` six times, a dozen or so seconds per video. `--hint` and `--prompt-names` are exactly what the daemon would pass (`JobRunner._declaration`, `JobRunner._run_asr`), so the language decision and the prompt are the ones the viewer will see: without `--prompt-names` the worker would recognise with no prompt at all, and the drops of Step 5 would be measured on the wrong output. If not zero, `$dir/$id.json` contains `{"error": …}`, with the cause in the output above; `CUDA out of memory` means Step 1 did not free the GPU.

- [ ] **Step 4: Record the fields**

```bash
dir=/var/tmp/yt-trans-calibration
for id in $(cat "$dir/ids"); do
  echo "== $id"
  jq -c '.verdict' "$dir/$id.json"
  jq -c '.detection | {bcmsShare, votes, probed}' "$dir/$id.json"
  jq -c '.detection.windows[]' "$dir/$id.json"
done
```

Expected: per video — the whole `verdict`, the BCMS share with the number of voting and probed windows, then the windows one line each like `["sr",0.81,24.1]` (family, its share in the window, seconds of speech; `null` instead of a family — the window did not vote). For the song, the seconds of speech in each window, `votes`/`probed` and the decision are recorded as they are: that is the measurement. Keep the whole output: it goes into the record and into the commit.

- [ ] **Step 5: Run the worker's answer through the daemon's filters**

The daemon drops, not the worker: the answer carries all segments with their metrics, and `asr_cues` from `helper/jobs.py` (Task 8) does with it exactly what `_accept_asr` does — “тишина” (silence) goes as a whole segment, “повтор” (repetition) and “эхо затравки” (prompt echo) as a sentence:

```bash
dir=/var/tmp/yt-trans-calibration
for id in $(cat "$dir/ids"); do
  .venv/bin/python -c '
import collections
import json
import sys
from helper.jobs import asr_cues
video, path = sys.argv[1], sys.argv[2]
result = json.loads(open(path, encoding="utf-8").read())
cues, dropped = asr_cues(result["cues"], result["language"], result.get("prompt"))
print("==", video, "затравка:", result.get("prompt"))
print("сегментов", len(result["cues"]), "реплик", len(cues), "выброшено", dict(collections.Counter(reason for reason, _ in dropped)))
for reason, text in dropped:
    print(f"  {reason}: {text}")
' "$id" "$dir/$id.json"
done
```

The cues of `jWrDvxtofLk` around the loop:

```bash
dir=/var/tmp/yt-trans-calibration
.venv/bin/python -c '
import json
import sys
from helper.jobs import asr_cues
result = json.loads(open(sys.argv[1], encoding="utf-8").read())
cues, _ = asr_cues(result["cues"], result["language"], result.get("prompt"))
for cue in cues:
    if 195 <= cue.start <= 235:
        print(f"{cue.start:7.1f}–{cue.end:7.1f}  {cue.text}")
' "$dir/jWrDvxtofLk.json"
```

Expected: per video — the prompt (`None` for the English and Russian ones: the phrase exists only for the
Serbian family), the number of segments and cues, the drops by reason and the text of every drop — all
of them, uncut; then the cues of `jWrDvxtofLk` from 195 to 235 s. Acceptance:
- what is dropped is noise, not speech: silence with an invented phrase (“Hvala na gledanju.”),
  repetition loops, prompt echo;
- `jWrDvxtofLk`: the dialogue before the loop (about 201,8–229,6 s) is in the cues, the loop, if any,
  is dropped as “повтор” (repetition); if it did not loop this time, record that;
- the song: not a single line of text is dropped as “тишина” (silence); “повтор” (repetition) may
  remove only a line repeated in the same chunk, and its first performance stays; record whether the
  song ended up with zero cues.

If there is speech among what was dropped, do not change the thresholds: write the numbers and texts into the calibration record (Step 9) and leave the decision to the owner. Keep the output together with the output of Step 4.

- [ ] **Step 6: Bring the daemon back**

```bash
systemctl --user start yt-trans
systemctl --user is-active yt-trans
```

Expected: `active`

- [ ] **Step 7: Check against acceptance**

| Video | Speech | `verdict.effective` | And also |
|---|---|---|---|
| `Q2QcYOBk0Jo` (Juske) | Serbian; YouTube called it `en` | `sr` | `reason` `detector` (`floor` if Step 8 lowered `BCMS_FLOOR` for this video); if the label was `en` — `declared` `en`, `declaredBy` `metadata`, `mismatch` `true`, `note` “YouTube указал en, по речи sr” |
| `3XJLfTSZy6I` (IZA MRAKA) | Serbian, no label | `sr` | `reason` `detector` or `floor`; `bcmsShare` not below `BCMS_FLOOR` (0.2, unless Step 8 changed it) |
| `3UbtKc9MlXg` | English | `en` | `reason` `detector`; `bcmsShare` below `BCMS_FLOOR` (0.2, unless Step 8 changed it; in the field it was 0.00 in all eight windows) |
| `2zcvNJdxPHE` | Russian | `ru` or the code Step 8 added to `SLAVIC_NEIGHBOURS` | `reason` `detector`, not `floor` |
| `jWrDvxtofLk` | Serbian: dialogue, followed by a loop in the same chunk | `sr` | the dialogue 201.8–229.6 s is in the cues, the loop, if any, is dropped as “повтор” (repetition) (Step 5) |
| the song (`song`) | Serbian singing, an intro without words | as it came out | not a single line of text in “тишина” (silence); “повтор” (repetition) removed only a line repeated in the same chunk, and its first performance stayed (Step 5) |

For the first five, `votes` is at least 1: there is speech everywhere; for the song `votes` is whatever came out, and that is what gets recorded. If every row matches, do not touch the constants, go straight to Step 9.

- [ ] **Step 8: If a row does not match**

- `3XJLfTSZy6I` gave `en` with `reason` `detector`: the Serbian share is below the threshold. If its `bcmsShare` is at least 0.1 higher than that of `3UbtKc9MlXg`, lower `BCMS_FLOOR` to the midpoint between them, rounded down to 0.05. If the gap is less than 0.1, do not touch the constants: the threshold does not separate these two videos; record the numbers and leave the decision to the owner (the remedy for such a channel is a pin).
- `3UbtKc9MlXg` gave `sr` with `reason` `floor`: English picked up a Serbian share above the threshold. The same midpoint rule, but upward; with a gap under 0.1 — stop as well.
- `Q2QcYOBk0Jo` gave something other than `sr`: with `reason` `hint` — the windows did not vote, see the `votes` item below; with `detector` and `en` — as in the first item.
- `2zcvNJdxPHE` gave `sr`: with `reason` `floor` — the winner is not listed as a neighbour; add to `SLAVIC_NEIGHBOURS` the code that comes first in most of the windows in `detection.windows`. With `reason` `detector` — Serbian is on top in the detector on its own, and constants do not cure that: record it and leave it to the owner.
- `votes` 0 or less than half of `probed` for a video that has speech: look at the third number of the windows, the seconds of speech. If for windows where speech certainly exists it is less than 3, VAD counts differently from what Task 5 expected. That is a review of its code, not a constant change: stop.
- `jWrDvxtofLk` or the song did not match — do not touch the constants and thresholds for them: write the numbers and texts into Step 9 and leave the decision to the owner.

After changing a constant:

1. The value goes into `helper/language.py`, with a measurement line at the constant (video, share, date). Along with it: the literal in `test_detector_constants_are_pinned` (`tests/test_language.py`); if `BCMS_FLOOR` becomes f, the first two lines of `test_floor_boundary` become `([("en", 1−f), ("sr", f)], "sr", True)` and `([("en", 1−f+0.01), ("sr", f−0.01)], "en", False)`, as literals with two decimal places (for 0.15 — `([("en", 0.85), ("sr", 0.15)], "sr", True)` and `([("en", 0.86), ("sr", 0.14)], "en", False)`); the threshold and the neighbour list go into `docs/running.md` (the “Which language the daemon decided” table, the `floor` row) and into the spec note after “Decisions”. Do not touch `IZA_MRAKA`, `ENGLISH`, `RUSSIAN`, the `mk` line or the worker tests.
2. `.venv/bin/python -m pytest -q` — PASS, 479 passed. The field-case tests — `test_floor_turns_the_english_bias_into_serbian`, `test_serbian_speech_under_an_english_label`, `test_english_without_bcms_stays_english`, `test_slavic_neighbour_keeps_its_language_over_the_floor` — are not edited and must stay green: a value that breaks them contradicts the field — do not take it.
3. Steps 1 and 3–7 again, on all videos (the audio, labels, names and the video list in `$dir/ids` are already there, Step 2 is not needed).
4. Do not raise `PROMPT_VERSION` a second time: Task 11 has already raised it to 2, and nothing outside the tests is cached under 2 — the branch has not been deployed yet. If the branch has been deployed, raise it to 3 (the rule from the constants comment): above `PROMPT_VERSION` in `helper/cache.py` — a history line `# 3 — calibration …`, and the number — in `docs/running.md` (“After the update”) and in the spec note (“`PROMPT_VERSION` and `SCHEMA_VERSION` raised to 2”); `helper/cache.py` — in the `git add` of Step 10.

- [ ] **Step 9: Record the calibration**

In the spec, at the end of the “How it was built” note that Task 11 placed after the “Decisions” list, add a
paragraph with the numbers from Steps 4 and 5:

````markdown
Calibration on the GPU (YYYY-MM-DD, `python -m helper.asr.worker --language auto` with the YouTube label in
`--hint` and the names in `--prompt-names`, as the daemon would pass them; drops are the daemon's
`asr_cues` over the worker's answer):

| Video | Speech | Windows (votes/total) | BCMS share | Decision | `reason` | Dropped (silence / repetition / echo) |
|---|---|---|---|---|---|---|
| Q2QcYOBk0Jo | Serbian, label en | … | … | sr | detector | … / … / … out of … |
| 3XJLfTSZy6I | Serbian, no label | … | … | sr | … | … / … / … out of … |
| 3UbtKc9MlXg | English | … | … | en | detector | … / … / … out of … |
| 2zcvNJdxPHE | Russian | … | … | ru | detector | … / … / … out of … |
| jWrDvxtofLk | Serbian, a loop in the chunk | … | … | sr | … | … / … / … out of …; dialogue 201.8–229.6 s: … |
| … (the song) | Serbian singing | … | … | … | … | … / … / … out of …; cues … |

The constants did not change. What the filters dropped is noise: …

The last lines are factual: if a constant changed, “`BCMS_FLOOR` 0.2 → …: …” with the reasoning from
Step 8; if the filters dropped speech, say so, with the numbers and texts from Step 5, and leave the
thresholds to the owner's decision.
````

- [ ] **Step 10: Commit and clean up**

Commit after Step 6, not in the middle of calibration: the hook loads `qwen3.8:27b` onto the GPU.

```bash
/usr/bin/git add docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md
/usr/bin/git commit -m "Калибровка определителя языка на карте: Juske, IZA MRAKA, английский и русский ролики дают sr, sr, en, ru" -m "<по строке на ролик: verdict.effective, reason, bcmsShare, votes/probed и окна из шага 4, выбросы фильтров из шага 5>"
rm -r /var/tmp/yt-trans-calibration
```

In `git add` — exactly what changed. If a constant changed, the first line becomes `/usr/bin/git add docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md helper/language.py tests/test_language.py docs/running.md`, and the first line of the message names the change; if `PROMPT_VERSION` was also raised to 3, add `helper/cache.py` to them.

After merging into `~/Work/yt-trans` — `systemctl --user restart yt-trans`: the daemon holds the code from startup, while the recognition worker takes it from disk (`docs/running.md`, “After the update”).

Each git command goes in a separate Bash call, via `/usr/bin/git`; the timeout must be at least 360000 ms (the hook runs a diff review for up to 300 s); do not use `--no-verify`; the message is in Russian, without `Co-Authored-By` lines or any other signatures.

---
