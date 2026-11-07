# Subtitles on the tablet: the page the daemon serves

Design. 2026-10-30.

## Why

Right now subtitles for Serbian videos live only where the extension is installed
and a browser is open on the computer. So one can watch only while sitting at the desk.
The owner wants to lie in another room with a tablet — and this is the only reason for
the undertaking. Everything else in the document serves it.

The computer remains the engine: the GPU recognizes and translates. The tablet is a thin
client that receives cues and shows them over the picture.

## Scene

A person lies in another room with a tablet in his hands. Opens the page the
home computer serves. Sees his channels and playlists, chooses a video,
watches with Russian subtitles. While a fresh video is being prepared, he goes to another
tab and watches something there without translation. Comes back — it's ready.

Sometimes he needs to come back to the computer to work. Then his own work
occupies the GPU, and the tablet will have to wait — honestly, with an explanation on the screen,
not silently.

There are several devices in the house: a tablet, a phone, a laptop, someone else's iPad. All go to
one address, with one key, with one translation language. There is no separation of people into
profiles and it is not needed yet — the word “yet” was said by the owner, and the road to profiles must not
be closed.

## Goals

1. Video plays on the tablet, subtitles over the picture, including in full screen.
2. Quality 1080p and above.
3. Entry — channel playlists, not a field for pasting a link (pasting remains
   a fallback).
4. Several devices in the house work simultaneously and do not break each other.
5. A job does not fail when the viewer leaves the page, closes the tab or falls asleep.
6. When the GPU is busy, the client says so in words and shows the place in the queue.
7. The existing extension keeps working without changes.

## Non-goals

- Replacing YouTube: no feed, no search, no comments, no subscriptions.
- Working outside the home network.
- Per-person profiles: language and settings are shared.
- Publishing to an app store. There is no app of its own at all.
- Playing in the background with the screen off.

## Anti-goals

Signs that it turned out badly, even if it formally works:

- Subtitles drift from speech enough that it is visible. The benchmark is set: on the computer
  the extension lands within a cue with frame accuracy, and the tablet must not be
  noticeably worse.
- An empty screen without explanation while something is being computed or waiting for the GPU.
- The viewer discovers that leaving the page cancelled the work.
- The tablet interferes with work at the computer more than the owner agreed to tolerate.
- ~~A second place where subtitle rendering must be kept in sync by hand.~~
  **Withdrawn by the owner's decision on 2026-10-30:** the daemon is a service, and the extension and
  the web app are two independent consumers of it. There is no shared rendering code between
  them and none is planned, a difference in appearance is acceptable. See “Owner's
  answers”.

## Owner's decisions

Made by him, not up for discussion:

- Watching on the tablet, lying down, in another room. The video plays on the tablet.
- A browser is enough. No app of its own is needed.
- Multi-user means many devices, not many accounts.
  One key for all, one language for all, one YouTube account — for now.
- Entry through playlists on channels. Copying a link by hand is acceptable as a first
  approximation; registration in the system “share” is desirable, not mandatory.
- 1080p and above.
- We decided not to do a device trial, and to write the design right away.

## Decisions made by me

These are my decisions, not ones agreed with the owner. The builder has the right to contest them
if he finds grounds, but must understand the reason.

### There is no embedded YouTube player in the scheme

The first version of the design put the official player in a frame (IFrame Player API)
inside the page and drew subtitles over it. This is cancelled for three
reasons, each sufficient on its own:

1. YouTube's terms for the embedded player directly forbid drawing over it:
   “You must not display overlays, frames, or other visual elements in front of
   any part of a YouTube embedded player” (Required Minimum Functionality).
   For a single private installation nobody would enforce it, but the prohibition is written precisely
   about what we were going to do, and closes off any future distribution.
2. Our layer disappears when the player goes into its own full screen:
   the element in full-screen mode is the frame, and our layer is no relative of it.
   It is worked around by our own full screen on our container, but that is already a fight.
3. Videos with embedding disabled and age restriction do not play at all
   (errors 101/150), and ads inside the frame run on the same clock, and during
   them `getCurrentTime()` is not the content time.

Plus, less important: the embedded player's clock is read by polling through the bridge, not
frame by frame.

### The daemon serves the picture, the player is an ordinary `<video>`

The daemon already downloads audio for recognition. Let it download the picture too, mux and
serve it. The page plays an ordinary `<video src="…from the daemon…">`.

What this gives:

- The clock is the decoder's real clock: `video.currentTime` plus
  `requestVideoFrameCallback`. This is exactly the mechanism that already works in
  [extension/content.js](../../../extension/content.js) and gives frame accuracy.
  No bridge, no polling, no interpolation.
- The rendering moves out of `content.js` almost verbatim, together with the shadow tree,
  the two lines, the study line and the special look of untranslated cues.
  This directly answers the anti-goal about a second place of maintenance.
- The embedding terms do not apply: we are not embedding the YouTube player.
- No ads. Age-restricted videos play — the daemon already knows how to substitute an account
  with confirmed age (this work is not committed in the repository, but exists).
- Full screen is ours: a container with the video and a layer inside, one `requestFullscreen`.
- Works in any browser, including iOS, without installing anything.

The price: the daemon becomes a distributor of bytes, and a disk for the video cache appears.

### Picture transport: download, mux, serve as a file

Measured on a real video (yt-dlp 2026.08.19, `KCR-dkZIhxY`, 1568 s):
48 formats, of which 28 over plain https and 16 m3u8 playlists. **There is not a single
muxed format** — neither 360p nor any other. 1080p exists
only as separate tracks: 137 (H.264, 1910 kbit/s), 248 (VP9, 1363),
399 (AV1, 896). Audio separately: 140 (AAC, 129), 251 (opus, 142).

That is, the 1080p requirement automatically means that picture and audio must be
joined at our end. The simplest of the working ways is chosen:

1. yt-dlp downloads the chosen pair of tracks.
2. ffmpeg muxes without re-encoding (`-c copy`, `-movflags +faststart`).
   ffmpeg 8.1.2 is present in the system.
3. The finished file lies in the cache and is served with `Range` support.

Why this and not a stream through a proxy with a DASH manifest:

- Seeking works by itself, without code.
- The browser does not need a third-party JS player (`<video>` cannot do DASH).
- No re-encoding: the GPU and CPU are not spent.
- No fiddling with re-signing googlevideo links on a 403 in the middle of viewing.
  Track links contain the address of the node that issued them inside the signed part;
  they cannot be given to the tablet directly, and this too is removed by the fact that the bytes
  go through the daemon.
- The file is reused: a second viewing and a second device are instant.

What we pay: viewing starts after muxing, not immediately. This is tolerable precisely
because there is preloading (below), and because the owner is waiting in
another tab anyway. If the wait turns out noticeable in practice, the upgrade path is
described in “left to the builder”.

The default profile is **H.264 1080p + AAC in mp4**: plays everywhere, including iOS and
old devices. Above 1080p YouTube gives only VP9 and AV1; this is a daemon setting,
not automation, because it narrows the list of devices that will be able to
open it. Disk: 1080p H.264 with audio is about 2 Mbit/s, that is roughly
400 MB for 26 minutes. The cache needs a ceiling and eviction by age.

### The job lives without a viewer

Verified in the code: a job in [helper/jobs.py](../../../helper/jobs.py) is a separate
task that does not look at subscribers. `_subscribers` only receive
events, progress does not depend on them. Exactly one path cancels a job — an explicit
`/drop`. `_evict` throws out only already finished jobs above the limit.

So the rule for the page: leaving the page, the tab falling asleep and a connection drop
break nothing. `/drop` goes only on an explicit press of “cancel”. This is already
true in the daemon and must remain true.

A rough edge that must be shown in words: for a job that has had the GPU taken away, the deferred audio lives for `PAUSED_AUDIO_TTL` = 30 minutes, then it is thrown out.
Returning to such a video an hour later will begin with a repeated download.

**How it is built** (branch `worktree-daemon-second-client`, 2026-11-01). Audio may
run out earlier than the deadline: when all deferred audio is more than 4 GiB
(`PAUSED_AUDIO_MAX_BYTES`), the oldest by recording time is removed. And a pause that
nobody has touched for 30 minutes stops occupying a place under the limit
of unfinished jobs (see “What must be fixed before the start”); it itself remains in the
registry with its cues and will resume when someone returns to it.

### Preloading the next in the list

While the viewer watches a video, the GPU and the network are free. The daemon prepares the next video
of the playlist: downloads, muxes, recognizes, translates. Then the transition to it is
instant, and the main drawback of file transport disappears.

