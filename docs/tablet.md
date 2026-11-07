# Page for the tablet

## What it is

The daemon serves a page with a player itself: open it on the tablet — and over the video
run the same two lines that the extension draws on the computer — the Russian translation and
the Montenegrin text of the same utterance. No extension is needed for this, and the YouTube
player is not involved. Subtitles are not burned into the picture: the daemon serves a clean mp4, and the lines
are drawn by the page, so they can be turned off, rewound and seen again.

The page can be installed on the tablet as an app — and then the YouTube app
itself offers it in “Share”: a video opens in ours with one tap, without
typing the address. What is needed for this (https and your own certificate) — in the section
“App and ‘Share’ from YouTube”.

The **RU** and **CNR** buttons in the top right corner of the stage turn each
line off separately: leave only the translation, only the Montenegrin text, or none.
Next to them — subtitle size selection: **very small**, **small**, **normal**,
**large**. From the other end of the room normal is not enough, and up close
large is in the way; the size multiplies the normal one rather than replacing it, so on a wide
screen “small” does not become smaller than on a phone. Both choices are remembered
in each device's browser separately. In the left
top corner — **⛶** (fullscreen): expands the whole stage, that is, the picture together with the
lines. The player's native button expands only the video itself, and the lines in this
mode are not visible, so where the browser allows it (`controlslist`), it is
hidden. iOS does not know such an attribute: there the native button remains, but there will be no lines in its
fullscreen mode — you need to expand with your own, **⛶**.

## Enable the network

By default the daemon listens only on loopback — `127.0.0.1`, that is, it is not visible from the tablet
and that is correct: the daemon talks to YouTube on the client's command, and it cannot simply be exposed
to the open network. For the tablet in another room to reach it,
add to `~/.config/yt-trans/config.toml`:

```toml
[server]
host = "0.0.0.0"
```

and restart the daemon — settings are read only at startup:

```sh
systemctl --user restart yt-trans
```

The daemon must also work without the owner signing in, otherwise after a reboot
the page will not open. That is `loginctl enable-linger`, details and rollback —
[`docs/running.md`](running.md).

## Access

The daemon has no token: the page and the extension talk to it without a key. Having opened the computer's
address, the page immediately shows the app — nothing needs to be pasted anywhere.

The daemon guards only the Origin. A foreign page in the browser cannot forge it,
so a site from the internet will not hit your daemon via DNS binding to loopback. Requests
without Origin — `curl`, `<video>`, non-browser apps — always pass, and this is
not a hole but the only way to serve video: `<video src>` does not send headers.

Remember: `host = "0.0.0.0"` opens the port to everyone on the home network. Whoever reaches
it not from a browser will use it — at home this is usually fine, on someone else's
Wi-Fi put loopback back.

On the network `/v1/health` answers only `{"ok": true}` and nothing else: on loopback it
shows model names, cache size and job state — this is a map of someone else's
machine, and not the whole home Wi-Fi needs to know it.

## Open on the tablet

In the tablet's browser type the computer's address and port:

```
http://<computer address>:8756/
```

The address — the one the computer has on the home network (`hostname -I` on the computer
will show it first). This way the page can be viewed even without any installation.

## App and “Share” from YouTube

For YouTube itself to offer our page among “Share”, it must be installed on the tablet once
as an app. Android lets only installed apps into this dialog, and Chrome installs a site
only over https: it considers plain http insecure. Therefore the daemon needs a certificate, and its own:
a home address like `192.168.*` does not belong to a public certificate authority, and a regular
certificate cannot be issued for it.

The certificate is issued by a script: its own root CA plus a certificate for the addresses of this
machine (all except container ones):

```sh
packaging/make-tls.sh
```

The script puts the files in `~/.config/yt-trans/tls/` and prints what to do next.
Order matters:

1. **While the daemon is still on http** — open on the tablet
   `http://<computer address>:8756/ca.crt` and install the downloaded file:
   Settings → Security → Encryption & credentials → Install a certificate
   → CA certificate. Android will ask for confirmation — this is your
   own CA, issued on this computer.
2. Add to `~/.config/yt-trans/config.toml`:

   ```toml
   [server]
   host = "0.0.0.0"
   tls_cert = "~/.config/yt-trans/tls/server.crt"
   tls_key = "~/.config/yt-trans/tls/server.key"
   ```

   and restart the daemon: `systemctl --user restart yt-trans`.
3. Open on the tablet `https://<computer address>:8756/` — the address bar without
   warnings. Chrome menu → “Install app” (on older versions —
   “Add to Home screen”).
4. In the YouTube app tap “Share” → **yt-trans**. Our
   page opens, the link is added to the saved ones, and if this is a single video, it
   immediately starts playing. Tapping “Share” again with a playlist opens the list of
   videos.

You can check that the installation succeeded in Chrome: menu → “Apps” (or the
icon on the home screen). In the share dialog itself the app appears immediately
after installation; if it is not there — then step 3 was not done or the page is
opened over http.

You can revoke trust in the certificate where it was installed: Settings →
Security → Encryption & credentials → User certificates.

## Computer

With the certificate enabled, the daemon listens only over https, and the extension on the
computer switches to it together with the tablet: in the extension settings
choose the **https** protocol. The “Save and check the connection” button itself tries the
second protocol if the selected one is silent, and remembers the working one. The computer's browser
must also trust the certificate, otherwise the request will fail on distrust of the
certificate, not on a daemon refusal. `ca.crt` from `~/.config/yt-trans/tls/`
is installed into the browser's certificate store:

