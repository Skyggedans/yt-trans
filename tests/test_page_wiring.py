"""Page wiring: every element app.js looks for exists in the markup."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ids_the_page_looks_for():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    return set(re.findall(r'getElementById\("([^"]+)"\)', source))


def stage_bar(html):
    """The stage's top bar together with the nested blocks: status and title."""

    start = html.index('id="stage-bar"')

    return html[start:html.index("</section>", start)]


def test_the_page_looks_for_something():
    assert ids_the_page_looks_for()


def test_every_element_the_page_looks_for_exists():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    present = set(re.findall(r'id="([^"]+)"', html))

    assert ids_the_page_looks_for() - present == set()


def test_the_video_carries_its_own_controls():
    # The browser does not allow starting sound outside a press: autoplay after
    # the daemon's answer is rejected silently, and without built-in buttons the
    # viewer is left with a black frame and no way to do anything.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    tag = re.search(r"<video[^>]*>", html)

    assert tag is not None
    assert "controls" in tag.group(0)


def test_the_layer_repaints_when_cues_arrive():
    # A pause where the translation is awaited is the page's main scenario.
    # While no frames run, timeupdate is silent, so a new translation must
    # repaint the layer by itself.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleMessage"):source.index("function takeSnapshot")]

    assert "paint()" in body


def test_the_viewer_can_switch_each_line_off():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert 'id="toggle-target"' in html
    assert 'id="toggle-study"' in html


def test_the_layer_obeys_the_switches():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function paint"):source.index("function startClock")]

    assert "visibleLines" in body


def test_the_home_screen_starts_with_the_link_field_not_the_app_name():
    # The heading "yt-trans" above the list took a line and said nothing: the
    # page is already open, and the first thing needed is a field for a link.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert "<h1" not in html
    assert "yt-trans</h1>" not in html


def test_the_stage_is_left_by_the_browser_history():
    # The episode list is the same address: without pushState and popstate,
    # browser history would take you off the page entirely.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "history.pushState" in source
    assert "popstate" in source


def test_the_stage_goes_back_to_the_list_by_its_own_arrow():
    # On a tablet there is nowhere to look for the browser's "back" button, and
    # leaving the stage is the most frequent action. The arrow stands first in
    # the bar, before "fullscreen".
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = stage_bar(html)

    assert 'id="back"' in bar
    assert bar.index('id="back"') < bar.index('id="fullscreen"')

    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    # The return goes the same way as the browser button: the history step laid
    # on entering the video is removed rather than left hanging.
    assert 'els.back.addEventListener("click"' in source
    assert "history.back()" in source


def test_the_title_is_attached_to_the_back_arrow_on_the_left():
    # On the left of the bar only the arrow and the video's title remain: the
    # title is what the viewer came for, so it sits right next to the way back.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = stage_bar(html)

    assert 'id="title"' in bar
    assert bar.index('id="back"') < bar.index('id="title"')
    assert bar.index('id="title"') < bar.index('id="stage-actions"')

    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")
    title = re.search(r"#title \{(.*?)\}", css, re.S).group(1)

    assert "text-align: left" in title
    assert "white-space: nowrap" in title
    assert "text-overflow: ellipsis" in title
    # The title no longer hangs over the frame: its place is in the top bar.
    assert "position: absolute" not in title
    assert "bottom:" not in title


def test_the_buttons_and_the_status_are_pinned_to_the_right():
    # The whole-frame buttons and the status text live on the right: the left
    # half of the bar belongs to the way back and the title.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = stage_bar(html)
    actions = bar[bar.index('id="stage-actions"'):]

    assert 'id="fullscreen"' in actions
    assert 'id="cache"' in actions
    assert 'id="status"' in actions
    # The buttons keep the outer edge, so their place does not move with the
    # length of a message; the status grows to the left of them.
    assert actions.index('id="status"') < actions.index('id="fullscreen"')

    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#stage-bar \{[^}]*display: flex", css, re.S)
    assert re.search(r"#stage-actions \{[^}]*margin-left: auto", css, re.S)


def test_the_players_fullscreen_turns_into_the_whole_stage():
    # The native fullscreen mode expands the <video> itself, while the line
    # layer is a neighbouring element: without switching to the stage the
    # subtitles are not drawn there at all.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "fullscreenchange" in source
    assert "requestFullscreen" in source