The preload depth is a setting (default one video ahead). Preloading
must yield: a live viewer's request is always more important than preparing the future, and when
there is not enough disk space or the GPU is busy, preloading simply does not start.

### Queue and GPU

The GPU is an RTX 4090, 24564 MiB, of which about 4.4 GiB is occupied during normal
desktop work (measured). The previous estimate “18.5 GiB” in the notes is the remainder with the
desktop loaded, not the card's size. That is, there is no hard ceiling per job, but there is
competition with the owner's own work at the desk.

Hence the requirement: the client shows the GPU state in words. Three distinguishable
cases, each with its own text:

- the GPU was taken by someone else in the house — show which video and one's place in the queue;
- the GPU was taken by the owner's work at the computer — say exactly that, because
  the decision here is human, not technical;
- the GPU is free, work is going on — show the stage and the share translated.

The third case already exists in the extension. The first two are new, and it is precisely their absence that
gives the current hang in `translating` with an endless “the card has not been freed”.

### Network, access, keys

- The port stays on loopback by default. Binding to the network is only by an explicit
  setting (`[server] host`), plus a list of allowed addresses. This is important:
  on the machine firewalld already lets through `1025-65535/tcp+udp`, that is, binding to loopback
  is currently the whole protection, and `0.0.0.0` would open the port to a dozen docker bridges as well.
- The install key remains, but the key store becomes a list with labels,
  re-read by the file's modification time. Currently changing the key requires
  restarting the daemon. One key for all is the owner's choice; a list with labels
  is needed so that “for now” one day ends without a rework.
- The `Origin` check currently lets a request without the header through and rejects everything except
  extension schemes. The page served by the daemon itself will come with its own
  `Origin`, and it must be allowed — otherwise `POST /v1/jobs`, `playhead`, `drop` and
  the WebSocket handshake will be rejected with code 403. For a `GET` of the same
  origin this is invisible, which makes the error especially hurtful when debugging.
- `/v1/health` is served without a key and tells the list of models, GPU memory,
  foreign models, cache size, the list of jobs and the path to the key file — that is, the user
  name. On loopback this does not matter, on the network it must be trimmed to “alive/not alive”.
  Separately: it fails with code 500 when ollama does not answer, that is, the obvious
  liveness check lies exactly when it is needed.
- The Wi-Fi network password is the whole trust boundary. This is a conscious choice for a private
  tool in one's own apartment. From this, however, follows a mandatory limit
  on unfinished jobs: currently `_evict` throws out only finished ones,
  ~~and the deferred audio `PAUSED_AUDIO_DIR` is not limited by anything~~.
  **Done** (branch `worktree-daemon-second-client`, 2026-11-01): no more than 16 unfinished
  jobs, no more than 4 GiB of deferred audio. Details and what is deferred are in “What must be fixed before the start”.
- ~~`loginctl enable-linger` is mandatory.~~ Currently `Linger=no` (verified), that is,
  the daemon unit exists only inside the graphical session: a computer on the login
  screen — no daemon, and this is exactly the situation for which everything is being done.
  **Not applied** (branch `worktree-daemon-second-client`, 2026-11-01): startup without a
  session is still needed, but lingering is not one command and not the only path.
  It brings up the whole user manager without a session, and with it sound and the eye tracker;
  what to fix before it is in item 2 of “What must be fixed before the start” and in
  [docs/running.md](../../running.md).

A secure context (https) is not needed in the first version: an ordinary `<video>`,
`fetch` and `requestVideoFrameCallback` work over open http. It is needed for exactly
two things — registration in the system “share” (Web Share Target requires
an installed PWA) and keeping the screen from sleeping via Wake Lock. The first the
owner called desirable, not mandatory, so it is not in the first version.
The second must be checked on the device: a playing video usually does not let the screen
go dark by itself, and then Wake Lock is not needed.

## Daemon surface

What is added. The names are conditional, the builder has the right to change them.

**Serving the page.** Static files of the page from the root. This also removes the question
of origin: the page and the API are one origin.

**Video.**
- `GET /v1/video/{videoId}` — the finished muxed file, with `Range` support.
  While the file is absent, it answers with a preparation state, not an error.
- The video preparation state is a separate stage in job events; new
  `Stage` values must not be introduced (see “compatibility”).

**Playlists.**
- `GET /v1/playlists` — the list of the owner's saved channels and playlists.
- `GET /v1/playlists/{id}` — the expanded list of videos. Expanded by yt-dlp,
  no Google Data API key is needed. Returns for each video a sign “already ready /
  being prepared / nothing there”, so that the list immediately shows what can be watched
  instantly.
- Saving and deleting a playlist.

**Subtitle track as a file.**
- `GET /v1/jobs/{videoId}/track?format=srt|vtt|json` — only for a finished
  job, starts nothing. `pysubs2` is already in `requirements.txt` and is not
  used. Needed for fallback paths (watch in a third-party player,
  take it with you) and for debugging. Untranslated cues must be marked
  or thrown out, otherwise the file silently passes Serbian off as Russian.

**The job snapshot** is extended with fields: `duration`, `source` (`asr` or
`manual:<language>`), `targetLang`, `studyLang`, `authorTrack`, `generation`.
**Already there** (branch `worktree-daemon-second-client`, 2026-11-01): `authorTrack`,
`targetLang` and `generation`; the other three are not there yet.