- Chrome: chrome://settings/certificates → “Authorities” → “Import”.
- Firefox and Zen: Settings → Privacy & Security → Certificates →
  “View Certificates” → “Authorities” → “Import”, check
  “Trust this CA to identify websites”.


## Links

On the home screen — a link field and a list of saved ones. Only YouTube addresses are saved:
the daemon will not accept a foreign address, because it downloads from YouTube.
A playlist expands into a list of videos with title, duration and marks:
floppy disk — a copy already lies on the device and will play immediately, even without the computer,
spinner — the picture is downloading, checkmark — the translation is already done and there is nothing to wait for. “Did not
download” — try again, “new” — the video has not been prepared yet and is waiting its
turn. Icons in the list differ by color: video and trash — red, floppy disk —
blue, checkmark — green, playlist — gray. The duration on the row is the one the daemon
remembered when the link was added; a playlist has no single number, and records
added earlier do not have it either — then the time is not shown. A single video
opens immediately, without a list of one item. From a video, the usual browser
“back” button returns.

On the stage, top left — back to list, **⛶**, **cache** and a line about the work
progress; top right — RU and CNR line toggles and the subtitle size list; in the middle — the video
title, one line in the center. The work-progress line is one, it does not lie over the frame,
and different messages in it are separated by dots **•**: what is being prepared, in what
language the speech is, what is wrong. The bar with it and the video title fade together with the native
player controls — 2.5 seconds after the last touch, while the video is playing, —
and come back on touch or mouse movement. On pause they are visible, as are the controls:
there is nothing to fade then.

## Why the video is prepared

YouTube does not serve a ready mp4: picture and sound lie separately. The daemon downloads
H.264 no higher than 1080p and AAC audio, glues them into mp4 and puts them on disk — this is about
2 Mbit/s, that is, about 400 MB per 26 minutes. While the gluing is going on, the page writes
“preparing the picture…”; the ready video starts playing by itself. The profile was chosen because it plays
everywhere, including iOS and old devices: above 1080p YouTube serves only VP9 and AV1,
and not every tablet opens them.

## Disk

Ready videos lie in `~/.cache/yt-trans/video` and survive a daemon restart.
The ceiling is 20 GiB: when the cache grows beyond it, the daemon deletes the oldest files,
never touching the one downloaded last. Nothing needs to be cleaned separately;
if you still regret the space, the files can be deleted by hand — the next viewing
will download the video again.

The directory and ceiling are set in the config:

```toml
[video]
dir = "/tmp/yt-trans-video"
max_gib = 4
```

`/tmp` is tmpfs, that is, RAM: videos are not written to disk at all,
but after a reboot the cache is empty, and the first viewing downloads again. The ceiling there
is set by the memory reserve, not by the free space on disk.

## Without the computer

A video can be taken to the tablet — together with subtitles. For this, on the stage at the top left,
next to **⛶**, there is a **cache** button; by default it is off, and until it is pressed nothing
settles on the device. Pressed, it starts copying as soon as the daemon
has assembled the mp4, and copying goes on while the page is open: you closed the app or the screen
fell asleep — the download stopped, and the button must be pressed again. While the download is going on, the button
shows a percentage, and at the end — a floppy disk: the copy lies on the device.

On the device remain the picture and those lines that managed to be translated by the end of the download;
subtitles continue to be appended while you watch, and the record is supplemented along the way. In
the list the saved one is marked with a floppy disk next to the name and the line `on device · 400 MB`,
and is deleted with the **delete** button — space on the tablet is not infinite.

The copy also plays with the daemon alive: the picture is taken from the device, so a connection drop
in the middle of viewing no longer interferes. When there is no daemon at all, the home page
shows the saved ones — with open links the videos expand under their link,
and links without copies give a message that there is no daemon, instead of an empty screen.

The cache lives in the tablet browser's storage, but not in the daemon's cache: this is a separate place,
its own for each tablet, and “clear site data” in the browser erases it entirely.
It occupies its own space entirely, not according to the common ceiling from the daemon config.

## If it doesn't work

| What is visible | Cause | What to do |
|---|---|---|
| the page does not open at all | the daemon listens on loopback, or linger is not enabled | “Enable the network” above, then `systemctl --user restart yt-trans` |
| empty screen | the page did not load fully or the browser keeps an old copy | refresh the page; cache — “clear site data” |
| “preparing the picture…” does not end | the download failed or is waiting its turn | `journalctl --user -u yt-trans -f` — the cause is there |
| “the video did not download” | YouTube refused: age, deleted video, network | retry later; 18+ videos require an account, see [`README.md`](../README.md) |
| 403 in the browser instead of the page | the request came from a foreign origin | open the page at the daemon's address, not from a foreign tab |
| Chrome says “Connection is not secure” | `ca.crt` is not installed on the tablet, or the certificate was issued for the wrong address | reissue `packaging/make-tls.sh` with the address you open the page by, and install the new `ca.crt` |
| yt-trans is missing in “Share” from YouTube | the page is opened over http or not installed as an app | the section “App and ‘Share’ from YouTube” |
| “no daemon, this video is not on the device” | the video is not saved on the tablet | enable **cache** on the next viewing with the daemon alive |
| “the copy was not saved: …” | ran out of space or the browser refused | free up space, delete what is not needed with the **delete** button |