def test_the_players_own_fullscreen_button_is_hidden():
    # It expands the bare <video>, while the line layer is a neighbouring
    # element and is not drawn in that mode. Our own button expands the whole
    # stage.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    tag = re.search(r"<video[^>]*>", html)

    assert tag is not None
    assert 'controlslist="nofullscreen"' in tag.group(0)


def test_the_stage_goes_fullscreen_by_the_pages_own_button():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'id="fullscreen"' in html
    assert "els.stage.requestFullscreen" in source


def test_the_page_is_installable_as_an_app():
    # Android shows the app in the share dialog only when installed, and
    # installation requires a manifest with a name, a launch screen and icons.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "page" / "manifest.json").read_text(encoding="utf-8"))

    assert 'rel="manifest"' in html
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert {"192x192", "512x512"} <= {icon["sizes"] for icon in manifest["icons"]}


def test_the_manifest_icons_are_really_there():
    manifest = json.loads((ROOT / "page" / "manifest.json").read_text(encoding="utf-8"))

    for icon in manifest["icons"]:
        assert (ROOT / "page" / icon["src"].lstrip("/")).is_file(), icon["src"]


def test_the_share_sheet_opens_the_page_itself():
    manifest = json.loads((ROOT / "page" / "manifest.json").read_text(encoding="utf-8"))
    target = manifest["share_target"]

    assert target["action"] == "/"
    assert target["method"] == "GET"
    assert target["params"] == {"title": "title", "text": "text", "url": "url"}


def test_a_shared_link_is_added_like_a_typed_one():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "sharedUrl(new URLSearchParams(location.search))" in source
    assert 'api("/v1/links", { method: "POST"' in source
    # Otherwise reloading the page would add the same link a second time.
    assert "history.replaceState" in source


def test_the_saved_links_can_be_searched_by_name():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'id="links-filter"' in html
    assert "filterLinks(" in source
    assert 'els.linksFilter.addEventListener("input"' in source


def test_the_search_knows_the_names_of_the_videos_too():
    # A playlist's title does not say what is inside: one usually searches for
    # a video, and a link with a matching video must stay in the list.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    logic = (ROOT / "page" / "logic.js").read_text(encoding="utf-8")

    assert "filterLinks(items, linksQuery, videosFor)" in source
    body = source[source.index("function videosFor"):source.index("function drawLinks")]
    # Known names — from the read playlist and from the copies on the device.
    assert "linkVideos.get(" in body
    assert "savedForLink(" in body
    # A match inside a playlist expands the card: otherwise what was found
    # would stay hidden under a collapsed row.
    card = source[source.index("function linkCard"):source.index("function copyLine")]
    assert "matchesQuery(" in card
    # A card opened by search can be collapsed again: a chevron that does
    # nothing looks like a defect.
    helper = source[source.index("function openedBySearch"):source.index("function linkCard")]
    assert "searchClosed" in helper
    # The playlist videos' names have not been read yet — they are requested
    # for the duration of the search. The walk goes over all links, not over the
    # visible ones: a link without a title match has already been filtered out,
    # and nobody would ask for its videos.
    drawn = source[source.index("function drawLinks"):source.index("function linkCard")]
    assert "loadVideos(link)" in drawn
    assert "for (const link of items)" in drawn
    # While the videos are being read, "nothing found" would be a lie.
    assert "searching the videos" in drawn


def test_every_header_shows_what_kind_of_thing_it_is():
    # Before the title — a type icon: a video is recognised by YouTube's
    # "television", a playlist by a list with a triangle. In words this would
    # take room that is already scarce on a tablet.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    icons = source[source.index("const ICONS"):source.index("function iconMarkup")]
    assert "video:" in icons
    assert "playlist:" in icons

    assert "function titledButton(" in source
    card = source[source.index("function linkCard"):source.index("function copyLine")]
    assert "const playlist = link.kind ===" in card
    assert 'playlist ? "playlist" : "video"' in card
    video = source[source.index("function videoCard"):source.index("function chevronButton")]
    assert re.search(r'titledButton\(\s*"video"', video)

    # The icon and the text stand on one line and centred, otherwise the title jumps.
    assert re.search(r"#links \.title-icon \{[^}]*display: inline-flex", css, re.S)


