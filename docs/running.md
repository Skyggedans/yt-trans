# Daemon: starting and diagnosing

A note about starting. 2026-11-01.

## Why

The tablet accesses the daemon from another room, while the computer at that time may be
sitting at the login screen: the owner logged out or the machine rebooted and
no one has logged in yet. The daemon is a user unit
([`packaging/yt-trans.service`](../packaging/yt-trans.service)), and the systemd manager
without lingering lives only while the user has at least one session open. No session — no manager, no unit, no daemon, and no one
listens on the port. A locked screen doesn't count: the session is still alive. The trouble is logging out and booting without signing in.

The client will see this as “no connection to the daemon”. The same phrase, most likely, is also produced by
another cause: after network addresses change (for example, docker brought up or took down an
interface), Gecko in Zen drops silent connections, including loopback. It's easy to confuse,
and they are treated differently, so you must distinguish by the daemon's state, not
by the error text:

```
loginctl list-users
systemctl --user is-active yt-trans
journalctl --user -u yt-trans
```

The owner is not in the list (the command is run from someone else's session) — no manager,
the logout or boot without sign-in is to blame. The owner is in the list but the daemon is not `active` —
it crashed on its own, the cause is in the log. The daemon is `active` — the matter is the loopback drop.

## How it is now

Nothing applied: `Linger=no`, there is no system unit,
`packaging/yt-trans.service` has not changed. The daemon is fine with any open
session, even a locked one; without a session (after a reboot, before sign-in)
the tablet won't reach it. Check:

```
loginctl show-user skyggedans -p Linger
```

The flag is an empty file `/var/lib/systemd/linger/skyggedans`: it is system
state, not repository state, that's why it is described here and not committed.

## What does not depend on the session

Checked 2026-11-01 against the code and the machine, so there is nothing to change in the unit itself:

- The unit is bound to `default.target`; neither `graphical-session.target` nor any other
  session dependency is in it.
- Ollama is a system service (`ollama.service`, enabled, active), translation does not
  disappear with the session. `/dev/nvidia*` are open to everyone (`crw-rw-rw-`, no ACL from
  logind): access to the graphics card is not granted to whoever sits at the console.
- The daemon does not need session variables. `DISPLAY` and `WAYLAND_DISPLAY` are in the
  manager but not in the environment of the running daemon: it came up before the
  desktop passed them on, and works. Its own code reads from the environment only
  `YT_TRANS_*`, `HOME` (via `Path.home()`) and `LD_LIBRARY_PATH` with `HF_HOME`,
  which it sets itself in [`helper/env.py`](../helper/env.py); `ffmpeg` and
  `node` are looked up via `PATH` (both in `/usr/bin`); the daemon does not access D-Bus, the keyring or browser
  cookies. So the “session variables” from the unit header
  are a weak cause.

## Option A: `loginctl enable-linger skyggedans`

Rollback — `loginctl disable-linger skyggedans`. The price is that lingering
brings up the whole user manager — at boot, without a graphical
session — and does not shut it down after logout. Together with yt-trans, without a session,
everything enabled in the manager and not tied to `graphical-session.target` starts.
Most of it is standard (`dbus-broker`, `obex`, `xdg-user-dirs`, the tmpfiles timer and service,
`grub-boot-success`), but two groups are a real change:

- sound: `keep-soundbar-alive` and `guitar-monitor-link` (both on `default.target` and
  with `Restart=always`); the second pulls `pipewire` via `Wants=`, and that one —
  `wireplumber`. According to the unit descriptions, they work with the Focusrite and the soundbar;
- gaze: `tobii-gaze-keys`, which pulls `tobiid` via `Requires=`, and
  `tobiid.socket`.

Without a session they would come up blind: access to the cards and the tracker there is given by the udev
rule `uaccess` (`/dev/snd/*` with ACL, `/etc/udev/rules.d/70-tobii-uaccess.rules`)
only to someone logged in locally, and the owner is not in the `audio` group. Most likely,
they would be left without devices; what exactly they would do is not checked.

### What was tried and why it was rolled back

2026-11-01 we tried to get by with binding to the session: lingering plus drop-ins
`session.conf` in `keep-soundbar-alive`, `guitar-monitor-link` and
`tobii-gaze-keys` (`ConditionEnvironment=|WAYLAND_DISPLAY` and `|DISPLAY`,
`PartOf=` and `After=graphical-session.target`) and links in
`graphical-session.target.wants`. It seemed this would be enough, but no:

- The condition cuts off the client, not what it pulls. systemd.unit(5), “Conditions
  and Asserts”: a skipped unit does not fail, but “other units are still pulled in
  and ordered as if this unit was successfully activated”.
  `guitar-monitor-link` would still bring up `pipewire` with `wireplumber`, and
  `tobii-gaze-keys` — `tobiid`. In the 2026-10-28 boot `pipewire` and
  `wireplumber` became active at 11:34:51, together with `default.target` and two
  seconds before `graphical-session.target`.
- A dependency cannot be removed with a drop-in. systemd.unit(5), example 2: “dependencies can
  only be added in drop-ins. If you want to remove dependencies, you have to
  override the entire unit”.
- What starting `pipewire`, `wireplumber` and `tobiid` before sign-in would give is not checked,
  so it was rolled back before the first reboot: `disable-linger` executed, the drop-ins and
  links removed (the previous `debug.conf` in `tobii-gaze-keys` is in place).

### The right way (recommendation, not done)

The cause is in the units themselves, so they must be fixed at the source:

1. Edit the units where they are laid out from:
   `~/Work/system-config/files/home/.config/systemd/user/`, and for
   `tobii-gaze-keys` also `~/Work/tobii/systemd/` (the tobii units come
   as a package). `deploy.sh` puts these units as `file` entries from
   `manifest.txt` via `put_file`: the previous file goes to backup (`cp -a`), the
   repository file is installed on top (`install -D -m`). So an edit directly in the deployed
   file will be lost on the next layout unless it is first collected back into the
   repository via `collect.sh`.
2. Optionally — remove the explicit pulls (`Wants=pipewire.service` in
   `guitar-monitor-link` and `Requires=tobiid.service` in `tobii-gaze-keys`) and
   rely on sockets: `pipewire.socket` and `tobiid.socket` are enabled for everyone.
   This step can be skipped: a unit that nobody wants at boot
   does not compute its `Wants=` and `Requires=` at all, so step 3 carries the weight. And
   the step has a price: according to the comment in the unit, `Requires=` ties the client's life
   to the daemon's life, and without it this tie will disappear.
3. Bind the session units to `graphical-session.target` in the units themselves, not
   via `ConditionEnvironment`, whose correctness depends on when the desktop
   passes the display to the manager: `WantedBy=graphical-session.target` in
   `[Install]` and `PartOf=graphical-session.target` in `[Unit]` (as
   systemd.special(7) advises; a sample in the same directory — `deskflow-rearm.service`).

   The new `[Install]` won't move the links by itself. An already enabled unit will keep the
   previous link from `default.target.wants`, `is-enabled` will answer
   `enabled`, and `enable_user` in `deploy.sh`, which enables only what is not yet
   enabled, will skip the unit. So after the layout, move the links
   by hand, once, and check that `default.target` is no longer in `WantedBy=`:

   ```
   systemctl --user reenable keep-soundbar-alive guitar-monitor-link tobii-gaze-keys
   systemctl --user show -p Id,WantedBy keep-soundbar-alive guitar-monitor-link tobii-gaze-keys
   ```

   `reenable`, like `disable`, removes all links to the unit, including the one put
   by hand `~/.config/systemd/user/tobiid.service.wants/tobii-gaze-keys.service`.
   If it is needed, it will have to be put back.
4. Log out and log in once: sound and gaze keys must work.
5. Only then `enable-linger`, and after logout make sure from another user's session
   that the port is listened on:

   ```
   ss -ltn 'sport = :8756'
   ```

## Option B: a system unit with `User=skyggedans`

It does not touch any user unit and does not depend on sign-in at all, but it is
installed as root (`/etc/systemd/system/`). This cancels the deliberate choice recorded
in the header of
[`packaging/yt-trans.service`](../packaging/yt-trans.service): the unit was made
user-level so as not to build up `User=`, environment and permissions. There would be
less to build up than it seemed there, but not nothing. What to move (a list, not a ready unit):

- `WorkingDirectory` and `ExecStart` with the interpreter from `.venv`, full
  paths: `%h` in a system unit is `/root`, not the home directory from
  `User=`.
- `Environment=PYTHONUNBUFFERED=1`, `Type=exec`, `Restart=on-failure`,
  `RestartSec=3`, `TimeoutStopSec=30` and `StartLimit*` — as is, and the reasons from
  the unit comments remain valid: `KillMode` by default is the same, so
  recognition still dies together with the daemon and returns video memory.
  In `[Install]` instead of `default.target` you need `multi-user.target`.
- Session environment is not needed (see above). For `User=` systemd itself sets
  `HOME`, `USER` and `LOGNAME`, and the cache and config [`helper/env.py`](../helper/env.py)
  takes from `Path.home()` (`~/.cache/yt-trans`, `~/.config/yt-trans`) or from
  `YT_TRANS_CACHE` and `YT_TRANS_CONFIG`, but not from session variables. The unit
  will see the same cache and config as the user one.
  It will not have `XDG_RUNTIME_DIR`, but the daemon does not access
  it. For the graphics card neither groups nor `DeviceAllow=` are needed:
  `/dev/nvidia*` are open to everyone.
- SELinux on the machine is in `Enforcing` mode, and a service with code in `/home` is a typical
  place for denials: after installation look at `journalctl -u yt-trans` and
  `ausearch -m avc -ts recent`.
- Disable the user unit, otherwise when the owner signs in, two instances
  will fight over port 8756:

  ```
  systemctl --user disable --now yt-trans
  ```

  It is installed as a symlink from `~/.config/systemd/user/` to a file in the repository,
  so the system variant needs its own file, not an edit of this one.

## Decision

The owner decides. Nothing applied. The recommendation is option A via editing
the units at the source; option B is a fallback if you don't want anything in the user
manager to change except the daemon, and root and a second unit file are not
scary.

## Which language the daemon chose

The daemon decides the speech language itself for each video. The Whisper detector listens to up to
eight 30-second windows spread over 10–90% of the video length, and the windows vote;
a window with less than three seconds of speech does not vote. A recording no longer than 30 seconds
is heard as one window from the start, and when no window voted and the recording is
longer than a window, one more pass goes over the first 30 seconds of speech of the whole video.
YouTube's language label is only a hint: it decides only when no
window voted, because YouTube sometimes labels Serbian channels
as English. The code in `[asr] language`
of the `~/.config/yt-trans/config.toml` file is a pin: recognition goes with it, and the
detector still works with it, so a mismatch is visible. The author's track
is taken only from the Serbian family (sr, hr, bs in any notation of the label), and its language is
the track's label: speech is not listened to then.

The decision lies in the job snapshot, in the `language` field (`null` until it is made):

```
curl -s http://127.0.0.1:8756/v1/jobs/<id> | jq .language
```

`<id>` — the video identifier from the address (`watch?v=…`). Jobs live in memory until
the daemon restarts, and no more than twelve ready ones are kept; a video that already
lies in the cache is viewed via `/restore` — it starts nothing:

```
curl -s http://127.0.0.1:8756/v1/jobs/<id>/restore | jq .snapshot.language
```

A video without a single utterance is not cached: a new job for it — after 12 other
ready ones or a restart — downloads the audio again and recognizes it under the pass.

Example — a video where YouTube labeled Serbian speech as English:

```json
{"effective": "sr", "declared": "en", "declaredBy": "metadata", "detected": "sr",
 "confidence": 0.97, "reason": "detector", "source": "asr", "track": null,
 "verified": null, "mismatch": true, "note": "YouTube declared en, by speech sr"}
```

| Field | Meaning |
|---|---|
| `effective` | the language it was recognized in; for the author's track — its label's language |
| `declared`, `declaredBy` | what is declared and by whom: `config` — the pin, `metadata` — YouTube; `null` — nobody |
| `detected`, `confidence` | what the detector heard and this language's share of the window votes (0–1); `null` — no speech found or it was not listened to |
| `reason` | where `effective` came from: `pin` — the pin; `detector` — the detector; `floor` — the language that won is not a Slavic neighbour of Serbian (mk, bg, sl, ru, uk, be, cs, sk, pl), and the Serbian share was not below 0.2: this is how Whisper tends to call Serbian speech English; `hint` — no window voted, YouTube's label taken; `fallback` — no speech found and no label, Serbian accepted; `track` — the author's track |
| `source`, `track` | `asr` — recognition, `manual` — the author's track; `track` — its label |
| `verified` | always `null` for now: the author's track is not checked against the speech |
| `mismatch`, `note` | the declared diverged from the heard (or from the track's label), or there is almost no speech and the language was taken from the hint or a fallback — then `note` explains this; otherwise `null` |

`mismatch: true` is a reason to look into the video. Either YouTube got the label wrong again, and
then everything is correct: `effective` is taken by speech. Or the detector got it wrong — then
there is only one remedy: a pin in `[asr] language`. It acts on all videos at once and
is suitable only when the language is the same for everyone; declaring a language for a video or link is not yet
possible. The pin is read at startup — after editing, restart the daemon. While it
is set, tracks made without it are not found: every opened video
is recognized anew. Removing the pin brings back the previous record along with the previous
error — fixing one video for good is not yet possible. In the log, every
recognized video has a line with the decision and the window votes, and a mismatch and
windows with speech that the detector did not answer come out as warnings:

```
journalctl --user -u yt-trans --since today | grep <id>
```

Page for the tablet and the network: [`docs/tablet.md`](tablet.md).

### After an update

Immediately after merging into the working copy from which the daemon is running (`~/Work/yt-trans`) —
`systemctl --user restart yt-trans`: the daemon holds the code from launch, and the recognition
worker takes it from disk; the previous process sends the new worker `--initial-prompt`,
which the latter does not know, and every recognition fails with the refusal “ASR failed:
usage: …”.

An update with this work (new cache key: `SCHEMA_VERSION` and `PROMPT_VERSION` 2
in `helper/cache.py`) invalidates the whole track cache: every already translated video
is recognized and translated anew the next time it is viewed — once, after that it is
in the cache again. Before that, F5 on such a video shows the launch button, not the ready
subtitles.

Previous records stay on disk — about 18 MB (169 records as of 2026-11-01) — and do not go away
by themselves: eviction triggers only when the cache grows beyond 2 GiB.
Whether to delete them is the owner's decision. Having decided to delete, first stop the daemon: it
keeps `index.sqlite` open, and on start it creates the empty index and directory
anew. Only these two paths are deleted: next to them in `~/.cache/yt-trans` lie the model
weights (`hf/`), they must not be deleted. Records made already after the update go
together with the previous ones — such videos are recognized again.

```
systemctl --user stop yt-trans
rm -r ~/.cache/yt-trans/tracks ~/.cache/yt-trans/index.sqlite
systemctl --user start yt-trans
```
