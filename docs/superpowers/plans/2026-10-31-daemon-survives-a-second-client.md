# The daemon survives a second client — implementation plan

## How it was done

Summary as of 2026-11-01, branch `worktree-daemon-second-client`. The task text below
is the original plan, it has not been rewritten; where what was built diverged from it,
what is said here holds. The decisions made along the way are recorded here on the merits:
the log in which they were made is not part of the repository.

**Architecture.** Not “the first subscriber decides”: a job is keyed by the pair (video,
`use_author_track`). “First decides” broke the author-track switch in the
extension: it expects a poll to carry the setting on every round and a changed flag to
yield a different job, but under “first decides” the switch did
nothing until the job was evicted, reset, or the daemon restarted.
`targetLang` is not part of the key, it stays with the first caller and is visible in the snapshot.
Each event journal starts from a random 48-bit base rather than zero:
a cursor from a foreign journal (another variant, a reset job) does not fall into
the range of this one and gets a snapshot through the ordinary `Job.poll` branch. 48 bits — so that
the cursor remains an exact integer in JavaScript (up to 2^53): the
extension carries it as a JSON number. The price of the pair key: two clients with
different flags — two pipelines per video, and
a switch in the middle of a running job leaves the previous variant holding the GPU until
the end, while the chosen one waits.

- **Task 2.** Clients with different flags get two jobs, not one:
  instead of `second is first` the tests check that each variant has its own job,
  started exactly once, and that a hundred diverging polls produce exactly two
  jobs. A cursor ahead of the journal gets a snapshot with `ahead["cursor"] ==
  job.journal_end`, not 0: `journal_end` is the end of the journal including the base.
  The repeatable-failure branch removes only its own job (`_remove`), not the whole
  video via `drop`: the neighbouring variant may be running for another client. A job
  recreated after a failure gets `generation` one higher than before; after `/drop`
  the count starts from 0 again. Where the video is named without a flag: `get` and the
  `GET /v1/jobs/{id}` snapshot take the most recently touched variant, the playhead and
  `unwatch` go to all variants, a reset without a key cancels all.
- **Task 3.** `authorTrack` in the body of `POST /v1/jobs` and in `watch` must be
  a real boolean: absent — `true`, anything else (`null`, `"false"`, 0, 1) —
  a refusal without a job: 400 on `POST`, on `watch` a `state` event with stage `error` and
  `retryable: false`. Otherwise a non-boolean would land in the key as a phantom third variant.
  A socket watches no more than one variant of a video: a `watch` with a different
  flag moves its queue to that variant.
- **Task 4.** Instead of “empty set → 0.0”: when the records of all clients
  are stale, `aim()` returns the freshest position among them, otherwise a frozen
  background tab of the only viewer would yank the translation window to the start of the video;
  0.0 — only when there are no records at all. `playhead` — a look without a turn: what
  `aim()` will return next, without turning the wheel (in the plan it was
  `aim()` itself). Liveness is decided in one place, `Job.live_slots()`, and a record exactly
  `PLAYHEAD_TTL` seconds old is still alive.
- **Task 5.** `/drop` accepts a request with no body at all — that is how the
  extension sends it. An invalid `clientId` (empty, not a string, longer than 64
  characters) is the client `"legacy"`: it removes only its record and does not cancel
  someone else's work. `clientId: null` or its absence — cancels all variants, as
  before. Registration is a live position record: a client that subscribed but did not
  report a position is not protected from someone else's reset. A body that is not
  a JSON object (a list, text, a form) — 422.
- **Task 6.** The limit is 16, not 4: it is protection against abuse, not the depth of
  the queue, and the fifth tab of an ordinary evening must not get a refusal. The refusal —
  `CapacityError`, not `PermanentJobError`: overflow passes by itself, while
  `PermanentJobError` is marked non-retryable by the runner. All three paths that
  create a job refuse, not two: `POST /v1/jobs` — 429 with a phrase; `watch` — a `state`
  event with stage `error` and `retryable: true`, without a subscription;
  `/events?create=true`, the only path by which the extension creates jobs —
  200 with a snapshot in stage `error` and with the client's cursor as sent (on a response that is not
  200 the extension writes “демон ответил …” and retries, while a snapshot with
  `error` shows the phrase and stops the poll). An abandoned pause — `PAUSED`, no job,
  nobody has touched it for longer than `PAUSED_AUDIO_TTL` — does not count; one flag
  for both the slot count and the cleanup of its audio (`Job.is_stale_pause`). A failed
  job with a cooled-down refusal at the limit is returned as is, not recreated: removing
  it and refusing would mean losing the generation number and the subscribers. The
  audio budget removes files by age and, when a file is accounted to a job, through its
  `discard_audio()`. Test identifiers are 11 characters (`f"video{i:05d}a"`), like
  the real ones. At the limit, a video whose track is already in the cache also gets
  a refusal. Audio cleanup with a disk error does not fail the request (added by
  a final edit).