def test_a_picture_on_disk_is_a_floppy_and_a_downloading_one_spins():
    # "Ready" as a word repeated in every row of the list. A check would say
    # "everything is ready", although it meant only a copy — so a copy is
    # labelled by a floppy, and while the picture is downloading a spinner turns
    # in its place: the motion is visible without a word.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    icons = source[source.index("const ICONS"):source.index("};", source.index("const ICONS"))]
    assert "disk:" in icons
    assert "spinner:" in icons
    assert 'preparing: "spinner"' in source
    assert 'ready: "check"' not in source
    words_at = source.index("const VIDEO_STATE_WORDS")
    words = source[words_at:source.index("};", words_at)]
    assert "ready" not in words
    assert "preparing" not in words

    marks = source[source.index("function iconStateMark"):source.index("function translationMark")]
    assert "iconMarkup" in marks
    assert "VIDEO_STATE_ICONS" in marks

    card = source[source.index("function linkCard"):source.index("function copyLine")]
    assert "videoMarks(link," in card

    video = source[source.index("function videoCard"):source.index("function chevronButton")]
    assert "videoMarks(video," in video

    assert re.search(r"#links \.state-mark \{[^}]*display: inline-flex", css, re.S)
    assert re.search(r"\.state-mark\.spin \.icon \{[^}]*animation", css, re.S)
    assert "@keyframes spin" in css


def test_the_floppy_means_a_copy_on_the_device_not_the_daemons_cache():
    # The viewer does not care about the server cache: the floppy answers the
    # question "what will play even without the computer". That is why the copy
    # on the device sets it, and the finished picture on the server draws no
    # icon at all any more.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'ready: "disk"' not in source

    marks = source[source.index("function videoMarks"):source.index("function episodeLabel")]
    assert "onDevice" in marks
    assert "copyMark()" in marks
    assert "stateMark(video.state)" in marks

    copy = source[source.index("function copyMark"):source.index("function stateMark")]
    assert 'iconStateMark("disk"' in copy
    assert "copy on the device" in copy

    card = source[source.index("function linkCard"):source.index("function copyLine")]
    assert "videoMarks(link, saved.length > 0)" in card

    video = source[source.index("function videoCard"):source.index("function chevronButton")]
    assert "videoMarks(video, Boolean(saved))" in video


def test_a_finished_translation_is_a_check():
    # The check answers a different question: the translation is already done
    # and there is nothing to wait for. No translation — no icon either: its
    # absence promises nothing.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    mark = source[source.index("function translationMark"):source.index("function episodeLabel")]
    assert 'iconStateMark("check"' in mark
    assert "translated" in mark
    assert "translation ready" in mark

    marks = source[source.index("function videoMarks"):source.index("function episodeLabel")]
    assert "stateMark(video.state)" in marks
    assert "translationMark(video.translated)" in marks


def test_an_untouched_video_is_marked_new():
    # "new" — nothing has been done to the video yet: neither a picture nor a
    # translation. As soon as even one mark appears, the icon disappears by
    # itself.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    mark = source[source.index("function newMark"):source.index("function episodeLabel")]
    assert '"new"' in mark
    assert "aria-label" in mark

    marks = source[source.index("function videoMarks"):source.index("function episodeLabel")]
    assert 'video.state === "absent"' in marks
    assert "!video.translated" in marks
    assert "newMark()" in marks

    assert re.search(r"#links \.new-mark", css)


def test_the_list_rereads_the_picture_state_on_the_way_back():
    # The picture is being prepared while the viewer is on the stage: on return
    # the list must show the fresh readiness, not the one from when the app was
    # opened.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function leaveStage"):source.index("async function registerWorker")]

    assert "loadLinks(" in body

    # The copy could have been deleted in another tab or via DevTools: the
    # floppy is now set by the copy, and the list of copies in memory must be
    # reread, otherwise it would promise "on the device" what is no longer there.
    assert "openCache()" in body
    assert "drawLinks(knownLinks" in body


def test_cards_start_collapsed_every_time():
    # Everything is collapsed by default: expanding is an action for this
    # viewing, not a device setting.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "yt-trans-expanded" not in source
    assert "parseExpanded(" not in source
    assert re.search(r"let expandedKeys = \[\]", source)


