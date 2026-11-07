# yt-trans

Russian subtitles for Serbian YouTube videos with local models. A browser
extension shows subtitles over the player, and the work is done by a daemon on the same
machine: it takes the audio via yt-dlp, recognizes it with Whisper, translates with an ollama model.
The second line is the Montenegrin text of the same utterance, for language learning.

Daemon settings — `~/.config/yt-trans/config.toml`, a sample with explanations —
[config.example.toml](config.example.toml). The daemon runs as a user
systemd service: [packaging/yt-trans.service](packaging/yt-trans.service).

## Page for the tablet

The daemon also serves a page with a player: on the tablet in another room the same two lines
run over the video without an extension and without the YouTube player, and subtitles are not
burned into the picture. How to enable the network and what to do with the disk —
[docs/tablet.md](docs/tablet.md).

## Account for 18+ videos

YouTube serves an age-restricted video only to a signed-in user.
The daemon talks to YouTube anonymously, and without an account such videos are not translated:
the viewer sees the refusal “the video is 18+: … no account is connected for such videos”.

The account is substituted only where there is no way around it. Each video the daemon
first requests anonymously, and only if YouTube refused specifically because of the age
restriction, repeats the request with the account — then the whole video goes
under it. All other videos do not involve the account.

### A separate account only

Do not use your main account here. yt-dlp warns directly: the account
through which it talks can be banned by YouTube, temporarily or permanently
([Exporting YouTube cookies](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)).
You need an account you don't mind losing.

### Connecting

1. **Create an account and confirm the age.** You can check in a browser:
   sign in with this account and open any 18+ video. If YouTube asks you to
   confirm the age — confirm it. The video must play.

2. **Export cookies from a private window.** YouTube regularly reissues cookies for open
   tabs, and those exported from a regular window quickly stop
   working. So this way:
   1. Open a private window and sign in to this account on youtube.com.
   2. In the same tab open <https://www.youtube.com/robots.txt>. There must be no other
      private tabs.
   3. Export youtube.com cookies to a Netscape-format file with an extension
      that is allowed to work in private mode. yt-dlp
      [advises](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)
      “Get cookies.txt LOCALLY” for Chrome and “cookies.txt” for Firefox.
      The first line of the file is `# Netscape HTTP Cookie File`.
   4. Close the private window without going anywhere else in it.

3. **Put the file and close access.** The file contains the account sign-in:

   ```sh
   mv <exported file> ~/.config/yt-trans/youtube-18.txt
   chmod 600 ~/.config/yt-trans/youtube-18.txt
   ```

   The file must be writable by the daemon: after each call yt-dlp
   appends updated cookies to it, and the session lives longer because of that.

4. **Add to `~/.config/yt-trans/config.toml`:**

   ```toml
   [youtube]
   cookies = "~/.config/yt-trans/youtube-18.txt"
   ```

5. **Restart the daemon** — settings are read only at startup:

   ```sh
   systemctl --user restart yt-trans
   ```

### Verification

Open an 18+ video and enable translation. The daemon log should show
a line about switching to the account:

```sh
journalctl --user -u yt-trans -f | grep '18+'
```

```
… helper.jobs <video id>: 18+ video, going for it with the account from the settings
```

### If it doesn't work

| What the viewer sees | Cause | What to do |
|---|---|---|
| “the video is 18+: … no account is connected for such videos” | the `[youtube]` section is missing or the daemon was not restarted | steps 4–5 |
| “the video is 18+, and the cookies file from the settings is missing: …” | wrong path in `cookies` | fix the path, restart the daemon |
| “the video is 18+, and the account from the settings did not open it on YouTube” | the cookies are stale or the account's age is not confirmed | check step 1, export the cookies again (steps 2–3), restart the daemon |

If the path is set but the file is missing, the daemon warns about it already at startup:
`[youtube] cookies: file … does not exist, 18+ videos will not open`.

All these refusals are final: the daemon does not repeat them itself, so as not to hit
YouTube under the account once a minute while the tab is open. Therefore after
fixing, a daemon restart is needed — without it, a video that has already received a refusal
will stay that way.