- **Task 7.** Documentation only, `docs/running.md`. Lingering with the audio and
  AI tracker bound to the session via drop-ins (`ConditionEnvironment`, `PartOf`) was
  tried and rolled back: a unit skipped by condition still pulls its `Wants=`/`Requires=`.
  Currently `Linger=no`, `packaging/yt-trans.service` unchanged; how to run
  the daemon without a session is decided by the owner — options in `docs/running.md`.

What is deferred — in the section “Deferred to the next plan”.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** make it so that the appearance of a second client on the same video does not recreate the job, does not cancel someone else's work, and does not drive the translation into a race for the playhead.

**Architecture:** changes inside `helper/jobs.py` and `helper/app.py`. Job identity stops depending on the client's settings (the first subscriber decides, the divergence is visible in the snapshot). The playhead and the subscription become per-client quantities, the key is an optional `clientId`; a client that does not send it behaves exactly as today, so the extension is untouched. Plus limits on unfinished jobs and on deferred audio.

**Tech Stack:** Python 3.14, FastAPI, pytest + pytest-asyncio, httpx ASGITransport.

**Spec:** `docs/superpowers/specs/2026-10-30-tablet-subtitles-design.md` (sections “What needs fixing before we start” and “Two holes the design missed”).

## Global Constraints

- Protocol changes are **additive only**. Do not introduce new `Stage` values: the extension checks against a known list of stages, an unfamiliar one would leave it with a stale caption and a poll that never stops.
- `extension/` does not change by a single line.
- A client without `clientId` keeps today's behaviour on all paths.
- Tests do not touch the network, the GPU or real cache paths. `YT_TRANS_CACHE` and `YT_TRANS_CONFIG` are overridden **before** the first `helper.*` import: `JobManager.__init__` runs `shutil.rmtree(PAUSED_AUDIO_DIR)`, that is, a run without isolation would delete the user's deferred audio.
- Russian in comments and refusal messages, as throughout the repository.

## Deferred to the next plan

Deliberately absent here are two items from the design's list of preconditions:
rate limiting of job creation per caller and `proxy_headers=False` on uvicorn.
Both make sense only when the port is reachable from the network, and both rely on
the caller's address, which does not exist when bound to the loopback. They belong to
the plan for exposing the daemon to the network.

Deferred along the way (2026-11-01):

- **The readiness boundary on the wire** (up to which second the translation has been carried) and the “not ready yet” view — to the tablet page plan.
- **A “currently translating for another device” field** — there too: the daemon does not have it yet.
- **Registration per variant rather than per video.** A position arrives without the track flag and lands on all variants, so a reset with a key will not cancel a variant nobody is watching while the neighbouring one is being watched. The work is extra, but not lost.
- **The previous variant on a switch — into a pause.** Now it holds the GPU until the end, while the chosen one waits. When a variant is created, the running neighbouring one should be moved to `PAUSED`, from where it will continue on the next request; it cannot be cancelled — that is what produced the storm.
- **Ready videos past the limit.** At the limit, a video whose track is already in the cache also gets a refusal: there is no cheap check at creation time, the key needs the duration and the track fingerprint. The prefetch plan should let a finished one past the limit and not spend the viewer's slots.
- **An orphaned yt-dlp on cancel.** `cancel` does not stop a download already running in `asyncio.to_thread`. The storm that multiplied this is gone, but the hole remains.
- **Starting without a session.** Lingering is not enabled: first the audio and AI-tracker units must be bound to the session at their source (`system-config`, `tobii`). The owner decides; options in `docs/running.md`.

## Review Focus

- Two clients with different `targetLang` on the same video: the second silently gets the first's language, and there is nothing to read about it in the snapshot. Test in task 2.
- A client disappeared without `/drop` (the tab was killed): its playhead stays in the set forever and keeps steering the translation. Test in task 4.
- `clientId` missing, empty, not a string or absurdly long — it goes as a key into the dictionary. Test in task 4.
- A poll cursor ahead of the journal: the client must get one snapshot and then proceed normally, not spin a tight loop. Test in task 2.
- `/drop` on a video that has no job. Test in task 5.