def test_the_saved_links_scroll_while_the_fields_stay_in_place():
    # The link field and search must stay in place: on a tablet with a hundred
    # saved ones, page scrolling would carry them off the screen.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    library = re.search(r'<div id="library">(.*?)</div>', html, re.S)

    assert library is not None
    assert 'id="links"' in library.group(1)
    assert 'id="links-filter"' not in library.group(1)
    assert re.search(r"#home \{[^}]*height: 100dvh", css, re.S)
    assert re.search(r"#library \{[^}]*overflow-y: auto", css, re.S)


def test_the_viewer_picks_the_subtitle_size():
    # From a tablet in another room 22px is not readable, and on a phone 34px
    # does not fit. The size is chosen from several options rather than guessed
    # from the screen width.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert 'id="font-size"' in html
    for size in ("tiny", "small", "normal", "large"):
        assert f'value="{size}"' in html
    assert 'value="huge"' not in html


def test_the_chosen_size_survives_a_reload_and_shapes_the_layer():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert "readFontSize(" in source
    assert "fontSize" in source
    assert 'addEventListener("change"' in source
    # The size is set on the stage by a flag: the layer itself is in the shadow
    # root, and the rules for it live in style.css next to the rest of the line
    # look.
    assert re.search(r"#stage\[data-font=", css)
    assert re.search(r'#stage\[data-font="tiny"\]', css)
    assert 'data-font="huge"' not in css
    # The lines are drawn in the shadow root: external rules do not get inside,
    # so the multiplier must stand in LAYER_CSS next to var(--font).
    layer = source[source.index("const LAYER_CSS"):source.index("function layerNodes")]
    assert "var(--font" in layer
    assert "--overlay-scale" in layer


def test_the_stage_has_a_cache_switch_next_to_fullscreen():
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = stage_bar(html)

    assert 'id="cache"' in bar
    assert 'id="fullscreen"' in bar
    # By default the cache is off: without a press nothing settles on the device.
    assert 'aria-pressed="false"' in bar


def test_the_cache_switch_is_an_icon_that_says_the_state_or_the_size():
    # On the stage the "cache" caption argued with the frame. Downloading is an
    # icon; when the copy lies on the device, the floppy stays with its size on
    # the button.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function drawCacheButton"):source.index("function toggleCache")]

    assert "ICON_DOWNLOAD" in body
    # A ready copy is the same floppy as in the list: a copy on the device. A
    # check would mean a translation, and two different icons would speak of the
    # same thing.
    assert "saved ? ICON_DISK : ICON_DOWNLOAD" in body
    assert "ICON_CHECK" not in body
    # A ready copy writes its size next to the floppy: the same megabytes the
    # list shows, so the viewer does not go back to the list to check.
    assert "formatBytes(saved.size)" in body
    # The state still comes from logic.js: percentages, waiting, failure.
    assert "copyState(" in body
    # An icon without a word: the name for the finger and the hint change
    # together with the copy.
    assert "copy on the device" in body
    assert "cancel copying" in body


def test_the_cache_switch_copies_the_rolled_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    logic = (ROOT / "page" / "logic.js").read_text(encoding="utf-8")

    assert 'els.cache.addEventListener("click"' in source
    for name in (
        "copyState(", "copyVerdict(", "formatBytes(", "percent(", "mergeCues(",
        "SPARE_BYTES", "downloadVideo(", "putVideo(", "freeBytes(", "askPersist(",
    ):
        assert name in source, name
    # The icon is drawn by the page, and the caption for it — still logic.js.
    assert "export function copyState" in logic
    # A repeated press during copying cancels it and removes what was not fully
    # downloaded.
    assert "controller.abort()" in source
    assert "copy = null" in source