**How it is built** (branch `source-language`, 2026-11-04): the source of cues and the language lie
in a `language` object (`source` — `asr` or `manual`, `track` — the label of the author's track),
and not in a `source` field at the top level of the snapshot: the extension would show it as the label
“downloading asr…” (`extension/content.js`, branch `message.source`). The fields are in the
“Decisions” section below.

`studyLang` is load-bearing: when the study mode is off, the `study` field of a cue
contains the source text of recognition, and nothing on the wire says so —
the client would confidently label Serbian as Montenegrin. ~~`generation` is needed so that the
client understands whether the incoming snapshot belongs to the same job: the cue numbering of the author's
track and of recognition do not match.~~

**How it is built** (there). Video variants are distinguished by `authorTrack`: each
variant has its own job, see “What must be fixed before the start”. `generation` counts
recreations under one key: the first job has 0, one recreated after
a cooled-down repeatable failure has one more (it may get a different language
or take the author's track where the previous one fell back to recognition, and their
cue numbering is different). After `/drop` the registry forgets the job, and the count
is again from 0, so through a reset the number distinguishes nothing. Cursors are distinguished by
the random journal base: a cursor from a foreign journal does not fall into the range of this one and
gets a snapshot. Hence the rule for the client: on every snapshot the cues are
**replaced** entirely, not merged with the shown ones — a snapshot comes when the
client connected, switched to another job or fell behind. The extension does not do
that (`mergeCues` in `extension/content.js` merges by number and does not clean); it is
frozen, and the rule is for new clients.

**Errors** get a machine code next to `retryable`. Currently everything except this
sign is free Russian text, and the client cannot decide what to show.

**GPU state** in the snapshot: busy/free and by whom — one's own work or another
job, plus the place in the queue.

### Compatibility

The extension must notice nothing. The main rule: **do not introduce new `Stage` values.**
The extension checks against a known list of stages, and an unfamiliar
stage will leave it with an outdated label and a poll that will never
stop. New information — only in new fields.

**What the extension does notice nevertheless** after the branch `worktree-daemon-second-client`
(2026-11-01). There are no new stages, but the behaviour shifted in three places:

- The first poll response is a snapshot right away, without waiting: cursor 0 lies below
  the random journal base. This is an extra request round, and the label “in queue”
  appears a few milliseconds earlier. The snapshot has no download source and attempt number, which
  the `state` events carry, so a tab that connected in the middle of a
  download shows a general label until the next such event.
- Switching the author's track in the middle of a running job leaves the previous variant
  working: the chosen one waits for the GPU until the previous one finishes (usually a minute or two), and
  the label speaks of waiting for the GPU.
- At the limit of unfinished jobs, the badge shows the refusal phrase with a retry
  button, and the poll stops.

## What must be fixed before the start

Not improvements but preconditions. Both found in live code.

*State as of 2026-11-01, branch `worktree-daemon-second-client`: the first is done,
the second is not applied; of the items of the last paragraph both limits are done. Details
are in the “How it is built” and “Not applied” notes below.*

**1. The author's track recreates a live job.**
[helper/jobs.py](../../../helper/jobs.py), `get_or_create`: if the `use_author_track` sign
of the caller does not match the job, the job is reset
through `drop` and created anew. The comment nearby justifies this by the fact that
“the value comes from settings and does not change between polls, so a mismatch
happens exactly once per switch”. This is true for one client and
false for two: the sign begins to differ on every poll. Reproduced:
113 recreations in 90 seconds.

Aggravated by two circumstances. `drop` calls `cancel`, but the downloader lives in
`asyncio.to_thread`, which cancellation does not stop — orphaned yt-dlp
processes remain, accessing googlevideo from the owner's address. This is a
plausible source of the 403s recorded in the notes. And separately:
[helper/app.py](../../../helper/app.py) in `POST /v1/jobs` and in the WebSocket `watch` operation hardcodes `authorTrack` by default, whereas the poll passes
the real value — that is, a client built on either of these two paths
will argue with the extension regardless of its intentions.

**The cure — as built** (branch `worktree-daemon-second-client`, 2026-11-01).
The job is keyed by the pair (video, `use_author_track`): each variant has its own
job, and nobody recreates someone else's. The design advised the cheaper rule
“the first client decides”, but it broke the switch in the extension: the extension
expects that the poll carries the setting on every round and a changed sign yields
another job, and under “first decides” the switch did nothing until
the job was evicted, reset or the daemon restarted. The translation language is not in the key and
remains with the first caller: it is one for all, and the client
sees a mismatch in the snapshot's `targetLang`.

What we pay. Two clients with different signs — two pipelines per video (their
number is limited by the limit on unfinished jobs, see below). Switching in the
extension in the middle of a running job leaves the previous variant working: it holds the
GPU until it finishes, and the chosen one waits. Cancelling the previous one is not allowed: it may be running
for another client, and cancelling the neighbouring variant when creating one's own is what produced the storm.
The previous one's track does not disappear, but goes into the cache. The mitigation is for the future
plan: when creating a variant, put the running neighbouring one into pause (`PAUSED`), from where
it will continue on the next access.

Where the video is named without a track sign: the snapshot `GET /v1/jobs/{id}` takes the most
recently touched variant; the viewing position and `unwatch` act on all variants
of the video; a reset without `clientId` cancels all variants.

The instant path with an outdated cursor is kept: this is the
resynchronization path, without it a client with a foreign cursor would silently wait until the journal
grows to its position and would lose everything up to it. The storm was born by recreation on
every poll, and the instant response only accelerated it. Every journal now
starts from a random 48-bit base, not from zero; 48 bits — so that the cursor
remains an exact integer in JavaScript, below 2^53: the extension carries it as a JSON number.
A cursor from a foreign journal, from another variant or from a reset job, does not fall into the range of
this one and gets one snapshot, and since the poll no longer recreates the job,
there is nowhere for a storm to come from.

Of the two circumstances above, `POST /v1/jobs` and `watch` **are fixed**: they pass
the supplied sign, not the default, and require a real boolean, otherwise a non-boolean
would get into the key as a phantom third variant. No sign — `true`; anything that is not a
boolean — refusal: 400 in `POST`, a refusal event without a subscription in `watch`. **Still open**
is the orphaned yt-dlp: `cancel` still does not stop a download
already running in `asyncio.to_thread`. The storm that multiplied this is gone, but a job
cancelled in the middle of a download leaves the process to finish.

**2. `loginctl enable-linger`.** ~~One command, without it the whole undertaking does not work
when the owner logs out of the session.~~

**Not applied** (branch `worktree-daemon-second-client`, 2026-11-01; `Linger=no`).
Without starting outside the session the undertaking still does not work when the owner logs out of the session
and after booting without login, but one command does not solve it. Lingering brings up
the whole user manager without a session, not one daemon: on boot, together with it,
sound starts (`keep-soundbar-alive`, `guitar-monitor-link`, and through its `Wants=` —
`pipewire` with `wireplumber`) and the eye tracker (`tobii-gaze-keys`, and through its
`Requires=` — `tobiid`). They cannot be tied to the session by a condition: a unit skipped by
condition still pulls in its `Wants=` and `Requires=` (that is how they tried it
on 2026-11-01 and rolled it back). The precondition is editing the units themselves where they are
deployed from (`system-config`, and for `tobii-gaze-keys` also `tobii`): in `[Install]`
`WantedBy=graphical-session.target` instead of `default.target`, then
`systemctl --user reenable`, so that the previous link from
`default.target.wants` does not remain. Then one log out and log in — sound and gaze keys must
work — and only after that `enable-linger`. In detail, with a fallback option
(a system unit with `User=`) — in [docs/running.md](../../running.md).

It would be good in the same pass, but not mandatory before the start: the limit on unfinished
jobs, the limit on `PAUSED_AUDIO_DIR`, rate limiting of job creation,
`proxy_headers=False` in uvicorn (by default it trusts `X-Forwarded-For`
from loopback and substitutes the caller's address before our check reaches it).

**How it is built** (branch `worktree-daemon-second-client`, 2026-11-01): both limits
are done, rate limiting and `proxy_headers` are not.

- **No more than 16 unfinished jobs** (`MAX_ACTIVE_JOBS`). This is protection from
  abuse, not queue depth: the extension queues as many
  videos as there are open tabs, and the fifth tab of an ordinary evening should not get a refusal,
  and a waiting job is cheap — its task stands on the pass to the GPU.
  An abandoned pause is not counted: stage `PAUSED`, no live task, and nobody has touched the job
  for 30 minutes (`PAUSED_AUDIO_TTL`). A pause continues only on
  a client's request, and without this exception sixteen abandoned ones would close the daemon
  to new videos until a restart. An abandoned one remains in the registry with cues, and
  a returning viewer will resume it. The limit guards only creation: a job that
  already exists is served even at the limit, and a failed one whose refusal has cooled — as it is, without
  a retry.
- **The form of refusal** on the three paths that create a job: `POST /v1/jobs` — 429 with the phrase
  in `detail`; `watch` — the event `{"type": "state", "stage": "error", "videoId": …,
  "message": …, "retryable": true}` without a subscription; `/events?create=true` — 200 with a
  snapshot in stage `error` and the phrase in its `error` field, and the job is not created.
  Why the poll does not get 429: on any non-200 response the extension writes “the daemon answered
  <code>”, as about a broken daemon, and repeats the request after a pause, whereas a snapshot with
  stage `error` shows it as a phrase and stops the poll; the viewer retries
  with the button. This is the only path by which the extension creates jobs.
- **No more than 4 GiB of deferred audio** in total (`PAUSED_AUDIO_MAX_BYTES`, about
  six three-hour videos in pause). Above the ceiling the oldest by recording
  time file is removed, and if it is listed for a job — through the job itself, so that
  its handle does not point to a missing file. A job without audio on resumption
  will download it again.
- **Rate limiting and `proxy_headers=False` are deferred** to the plan for exposing the daemon to the network:
  both rely on the caller's address, which does not exist on loopback.
- **Known limitation.** At the limit the paths that create a job refuse also
  a video whose track already lies in the cache: there is no cheap cache check at the moment of creation —
  the key needs the video duration and the track fingerprint, and they are known only
  after a metadata request. Picking up a ready one through `/restore` is not affected by the limit.
  The preloading plan must let ready videos past the limit and not spend the viewer's
  place on preloading.

## Measurements the design relies on

Made on this machine, 2026-10-29/30.

| What | Value | Source |
|---|---|---|
| GPU | RTX 4090, 24564 MiB, 4422 in use | `nvidia-smi` |
| Recognition | 81.7–96.1 times faster than real time | `spikes/asr_real_v3.json`, `asr_real_turbo.json` |
| Translation | 120 cues in 28.4–32.4 s (≈4 cues per second), `qwen3.8:27b` | `spikes/mt_quality.json` |
| Video formats | 48 total, 28 over https, 16 m3u8, 0 muxed | yt-dlp 2026.08.19 on `KCR-dkZIhxY` |
| 1080p | only as separate tracks: 137 H.264 1910 kbit/s, 248 VP9, 399 AV1 | same place |
| Audio | 140 AAC 129 kbit/s, 251 opus 142 | same place |
| Daemon autostart | `Linger=no` | `loginctl show-user` |
| ffmpeg | 8.1.2 | `ffmpeg -version` |

The consequence of the first three rows: translation overtakes the viewer by about twenty
times, so the mode “watch immediately, cues will catch up” is real. There is no need to put things
in a queue and watch later. One and a half to two minutes of waiting on a fresh
video is almost entirely model loading and downloading, not throughput.

## What is left to the builder

- Layout and appearance of the page. The only requirement — recognizability
  relative to the extension: the same two lines, the same study line, the same
  special look of untranslated cues.
- Names and the exact shape of the new endpoints.
- How exactly the on-disk video cache is arranged: ceiling, eviction, neighbourhood with the
  existing cue cache.
- How to show the queue and the GPU state: words, not a diagram.
- The choice between “one page for all devices” and a separate layout for
  the phone.
- A transport upgrade, if the muxing wait turns out noticeable in practice:
  the daemon generates a manifest (HLS or DASH) referencing its own
  proxy endpoints for the two tracks, and the page plays it through Shaka or hls.js.
  This gives an instant start without disk, but adds a third-party JS player,
  segmentation into chunks and re-signing of links on a 403 in the middle of viewing.
  Decide by measurement, not in advance.
- Whether a certificate for a home name is needed. It buys registration in the system
  “share” and Wake Lock; the owner called “share” desirable, not
  mandatory. If taken on — a local certificate authority (mkcert) or
  Tailscale.
- Daemon discovery: the address by hand, a QR code from the host screen, or mDNS.
  The simplest of the working ones.

## Assumptions made

Where the owner did not speak and a decision was required:

- They watch on the home network. Access from outside is not being designed, but binding to the network and
  the list of allowed addresses are made so that Tailscale later does not require
  a rework.
- The device is an Android tablet with a fresh browser. The H.264 profile is chosen so
  that this assumption is optional.
- Playing with the screen off is not needed.
- The quality is chosen by a daemon setting once, not by a switch for each
  video.

## Added after review by builders

The document was read by three independent builders: from the daemon side, from the
page side, and the one who arranges everything into a sequence. Below is what they
found, and the decisions on it.

### Two holes the design missed

**The playhead race between two devices.** This hits goal 4, and it is
more serious than the author's track storm. In `Job._next_batch` the next batch is taken
around `job.playhead`: `nearest = min(pending, key=lambda c: abs(c.start -
job.playhead))`. And `job.playhead` is a single number assigned by the last
writer, and both clients write it once a second. Two viewers at the second and at
the twentieth minute of the same video will drive the translation window between themselves, and neither
will get a coherent piece.

The cure is part of the preconditions, next to the author's track: the playhead becomes
a per-client value, and the job chooses the window by one of them with an explicit rule
~~(the first subscriber, or the one that lags)~~ (the chosen one — below). The queue screen must be able to say “currently translating for another device” — otherwise the
viewer sees silence and considers it a breakdown.

**How it is built** (branch `worktree-daemon-second-client`, 2026-11-01). Each
client has its own position record; the key is an optional `clientId` in `playhead`, over HTTP
and over WebSocket. Without a key or with an invalid one (empty, not a string, longer than 64 characters)
the client is a single shared `"legacy"`, so the extension works as with a single
playhead. The rule is a round, not “first” and not “lagging”: each batch is taken
around the next in turn of the distinct positions of live clients, one move per batch.
The minimum would make a viewer at the twentieth minute wait the whole video, and translation
overtakes the viewer by about twenty times, so even half the speed is
a tenfold margin. Live — the one who reported about itself no longer than 120 seconds ago
(`PLAYHEAD_TTL`, with margin for a background tab that the browser throttles to once a
minute). All fell silent — the window stands at the last known position among their
records, and does not jump to the start of the video.

`/drop` with `clientId` removes only that client and cancels the job only when
no live records remain; without `clientId` it cancels everything, as before (that is how the
extension sends). “Registered” means “has a live position record”: a subscription and a
long poll do not count as registration. Therefore an extension tab with a frozen
page whose background poll is still running loses the job from a tablet reset with a key,
and the next poll creates it anew. The position comes without a track sign and
falls on all variants of the video, so in reality registration is per video, not per
variant. Hence the rule for the tablet page: report the position immediately on subscription
(0, if not yet played), otherwise someone else's reset will remove the job before it has declared itself.

**Not done:** the fields for “currently translating for another device” in the daemon do not
exist yet — this is the work of the tablet page plan.

**Untranslated cues are not visible on the wire.** The snapshot returns only
translated ones: `"cues": [c.payload() for c in self.cues if c.translated]`,
and the `segmented` event brings one total number. The page cannot distinguish
“silence here” from “translation has not reached here yet” — and in the mode “watch immediately,
cues will catch up”, which this design calls real, the viewer half the time
looks at empty space without knowing whether to wait. This is a direct
hit on the anti-goal about an empty screen without explanation.

The cure: a readiness boundary by video time appears on the wire (up to which
second the translation has been brought), and the rendering gets a distinguishable look for “there will be text here,
it is not ready yet”. Lay it down at once: the rendering's data model changes, currently it is
a flat sorted array with binary search.

**Not done** (branch `worktree-daemon-second-client`, 2026-11-01): this hole is outside
the goal of the second-client plan, and it did not touch it. The readiness boundary on the wire and the
“not ready yet” look are deferred to the tablet page plan.

### Muxing is a separate resource, not part of a job

Video muxing is keyed by the pair (video, quality profile) and lives in its own registry,
with its own parallelism limit and its own space accounting. The sign “is video needed”
is not part of the job identity, because the cues with and without video are the same.
Variants by `authorTrack` give different cues and therefore do enter the key.
(Corrected 2026-11-01, branch `worktree-daemon-second-client`: here it said that
the client's sign in the job identity is bug number one. The other way round: bug number
one was a sign that was compared but not included in the key.) The extension on
the computer never starts muxing and does not pay 400 MB per video.

### Recognition keeps downloading audio separately

It is tempting to take the audio from the muxed file: one trip to YouTube instead of two.
Rejected — recognition would then not start until muxing ends, and it is the fastest
link (80–96 times faster than real time) and must go first.

But hence follows a condition that is easy to miss and painful to discover:
**the audio track for muxing must be chosen by the same sorting as in
`helper/sources/ytdlp.py` for recognition** (`-S lang,+size`). Otherwise, on a video
with automatic dubbing the viewer hears one speech, and the subtitles are made from
another. This is a silent failure: everything works, the translation is just about the wrong thing.

Separately: currently the audio download goes **under the pass to the GPU**
(`lease.acquire(asr_vram_mib)` in `helper/jobs.py`). Holding minutes of muxing under
the single pass is impossible. The order of phases and the places where eviction is checked
are rewritten in any case — muxing lives outside the pass.

### Decisions made by me on the builders' questions

- **Full screen.** The target device is an Android tablet. Full screen
  is done by us: `requestFullscreen` on the container that holds both the video and the
  layer, plus `playsinline`. The same works on iPadOS. On iPhone the system
  full screen of the video is `webkitEnterFullscreen`, and a layer over the picture there is
  impossible in principle; the iPhone is declared out of scope, with one line in
  the known limitations.
- **Viewing position and the “watched” mark** — per device, in
  `localStorage`. Zero server work. The “continue watching” section on the
  home screen comes from the same.
- **Watching without subtitles while the GPU is busy** — allowed and needed: the bytes already
  lie on disk, it is one button, and it directly answers the anti-goal about an empty
  screen. The non-goal “replacing YouTube” is not violated: neither a feed nor a search
  appears.
- **What triggers preloading.** The page sends context (playlist and
  position in it), the daemon knows nothing about device sessions. No context —
  no preloading, and it costs nothing. Preloading always yields to a live
  viewer; when the GPU is busy or space is short it simply does not start.
- **The policy of requests to YouTube.** For video we go anonymously, as now, with a
  limit on simultaneous downloads and with resumption instead of downloading from scratch (audio has
  `--no-part`, but losing 400 MB on a 403 at the end is expensive). The account
  from `[youtube] cookies` remains only under an age restriction: the repository
  kept it deliberately, because yt-dlp warns of a ban. If a rate limit comes from the home
  address, the first thing cut is preloading, not anonymity.

## Open questions to the owner

Four questions; without answers the builder will guess, and the error
will be expensive. The rest of what the review found is decided above or was already in the
document.

1. **“1080p and above” — is this a floor or a ceiling?** All three builders called this
   the first question independently of each other. H.264 1080p with audio is about
   400 MB for 26 minutes and plays everywhere. Above 1080p YouTube gives only VP9 and
   AV1: that is 4–8 times more bytes (1.5–3 GB for the same video), minutes instead of
   seconds for download and muxing, and on someone else's iPad these codecs are not guaranteed. If
   both are needed, the cache will have to hold two or three variants per video and choose
   the profile for the device, which the accepted assumptions do not provide for. And separately:
   many videos have no 1080p at all — “floor” would mean a refusal where it is more logical
   to silently give 720p.
2. **Where does the list on the home screen come from** — from the YouTube account (subscriptions,
   “Watch later”, personal and private playlists) or is it public links
   saved by hand? Subscriptions require cookies on every metadata request and
   specifically of the main account — a different policy and a different risk, plus a login
   state screen. Saved links are a list and an “add” button, three times less
   work.
3. **May the extension be edited?** The anti-goal forbids a second place where
   rendering is kept in sync by hand, and goal 7 says “the extension works
   without changes”. Both at once is impossible. A shared module requires
   edits to `extension/manifest.json` and `extension/content.js` plus a build
   step or dynamic import (in `extension/` there is currently neither a build nor
   modules, and Firefox 128 is declared). A copy is cheaper today and gives a divergence
   in a month.
4. **Will there be non-Serbian channels in these playlists?** The recognition language and the
   study line language are currently global daemon settings and part of the cache key. If
   yes, they stop being one setting for the whole house and become a property of the
   playlist; this touches the cache key, the initial prompt, and requires the page to
   be able to hide the study line. One cannot guess: the notes already contain a case
   where Serbian speech was recognized as English.

**All four were answered on 2026-10-30.** The answers and their consequences are in the section
“Owner's answers” below; it takes precedence over the wording above.

## Owner's answers, 2026-10-30

This section takes precedence over the wording above: where it diverges from the
assumptions of earlier sections, it is right.

### 1. Quality: 1080p is a ceiling, not a floor

The reason was named by the owner directly: saving traffic, time and disk space.
Consequences that remove several forks at once:

- **One profile for all devices:** H.264 + AAC in mp4. VP9 and AV1 are not
  used at all. So there is no choosing a profile for a device, no two or three
  muxed variants of one video in the cache, no requirement for the page to report
  its decoding capabilities. Someone else's iPad and an old phone open the same
  file as the tablet.
- **A ladder down, not a refusal:** 1080p is the upper bound. If the video has no 1080p —
  the best available below is taken (720p, 480p). Refusing is not allowed: many
  videos simply have no 1080p.
- **A countable budget:** about 2 Mbit/s, that is roughly 400 MB for 26 minutes and
  about 15 MB per minute. The video cache ceiling and eviction by age are counted
  from this figure; the builder has nothing more to guess.
- The transport upgrade to a manifest with segmentation remains where it was: decide
  later by measurement, not in the first version.

### 2. Home screen: saved links only

The YouTube account is not used for metadata. No subscriptions, no “Watch
later”, no private playlists.

- The home screen is the list of links saved by the owner (channel, playlist, video) and
  an “add link” button. Expanded by yt-dlp, anonymously.
- Cookies remain exactly where they were: substituted only under an age
  restriction. The account-saving policy already laid down in
  `helper/sources/ytdlp.py` and `config.example.toml` does not change.
- There is no login state screen and its refusals. This crosses out a noticeable
  piece of work and a noticeable class of failures.
- A pasted link to a single video is the same saved link, just without
  playlist context. There is no preloading for it, because there is no “next”.

### 3. We do not touch the extension at all

The owner's wording: the goal is to make a service that serves video and subtitles into
the web app, without intersecting with the extension.

- In `extension/` not a line changes. No build step, no shared module, no
  dynamic import, no re-checking of the only tool that
  currently works.
- The web app has its own rendering, written anew. Recognizability relative to the
  extension is pleasant but not a requirement; a divergence of appearance over time is
  acceptable and acknowledged.
- The withdrawn anti-goal is marked above in the text.
- What follows for the protocol: the daemon becomes a service with two
  independent consumers, and its changes must be **additive only**. The rule
  “do not introduce new `Stage` values” becomes stricter from this too: it is now
  the extension's only protection.

### 4. Languages: any input, always one output

Clarified by the owner on 2026-10-30: English and other videos must be **recognized and
translated into Montenegrin and Russian, just like Serbian ones**.

This removes what I wrote here first. I read “read the Serbian translation
on English videos” as “the translation target changes” — wrong. The target does not change:
the output is always the same two lines as today, Russian and Montenegrin.
Only the input language changes. The earlier “one language for all” also remains in force
in its part: the key and settings are shared, and the source language is a property of the material.

**What follows.**

Today the second line is almost free: `helper/montenegrin.py` converts
the Serbian recognition text into a Montenegrin reading by rules, without calling the
model. For an English video there is nothing to convert — the source is not Serbian.

Hence an arrangement that keeps the existing code entirely: **the Montenegrin
conversion remains the last step, only what is fed to it changes.**

| Source | First line (Russian) | What is fed to `montenegrin.py` |
|---|---|---|
| Serbian | sr → ru translation, as now | recognition text, as now — free |
| English and others | xx → ru translation | xx → sr translation, a new model call |

That is, `montenegrin.py` is not a dead end, but on the contrary, the only part that does not
change at all. There is exactly one new thing here: for a non-Serbian source a
second translation direction appears, xx → sr, which was not in the pipeline.

**What we pay.** On a non-Serbian video the translation doubles: two targets instead of
one. By measurement (about four cues per second per target) this is about two
cues per second, that is, still about ten times faster than the viewer. The mode
“watch immediately” holds.

**What this, on the contrary, removes.** The translator output check looks at
the script, and I wrote above that in the reverse direction it would flip and
start rejecting correct output. This will not happen: the target is always Russian and
Montenegrin, that is, the expected script is the same as today. The danger is
removed, consider the line above invalid.

**What becomes the main risk instead.** Source language
detection. While all videos are Serbian, “Whisper decided it is English” is a reliable sign
of breakage, and the notes contain a case where five videos were caught by this sign
where Serbian speech was recognized as English and translated instead of transcribed. Now English in playlists is legitimate, and this sign stops
working. So the source language must be **a property of the saved link**, and
not a result of detection: an English channel is marked once, and afterwards
recognition does not guess. Detection remains only for links whose
language was not set, and its result must be visible on the screen, not applied
silently.

**A cheap path for English videos.** English videos, unlike
Serbian ones, almost always have real author subtitles. The daemon already knows how to take them
(`manual:<language>` in the code) and skip recognition entirely. For the English
part of playlists this removes both the GPU and the language detection risk: only
translation remains. Being checked in the ongoing research.

The exact breakdown — what the pipeline can do by direction, what exactly changes in
recognition, how the language passes through the cache key and the job identity — is
being written by the research and will land below.

**How it is built** (branch `source-language`, 2026-11-04). There are no saved links in the daemon
yet, and a person can declare the language in one way: by a code in `[asr] language`
of the `config.toml` file, for all videos at once (a pin); `auto` — do not declare.
The detector runs on every recognized video, and under a pin too: the pin
decides the decoder token, and a mismatch with what was heard goes onto the wire (see “Decisions”).
Declaration per link or per video is deferred together with its consequence: the language does not enter the job
registry key, and under declaration per link it must. The cheap path for
English videos is not open: the author's track is still taken only for the Serbian
family.

## Language research: what turned out wrong and what really breaks

A breakdown of the pipeline by direction, 2026-10-30. It refutes three things
written above in this same document, so it starts with them.

### Refuted

**There is no distilled translation model.** There is one translator:
an HTTP request to ollama `/api/chat` (`helper/translate.py:245`) with one model name
from the config. `ct2-opus-mt-converter` in `.venv/bin/` is the console entry
point of the `ctranslate2` package, which is pulled as a dependency of faster-whisper for
recognition; there are no opus-mt weights anywhere. Where the premise came from: in
`.remember/archive.md` it is written “MT distillation (opus-mt-tc-big, 27M pairs)”, and
in the original, uncompressed wording it said “MT distillation **strategy**” —
a decided, recorded, but never trained plan. The word was lost when the
memory was compressed. So the branch is not empty, but looks to the future: there is no
distillation, and the new language requirement is an argument **not** to build it per pair per direction.

**The second line is not a rule but a second model output.** This is the main correction.
`dual_cue_schema` (`helper/translate.py:135-171`) puts two
literal keys into the response schema, `"crnogorski"` and `"ruski"`, ollama compiles it into a grammar,
and the decoder physically emits both fields in one call. `translate_dual`
(`translate.py:258-310`) returns the first element in `cue.study`. The regular
expressions in `helper/montenegrin.py` are not the producer but a **post-hoc checker**
(`score`/`check`, lines 126-168), whose result `jobs.py:1100`
only writes to the log.

From this follow two conclusions that cancel what was written above:

- **Translation on a non-Serbian source does not double.** I wrote “two targets instead of
  one, about two cues per second instead of four” — wrong: both lines are
  already obtained by one call. Only the source language changes, the price is the same.
- **The scheme “`montenegrin.py` remains the last step, only what is fed to it changes”
  has nothing behind it** — there is no converting step, there is nowhere to feed.
  Consider the table in section 4 invalid.

**Removing the danger from the script check was hasty.** I wrote that
the check would not flip, because the target is always Russian and Montenegrin. But the
danger is not removed, it has moved — and, more importantly, it is not of that kind at all.
**No checker can reject:** both `validate_dual` and `validate`
only reach `log.info` (`jobs.py:1100-1103`, `1107-1109`), and the repair
ladder `_translate_halves` is entered solely through an exception from the model
(`RETRYABLE` at `jobs.py:64`, entry at `jobs.py:1052-1054`). So a wrong
output is not rejected but passes and gets into the cache — exactly the mechanism of the recorded
case “36 failures out of 40, the batch is cached”.

### What really breaks on a non-Serbian source

In the order of what the viewer will see.

1. **Cues shift, and nobody notices.** `alignment_shifted`
   (`translate.py:350-397`) — the only protection against the nastiest failure —
   works on the coincidence of four-letter stems between the source and the output, and
   the code says directly that it relies on their closeness. For English
   source with Serbian or Russian output there is no common vocabulary: coverage is about
   zero at every position, the threshold never fires, the function returns
   “no shift” on any batch. On screen: the fifth cue shows the text
   of the tenth, coherently phrased, and neither the daemon nor the log sees it.
   **This is what must decide the fate of the design** — not translation quality.
2. **The Montenegrin checker certifies English.** `score()` returns
   `ratio = 1.0` when not a single marker matched (`montenegrin.py:137`), so a
   batch without a single Montenegrin feature gets a perfect score, and
   `check()` is silent.
3. **The recognition prompt is unconditionally Serbian**, and the proper-name collection drives
   English titles through Serbian transliteration: verified, from English
   Title Case it catches “Copyright”, “All”, “Rights”, “Reserved”,
   “Subscribe”. And all the hallucination guards that the worker sets, in the batched
   mode of faster-whisper 1.2.1 are documented no-ops
   (`transcribe.py:351-369`), and the prompt is re-fed to every chunk. On screen
   in the middle of an English video — a Serbian service phrase as a cue, carried
   further into the translator.
4. **The language is taken from metadata before the detector** — this is the recorded
   case: `_source_language` (`jobs.py:955-979`) trusts the yt-dlp field and only
   then calls the detector, and YouTube marks Serbian channels as `en`. The log
   line that would reveal the mismatch (`jobs.py:669`) is structurally dead:
   in batched mode `info.language` equals what we passed, so
   “detected” is always equal to “passed”.
5. **A detection error becomes permanent.** The cache key includes
   `self._config.asr.language`, that is, the literal string `"auto"`, not the language
   with which recognition actually ran (`jobs.py:462`, `1318`). Plus `_serve_cached`
   runs before recognition. There is no second chance: in the recorded case about 80%
   of the `src` of the cached track remained English.
6. **The language code is not checked anywhere.** faster-whisper accepts exactly 100 codes
   and fails on the rest; `sh`, `me`, `cnr` and the empty string fail, and
   `sh`/`me`/`cnr` lie in the repository's own BCMS sets. Outward
   goes a traceback tail of 500 characters, almost entirely the list of
   allowed codes, marked as retryable — so the poll recreates the
   job every 60 seconds, re-downloading audio under the GPU pass. The same
   pattern that gave 43 downloads in 90 minutes and then a 403.
7. **Segmentation is tuned for two languages.** `SENTENCE_END` has no list of abbreviations:
   “Today we test the U.S. market, e.g. tech stocks.” falls apart into four
   cues, and it is the fragment that goes to the translator. And `CONJUNCTIONS`
   (`segment.py:36-40`) contains only Serbian and Russian function words, so
   English lines are wrapped with a dangling “but” at the end.
8. **The cheap path is closed by one regex.** `usable_manual_track`
   (`ytdlp.py:321-336`) takes a track only if its language matches
   `^(sr|hr|bs|sh)`, so an English video with real author subtitles
   goes to full recognition every time: a place for the GPU, download under
   the pass, one and a half to two minutes, segmentation artifacts and prompt echo — instead of
   one anonymous yt-dlp call. The `has_automatic_captions` field is collected and read by
   nobody.
9. **`targetLang` is decorative.** It reaches `Job.target_lang` and the cache key
   and goes no further: in `translate.py` it is absent entirely. A client that requested
   `targetLang: "sr"` will get Russian subtitles under a separate cache fingerprint,
   that is, the same video will be recognized and translated twice for the same
   result.
10. **A successful second line is thrown away if the first failed.** The fields
    are assigned independently (`jobs.py:1069-1085`), and the rendering suppresses the second
    line by the `failed` sign.

**How it is built** (branch `source-language`, 2026-11-04). Item 3 is closed: the prompt
exists only for the Serbian family, names are taken from the title and tags, segments
are filtered by `no_speech_prob`, `avg_logprob` and `compression_ratio` (for “repetition” —
only looped sentences), the prompt echo does not get into cues, and the empty
guards in batched mode are removed from the call. Item 4 is closed: there is no early return on
metadata, the mismatch is written to the log as a warning and goes onto the wire.
Item 5 is closed partially and differently from what was proposed — see “Decisions”. Item 6 is closed:
codes are checked, `""` does not reach Whisper. Item 7 is half closed: abbreviations
(“U.S. market, e.g.”, “5. maja”) no longer break a cue, but `CONJUNCTIONS` is untouched —
line wrapping does not reach the wire (`_to_job_cues` takes only the text). Item 8 is half closed: labels of the Serbian
family are suitable in any script, other languages are deferred. Item 9 is closed:
`targetLang` left the cache key, remained in the snapshot. Items 1, 2 and 10 — the work
“Translation check”.

### Decisions

- **The source language is a property of the saved link**, with an override per
  video. The detector runs **always**, but as a check, not as a fallback
  path: the early return on metadata is removed, the declared code goes to the worker
  as a hint, and the worker returns the declared, the detected and the confidence.
  A mismatch is not resolved silently — it goes onto the wire and to the screen in words.
- **The effective language goes into the cache key**, not the config string. Lookup by
  the declared, write under the effective; `PROMPT_VERSION` is raised, otherwise
  corrupted tracks would survive the fix.
- **Language codes are checked** against the faster-whisper list before the worker starts, with a
  replacement table (`sh`/`me`/`cnr` → `sr`), and the refusal is given as
  `PermanentJobError` with a human phrase, not a traceback. An empty detector
  result means `None`, not `""`.
- **The prompt becomes a function of the effective language**, and where there is no phrase — there is none
  at all; `to_latin` is limited by the same BCMS test that is already in
  `script.py:76`; segments are filtered by the fields that the library already
  returns (`no_speech_prob`, `avg_logprob`, `compression_ratio`), and a cue
  coinciding with the prompt does not get into either line.
- **The author's track opens for any language**: the condition becomes “the track's language
  equals the effective source language”, `skip=translated_subs`
  is preserved, and the label is checked against the detector on one thirty-second
  probe. For the English part of playlists this removes both the GPU and the detection
  risk.
- **The second line becomes typed**: `{text, lang, kind}`, where `kind`
  is one of `source` (source text), `source_script` (the same in another
  alphabet), `dialect` (the current Montenegrin), `gloss`. The `study` field
  is kept as a deprecated synonym, so that the extension stays untouched.
  Every output line gets its own `lang` — then a cue whose translation
  failed describes itself, instead of being derived from a nameless sign.
- **The checkers get a verdict that the caller obeys**: on a refusal
  above the threshold the batch goes into the existing halves ladder, and if that did not help —
  the cues are marked unverified, not silently passed. Outside the sr/cnr pair
  `montenegrin.check` is not called at all, and the leak detector is never aimed at a
  Serbian-family field — the `validate_dual` docstring warns of this directly
  (`translate.py:402-409`).
- **Replacing the protection against cue shift is separate work and a readiness condition.**
  The current one relies on shared vocabulary and is blind outside close languages. A
  language-independent one is needed: matching by the number of cues, by durations and by the
  ratio of lengths, not by word stems. While it is missing, non-Serbian videos go with an
  honest mark “alignment not verified”.
- **One model for all directions.** One resident model serves all pairs, and the
  admission passes the memory requirement when it is already in the GPU
  (`gpulease.py:217`), so two playlists in two directions cost nothing
  extra. A second model per pair will not fit into the GPU (17800×2 versus 24564
  MiB) and will be identified by the watchdog as foreign, evicting the daemon's own job.

**How it is built** (branch `source-language`, 2026-11-04), by decisions:

- **Detector always.** The worker listens to up to eight windows of 30 seconds, spread
  over 10–90% of the length (a recording no longer than a window — one window). A window where speech by VAD is less than
  three seconds does not go into the model and does not vote; a window on which the model failed, either.
  The shares of bs/hr/sr add up into one family, the winner is the largest average share over
  the voting windows. If the winner is not Serbian and not a Slavic neighbour (mk, bg,
  sl, ru, uk, be, cs, sk, pl), and the Serbian share is not below 0.2, the decision is Serbian
  (`reason: "floor"`): that is how Whisper lost Serbian speech to English
  (3XJLfTSZy6I: en 0.55 versus 0.44). No window voted — one pass over
  the first 30 seconds of speech of the whole video; and empty there too — the YouTube label, and without it
  Serbian (`fallback`, `detected: null`). Metadata decides only then; with
  window votes they only mark the mismatch (Juske: YouTube en, speech sr → sr,
  “YouTube indicated en, by speech sr”). A pin decides the decoder token as set (hr and bs
  are preserved), the detector works under it too. All — in the same worker run and
  under the same GPU pass: neither a second model nor a second download. Constants are in
  `helper/language.py`, and any change to them requires raising `PROMPT_VERSION`.
- **On the wire** — a `language` object in the snapshot and the event `{"type": "language"}`; it
  comes before `segmented` and `cues` on every path: recognition, author's track,
  cache, restore. Fields: `effective`, `declared`, `declaredBy`, `detected`,
  `confidence` (two digits), `reason`, `source`, `track`, `verified`, `mismatch`,
  `note`. `note` is a ready phrase no longer than 60 characters: “YouTube indicated en, by speech
  sr”, “in settings sr, by speech en”, “almost no speech, language taken as sr”. A job
  resumed after a pause keeps the decision. The extension does not show it — it is
  frozen; the decision will reach the screen with the tablet page, and for now it is visible in the
  snapshot and in the log (`docs/running.md`, “What language the daemon decided”).
- **The cache key is a deviation from “lookup by declared, write under effective”.**
  The key holds the person's declaration (the pin; `""` if there is none), not the effective language:
  the cache must answer before any GPU work, and the effective language appears only
  inside the worker. Where the person declared, the effective language equals the declared, and
  the formula holds; where not — the decision lies in the record and arrives on the wire on
  every viewing from the cache. `targetLang` has left the key (`Job.target_lang` and
  `targetLang` in the snapshot remained). `PROMPT_VERSION` and `SCHEMA_VERSION` are raised to 2:
  previous records are not found, not deleted from disk (about 18 MB as of 2026-11-01; whether to delete
  is the owner's decision). A result without a single cue is not written to the cache, and such an old
  record is not served by either the pipeline or restore. Restore recomputes the
  key from the saved fingerprint by the current pin and models, and with several
  author's track records takes the freshest by `created_at`; it does not believe
  the versions and models from the file. A low-confidence decision is cached: a repeat would cost the GPU
  on every viewing, and the remedy for the error is
  a mark on the wire and a pin.
- **Codes.** The one hundred codes of faster-whisper 1.2.1 are written into `helper/language.py` and checked
  by a test against the installed package. Replacements: sh, me, cnr → sr; iw → he, jv → jw, fil →
  tl, nb → no, in → id. An invalid pin does not crash the daemon: at startup — `log.error`, and
  for each job — a refusal before the YouTube request with the phrase “unknown language “…”: [asr]
  language in config.toml”, without a retry. A YouTube label not in the list simply
  does not hint.
- **The prompt** is a function of the effective language: the phrase exists only for the Serbian family,
  for the rest there is no prompt at all. Names — from the title and tags, not from the description (echo into the
  field came from its template lines), in Latin script; a name that remains Cyrillic
  is thrown out. Segments are filtered: “silence” (`no_speech_prob` > 0.6 with
  `avg_logprob` < −1.0) throws the segment out entirely, and “repetition”
  (`compression_ratio` > 2.4) only marks it: a batched-mode segment is a piece of
  speech up to 30 s, and only looped sentences leave it — with their own degree of
  compression above 2.4 or repeating what was already said in this segment; the dialogue before
  the loop remains. Every filtering is written to the log. Echo is a deviation from “coinciding with the
  prompt”: there is no exact match in any of the 66 620 cues of the cache, so echo is
  considered a sentence of at least two different words, all of whose words are in the
  prompt, at least one not a function word (ovo, je, video, na, se, srpskom, jeziku), and
  among them there is an uncommon word of the phrase or of the name introduction (transkript, snimka, pominju)
  or two different names not function words: one title name in a common phrase
  (“Šta je ovo?”) — speech.
- **The author's track in any language — deferred.** There is nothing to check the label against the speech without the GPU:
  there is no detector smaller than large-v3 and turbo on disk, a probe on the GPU would take away from the path of
  the track its meaning, and taking a foreign track without checking means returning the Juske case
  (label en on Serbian speech). Return conditions — in the plan, section “Deferred”. Less was done:
  labels of the Serbian family are suitable in any script (sr-Latn, sr-Cyrl, SR, sh,
  cnr) and are latinized, `live_chat` is never suitable, a pin outside the family does not take the
  track. Decision on the track: `source: "manual"`, `reason: "track"`,
  `detected` and `verified` — `null`.
- **Not done here** (the work “Translation check”): the typed second line,
  the checkers' verdict, language-independent protection against cue shift and visible marks
  “alignment not verified” and “second line not certified”. “One model for all
  directions” is true without edits.

### What must be fixed right now, regardless of the tablet

`config.example.toml:24` still gives `language = "sr"` with a comment
that auto-detection is not used, whereas `helper/config.py:72` already sets `"auto"` as the default
and explains why a hard-pinned language on non-Serbian audio leaves the decoder in
uncertainty. The default `"auto"` came in c96cc58, and the line in the example was not touched. Whoever configures by the example will reproduce the
recorded case with five videos on English material. It is one line.

**How it is built** (branch `source-language`, 2026-11-04): in `config.example.toml`
there is `language = "auto"` and an explanation that the detector runs always, and the code
pins the language of each video and is suitable only when the language is the same for all; sh, me and
cnr are accepted as sr.

### ~~Open question to the owner~~ — answered 2026-10-31

The answer and its consequences are in the section “The second line: the owner's decision” below.
The short summary: the Montenegrin line remains, the recommendation to replace it with the
source text is rejected. The text of the question is kept below as context.

**What should the second line be?** The research claims that for both of your
tasks the most useful and cheapest is the source text — it is free, and a Montenegrin
retelling, on the contrary, leads away from those very words that you hear: the learner
reads not what was spoken. But you made the Montenegrin line deliberately, and this is
your decision, not mine. Options: leave as is; replace with the source
text; show both and choose on the device. The typed field above
supports any of the three, so the answer does not block the rest of the work.

## The second line: the owner's decision, 2026-10-31

Wording: to see Montenegrin variants of phrases and hear Serbian originals,
in order to learn both languages and at the same time see the difference — where which words are used.

**The Montenegrin line remains.** The research's argument against it (“the retelling
leads away from those very words that you hear”) was right in fact and wrong in
meaning: it is exactly this divergence that is the content of the exercise. Consider the recommendation
to replace the second line with the source text rejected.

### A consequence that outweighs everything else in this section

**A desired capability and a known breakage look the same on the screen.**

- A legitimate lexical divergence of Serbian and Montenegrin
  (`bezbednost` versus `sigurnost`) — this is what the owner wants to see.
- An artifact of a language detection error — Whisper recognized the speech as English,
  and the “Montenegrin” line was obtained from English — looks exactly the same:
  the line diverges from the speech.

The case recorded in memory is described by literally the same pair of words. So the sign
“the Montenegrin line does not match the speech”, by which the breakage has been caught until now,
stops being a sign of breakage: now it is the norm.

From this follows a reassessment of priorities, made above in the document:

1. **Showing the declared and detected language with confidence is no longer
   a convenience but the only way to distinguish these two cases.** It is raised from
   “decisions” to readiness conditions.
2. **Protection against cue shift becomes stricter in meaning.** A shifted cue in the
   learning scenario does not simply interfere with watching — it teaches a wrong
   correspondence of words. A language-independent replacement for `alignment_shifted` is mandatory before
   non-Serbian material appears on the tablet.
3. **Unverified cues must be visible.** The mark “alignment not
   verified” and “second line not certified” (`montenegrin.check` is not called outside the sr/cnr pair)
   must reach the screen, not remain in the log.

### Hearing the original is a requirement on muxing, not a wish

“Hear the Serbian originals” means that the muxed file must contain the
**original** audio track, not auto-dubbing. In the recognition pipeline this is
already accounted for: `audio_pcm` sorts by `-S lang,+size`, and the comment on
`helper/sources/ytdlp.py:529-531` explains that without `lang` in front, on videos with
auto-dubbing the smallest track will be chosen, that is, a foreign language.

Video muxing is **new code**, and it must choose the track by the same sorting.
Otherwise the viewer hears dubbing and reads subtitles made from the original: a silent
failure, where everything works and the exercise is meaningless. Readiness check: the language of the track in the muxed file matches the effective source language
of the job.

### ~~Layout: three lines, with the difference marked up~~ — CANCELLED 2026-10-31

Cancelled by the owner's decision on the same day. Read the section “Layout: exactly
two lines” at the end of the document; everything to the end of this subsection and the subsection
“Non-Serbian source: what to compare” is invalid.

<details>
<summary>Cancelled variant, kept as context</summary>


“See the difference, where which words are used” is not a consequence of showing two
lines, but a separate capability. Serbian and Montenegrin texts almost coincide,
so the difference is counted word by word and marked up right in the cues. Cheap:
both lines are short and in the same language family, no model is needed.

| Line | Content | `kind` | Why |
|---|---|---|---|
| first | Russian translation | `target` | to understand |
| second | Montenegrin variant | `dialect` | what you are learning to read |
| third | source text | `source` | what you hear, for comparison |

Divergences between the second and the third are marked up. Showing the third line is a device
setting, like `showOriginal` in the extension (`extension/content.js:21`,
default off); here the default is **on**, because that is the point.

Suppressing the second line by the `failed` sign (`extension/content.js:405`) is not reproduced in the new
page: the rule “hide only if the language matches the
first line”, already laid down by the typed field, is mandatory here — otherwise
a translation failure takes away exactly the line for which everything is done.

### Non-Serbian source: what to compare

On an English video there is nothing to compare with — English is heard, and the pair
“I hear Serbian / I read Montenegrin” does not exist. My decision: for a non-Serbian
source the model is asked for **three fields** — Russian, Serbian, Montenegrin. Then the
marking of differences keeps its meaning (one can see where the languages diverge), and the comparison
moves from “I hear versus I read” to “two translation variants side by side”. This is one and
the same call, not a second one: the fields are added to the same grammar. We pay only in
emitted tokens.

If this turns out superfluous — the `kind` field allows turning off the third variant
by a setting, without reworking either the protocol or the page.

</details>

## Layout: exactly two lines

The owner's clarification of 2026-10-31, cancelling the three-line variant above: remove the Serbian
caption. The video is watched in the original language, be it Serbian or any other.
On the screen exactly two lines:

| Line | Content | `kind` |
|---|---|---|
| first | Russian translation of the original | `target` |
| second | Montenegrin translation of the recognized text | `dialect` |

**This is the current behaviour of the daemon**, extended to an arbitrary source
language: `translate_dual` already emits both fields, `crnogorski` and `ruski`, in one
call with constrained decoding. No third line, no markup of
differences, no third field in the grammar for English videos.

What this removes from the work: word-by-word comparison of two texts, the setting for showing the
source on the device, the extension of the response schema and the associated increase in
emitted tokens. What it does not remove — nothing from the readiness conditions below.

### Reading “original” as the source text of the cue

The owner named the lines differently: the Montenegrin one is a translation of the **recognized text**,
the Russian one is a translation of the **original**. The daemon has no second source and will not have one:
both lines use the same source text of the cue. Where the video has an
author subtitle track, that text is it (and it is indeed closer to the
original than recognition); where not — Whisper's output. The choice between them has already been
made in the pipeline and does not change.

### What of the previous decision remains in force

Three things, and none has weakened.

**The original audio track is a requirement.** “We watch in the original language”
means that the muxed file must contain the original track, not
auto-dubbing. Recognition already accounts for this (`-S lang,+size`,
`helper/sources/ytdlp.py:529-531`); muxing is new code and must sort
the same way.

**Distinguishing capability and breakage has only become sharper.** By removing the source
line from the screen, we also removed the only written reference point: now the
Montenegrin line is checked by the viewer **by hearing alone**. The legitimate
divergence of Serbian and Montenegrin and the artifact of a language detection error
still look the same, but there is now nothing to compare them with. Therefore showing
the declared and detected language with confidence, language-independent protection against
cue shift and a visible mark of unverified cues remain readiness conditions, not conveniences.

**The rule for hiding the second line.** In the extension it is suppressed when a cue's
translation failed (`extension/content.js:405`). Do not reproduce this in the new
page: hide the second line only if its language matches the first.
Otherwise a Russian translation failure takes away the Montenegrin line, that is, exactly what
everything is done for.

## Source language: an internal value, not a setting

The owner's question of 2026-10-31: so the original language is not important to us at all, the main thing is
to recognize or take the author's captions, translate into Montenegrin and Russian and
show?

**At the product level — yes, and this is a real simplification.** There is nothing for the viewer to
choose: the output is always the same, two lines, regardless of what is heard. From this
everything that previously seemed necessary is crossed out: there is no language switch either on the
page, or in the link, or in the daemon settings as a product choice; the translation
target is never different; `targetLang` on the wire remains decorative and is not set in
the new client at all (and its current decorativeness is a separate
bug, see above); there is no third line and no comparison of texts.

**At the pipeline level — no.** The source language stops being a *decision* and
remains *a value that the pipeline must determine correctly*. All the found
breakages remain in full, because they are not about the viewer's choice but about how
this value is obtained and used:

| Where the language is still needed | What the viewer sees if it is wrong |
|---|---|
| Whisper decoder language token | The recorded case: Serbian speech comes out as English text, translated further. Coherent and wrong |
| Recognition prompt | A Serbian service phrase as a cue in the middle of an English video |
| Choosing the author's track | The wrong track is taken: a translation instead of the original, that is, a translation of a translation |
| Segmentation into cues | Fragments like “market, e.g.” go to the translator as independent phrases |
| Hint to the translator | The prompt asserts “from Serbian to Russian” about English speech, and the false-friends glossary is mixed in as noise |
| Cache key | A wrong recognition is fixed forever and arrives on all devices |
| Protection against cue shift | Cues shift silently, and the viewer learns a wrong correspondence of words |

That is, the earlier entry “the source language is a property of the saved link” remains in
force, but its justification changes, and this is important for whoever will build: it is
**not a setting for the viewer, but protection from wrong guessing** on a channel whose language
the owner already knows. The field is optional; empty means “determine
yourself”.

### What follows to clarify

**Detection runs always, metadata is only a hint.** Before, this was
one of the decisions; now it is the only reasonable path, because there is no person left in the loop
who would correct the error. The early return on the yt-dlp value
(`helper/jobs.py:973-976`) is removed: YouTube marks Serbian channels as
`en`, and this is exactly what gave the recorded case.

**Author captions have no detected language at all.** If the track was taken, Whisper
was not run, and all we have is the language label from YouTube, that is, exactly the
source that once already lied. Therefore the author's track path gets
its own check: one thirty-second probe with the detector against the label. A mismatch
— not a silent choice of one of the two, but a mark on the screen.

**How it is built** (branch `source-language`, 2026-11-04). The first is done, see “Decisions”.
There is no probe for the author's track: it is deferred together with tracks in other languages (in the
same place). While tracks are taken only for the Serbian family, and the decision on them honestly
says that the speech was not listened to: `detected` and `verified` — `null`, and the mismatch
is counted between the declaration and the track label (“YouTube indicated en, author's track
sr”).

**The case “the source is already Russian or Montenegrin”.** Then one of the two lines
degenerates into a translation into the same language. `helper/montenegrin.py:110-117` provides for this
with branches for sr/cnr and for ru, so the pipeline is ready for it, but
the page must not show two identical lines: the rule of hiding by language
match, already written above, covers this case too.