---

### Task 1: Test harness

**Files:**
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/conftest.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: nothing.
- Produces: fixture `manager` → `helper.jobs.JobManager` on temporary paths; fixture `client` → `httpx.AsyncClient` over `helper.app.app` with a valid token.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
from pathlib import Path

def test_cache_paths_are_isolated():
    from helper import env

    assert Path.home() not in env.CACHE_DIR.parents
    assert Path.home() not in env.CONFIG_DIR.parents


def test_manager_fixture_builds(manager):
    assert manager.stats()["jobs"] == 0
```

- [ ] **Step 2: Run and make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: FAIL — `pytest: command not found` or `fixture 'manager' not found`

- [ ] **Step 3: Set up the harness**

`requirements-dev.txt`: `pytest`, `pytest-asyncio`. `pytest.ini`: a `[pytest]` section with `asyncio_mode = auto` and `testpaths = tests`.

`tests/conftest.py` sets `os.environ["YT_TRANS_CACHE"]` and `["YT_TRANS_CONFIG"]` via `tempfile.mkdtemp()` **at module level, before any `helper.*` import** — the `tmp_path_factory` fixture is not suitable for this, it is available only inside fixtures, while conftest is imported before the test modules. Then the fixtures:

- `manager` — `JobManager(config=...)`, where the configuration is assembled in memory via `helper.config.Config` without reading the disk;
- `client` — `httpx.AsyncClient(transport=ASGITransport(app=helper.app.app), base_url="http://test")`, header `Authorization: Bearer <helper.app.TOKEN>`.

- [ ] **Step 4: Run and make sure it passes**

Run: `.venv/bin/python -m pytest tests/test_harness.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt pytest.ini tests/
git commit -m "Оснастка для тестов на временных путях, чтобы прогон не трогал кэш"
```

---

### Task 2: The author-track flag does not recreate a live job

**Files:**
- Modify: `helper/jobs.py:1245-1261` (`JobManager.get_or_create`), `helper/jobs.py:306-320` (`Job.snapshot`)
- Test: `tests/test_job_identity.py`

**Interfaces:**
- Consumes: fixture `manager` from task 1.
- Produces: `Job.snapshot()` additionally returns `"authorTrack": bool`, `"targetLang": str`, `"generation": int`. `Job.generation` — a counter that grows on every recreation of a job for the same video.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_job_identity.py
def test_second_client_with_other_author_track_flag_reuses_job(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "ru", False)

    assert second is first
    assert first.use_author_track is True


def test_hundred_disagreeing_polls_create_one_job(manager):
    for i in range(100):
        manager.get_or_create("abcdefghijk", "ru", i % 2 == 0)

    assert manager.stats()["jobs"] == 1
    assert manager.get("abcdefghijk").generation == 0


def test_snapshot_reports_the_settings_the_track_was_made_with(manager):
    job = manager.get_or_create("abcdefghijk", "sr", False)
    snapshot = job.snapshot()

    assert snapshot["authorTrack"] is False
    assert snapshot["targetLang"] == "sr"
    assert snapshot["generation"] == 0


def test_second_client_with_other_target_lang_reuses_job_and_can_see_it(manager):
    first = manager.get_or_create("abcdefghijk", "ru", True)
    second = manager.get_or_create("abcdefghijk", "sr", True)

    assert second is first
    assert second.snapshot()["targetLang"] == "ru"


async def test_cursor_ahead_of_the_journal_resyncs_once(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    ahead = await job.poll(cursor=9999, timeout=0.01)

    assert "snapshot" in ahead
    assert ahead["cursor"] == 0

    again = await job.poll(cursor=ahead["cursor"], timeout=0.01)

    assert "snapshot" not in again
```

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_job_identity.py -v`
Expected: FAIL — the first test on `second is first`, the rest on the missing snapshot keys

- [ ] **Step 3: Remove the recreation branch and extend the snapshot**

In `get_or_create` delete the branch `if job is not None and job.use_author_track != use_author_track` entirely, together with `drop`/`replaced`. Keep the repeatable-failure branch (`Stage.ERROR` + `RETRY_COOLDOWN`) unchanged — it is about something else.

Add `self.generation = 0` in `Job.__init__` and increment it in `get_or_create` when a job is recreated after a failure. In `Job.snapshot()` add three keys.

Replace the comment at the removed branch with an explanation of the rule: the first subscriber decides, a settings divergence is visible to the client via `authorTrack`/`targetLang` in the snapshot, and a rerun with a different setting is an explicit `/drop` and a new subscription, not a silent recreation under someone else's poll.

- [ ] **Step 4: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_job_identity.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add helper/jobs.py tests/test_job_identity.py
git commit -m "Решает первый подписавшийся: расхождение настроек больше не пересоздаёт задание"
```