def test_the_base_is_named_in_one_place():
    # The database and table names live in cache.js; the page does not repeat
    # them, otherwise a single rename would have to be hunted down in two files.
    cache = (ROOT / "page" / "cache.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'DB_NAME = "yt-trans"' in cache
    assert '"yt-trans"' not in source
    assert "openDb(" in source


def test_the_copy_keeps_the_rolled_video_subscribed():
    # Leaving the stage with a copy in progress — the subtitles must keep being
    # written further, otherwise what was saved would break off at the moment of
    # leaving.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function leaveStage"):source.index("// --- Launch")]

    assert body.index("copy") < body.index("unwatchMessage")


def test_the_copy_catches_cues_its_own_video():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function handleMessage"):source.index("function takeSnapshot")]

    assert "rememberCues(" in body
    assert body.index("rememberCues(") < body.index("isForVideo(")


def test_the_saved_cues_keep_arriving_after_the_picture_is_stored():
    # The translation takes longer than the download: by the end of the
    # translation both the picture and all the lines must lie on the device, not
    # only those that made it by the end of the download.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "let follow = null" in source
    assert "followSoon(" in source
    assert "FOLLOW_MS" in source


def test_the_stage_bar_holds_both_buttons():
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#stage-bar \{[^}]*display: flex", css, re.S)


def test_the_copy_confirmation_does_not_repeat_the_floppy():
    # The floppy with megabytes already says the video is on the device, and
    # the status line stays for what changes.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'note("saved on the device")' not in source


def test_the_status_is_one_bulleted_line_in_the_top_bar():
    # The status hung over the frame in separate lines and argued with the
    # subtitles. Now it is in the top bar next to the buttons, and dots divide
    # the messages: "preparation is under way", "what is wrong", "the video's
    # language" read as one.
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")
    bar = stage_bar(html)

    assert 'id="status"' in bar

    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    render = source[source.index("function render()"):source.index("// --- Subtitle layer")]

    assert 'join(" • ")' in render
    assert "replaceChildren" not in render


def test_the_bar_and_the_title_hide_like_the_players_controls():
    # The bar with the status and the title fade after the same 2.5 seconds of
    # rest as the player's own controls, and come back on a touch. On pause they
    # stay: the controls do not disappear on pause either.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert "CONTROLS_IDLE_MS = 2500" in source
    idle = source[source.index("function wakeStage"):source.index("function leaveStage")]
    assert "paused" in idle
    assert "setTimeout" in idle
    assert 'classList.add("idle")' in idle
    assert 'classList.remove("idle")' in idle
    # A touch, the mouse and a key bring the bar back: nobody will wait for it.
    assert "pointermove" in source
    assert "pointerdown" in source
    assert "keydown" in source
    assert re.search(r"#stage\.idle #stage-bar", css)
    assert re.search(r"#stage\.idle #title", css)


def test_the_list_icons_are_told_apart_by_colour():
    # Colour answers before the word: the check is green, the trash red, the
    # floppy blue, a YouTube video red, the playlist stays quiet grey.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")
    root = re.search(r":root \{(.*?)\}", css, re.S).group(1)

    assert "--ok:" in root
    assert "--alert:" in root
    assert "--copy:" in root

    # The icon knows whose it is: the class comes from the icon's name, not from
    # its place in the list.
    assert 'mark.className = `title-icon is-${icon}`' in source
    assert 'mark.className = `state-mark is-${icon}`' in source
    assert "trashButton(" in source

    def colour(selector):
        block = re.search(re.escape(selector) + r" \{(.*?)\}", css, re.S)
        assert block is not None, selector
        found = re.search(r"color: var\((--[a-z]+)\)", block.group(1))
        assert found is not None, selector

        return found.group(1)

    assert colour("#links .title-icon.is-video") == "--alert"
    assert colour("#links .title-icon.is-playlist") == "--muted"
    assert colour("#links .icon-only.trash") == "--alert"
    assert colour("#links .state-mark.is-check") == "--ok"
    assert colour("#links .state-mark.is-disk") == "--copy"


def test_a_saved_video_plays_from_the_device():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "URL.createObjectURL" in source
    assert "URL.revokeObjectURL" in source
    assert "switchToCopy(" in source
    assert "playLocal(" in source
    assert "playSource(" in source


def test_the_copy_carries_its_cues_without_the_daemon():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "seedCues(" in source
    assert "mergeCues(" in source
    assert "no daemon — playing the saved copy" in source


def test_offline_says_why_a_rolled_video_does_not_play():
    # Spec: without a copy the stage does not open offline at all — the message
    # stays on the home page, so that a black screen does not pretend to be a
    # breakdown.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function play("):source.index("async function createJob")]

    assert "no daemon, this video is not on the device" in body
    assert body.index("playSource(") < body.index("els.home.hidden = true")


def test_an_evicted_copy_does_not_lie_in_the_list():
    # The browser is free to evict a Blob: there is a record in the database,
    # nothing to play.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "savedVideos.delete(" in body
    assert "deleteVideo(" in body


def test_the_home_stays_with_the_saved_copy_when_the_daemon_is_gone():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function loadLinks"):source.index("function savedForLink")]

    # A silent daemon is not a refusal: the previous list is drawn at once, and
    # the request to the daemon gets a short deadline and replaces it when it
    # answers.
    assert "AbortSignal.timeout(LINKS_TIMEOUT_MS)" in body
    assert 'getMeta(db, "links")' in body
    assert body.index("getMeta") < body.index("api(")
    # Offline is "the daemon did not answer at all", not every failed response:
    # the difference is decided by api(), by setting daemonAlive.
    assert "offline = !daemonAlive" in body
    assert "savedForLink(" in source
    assert "on the device" in source


def test_a_saved_video_is_marked_in_the_list_and_can_be_deleted():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "cacheLabel(" in source
    assert "deleteVideo(" in source
    assert "dropCopy(" in source
    # Deleting is a trash: a word in every row took room from what the row is
    # read for (runtime, status, size).
    assert "ICON_TRASH" in source
    assert 'trashButton("delete copy", () => dropCopy(record))' in source


def test_a_saved_row_does_not_repeat_the_title():
    # The copy row stands in the card of its video, and the title has already
    # been drawn above. Repeating it means taking room from what the row is read
    # for: how much lies on the device and how much room is left there.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    rows = source[source.index("function copyLine"):source.index("function playlistChildren")]
    label = source[source.index("function savedLabel"):source.index("async function dropLink")]

    assert "episodeLabel(" not in rows
    assert "savedLabel(" in rows
    assert "formatTime(" in label
    assert "cacheLabel(" in label
    assert "title" not in label


def test_the_link_row_carries_the_length_of_its_only_copy():
    # The runtime moved from the copy row to the title: with a single copy it is
    # the video's runtime too, and there is no point repeating it twice. With
    # several copies the link title is shared, and the runtime stays with each
    # copy.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    logic = (ROOT / "page" / "logic.js").read_text(encoding="utf-8")

    assert "export function linkTitle(" in logic
    assert "linkTitle(" in source
    body = source[source.index("function linkCard"):source.index("function copyLine")]
    assert "saved.length === 1" in body


def test_a_card_with_children_opens_by_a_chevron_after_the_trash():
    # The chevron is the header's last button: the trash first, then it.
    # Otherwise expanding would stand between "delete" and the title, and the
    # finger would reach for deletion while expanding the card.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function linkCard"):source.index("function copyLine")]

    assert "ICON_CHEVRON" in source
    assert body.index("trashButton(") < body.index("chevronButton(")
    assert 'aria-expanded' in source
    # Expanding survives a redraw: otherwise every new copy would collapse the
    # list right under the finger. It does not survive a reload — there
    # everything is collapsed.
    assert "toggleExpanded(" in source


def test_a_playlist_keeps_its_videos_inside_the_card():
    # A playlist expands into its videos, and a video's copy is its child row.
    # A separate episodes screen is no longer needed for this.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "page" / "index.html").read_text(encoding="utf-8")

    assert "function playlistChildren" in source
    assert 'kind === "playlist"' in source
    assert "linkVideos" in source
    assert "function videoCard" in source
    assert "expandedKeys.includes(videoKey(" in source
    assert 'id="episodes"' not in html


def test_the_playlist_trash_takes_its_copies_with_it():
    # The card's trash removes both the link and everything under it: otherwise
    # a deleted playlist would leave its videos on the device with no way to
    # find them.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function dropLink"):source.index("async function dropCopy")]

    assert "savedForLink(link.id)" in body
    # Copies are forgotten the same way as a single one: the database and the
    # list must not diverge because of two different deletion paths.
    assert "forgetCopy(record.videoId)" in body
    assert "deleteVideo(" in source[source.index("async function forgetCopy"):source.index("async function dropLink")]


def test_every_trash_asks_before_deleting():
    # A copy is hundreds of megabytes on the tablet, and a link is a record in
    # the list; one wrong touch must not carry them off.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    for start, end in (
        ("async function dropLink", "async function dropCopy"),
        ("async function dropCopy", "async function addLink"),
    ):
        body = source[source.index(start):source.index(end)]

        assert "confirm(" in body, start


def test_the_link_and_its_copy_are_one_card():
    # Separate plates read as two different list items: it was unclear that the
    # lower copy belonged to the row above. One card — one link.
    css = (ROOT / "page" / "style.css").read_text(encoding="utf-8")

    assert re.search(r"#links li\.card \{[^}]*flex-direction: column", css, re.S)
    assert re.search(r"#links \.children \{[^}]*border-top", css, re.S)
    assert re.search(r"#links \.link-line button[^}]*background: transparent", css, re.S)


def test_every_row_in_the_list_deletes_with_a_trash_icon():
    # "Delete" as a word took half the row, and the trash was already there by
    # the copy: two different buttons of the same kind guarantee that the wrong
    # one is pressed.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'button("Delete"' not in source
    assert 'trashButton("delete link", () => dropLink(link))' in source
    assert 'trashButton("delete copy", () => dropCopy(record))' in source


def test_a_shared_link_does_not_wipe_the_saved_home():
    # With a dead daemon, sharing from YouTube must not leave an empty screen:
    # what is saved with its marks matters more than a failed addition.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function consumeShare"):source.index("function row(")]

    assert "drawLinks(knownLinks" in body


def test_the_shell_is_kept_for_the_day_the_computer_is_off():
    worker = (ROOT / "page" / "sw.js").read_text(encoding="utf-8")
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'serviceWorker.register("/sw.js")' in source
    assert "registerWorker()" in source
    for name in ("/app.js", "/logic.js", "/cache.js", "/style.css", "/manifest.json"):
        assert name in worker
    # The daemon's live data and the certificate are not the shell.
    assert 'startsWith("/v1/")' in worker
    assert "/ca.crt" in worker