---

### Task 3: Both creating paths pass the real flag

**Files:**
- Modify: `helper/app.py:140` (`create_job`), `helper/app.py:290` (the `watch` operation in `websocket`)
- Test: `tests/test_create_paths.py`

**Interfaces:**
- Consumes: fixture `client` from task 1; the “first decides” rule from task 2.
- Produces: `POST /v1/jobs` accepts `authorTrack: bool` (default `true`); the `watch` operation accepts `authorTrack` in the same sense.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_create_paths.py
async def test_post_jobs_honours_author_track_false(client, manager):
    await client.post("/v1/jobs", json={"videoId": "abcdefghijk", "authorTrack": False})

    assert manager.get("abcdefghijk").use_author_track is False


async def test_post_jobs_defaults_author_track_to_true(client, manager):
    await client.post("/v1/jobs", json={"videoId": "abcdefghijk"})

    assert manager.get("abcdefghijk").use_author_track is True
```

- [ ] **Step 2: Run and make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_create_paths.py -v`
Expected: FAIL — the first test: `use_author_track` is `True`, because the value is hardcoded by default

- [ ] **Step 3: Pass the flag through**

In `create_job` read `(payload or {}).get("authorTrack", True)` and pass it as the third argument. In the `watch` operation read `message.get("authorTrack", True)` and pass it the same way. Do not change the default anywhere: `true` preserves today's behaviour.

- [ ] **Step 4: Run and make sure it passes**

Run: `.venv/bin/python -m pytest tests/test_create_paths.py -v`
Expected: PASS, 2 passed

- [ ] **Step 5: Commit**

```bash
git add helper/app.py tests/test_create_paths.py
git commit -m "POST /v1/jobs и watch передают признак дорожки автора, а не умолчание"
```

---

### Task 4: Playhead per client, with a round-robin queue

**Files:**
- Modify: `helper/jobs.py:193` (`Job.__init__`), `helper/jobs.py:1151` (`_next_batch`), `helper/app.py:221-230` (`set_playhead`), `helper/app.py:296-302` (the `playhead` operation)
- Test: `tests/test_playhead.py`

**Interfaces:**
- Consumes: fixture `manager`.
- Produces: `Job.report_playhead(client_id: str | None, t: float) -> None`; `Job.aim() -> float` — the position around which the next batch is taken, round-robin between distinct playheads; `Job.forget_client(client_id: str | None) -> None`. The `Job.playhead` attribute is kept as a read-only property and equals `aim()`.
- `POST /v1/jobs/{id}/playhead` and the `playhead` operation accept an optional `clientId: str`. Absence of the key means the single client `"legacy"` — the extension's behaviour does not change.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_playhead.py
import time

from helper.jobs import PLAYHEAD_TTL