def test_a_browser_without_a_worker_says_so_once():
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "workerMissing = true" in source
    assert '"yt-trans-no-worker"' in source
    assert "will not keep the page for offline" in source


def test_a_broken_answer_is_not_taken_for_an_empty_daemon():
    # The response body may break off (the daemon died, the deadline passed),
    # and then the page must not take the break for "nothing there": an empty
    # list would wipe the links saved on the device.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    cache = (ROOT / "page" / "cache.js").read_text(encoding="utf-8")

    assert ".json().catch" not in source
    assert "readJson(" in source
    assert "export async function readJson" in cache

    body = source[source.index("async function loadLinks"):source.index("function savedForLink")]

    assert "Array.isArray(data.links)" in body
    # An answer without a list goes to refusal before the list could land on the
    # device: otherwise one crooked answer would replace the saved links with
    # emptiness.
    assert body.index("if (!Array.isArray(data.links))") < body.index('putMeta(db, "links"')


def test_a_failed_request_does_not_wipe_the_rows_already_drawn():
    # A failed request is no reason to wipe the home: the links are already
    # drawn (from the device or by the previous answer), and the error row
    # stands above them. By drawing an empty list instead, the page left only
    # the saved copies on the screen, and the video rows disappeared, although
    # the links themselves had not gone anywhere.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert re.search(r"drawLinks\(\s*\[\s*\]", source) is None
    assert "drawLinks(last && last.length ? last : knownLinks" in source


def test_an_offline_home_does_not_wait_for_a_sleeping_daemon():
    # A sleeping computer does not answer for minutes: an action with a known
    # dead daemon must say at once that there is no daemon rather than hang.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    for name in ("addLink", "dropLink", "openLink", "consumeShare"):
        start = source.index(f"async function {name}(")
        body = source[start:source.index("await api(", start)]

        assert "!daemonAlive" in body, name


def test_the_copy_stays_subscribed_after_a_reconnect():
    # A copy and its subtitles survive leaving the stage: after a reconnect the
    # subscription is needed both by them and by the copy, otherwise the record
    # would break off on reconnection.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[
        source.index('opening.addEventListener("open"'):source.index('opening.addEventListener("message"')
    ]

    assert "copy && copy.videoId" in body
    assert "follow && follow.videoId" in body


def test_an_evicted_copy_disappears_from_the_list():
    # There is a record, but the browser has evicted the Blob: the list row must
    # no longer promise "on the device".
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "drawLinks(knownLinks" in body


def test_a_video_with_nothing_to_play_closes_the_stage():
    # The copy was evicted, there is no daemon: the stage would open empty, so
    # the message stays on the home page.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function prepareVideo"):source.index("// --- Video cache on the device")]

    assert "leaveStage()" in body