def test_two_clients_take_turns(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("a", 120.0)
    job.report_playhead("b", 1200.0)

    aims = [job.aim() for _ in range(4)]

    assert aims == [120.0, 1200.0, 120.0, 1200.0]


def test_one_client_always_gets_its_own_position(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("a", 42.0)

    assert [job.aim() for _ in range(3)] == [42.0, 42.0, 42.0]


def test_client_without_id_keeps_todays_behaviour(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead(None, 7.0)

    assert job.aim() == 7.0
    assert job.playhead == 7.0


def test_silent_client_stops_steering_after_the_ttl(manager, monkeypatch):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("gone", 1200.0)
    job.report_playhead("here", 60.0)

    later = time.monotonic() + PLAYHEAD_TTL + 1
    monkeypatch.setattr(time, "monotonic", lambda: later)
    job.report_playhead("here", 61.0)

    assert [job.aim() for _ in range(2)] == [61.0, 61.0]


def test_absurd_client_id_is_refused_not_stored(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("x" * 500, 5.0)
    job.report_playhead("", 6.0)

    assert job.aim() == 6.0
```

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_playhead.py -v`
Expected: FAIL — `AttributeError: 'Job' object has no attribute 'report_playhead'`

- [ ] **Step 3: Implement**

In `Job.__init__` replace `self.playhead = 0.0` with `self._playheads: dict[str, tuple[float, float]]` (position and monotonic time) and `self._turn = 0`. The constant `PLAYHEAD_TTL = 120.0` next to `PAUSED_AUDIO_TTL`.

`report_playhead` normalises the key: empty, not a string or longer than 64 characters — counts as `"legacy"`; stores `(t, time.monotonic())`. `aim()` drops records older than `PLAYHEAD_TTL`, takes distinct positions in key order and returns the next one round-robin via `self._turn`, incrementing the counter; an empty set gives `0.0`. `playhead` — a property returning `aim()`.

The round-robin rule was chosen instead of “the lagging one”: with two viewers at the second and twentieth minute, the minimum would make the second wait for the whole video, while the translation overtakes the viewer roughly twentyfold anyway, so half the speed remains a tenfold margin.

In `_next_batch` replace `job.playhead` with `job.aim()` — one call per batch, otherwise the round counter would be turned an extra time.

In `set_playhead` and in the `playhead` operation read `clientId` and call `report_playhead`.

- [ ] **Step 4: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_playhead.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add helper/jobs.py helper/app.py tests/test_playhead.py
git commit -m "Плейхед на клиента: окно перевода идёт по кругу, а не достаётся последнему писавшему"
```

---

### Task 5: `/drop` cancels only its own

**Files:**
- Modify: `helper/app.py:233-237` (`drop_job`), `helper/jobs.py:1464-1468` (`JobManager.drop`)
- Test: `tests/test_drop.py`

**Interfaces:**
- Consumes: `Job.forget_client` from task 4.
- Produces: `JobManager.drop(video_id: str, client_id: str | None = None) -> bool` — returns whether the job was cancelled. `POST /v1/jobs/{id}/drop` accepts an optional `clientId` and responds `{"dropped": <video_id>, "cancelled": bool}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_drop.py
def test_drop_by_one_of_two_clients_keeps_the_job(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("tablet", 10.0)
    job.report_playhead("desktop", 20.0)

    assert manager.drop("abcdefghijk", "desktop") is False
    assert manager.get("abcdefghijk") is job


def test_drop_by_the_last_client_cancels(manager):
    job = manager.get_or_create("abcdefghijk", "ru", True)
    job.report_playhead("tablet", 10.0)

    assert manager.drop("abcdefghijk", "tablet") is True
    assert manager.get("abcdefghijk") is None


def test_drop_without_client_id_cancels_as_today(manager):
    manager.get_or_create("abcdefghijk", "ru", True)

    assert manager.drop("abcdefghijk") is True
    assert manager.get("abcdefghijk") is None


def test_drop_of_an_unknown_video_is_quiet(manager):
    assert manager.drop("zzzzzzzzzzz", "tablet") is False


async def test_drop_endpoint_reports_whether_it_cancelled(client, manager):
    manager.get_or_create("abcdefghijk", "ru", True)
    response = await client.post("/v1/jobs/abcdefghijk/drop", json={})

    assert response.json() == {"dropped": "abcdefghijk", "cancelled": True}
```

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: FAIL — `drop() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: Implement**

`JobManager.drop` with `client_id`: when the key is passed, remove its registration via `job.forget_client`; cancel and remove the job from the registry only when no live registrations remain. Without a key — cancel, as today.

This deliberately leaves the extension the ability to cancel the tablet's job if the extension subscribed without a key: an additive change cannot take away from an old client what it already can do. Record this in a comment so the rule does not look like an oversight.

`drop_job` reads `clientId` from the body and returns `cancelled`.

- [ ] **Step 4: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_drop.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add helper/app.py helper/jobs.py tests/test_drop.py
git commit -m "Отмена задания снимает регистрацию клиента, а не чужую работу"
```

---

### Task 6: Limits on unfinished jobs and on deferred audio

**Files:**
- Modify: `helper/jobs.py:73` (constants), `helper/jobs.py:1432-1460` (`_evict`), `helper/jobs.py:1245` (`get_or_create`)
- Test: `tests/test_limits.py`

**Interfaces:**
- Consumes: fixture `manager`.
- Produces: `MAX_ACTIVE_JOBS = 4`, `PAUSED_AUDIO_MAX_BYTES = 4 * 1024**3`. `get_or_create` raises `PermanentJobError` on overflow; `create_job` and the `watch` operation turn it into a refusal with a human phrase.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_limits.py
import pytest
from helper.jobs import MAX_ACTIVE_JOBS, PermanentJobError


def test_active_jobs_are_capped(manager):
    for i in range(MAX_ACTIVE_JOBS):
        manager.get_or_create(f"video{i:06d}a", "ru", True)

    with pytest.raises(PermanentJobError):
        manager.get_or_create("overflowxyz", "ru", True)


def test_the_cap_counts_only_unfinished_jobs(manager):
    from helper.jobs import Stage

    for i in range(MAX_ACTIVE_JOBS):
        job = manager.get_or_create(f"video{i:06d}a", "ru", True)
        job.stage = Stage.DONE

    assert manager.get_or_create("overflowxyz", "ru", True) is not None


def test_paused_audio_over_budget_is_discarded_oldest_first(manager):
    import os

    from helper.jobs import PAUSED_AUDIO_DIR, PAUSED_AUDIO_MAX_BYTES

    PAUSED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    half = PAUSED_AUDIO_MAX_BYTES // 2 + 1
    made = []

    for i, name in enumerate(("old.f32", "middle.f32", "fresh.f32")):
        path = PAUSED_AUDIO_DIR / name
        with path.open("wb") as handle:        # sparse file: takes no space
            handle.truncate(half)
        os.utime(path, (1000 + i, 1000 + i))
        made.append(path)

    manager._evict()

    assert not made[0].exists()
    assert made[2].exists()
    assert sum(f.stat().st_size for f in PAUSED_AUDIO_DIR.iterdir()) <= PAUSED_AUDIO_MAX_BYTES
```

- [ ] **Step 2: Run and make sure they fail**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: FAIL — `DID NOT RAISE PermanentJobError`

- [ ] **Step 3: Implement**

`MAX_ACTIVE_JOBS = 4` next to `MAX_RETAINED_JOBS`: four is as many simultaneous jobs as make sense with one slot on the GPU, the rest would queue anyway. The check in `get_or_create` before creation, by the number of jobs with `finished is False`; refusal — `PermanentJobError` with a phrase about how many videos are already in progress.

`PAUSED_AUDIO_MAX_BYTES = 4 * 1024**3`: in `_evict`, after the current time-based cleanup, sum the sizes of the files in `PAUSED_AUDIO_DIR` and delete the oldest until the sum is below the limit. A three-hour video gives about 659 MiB of float32, so four gigabytes is roughly six paused jobs.

In `create_job` and in the `watch` operation wrap the call and return `HTTPException(status_code=429, detail=...)` and, respectively, a refusal in events.

- [ ] **Step 4: Run and make sure they pass**

Run: `.venv/bin/python -m pytest tests/test_limits.py -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add helper/jobs.py helper/app.py tests/test_limits.py
git commit -m "Предел на незавершённые задания и бюджет на отложенный звук"
```

---

### Task 7: The daemon lives without a graphical session

**Files:**
- Create: `docs/running.md`
- Modify: `packaging/yt-trans.service`
- Test: verification by command, no automated test — this is a system state, not code.

**Interfaces:**
- Consumes: nothing.
- Produces: a documented startup precondition.

- [ ] **Step 1: Check the current state**

Run: `loginctl show-user "$USER" | grep Linger`
Expected: `Linger=no` — the unit exists only inside the graphical session, that is, when the owner logs out the daemon disappears

- [ ] **Step 2: Enable it**

Run: `loginctl enable-linger "$USER"`

- [ ] **Step 3: Make sure**

Run: `loginctl show-user "$USER" | grep Linger`
Expected: `Linger=yes`

- [ ] **Step 4: Write it into `docs/running.md`**

Why this is needed: the tablet accesses the daemon from another room, and the computer may be sitting at the login screen at that time. Without `linger` the user unit will not start, and the refusal will look like “no connection to the daemon” — the same thing a loopback break gives, so it is easy to confuse. There also check that `packaging/yt-trans.service` has no dependency on the graphical session (`WantedBy=default.target`).

- [ ] **Step 5: Commit**

```bash
git add docs/running.md packaging/yt-trans.service
git commit -m "Демон поднимается без графического сеанса: linger и заметка о запуске"
```