def test_the_saved_copy_is_marked_while_the_daemon_is_alive():
    # Otherwise the copy can be neither seen nor deleted while the computer works.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function drawLinks"):source.index("async function loadLinks")]

    assert "if (offline)" not in body
    assert "savedForLink(link.id)" in body


def test_a_saved_copy_keeps_collecting_cues_while_the_daemon_is_alive():
    # The copy could have been taken before the translation finished: its record
    # must keep growing during ordinary viewing too, otherwise offline only the
    # subtitles from the moment of copying would remain.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function playLocal"):source.index("function switchToCopy")]

    assert "follow = {" in body
    assert "daemonAlive" in body


def test_opening_a_video_rereads_the_copies_from_the_database():
    # The copy could have been deleted in another window (the installed app and
    # a browser tab are two instances of the page). This window's memory then
    # lies: the row promises "on the device", but there is nothing to play, and
    # offline all that remains is the message "this video is not on the device".
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function play("):source.index("async function createJob")]

    assert "await openCache()" in body
    assert body.index("await openCache()") < body.index("playSource(")


def test_the_copies_list_is_rebuilt_and_not_only_extended():
    # openCache is called on every opening of a video: topping up would leave in
    # memory what is no longer in the database.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function openCache("):source.index("function drawCacheButton")]

    assert "savedVideos.clear()" in body


def test_deleting_a_copy_redraws_the_cache_switch():
    # The switch on the stage lives by its own caption: without a redraw it
    # would keep showing the size of the deleted copy.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function dropCopy"):source.index("async function addLink")]

    assert "drawCacheButton()" in body


def test_returning_to_the_window_refreshes_the_copies():
    # A tab is hidden and one returns to it: while it was gone, the copy could
    # have been deleted.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert 'document.addEventListener("visibilitychange"' in source


def test_the_app_shows_without_asking_for_anything():
    # The token screen is gone: the daemon is open to whoever Origin lets in,
    # and the app is visible from the first frame. Worker registration follows
    # and still delays neither the screen nor what is saved.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function boot("):source.index("boot();", source.index("async function boot("))]

    assert "els.app.hidden = false" in body
    assert "start()" in body
    assert body.index("els.app.hidden = false") < body.index("await registerWorker()")


def test_the_page_keeps_no_token_to_send():
    # There is no token in the page's storage, nor in headers, nor in the file
    # address: a leftover from the input screen would otherwise lead the viewer
    # back into a dead end.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    assert "TOKEN_KEY" not in source
    assert "Authorization" not in source
    assert "token=" not in source


def test_a_missing_worker_is_reported_over_the_drawn_list():
    # Registration goes after the screen, so the list of links could have been
    # drawn before its answer. The warning must appear above what is already
    # drawn, otherwise it would never be learned about.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("async function boot("):source.index("boot();", source.index("async function boot("))]

    assert "drawLinks(knownLinks, warning)" in body
    # The list on load carries this warning too: registration could have
    # answered before the daemon's answer arrived.
    links = source[source.index("async function loadLinks"):source.index("function savedForLink")]
    assert "workerNote()" in links
    # A registration that failed without a network is not a verdict on the
    # browser: the shell could already be standing, and the page opened offline
    # precisely out of it.
    worker = source[source.index("async function registerWorker"):source.index("function workerNote")]
    assert "!navigator.serviceWorker.controller" in worker


def test_the_page_imports_every_helper_it_uses():
    # A name missing from the import is visible only in the browser: node
    # --check does not know names, while the markup and text stay in place. That
    # is why the names are checked as a list: everything the page calls from
    # logic.js and cache.js must be in its import.
    source = (ROOT / "page" / "app.js").read_text(encoding="utf-8")

    for module in ("logic.js", "cache.js"):
        text = (ROOT / "page" / module).read_text(encoding="utf-8")
        block = re.search(rf'import \{{([^}}]*)\}} from "\./{re.escape(module)}";', source)
        names = {name.strip() for name in block.group(1).replace("\n", " ").split(",") if name.strip()}
        exported = set(re.findall(r"export (?:async )?function (\w+)", text))
        exported |= set(re.findall(r"export const (\w+)", text))

        for name in sorted(exported):
            if re.search(rf"\b{name}\(", source):
                assert name in names, f"{module}: {name}"
