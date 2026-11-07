// Wiring of the tablet page: saved links, videos from the daemon, the socket
// and the subtitle layer. Pure logic lives in logic.js, here only DOM and network.

import {
  activeCueIndex, cacheLabel, cueLines, filterLinks, flipSwitch, formatTime, isForVideo,
  languageText, linkTitle, matchesQuery, parsePositions, pendingAt, positionFor, readFontSize,
  readSwitches, rememberPosition, sharedUrl, SPARE_BYTES, copyState, copyVerdict, formatBytes,
  mergeCues, percent, playSource, toggleExpanded, stageText, unwatchMessage, visibleLines,
  watchMessages,
} from "./logic.js";

import {
  askPersist, deleteVideo, downloadVideo, freeBytes, getMeta, getVideo, listVideos,
  openDb, putMeta, putVideo, readJson,
} from "./cache.js";

const CLIENT_KEY = "yt-trans-client";
const POSITIONS_KEY = "yt-trans-positions";
const SWITCHES_KEY = "yt-trans-switches";
const FONT_KEY = "yt-trans-font";
const POLL_VIDEO_MS = 2000;
const RECONNECT_MS = 1000;
const PLAYHEAD_MS = 1000;
const LINKS_TIMEOUT_MS = 3000;
// The same as the browser keeps the player's own controls on screen after the
// last touch: the status bar and the title fade out together with them.
const CONTROLS_IDLE_MS = 2500;
const WORKER_KEY = "yt-trans-no-worker";

// The markup does not change during the life of the page, so each element is
// looked up once: in the layer's frame loop a DOM access must not cost a lookup.
const els = {
  app: document.getElementById("app"),
  home: document.getElementById("home"),
  links: document.getElementById("links"),
  linksFilter: document.getElementById("links-filter"),
  linkForm: document.getElementById("link-form"),
  linkUrl: document.getElementById("link-url"),
  stage: document.getElementById("stage"),
  video: document.getElementById("video"),
  overlay: document.getElementById("overlay"),
  status: document.getElementById("status"),
  title: document.getElementById("title"),
  toggleTarget: document.getElementById("toggle-target"),
  toggleStudy: document.getElementById("toggle-study"),
  fontSize: document.getElementById("font-size"),
  back: document.getElementById("back"),
  fullscreen: document.getElementById("fullscreen"),
  cache: document.getElementById("cache"),
};

// Icons are drawn together with the button: the page pulls no fonts or images
// from the network, and without the network they would not come anyway. The
// outline inherits the button colour.
const ICONS = {
  back: '<path d="M15 5l-7 7 7 7"/>',
  download: '<path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14"/>',
  check: '<path d="M4 12.5l5 5.5L20 6.5"/>',
  // Floppy disk: the picture already lies on the disk, it can be watched right away.
  disk: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
  // Spinner: a ring with a gap — it spins while the picture is downloading.
  spinner: '<circle cx="12" cy="12" r="8" opacity=".25"/><path d="M12 4a8 8 0 0 1 8 8"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13M10 11v6M14 11v6"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  // YouTube "TV": a rounded frame with a triangle inside.
  video: '<rect x="2.5" y="5" width="19" height="14" rx="4"/><path d="M10 9.2l5.5 2.8-5.5 2.8z" fill="currentColor"/>',
  // List with a triangle — like a playlist on YouTube itself.
  playlist: '<path d="M3.5 7h11M3.5 12h11M3.5 17h7"/><path d="M15.5 13.5l5 3.5-5 3.5z" fill="currentColor"/>',
};

function iconMarkup(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round"
    stroke-linejoin="round">${ICONS[name]}</svg>`;
}

const ICON_DOWNLOAD = iconMarkup("download");
const ICON_DISK = iconMarkup("disk");
const ICON_TRASH = iconMarkup("trash");
const ICON_CHEVRON = iconMarkup("chevron");

const state = {
  videoId: null,
  cues: [],
  pending: [],
  language: null,
  stage: "",
  waiting: undefined,
  error: null,
  retryable: false,
  total: 0,
  translated: 0,
  title: "",
  notice: "",
  // Which link the video came from and how long it is: the copy writes this to
  // the database, and play() fills it in. While the stage is not open, it is empty.
  link: { id: "", title: "" },
  duration: 0,
};

// Cues arrive in batches and out of order: they are poured in by number, and
// handed to the screen as a dense list sorted by time.
const cuesByIndex = new Map();

let socket = null;
let videoTimer = null;
// The timer that takes the status bar and the title off the frame.
let idleTimer = null;
let layer = null;
let paintedKey = null;
// The watch position is per device: a tablet in another room continues from
// where it was left and does not drag the computer's tabs along.
let positions = parsePositions(store(POSITIONS_KEY));
// What is in the list now and what is typed in the search: a redraw on input
// must not go to the server for the same list.
let knownLinks = [];
let linksQuery = "";
// Expanded cards: "link:<id>" on a link and "video:<id>" on a video under
// which a copy lies. Everything is collapsed by default, and expansion lives
// until a reload: it is an action for this viewing, not a device setting.
let expandedKeys = [];
// Cards that the search opened itself and the viewer collapsed back. It lives
// until the query changes: otherwise the chevron on a found card would do nothing.
let searchClosed = new Set();
// Playlist videos: they are read from the device at startup and from the
// daemon when it answers. Storing them is what lets a playlist open without
// the daemon. `askedVideos` marks the ones the daemon was asked for in this
// session, so a redraw or an expansion does not ask YouTube twice; a
// connection failure is not marked — the daemon may come back. A request
// error is remembered too, otherwise the card would stay silent.
const linkVideos = new Map();
const askedVideos = new Set();
const loadingVideos = new Set();
// Which lines to show is per device too: a tablet in another room must not
// change what is set up on the computer.
let switches = readSwitches(store(SWITCHES_KEY));
// The line size is remembered on the device too: on a tablet in another room
// it needs to be different from the one at the computer.
let fontSize = readFontSize(store(FONT_KEY));

const clientId = makeClientId();

// Cache on the device. The database is opened once; savedVideos is what is
// already known about it, without the mp4s themselves, so the list answers
// without reading hundreds of megabytes.
let db = null;
const savedVideos = new Map();
// The copy that is downloading now: {videoId, controller, waiting, seen, total,
// cues, linkId, linkTitle, title, duration, language}. One at a time.
let copy = null;
// A video whose copy failed to save: the button says so until the next attempt.
let copyFailure = null;
// The copy's address for <video>; revoked when leaving the stage or changing videos.
let objectUrl = null;
// Offline is a property of the page, not of the network: it is set by a failed
// request to the daemon and cleared by any successful answer. navigator.onLine
// is not consulted.
let offline = false;
let daemonAlive = true;
// We ask for persistent storage once per page life, not for every copy.
let persistAsked = false;
// There is no service worker: the page will open only with a live daemon, and
// that is worth saying — but once, not on every launch.
let workerMissing = false;
// Whether this launch has already said there will be no shell: a redraw must
// not wipe the list row.
let workerWarning = "";
// A saved copy whose cues are still being appended: {videoId, record, timer}.
let follow = null;

function store(key, value) {
  try {
    if (value === undefined) {
      return localStorage.getItem(key) || "";
    }

    localStorage.setItem(key, value);

    return value;
  } catch {
    // Storage may be closed by privacy settings: then the key lives until a
    // reload, but the page works.
    return "";
  }
}

function makeClientId() {
  const saved = store(CLIENT_KEY);

  if (saved) {
    return saved;
  }

  const fresh = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

  return store(CLIENT_KEY, fresh) || fresh;
}

/** Request to the daemon. The daemon asks for no key: it lets in by Origin. */
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };

  if (options.body) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, { ...options, headers }).catch((error) => {
    // The request did not arrive at all — there is no daemon.
    daemonAlive = false;

    throw error;
  });

  daemonAlive = true;

  let data = {};

  try {
    data = await readJson(response);
  } catch (error) {
    if (!response.ok) {
      throw new Error(`daemon answered ${response.status}`);
    }

    // The body broke in the middle of the response — the daemon died or the
    // deadline passed. This is a refusal, not an empty answer: otherwise the
    // saved list would be overwritten with emptiness.
    daemonAlive = false;

    throw error;
  }

  if (!response.ok) {
    throw new Error(data.detail || `daemon answered ${response.status}`);
  }

  return data;
}

// --- Home: saved links and episodes -------------------------------------

async function start() {
  await openCache();
  connectSocket();
  await loadLinks();
  consumeShare();
}

// The app is registered in Android's share dialog, so from YouTube it is
// opened with an address like /?text=<link>. The link is added the same way as
// one typed by hand: the daemon checks it with YouTube itself and, if it is a
// single video, the page opens it right away.
async function consumeShare() {
  const shared = sharedUrl(new URLSearchParams(location.search));

  if (!shared) {
    return;
  }

  // First clean the address, then work with the network: otherwise a page
  // reload would repeat the addition, and "back" from the video would return
  // to the same share.
  history.replaceState(null, "", location.pathname);

  if (!daemonAlive) {
    drawLinks(knownLinks, "no daemon — cannot add the link from YouTube");

    return;
  }

  try {
    const data = await api("/v1/links", { method: "POST", body: JSON.stringify({ url: shared }) });
    await loadLinks();

    if (data.link && data.link.id) {
      await openLink(data.link.id);
    }
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "no daemon — cannot add the link from YouTube");

      return;
    }

    drawLinks(knownLinks, `failed to open the link from YouTube: ${error.message}`);
  }
}

function row(text, classes = []) {
  const item = document.createElement("li");
  item.className = classes.join(" ");
  item.textContent = text;

  return item;
}

function button(text, onClick) {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = text;
  node.addEventListener("click", onClick);

  return node;
}

/**
 * Header button: a type icon before the title. The icon is part of the button
 * but not of its name: only the title is left for the screen reader.
 */
function titledButton(icon, text, onClick) {
  const node = button("", onClick);
  const mark = document.createElement("span");
  // The icon name becomes a class: style.css uses it to colour the video,
  // the playlist and the other marks differently.
  mark.className = `title-icon is-${icon}`;
  mark.innerHTML = iconMarkup(icon);
  mark.setAttribute("aria-hidden", "true");
  node.append(mark, document.createTextNode(text));

  return node;
}

/** Icon button: there is no caption on screen, so the name is needed for the finger and for hearing. */
function iconButton(markup, label, onClick) {
  const node = button("", onClick);
  node.classList.add("icon-only");
  node.innerHTML = markup;
  node.setAttribute("aria-label", label);
  node.title = label;

  return node;
}

/** Trash: red, because a miss on it costs a link or a copy. */
function trashButton(label, onClick) {
  const node = iconButton(ICON_TRASH, label, onClick);
  node.classList.add("trash");

  return node;
}

/**
 * Video names known without the network: a playlist that was read and copies
 * on the device. The search goes through them too — a playlist title does not
 * say what is inside.
 */
function videosFor(link) {
  const known = linkVideos.get(link.id);
  const list = known && known.videos ? known.videos : [];
  const seen = new Set(list.map((video) => video.videoId));
  const extra = savedForLink(link.id)
    .filter((record) => !seen.has(record.videoId))
    .map((record) => ({ videoId: record.videoId, title: record.title }));

  return [...list, ...extra];
}

function drawLinks(items, message) {
  knownLinks = items;
  els.links.replaceChildren();

  // There is nothing to search while there is nothing to show: an empty field
  // above "empty for now" is only an extra question.
  els.linksFilter.hidden = items.length === 0;

  if (message) {
    els.links.append(row(message, ["empty"]));
  }

  if (!items.length) {
    // The daemon gave no links, but what was saved lies on the device: it is
    // the list, and it can be searched too. The cards here are lonely — they
    // have no link of their own on screen, so the length stays with the copy.
    if (savedVideos.size) {
      els.links.append(...savedRows([...savedVideos.values()]));

      return;
    }

    if (!message) {
      els.links.append(row(
        offline ? "no daemon, nothing is saved on the device" : "empty for now, add a link",
        ["empty"],
      ));
    }

    return;
  }

  const visible = filterLinks(items, linksQuery, videosFor);
  // Playlist videos have nowhere to learn names from until they are read: we
  // ask during the search, otherwise a match inside a playlist would stay
  // unfound. The answer comes once — after that the name is in page memory.
  const searching = linksQuery.trim() !== "";

  if (!visible.length) {
    // A playlist with no title match is already filtered out, and its videos
    // are still being read: "nothing found" here would be untrue.
    const pending = searching && daemonAlive && items.some((link) => (
      link.kind === "playlist" && (loadingVideos.has(link.id) || !linkVideos.has(link.id))
    ));

    els.links.append(row(pending ? "searching the videos…" : "nothing found", ["empty"]));
  }

  for (const link of visible) {
    els.links.append(linkCard(link));
  }

  // The walk is over all links, not the visible ones: otherwise a search by
  // video name would drop a playlist before learning its videos.
  for (const link of items) {
    if (link.kind !== "playlist") {
      continue;
    }

    if (expandedKeys.includes(linkKey(link.id)) || searching) {
      // A request after drawing: loadVideos redraws the list itself, and
      // calling it straight from the walk would nest drawing in drawing.
      queueMicrotask(() => loadVideos(link));
    }
  }
}

/**
 * Whether the card was opened by the search itself: not its title matched, but
 * a video name. The chevron closes such a card without touching expansions
 * made by the viewer.
 */
function openedBySearch(link) {
  const key = linkKey(link.id);

  if (expandedKeys.includes(key) || searchClosed.has(key) || linksQuery.trim() === "") {
    return false;
  }

  const byTitle = matchesQuery(link.title, linksQuery) || matchesQuery(link.url, linksQuery);

  return !byTitle && videosFor(link).some((video) => matchesQuery(video.title, linksQuery));
}

/**
 * One link — one card: the title, and children under it. For a playlist the
 * children are videos, for a video — its copy. As separate tiles they read as
 * different list items, and it was unclear that the lower row belongs to the
 * one above.
 */
function linkCard(link) {
  const item = document.createElement("li");
  item.className = "card";

  // Copies of this link live under it even with a live daemon: otherwise they
  // could be neither seen nor deleted while the computer is running.
  const saved = savedForLink(link.id);
  const playlist = link.kind === "playlist";
  // A match on the link title is visible in the header itself, and there is
  // nothing to expand. A match on a video name is the opposite: without
  // expanding, the found item would stay hidden under a collapsed row.
  const byTitle = matchesQuery(link.title, linksQuery) || matchesQuery(link.url, linksQuery);
  const key = linkKey(link.id);
  const searchOpened = openedBySearch(link);
  const open = expandedKeys.includes(key) || searchOpened;
  // The length moves into the title only when there is one copy: in a playlist
  // there are many behind one link, and one number would lie about the rest.
  const only = !playlist && saved.length === 1 ? saved[0] : null;

  const line = document.createElement("div");
  line.className = "link-line";
  const openButton = titledButton(
    playlist ? "playlist" : "video",
    linkTitle(link, only),
    () => openCard(link),
  );
  // The marks are about the video and about the copy on the device: the copy
  // plays without the daemon too, so its icon is needed even when the daemon
  // is silent. A playlist has neither its own picture nor its own translation.
  if (!playlist && (saved.length || daemonAlive)) {
    openButton.append(...videoMarks(link, saved.length > 0));
  }

  line.append(openButton);
  line.append(trashButton("delete link", () => dropLink(link)));

  // The chevron is the last button of the header: expansion must not stand
  // between the finger and deletion, otherwise both would be missed.
  if (playlist || saved.length) {
    line.append(chevronButton(open, () => toggleCard(
      key,
      playlist ? () => loadVideos(link) : null,
      searchOpened,
    )));
  }

  item.append(line);

  if (!open) {
    return item;
  }

  if (playlist) {
    item.append(playlistChildren(link, byTitle));

    return item;
  }

  const kids = document.createElement("div");
  kids.className = "children";
  // During a search only matching copies stay under the card; a match on the
  // link title shows them all.
  const records = byTitle
    ? saved
    : saved.filter((record) => matchesQuery(record.title, linksQuery));

  for (const record of records) {
    kids.append(copyLine(record, !only));
  }

  item.append(kids);

  return item;
}

/** Copy row in the card: open from the device and remove it. */
function copyLine(record, withDuration) {
  const line = document.createElement("div");
  line.className = "saved-line";
  line.append(
    button(
      withDuration ? savedLabel(record) : cacheLabel(record.size),
      () => play(record, { id: record.linkId, title: record.linkTitle }),
    ),
    trashButton("delete copy", () => dropCopy(record)),
  );

  return line;
}

/**
 * Playlist children. Videos come from the daemon: without them an expanded
 * card must still show what already lies on the device — copies.
 */
function playlistChildren(link, all) {
  const kids = document.createElement("div");
  kids.className = "children";
  const known = linkVideos.get(link.id);

  if (known && known.error) {
    kids.append(cardNote(`the playlist videos could not be read: ${known.error}`));
  } else if (known) {
    // When searching by video name only matches stay: the rest have nothing
    // to do with the query. A match on the playlist title means all videos.
    const videos = all
      ? known.videos
      : known.videos.filter((video) => matchesQuery(video.title, linksQuery));

    if (!videos.length) {
      kids.append(cardNote(all ? "the link has no videos" : "no matches in the playlist"));
    }

    for (const video of videos) {
      kids.append(videoCard(video, link));
    }

    return kids;
  } else if (loadingVideos.has(link.id)) {
    kids.append(cardNote("reading videos…"));

    return kids;
  } else if (!daemonAlive) {
    kids.append(cardNote("no daemon — the playlist videos are not on the device"));
  }

  // Playlist videos may be unknown even with a live daemon: then at least the
  // copies that already exist stay under the card.
  if (!known) {
    const records = all
      ? savedForLink(link.id)
      : savedForLink(link.id).filter((item) => matchesQuery(item.title, linksQuery));

    for (const record of records) {
      kids.append(copyLine(record, true));
    }
  }

  return kids;
}

/** A video inside a playlist: the copy under it is its child. */
function videoCard(video, link) {
  const saved = savedVideos.get(video.videoId);
  const open = expandedKeys.includes(videoKey(video.videoId));

  const item = document.createElement("div");
  item.className = "card-row";

  const line = document.createElement("div");
  line.className = "video-line";
  const playButton = titledButton(
    "video",
    episodeLabel(video),
    () => play(video, { id: link.id, title: link.title }),
  );
  playButton.append(...videoMarks(video, Boolean(saved)));

  line.append(playButton);

  if (saved) {
    line.append(chevronButton(open, () => toggleCard(videoKey(video.videoId))));
  }

  item.append(line);

  if (saved && open) {
    const kids = document.createElement("div");
    kids.className = "children";
    // The length already stands in the video caption: the copy only has to say where it is.
    kids.append(copyLine(saved, false));
    item.append(kids);
  }

  return item;
}

/** Chevron at the end of the header: expands and collapses the card's children. */
function chevronButton(open, onClick) {
  const node = iconButton(ICON_CHEVRON, open ? "collapse" : "expand", onClick);
  node.classList.add("chevron");
  node.setAttribute("aria-expanded", open ? "true" : "false");

  return node;
}

const linkKey = (id) => `link:${id}`;
const videoKey = (id) => `video:${id}`;

/**
 * Expand or collapse a card. The state survives a redraw but not a reload:
 * otherwise every new copy would collapse the list under the finger.
 * `bySearch` — the card was opened by the search, not by the viewer: its
 * chevron collapses what was found without touching an earlier expansion.
 */
function toggleCard(key, onOpen = null, bySearch = false) {
  if (bySearch && !expandedKeys.includes(key)) {
    searchClosed.add(key);
  } else {
    searchClosed.delete(key);
    expandedKeys = toggleExpanded(expandedKeys, key);
  }

  drawLinks(knownLinks, "");

  if (onOpen && expandedKeys.includes(key)) {
    onOpen();
  }
}

/** Card header: a video plays right away, a playlist expands. */
function openCard(link) {
  if (link.kind === "playlist") {
    toggleCard(linkKey(link.id), () => loadVideos(link), openedBySearch(link));

    return;
  }

  openLink(link.id);
}

/**
 * Playlist videos from the daemon, written to the device at once: without them
 * a silent daemon leaves the playlist a title with nothing under it.
 */
async function fetchVideos(id) {
  const data = await api(`/v1/links/${encodeURIComponent(id)}/videos`);
  const videos = Array.isArray(data.videos) ? data.videos : [];

  linkVideos.set(id, { videos });
  askedVideos.add(id);
  await saveVideos();

  return { ...data, videos };
}

/**
 * What was read from the daemon, by link. The list survives a reload and the
 * daemon going away; an error is not knowledge and is not written.
 */
async function saveVideos() {
  if (!db) {
    return;
  }

  const stored = {};

  for (const [linkId, entry] of linkVideos) {
    if (entry && Array.isArray(entry.videos)) {
      stored[linkId] = entry.videos;
    }
  }

  await putMeta(db, "videos", stored).catch(() => {});
}

/**
 * Playlist videos are requested on expansion, not on every draw: the daemon
 * goes to YouTube for the answer, and the list must not do that for nothing.
 * A daemon answer refreshes what was read before; without one the device's
 * copy stays on screen.
 */
async function loadVideos(link) {
  if (loadingVideos.has(link.id) || !daemonAlive || askedVideos.has(link.id)) {
    return;
  }

  loadingVideos.add(link.id);
  drawLinks(knownLinks, "");

  try {
    await fetchVideos(link.id);
  } catch (error) {
    // A failed refresh does not erase what already lay on the device: a list
    // read yesterday is better than an error row about a list we still have.
    if (!linkVideos.has(link.id)) {
      linkVideos.set(link.id, { error: daemonAlive ? error.message : "no daemon" });
    }

    // A refusal is not repeated by every redraw; a lost connection is, because
    // the daemon may come back before the viewer does.
    if (daemonAlive) {
      askedVideos.add(link.id);
    }
  } finally {
    loadingVideos.delete(link.id);
    drawLinks(knownLinks, "");
  }
}

/**
 * The videos of every playlist are stored the first time the list is seen, not
 * when a card happens to be expanded: a playlist must open without the daemon.
 * One at a time — the daemon goes to YouTube for each answer.
 */
async function prefetchVideos(links) {
  for (const link of links) {
    if (!daemonAlive) {
      return;
    }

    if (link.kind === "playlist" && !linkVideos.has(link.id)) {
      await loadVideos(link);
    }
  }
}

/** Caption row inside a card: a div, not an li — there is no nested list here. */
function cardNote(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;

  return node;
}

async function loadLinks() {
  // The previous list from the device matters more than the answer speed:
  // while the daemon thinks, the screen is already not empty, and a silent
  // daemon leaves this list in place.
  const last = db ? await getMeta(db, "links") : null;

  if (last && last.length) {
    offline = false;
    drawLinks(last, "");
  }

  let data;

  try {
    data = await api("/v1/links", { signal: AbortSignal.timeout(LINKS_TIMEOUT_MS) });
  } catch (error) {
    offline = !daemonAlive;

    // A request failure does not wipe what is already on screen. The links
    // came from the device or a previous answer, and the error row stands
    // above them; otherwise one failed request left only the saved copies out
    // of the home, and video rows disappeared although the links themselves
    // had not gone anywhere.
    drawLinks(last && last.length ? last : knownLinks, daemonAlive ? error.message : "");

    return;
  }

  offline = false;

  // The list of links is an array. An answer without it (the daemon answered
  // with the wrong thing) must neither wipe the saved data nor pretend to be
  // an empty list.
  if (!Array.isArray(data.links)) {
    drawLinks(last && last.length ? last : knownLinks, "the daemon answered without a list of links");

    return;
  }

  const links = data.links;

  if (db) {
    await putMeta(db, "links", links).catch(() => {});
  }

  drawLinks(links, workerNote());

  // The children of every playlist are stored right away, not when a card is
  // opened: without them the playlist is a title with nothing under it as
  // soon as the daemon goes away.
  queueMicrotask(() => prefetchVideos(links));
}

/** Saved videos of this link, freshest first. */
function savedForLink(linkId) {
  return [...savedVideos.values()]
    .filter((record) => record.linkId === linkId)
    .sort((left, right) => (right.savedAt || 0) - (left.savedAt || 0));
}

/**
 * Copies without links of their own: a card for each, with the length inside —
 * there is nowhere outside to take it from.
 */
function savedRows(records) {
  return records.map((record) => {
    const item = document.createElement("li");
    item.className = "card saved";
    item.append(copyLine(record, true));

    return item;
  });
}

/**
 * Caption of a copy under its link. The title is not repeated here — it is
 * already drawn a row above; what is left is what is not there: the length,
 * the state and the place on the device.
 */
function savedLabel(record) {
  const parts = [];

  if (typeof record.duration === "number" && record.duration > 0) {
    parts.push(formatTime(record.duration));
  }

  parts.push(cacheLabel(record.size));

  return parts.join(" · ");
}

/** Forget a copy: the database, the list and the tail of cues still being appended. */
async function forgetCopy(videoId) {
  if (follow && follow.videoId === videoId) {
    clearTimeout(follow.timer);
    follow = null;
  }

  if (db) {
    await deleteVideo(db, videoId).catch(() => {});
  }

  savedVideos.delete(videoId);
}

async function dropLink(link) {
  const saved = savedForLink(link.id);
  const title = link.title || link.url;
  const question = saved.length
    ? `Delete "${title}" together with its saved copies (${saved.length})?`
    : `Delete "${title}"?`;

  if (!confirm(question)) {
    return;
  }

  if (!daemonAlive) {
    drawLinks(knownLinks, "no daemon — cannot delete the link");

    return;
  }

  try {
    await api(`/v1/links/${encodeURIComponent(link.id)}`, { method: "DELETE" });
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "no daemon — cannot delete the link");

      return;
    }

    drawLinks(knownLinks, `failed to delete: ${error.message}`);

    return;
  }

  // The link is gone — so is everything that lay under it. Otherwise the
  // playlist videos would stay on the device without a row to see them from.
  linkVideos.delete(link.id);
  await saveVideos();

  for (const record of saved) {
    await forgetCopy(record.videoId);
  }

  expandedKeys = expandedKeys.filter((key) => key !== linkKey(link.id));

  // The list is edited in place: if the daemon died right after the deletion,
  // a request for the list would bring the deleted link back.
  const left = knownLinks.filter((item) => item.id !== link.id);

  if (db) {
    await putMeta(db, "links", left).catch(() => {});
  }

  drawCacheButton();
  drawLinks(left, "");
}

/** Remove a copy from the device: space on the tablet runs out, and this must be possible. */
async function dropCopy(record) {
  const title = record.title || record.videoId;

  // A copy is hundreds of megabytes that will have to be downloaded again:
  // one wrong touch must not take them away.
  if (!confirm(`Delete the copy "${title}" from the device?`)) {
    return;
  }

  await forgetCopy(record.videoId);
  // The switch on the stage lives by its own caption and without this would
  // keep showing the size of the deleted copy.
  drawCacheButton();
  drawLinks(knownLinks, "");
}

async function addLink(event) {
  event.preventDefault();

  const url = els.linkUrl.value.trim();

  if (!url) {
    return;
  }

  if (!daemonAlive) {
    drawLinks(knownLinks, "no daemon — cannot add the link");

    return;
  }

  try {
    await api("/v1/links", { method: "POST", body: JSON.stringify({ url }) });
    els.linkUrl.value = "";
    await loadLinks();
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "no daemon — cannot add the link");

      return;
    }

    drawLinks(knownLinks, `failed to add: ${error.message}`);
  }
}

// The picture mark is on every video at once, so the list answers "what can be
// watched instantly". A copy on the device is a floppy: only it plays without
// the computer too, and the server cache is not for the viewer to see. A check
// would promise more — it is for a finished translation. A downloading picture
// is a spinner: the motion speaks for itself, without a word that would have
// to be read in every row.
const VIDEO_STATE_ICONS = { preparing: "spinner" };
const VIDEO_STATE_LABELS = {
  preparing: "the picture is being prepared",
};
const VIDEO_STATE_WORDS = {
  error: "did not download",
};

/** State icon into the title row: quiet, with a name for the screen reader. */
function iconStateMark(icon, label) {
  const mark = document.createElement("span");
  // By the icon class style.css makes the check green and the floppy blue.
  mark.className = `state-mark is-${icon}`;
  // The mark will not tell about itself, so it has a name.
  mark.setAttribute("role", "img");
  mark.setAttribute("aria-label", label);
  mark.innerHTML = iconMarkup(icon);

  return mark;
}

/** A copy on the device is a floppy: it is the only readiness we vouch for. */
function copyMark() {
  return iconStateMark("disk", "copy on the device");
}

/**
 * State mark after the title: a spinner for a downloading picture, a word for
 * an error. Readiness is visible, but it does not argue with the title.
 */
function stateMark(stateName) {
  const icon = VIDEO_STATE_ICONS[stateName];

  if (icon) {
    const mark = iconStateMark(icon, VIDEO_STATE_LABELS[stateName]);
    // Only the spinner spins: a still mark would mean "frozen".
    mark.classList.toggle("spin", stateName === "preparing");

    return mark;
  }

  const word = VIDEO_STATE_WORDS[stateName];

  if (!word) {
    return null;
  }

  const mark = document.createElement("span");
  mark.className = "state-word";
  mark.textContent = word;

  return mark;
}

/**
 * A finished translation is a check: it answers "is there a translation to
 * wait for", not "what lies on the disk". No translation — no mark: absence
 * promises nothing.
 */
function translationMark(translated) {
  return translated ? iconStateMark("check", "translation ready") : null;
}

/**
 * "new" — a fresh row: nothing has been done to the video yet, neither the
 * picture nor the translation. The link has no timestamp, so freshness is
 * "nothing touched yet"; as soon as any other mark appears, the icon goes away
 * by itself.
 */
function newMark() {
  const mark = document.createElement("span");
  mark.className = "new-mark";
  // The word is in Latin script — a sign, not text to read: the mark has a name.
  mark.setAttribute("aria-label", "the video has not been processed yet");
  mark.textContent = "new";

  return mark;
}

/**
 * All marks of a video row in one place: both the top list row and the
 * playlist videos are captioned the same way, and they have nowhere to diverge.
 *
 * onDevice — the copy lies on the device: the floppy draws it. Readiness on
 * the server promises nothing to the list, so the state "ready" gives no icon.
 */
function videoMarks(video, onDevice = false) {
  const untouched = video.state === "absent" && !video.translated;

  return [
    onDevice ? copyMark() : stateMark(video.state),
    translationMark(video.translated),
    untouched && !onDevice ? newMark() : null,
  ].filter(Boolean);
}

function episodeLabel(video) {
  const parts = [video.title || video.videoId];

  if (typeof video.duration === "number") {
    parts.push(formatTime(video.duration));
  }

  return parts.join(" · ");
}

/**
 * A playlist whose videos lie on the device opens without the daemon: that is
 * what they are kept on the device for. Returns whether the card was opened.
 */
function openStored(id) {
  const link = knownLinks.find((item) => item.id === id);

  if (!link || link.kind !== "playlist" || !linkVideos.has(id)) {
    return false;
  }

  if (!expandedKeys.includes(linkKey(id))) {
    expandedKeys = toggleExpanded(expandedKeys, linkKey(id));
  }

  drawLinks(knownLinks, "");

  return true;
}

async function openLink(id) {
  if (!daemonAlive) {
    if (openStored(id)) {
      return;
    }

    // Videos come from the daemon, and it is gone: but copies of this link
    // could already lie on the device, and the list shows them.
    const saved = savedForLink(id);

    drawLinks(knownLinks, saved.length
      ? "no daemon — the list shows the saved videos of this link"
      : "no daemon, this link's videos are not on the device");

    return;
  }

  let data;

  try {
    data = await fetchVideos(id);
  } catch (error) {
    if (!daemonAlive) {
      if (openStored(id)) {
        return;
      }

      const saved = savedForLink(id);

      drawLinks(knownLinks, saved.length
        ? "no daemon — the list shows the saved videos of this link"
        : "no daemon, this link's videos are not on the device");

      return;
    }

    drawLinks(knownLinks, `failed to read the link: ${error.message}`);

    return;
  }

  const videos = data.videos;

  // A single video is not a list but viewing right away: a button with one
  // item would only make you press twice.
  if (videos.length === 1) {
    play(videos[0], { id, title: data.title || "" });

    return;
  }

  // Looks like a playlist turned up under the "video": expand its card instead
  // of opening a separate screen — the list is the tree.
  if (!expandedKeys.includes(linkKey(id))) {
    expandedKeys = toggleExpanded(expandedKeys, linkKey(id));
  }

  drawLinks(knownLinks, "");
}

// --- Stage ----------------------------------------------------------------

async function play(video, link = {}) {
  // The device is the only source of truth about copies: otherwise the page
  // would take a copy deleted in another window for a saved one and open an
  // empty stage.
  await openCache();

  // Without a copy and without the daemon there is nothing to play: a black
  // screen would pretend to be a breakdown, so the stage does not open, and
  // the home page says what is the matter.
  if (playSource(savedVideos.has(video.videoId), daemonAlive) === "none") {
    drawLinks(knownLinks, "no daemon, this video is not on the device");

    return;
  }

  const previous = state.videoId;

  Object.assign(state, {
    videoId: video.videoId,
    cues: [],
    pending: [],
    language: null,
    stage: "",
    waiting: undefined,
    error: null,
    retryable: false,
    total: 0,
    translated: 0,
    title: video.title || link.title || "",
    notice: "",
    link: { id: link.id || "", title: link.title || "" },
    duration: Number(video.duration) || 0,
  });

  cuesByIndex.clear();
  paintedKey = null;

  copyFailure = null;

  els.title.textContent = state.title;
  els.home.hidden = true;
  els.stage.hidden = false;
  // The stage opens paused on the video: the bar and the title are visible
  // while preparation goes on, and fade only when the picture is already playing.
  wakeStage();
  drawCacheButton();
  render();

  history.pushState({ videoId: state.videoId }, "");

  createJob(state.videoId);
  watch(state.videoId, previous);
  prepareVideo();
}

async function createJob(videoId) {
  try {
    await api("/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ videoId, targetLang: "ru", authorTrack: true }),
    });
  } catch (error) {
    // There is no daemon, but there is a copy on the device — there is no one
    // to create a job for.
    note(!daemonAlive && savedVideos.has(videoId) ? "no daemon — playing the saved copy" : error.message);
  }
}

function note(message) {
  state.notice = message || "";
  render();
}

function stopVideoPoll() {
  if (videoTimer !== null) {
    clearTimeout(videoTimer);
    videoTimer = null;
  }
}

/** Ask the daemon for the finished mp4 and wait until it stitches it. */
async function prepareVideo() {
  const videoId = state.videoId;

  stopVideoPoll();

  // A copy on the device is preferable to the daemon: it does not wait for
  // stitching and does not break together with the connection.
  if (playSource(savedVideos.has(videoId), daemonAlive) === "copy" && await playLocal(videoId)) {
    return;
  }

  let status;

  try {
    status = await api(`/v1/videos/${encodeURIComponent(videoId)}`, { method: "POST" });
  } catch (error) {
    if (playSource(false, daemonAlive) === "none") {
      // The copy was counted, but the browser evicted it, and there is no
      // daemon: the stage would open empty, so the message stays on the home
      // page.
      leaveStage();
      drawLinks(knownLinks, "no daemon, this video is not on the device");

      return;
    }

    note(error.message);

    return;
  }

  const step = (current) => {
    // While waiting for the two-second answer, the viewer could go to another video.
    if (videoId !== state.videoId) {
      return;
    }

    if (current.state === "ready") {
      note("");

      const resume = positionFor(positions, videoId);

      els.video.src = `/v1/videos/${encodeURIComponent(videoId)}/file`;
      els.video.addEventListener("loadedmetadata", () => {
        if (resume > 0) {
          els.video.currentTime = resume;
        }
      }, { once: true });
      els.video.play().catch(() => {
        // The browser may refuse: a launch outside a press counts as autoplay.
        // The player has its own buttons, so the viewer will start it
        // themselves — being silent here is honest.
      });

      return;
    }

    if (current.state === "error") {
      note(current.message || "the video did not download");

      return;
    }

    note("preparing the picture…");
    videoTimer = setTimeout(async () => {
      try {
        step(await api(`/v1/videos/${encodeURIComponent(videoId)}`));
      } catch (error) {
        note(error.message);
      }
    }, POLL_VIDEO_MS);
  };

  step(status);
}

// --- Video cache on the device -------------------------------------------

/** Saved cues into page memory: they depend neither on the socket nor on the daemon. */
function seedCues(record) {
  if (!record.cues || !record.cues.length) {
    return;
  }

  cuesByIndex.clear();

  for (const cue of record.cues) {
    cuesByIndex.set(cue.i, cue);
  }

  state.cues = [...cuesByIndex.values()].sort((a, b) => a.start - b.start);
  state.language = record.language || state.language;
  state.translated = countTranslated();
  paintedKey = null;
  render();
  paint();
}

/** Open a video from a copy; false — there is no copy or it is not ours. */
async function playLocal(videoId) {
  if (!savedVideos.has(videoId) || !db) {
    return false;
  }

  const record = await getVideo(db, videoId);

  if (videoId !== state.videoId) {
    return false;
  }

  if (!record || !record.blob) {
    // The browser may evict a Blob: the record is in the database, there is
    // nothing to play, and the list row must no longer lie that the video is
    // on the device.
    savedVideos.delete(videoId);
    deleteVideo(db, videoId).catch(() => {});
    drawCacheButton();
    // The list row promised "on the device" — now that is no longer true.
    drawLinks(knownLinks, "");

    return false;
  }

  switchToCopy(record, { resume: positionFor(positions, videoId), autoplay: true });
  seedCues(record);

  if (!daemonAlive) {
    note("no daemon — playing the saved copy");
  } else {
    // The copy could have been taken before the translation finished. The
    // record must grow during ordinary viewing too: otherwise only the cues
    // that made it to the copy would stay offline, and new ones would
    // disappear together with this viewing.
    follow = { videoId, record, timer: null };

    // The subscription snapshot could arrive before the copy opened: its cues
    // are already in page memory, and the record is worth catching up with them.
    const merged = mergeCues(record.cues, state.cues);

    if (merged.length !== (record.cues || []).length) {
      record.cues = merged;
      followSoon();
    }
  }

  return true;
}

/**
 * Play from a copy without losing the position or the pause: the object
 * address is put into <video>, and the time is restored on loadedmetadata.
 * `resume` is not given — we take the place where the video stands now.
 */
function switchToCopy(record, { resume = 0, autoplay = false } = {}) {
  const from = resume || els.video.currentTime;
  const wasPlaying = !els.video.paused || autoplay;

  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
  }

  objectUrl = URL.createObjectURL(record.blob);

  els.video.addEventListener("loadedmetadata", () => {
    if (from > 0) {
      els.video.currentTime = from;
    }

    if (wasPlaying) {
      els.video.play().catch(() => {
        // Outside a press the browser may refuse — then the viewer will start it themselves.
      });
    }
  }, { once: true });

  els.video.src = objectUrl;
}

// Writing saved cues is expensive (a whole mp4 on top), so we accumulate them
// and write no more than once every five seconds — by the end of the
// translation everything is in the database anyway.
const FOLLOW_MS = 5000;

const filePath = (videoId) => `/v1/videos/${encodeURIComponent(videoId)}/file`;

/** Size of the finished mp4: HEAD does not return a body, but it does have Content-Length. */
async function fileSize(path) {
  try {
    const response = await fetch(path, { method: "HEAD" });

    return Number(response.headers.get("Content-Length") || 0);
  } catch {
    // The size was not asked for — no reason to refuse: the verdict will be "unknown".
    return 0;
  }
}

/** Wait for the finished mp4 the way the stage waits for it; -1 — the daemon said it will not be. */
async function waitForFile(videoId) {
  const mine = copy;

  for (;;) {
    if (!copy || copy !== mine) {
      return 0;
    }

    const status = await api(`/v1/videos/${encodeURIComponent(videoId)}`, { method: "POST" })
      .catch(() => null);

    if (copy !== mine) {
      return 0;
    }

    if (status && status.state === "error") {
      note(status.message || "the video did not download");

      return -1;
    }

    if (status && status.state === "ready") {
      return fileSize(filePath(videoId));
    }

    await new Promise((resolve) => setTimeout(resolve, POLL_VIDEO_MS));
  }
}

/** Open the database and recall what already lies on the device. Called from start() too. */
async function openCache() {
  if (!db) {
    try {
      db = await openDb();
    } catch {
      db = null;
    }
  }

  if (!db) {
    return;
  }

  try {
    const records = await listVideos(db);

    // Not topping up but rebuilding: a copy may have been deleted in another
    // window, and this window's memory would keep a row promising what is not
    // on the device. We rebuild only after a successful read: otherwise a
    // failure would wipe the list.
    savedVideos.clear();

    for (const record of records) {
      savedVideos.set(record.videoId, record);
    }
  } catch {
    // The list was not read — the copy will still try to save.
  }

  try {
    const stored = await getMeta(db, "videos");

    // The playlist videos were read while the daemon was alive: they stay on
    // the device, and a silent daemon leaves them under their link.
    if (stored && typeof stored === "object" && !Array.isArray(stored)) {
      for (const [linkId, videos] of Object.entries(stored)) {
        if (Array.isArray(videos)) {
          linkVideos.set(linkId, { videos });
        }
      }
    }
  } catch {
    // The playlist videos were not read — the daemon will answer for them.
  }
}

/**
 * Cache button: a download icon, and a floppy with its megabytes for a finished
 * copy — the same size the list shows, so the viewer does not go back to the
 * list to check. While the copy is under way the caption is its state: a
 * percentage, a wait or a failure.
 */
function drawCacheButton() {
  const mine = copy && copy.videoId === state.videoId ? copy : null;
  const saved = savedVideos.get(state.videoId);

  let label = "";

  if (mine) {
    label = copyState({
      waiting: Boolean(mine.waiting),
      active: true,
      percent: percent(mine.seen, mine.total),
      failed: false,
    });
  } else if (copyFailure === state.videoId && !saved) {
    label = copyState({ failed: true });
  } else if (saved) {
    label = formatBytes(saved.size);
  }

  // A finished copy is the same floppy as in the list: both icons say one
  // thing — "the video lies on the device".
  els.cache.innerHTML = `${saved ? ICON_DISK : ICON_DOWNLOAD}<span class="cache-label"></span>`;

  // The caption as a separate node, not innerHTML: an empty string must not
  // leave spaces and extra padding on the button.
  if (label) {
    els.cache.querySelector(".cache-label").textContent = label;
  }

  // An icon without a word: what a press will do must be heard and seen on
  // hover. On a finished copy a press does nothing — so we say that.
  const hint = mine ? "cancel copying" : saved ? "copy on the device" : "copy the video to the device";

  els.cache.setAttribute("aria-label", hint);
  els.cache.title = hint;
  els.cache.setAttribute("aria-pressed", mine || saved ? "true" : "false");
}

function toggleCache() {
  const videoId = state.videoId;

  if (!videoId) {
    return;
  }

  if (copy && copy.videoId === videoId) {
    cancelCopy();

    return;
  }

  // "Cache ✓" deletes nothing by itself: an expensive copy must not be
  // destroyed by a wrong touch, there is a button for that in the list.
  if (savedVideos.has(videoId)) {
    return;
  }

  if (copy) {
    cancelCopy();
  }

  startCopy();
}

async function startCopy() {
  const videoId = state.videoId;

  if (!db) {
    await openCache();
  }

  if (!db) {
    note("the browser did not give storage");

    return;
  }

  const mine = {
    videoId,
    controller: new AbortController(),
    waiting: true,
    seen: 0,
    total: 0,
    // Cues that arrived before copying began are already in page memory —
    // everything that exists at this moment is saved.
    cues: new Map(cuesByIndex),
    linkId: state.link.id,
    linkTitle: state.link.title,
    title: state.title,
    duration: state.duration,
    language: state.language,
  };

  copy = mine;
  copyFailure = null;
  drawCacheButton();

  if (!persistAsked) {
    persistAsked = true;
    askPersist().catch(() => {});
  }

  const size = await waitForFile(videoId);

  if (copy !== mine) {
    return;
  }

  if (size < 0) {
    copy = null;
    drawCacheButton();

    return;
  }

  const free = await freeBytes();

  if (copy !== mine) {
    return;
  }

  if (copyVerdict(size, free) === "tight") {
    note(`not enough space: ${formatBytes(size + SPARE_BYTES - free)} more needed, ${formatBytes(free)} free`);
    copy = null;
    drawCacheButton();

    return;
  }

  mine.waiting = false;
  mine.total = size;
  drawCacheButton();

  let drawn = 0;

  try {
    const { blob } = await downloadVideo(filePath(videoId), {
      signal: mine.controller.signal,
      onProgress: (seen, total) => {
        mine.seen = seen;

        if (total > 0) {
          mine.total = total;
        }

        // Redraw only when the percentage changed: otherwise the button would
        // twitch on every chunk of the stream.
        const now = percent(mine.seen, mine.total);

        if (now !== drawn) {
          drawn = now;
          drawCacheButton();
        }
      },
    });

    await finishCopy(blob);
  } catch (error) {
    const cancelled = error.name === "AbortError";

    if (copy === mine) {
      copy = null;
    }

    if (!cancelled) {
      copyFailure = videoId;
      note(`the copy was not saved: ${error.message}`);
    }

    drawCacheButton();

    if (state.videoId !== videoId) {
      send(unwatchMessage(videoId));
    }
  }
}

function cancelCopy() {
  const mine = copy;

  if (!mine) {
    return;
  }

  copy = null;
  mine.controller.abort();

  if (state.videoId !== mine.videoId) {
    send(unwatchMessage(mine.videoId));
  }

  drawCacheButton();
}

async function finishCopy(blob) {
  const mine = copy;

  if (!mine) {
    return;
  }

  const record = {
    videoId: mine.videoId,
    linkId: mine.linkId,
    linkTitle: mine.linkTitle,
    title: mine.title,
    duration: mine.duration,
    // The size comes from the file itself: HEAD might not answer, and the
    // blob is all here already.
    size: blob.size,
    savedAt: Date.now(),
    language: mine.language,
    cues: mergeCues([], [...mine.cues.values()]),
    blob,
  };

  try {
    await putVideo(db, record);
  } catch (error) {
    copy = null;
    copyFailure = mine.videoId;
    note(`the copy was not saved: ${error.message}`);
    drawCacheButton();

    return;
  }

  copy = null;
  savedVideos.set(record.videoId, record);
  drawCacheButton();
  drawLinks(knownLinks, "");

  if (state.videoId === record.videoId) {
    // No message here: the floppy now carries the size, which says the video
    // is on the device better than a word, and the status line stays for what
    // changes.
    // The copy is ready in the middle of viewing — pick it up without losing the position.
    switchToCopy(record);
    // The translation goes on: saved cues are appended while the viewer is here.
    follow = { videoId: record.videoId, record, timer: null };
  } else {
    send(unwatchMessage(record.videoId));
  }
}

/** Cues go both into a not-yet-downloaded copy and into an already saved one. */
function rememberCues(message) {
  if (message.videoId === undefined || message.videoId === null) {
    return;   // a job event: it does not belong to a video
  }

  const list = message.type === "snapshot" || message.type === "cues" ? message.cues || [] : [];

  if (copy && message.videoId === copy.videoId) {
    for (const cue of list) {
      copy.cues.set(cue.i, cue);
    }

    copy.language = message.language || copy.language;
  }

  if (follow && message.videoId === follow.videoId) {
    follow.record.cues = mergeCues(follow.record.cues, list);
    follow.record.language = message.language || follow.record.language;
    followSoon();
  }
}

function followSoon() {
  if (!follow || follow.timer !== null) {
    return;
  }

  const mine = follow;

  mine.timer = setTimeout(() => {
    mine.timer = null;
    putVideo(db, mine.record).catch(() => {});
  }, FOLLOW_MS);
}

// --- Socket ----------------------------------------------------------------

function connectSocket() {
  if (socket) {
    try {
      socket.close();
    } catch {
      // The socket may have closed itself already; this does not bother the new one.
    }
  }

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/v1/ws`;
  const opening = new WebSocket(url);

  socket = opening;

  opening.addEventListener("open", () => {
    daemonAlive = true;

    // The copy and its cues survive leaving the stage: after a reconnect the
    // subscription is needed for them too, otherwise the record would break on
    // the reconnect.
    for (const videoId of new Set([state.videoId, copy && copy.videoId, follow && follow.videoId])) {
      if (videoId) {
        watch(videoId);
      }
    }
  });

  opening.addEventListener("message", (event) => {
    let message;

    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    handleMessage(message);
  });

  opening.addEventListener("close", () => {
    // The daemon restarted or the connection dropped — we reconnect silently:
    // the snapshot will bring back the whole state, there is nothing to lose.
    if (socket === opening) {
      socket = null;
      daemonAlive = false;
      setTimeout(connectSocket, RECONNECT_MS);
    }
  });
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return false;
  }

  socket.send(JSON.stringify(message));

  return true;
}

function watch(videoId, previous = null) {
  // The subscription will leave the open handler as soon as the connection
  // rises: there is no socket yet, and there is nothing here to remember it
  // with — it will subscribe by state.videoId.
  for (const message of watchMessages(
    videoId, positionFor(positions, videoId), clientId, previous,
  )) {
    send(message);
  }
}

function handleMessage(message) {
  // The copy catches cues before the check for its own video: equality by
  // videoId is more honest, because an event without videoId does not belong
  // to the copy at all.
  rememberCues(message);

  // Job events carry the video, but an older daemon did not subscribe it: we
  // count such an event as ours, otherwise the cues would disappear together
  // with a page reload. Someone else's is not ours — otherwise the cues of
  // the previous episode would land on the new episode's numbers.
  if (!isForVideo(message, state.videoId)) {
    return;
  }

  switch (message.type) {
    case "snapshot":
      takeSnapshot(message);
      break;
    case "state":
      state.stage = message.stage || "";
      state.waiting = message.waiting;
      state.error = message.message || null;
      state.retryable = Boolean(message.retryable);
      break;
    case "language":
      state.language = message.language || null;
      break;
    case "segmented":
      state.total = message.total || 0;
      state.pending = message.pending || [];
      break;
    case "cues":
      takeCues(message.cues || []);
      state.pending = message.pending || state.pending;
      state.translated = countTranslated();
      break;
    default:
      // ping holds the connection; there is nothing in it to show.
      return;
  }

  render();
  // On pause there are no frames, timeupdate is silent — otherwise a new
  // translation would not reach the screen until the resume. paint() is cheap:
  // an identical line does not touch the DOM.
  paint();
}

function takeSnapshot(snapshot) {
  cuesByIndex.clear();
  paintedKey = null;

  Object.assign(state, {
    videoId: snapshot.videoId || state.videoId,
    stage: snapshot.stage || "",
    waiting: snapshot.waiting,
    error: snapshot.error || null,
    retryable: Boolean(snapshot.retryable),
    language: snapshot.language || null,
    total: snapshot.total || 0,
    pending: snapshot.pending || [],
    title: snapshot.title || state.title,
    notice: "",
  });

  takeCues(snapshot.cues || []);
  state.translated = typeof snapshot.translated === "number"
    ? snapshot.translated
    : countTranslated();
}

function takeCues(list) {
  for (const cue of list) {
    cuesByIndex.set(cue.i, cue);
  }

  state.cues = [...cuesByIndex.values()].sort((a, b) => a.start - b.start);
}

/** A translated cue is one that has lines: the daemon counts it the same way. */
function countTranslated() {
  return state.cues.filter((cue) => (cue.lines || []).length > 0).length;
}

// --- Captions -------------------------------------------------------------

/**
 * Status is one line in the top bar. Different messages (what is being
 * prepared, what is wrong, in what language the video is) are separated by
 * dots: this is one place to read, not three rows in a row over the frame.
 */
function render() {
  const lines = [];
  const stage = stageText(state.stage, state);

  if (stage) {
    lines.push(stage);
  }

  if (state.notice) {
    lines.push(state.notice);
  }

  const language = languageText(state.language);

  if (language) {
    lines.push(language);
  }

  els.status.textContent = lines.join(" • ");
  els.status.hidden = lines.length === 0;
}

// --- Subtitle layer -------------------------------------------------------

// The layer's styles live in its shadow tree, not in style.css: the shadow
// boundary does not let outside rules in. The look is the same as in
// extension/content.js; --font and --overlay-scale are inherited across the
// boundary, so the wide screen's large font and the size chosen by the viewer
// arrive here by themselves.
const LAYER_CSS = `
  .box {
    max-width: 88%;
    padding: 6px 12px;
    border-radius: 8px;
    background: rgba(8, 8, 8, .78);
    text-align: center;
  }
  .line {
    margin: 0;
    color: #fff;
    /* --overlay-scale — the size chosen on the stage; a multiplier, not a
       replacement: on a wide screen the usual one is already larger. */
    font-size: calc(var(--font, 22px) * var(--overlay-scale, 1));
    line-height: 1.3;
    text-shadow: 0 1px 2px rgba(0, 0, 0, .9);
    white-space: pre-wrap;
  }
  .line + .line {
    margin-top: 4px;
  }
  .line[hidden] {
    display: none;
  }
  .box.failed .line {
    color: #ffd9a0;
    font-style: italic;
  }
  .box.hidden {
    display: none;
  }
`;

function layerNodes() {
  if (layer) {
    return layer;
  }

  const root = els.overlay.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  const box = document.createElement("div");
  const target = document.createElement("div");
  const secondary = document.createElement("div");

  style.textContent = LAYER_CSS;
  box.className = "box hidden";
  target.className = "line";
  secondary.className = "line";
  box.append(target, secondary);
  root.append(style, box);

  layer = { box, target, secondary };

  return layer;
}

function paint() {
  const nodes = layerNodes();
  const index = activeCueIndex(state.cues, els.video.currentTime);
  let lines = null;

  if (index >= 0) {
    const found = cueLines(state.cues[index]);

    // A cue without a translation does not go on screen: its place is taken
    // by a caption saying there is no translation yet, if it falls into an
    // untranslated piece.
    if (found.target) {
      lines = found;
    }
  }

  // "The translation is not ready yet…" stands in place of the Russian line:
  // switched it off — there is nothing to wait for either, the Montenegrin
  // line comes together with it.
  if (!lines && switches.target && pendingAt(state.pending, els.video.currentTime)) {
    lines = { target: "the translation is not ready yet…", secondary: "", failed: true };
  }

  if (lines) {
    lines = visibleLines(lines, switches);
  }

  // Both lines are off — the layer is silent as a whole, together with the
  // waiting caption.
  if (!lines || (!lines.target && !lines.secondary)) {
    paintedKey = null;
    nodes.box.classList.add("hidden");

    return;
  }

  const key = `${lines.target}\u0000${lines.secondary}\u0000${lines.failed}`;

  if (key === paintedKey) {
    return;
  }

  paintedKey = key;
  nodes.target.textContent = lines.target;
  nodes.target.hidden = !lines.target;
  nodes.secondary.textContent = lines.secondary;
  nodes.secondary.hidden = !lines.secondary;
  // failed changes the look of the first line only: the second goes dark only
  // when it is empty or repeats the first (cueLines has already decided that).
  nodes.box.classList.toggle("failed", lines.failed);
  nodes.box.classList.remove("hidden");
}

// The player's own button expands the <video> itself, while the line layer is
// a neighbouring element, and in this mode it is not visible. The browser does
// not let the element be swapped "after the fact": fullscreen mode requires a
// press, and a request from the fullscreenchange handler is rejected — you can
// see the screen expand and return at once. So the player's button is hidden
// (controlslist), and the stage is expanded by our own — on a press, when the
// browser allows.
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
    return;
  }

  els.stage.requestFullscreen().catch(() => {});
}

// Where there is nothing to hide the native button with (iOS), interception
// remains: if the video became fullscreen, we ask for the stage. A refusal no
// longer throws us out of the mode — otherwise it would be the same jump there
// and back.
function watchFullscreen() {
  document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement !== els.video || !els.stage.requestFullscreen) {
      return;
    }

    els.stage.requestFullscreen().catch(() => {});
  });
}

// The window is hidden and returned to: while it was away, a copy could be
// deleted in a neighbouring one (an installed app and a browser tab are
// different instances of the page). On return, the list asks the device, not
// its own memory.
function watchCopies() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      return;
    }

    openCache().then(() => {
      drawCacheButton();
      drawLinks(knownLinks, "");
    });
  });
}

function startClock() {
  // A frame callback is more precise than timeupdate: that one fires four
  // times a second and leaves the subtitle lagging a quarter second behind
  // the picture.
  if (typeof els.video.requestVideoFrameCallback === "function") {
    const tick = () => {
      paint();
      els.video.requestVideoFrameCallback(tick);
    };

    els.video.requestVideoFrameCallback(tick);

    return;
  }

  els.video.addEventListener("timeupdate", paint);
}

function reportPlayhead() {
  setInterval(() => {
    if (!state.videoId) {
      return;
    }

    // The position is remembered on pause too: that is exactly when the
    // viewer leaves, and on return they should land in the same place, not at
    // the beginning.
    savePosition();

    if (els.video.paused) {
      return;
    }

    send({
      op: "playhead", videoId: state.videoId, t: els.video.currentTime, clientId,
    });
  }, PLAYHEAD_MS);
}

function savePosition() {
  if (!state.videoId) {
    return;
  }

  positions = rememberPosition(positions, state.videoId, els.video.currentTime);
  store(POSITIONS_KEY, JSON.stringify(positions));
}

// --- Line switches --------------------------------------------------------

function drawSwitches() {
  els.toggleTarget.setAttribute("aria-pressed", String(switches.target));
  els.toggleStudy.setAttribute("aria-pressed", String(switches.study));
}

function flipLine(name) {
  switches = flipSwitch(switches, name);
  store(SWITCHES_KEY, JSON.stringify(switches));
  drawSwitches();
  // Redraw at once: on pause there are no frames, and the layer would not
  // update itself until the resume.
  paintedKey = null;
  paint();
}

// --- Line size ------------------------------------------------------------

function drawFontSize() {
  els.fontSize.value = fontSize;
  // The layer lives in a shadow root, but the attribute stands on the stage:
  // the rule for .line in LAYER_CSS finds it through an inherited variable.
  els.stage.dataset.font = fontSize;
}

function pickFontSize() {
  fontSize = readFontSize(els.fontSize.value);
  store(FONT_KEY, fontSize);
  drawFontSize();
}

// --- Leaving the stage ----------------------------------------------------

/** Return to the list: the browser's "back" button calls the same thing. */
/**
 * Bring back the status bar and the title and start the timer for their exit.
 *
 * They leave after exactly the same time as the player's own controls after
 * the last touch — and only while the video is playing: on pause the controls
 * do not disappear either, and then there is nothing to hide.
 */
function wakeStage() {
  clearTimeout(idleTimer);
  idleTimer = null;
  els.stage.classList.remove("idle");

  if (els.stage.hidden || els.video.paused) {
    return;
  }

  idleTimer = setTimeout(() => els.stage.classList.add("idle"), CONTROLS_IDLE_MS);
}

function leaveStage() {
  if (!state.videoId) {
    return;
  }

  // We leave — remember the position and let the video go. The exception is
  // copying running on it: without the subscription the saved cues would break
  // at the moment of leaving, so their collection goes on until the copy ends.
  savePosition();

  const copying = Boolean(copy && copy.videoId === state.videoId);

  if (follow && follow.videoId === state.videoId) {
    clearTimeout(follow.timer);
    follow.timer = null;
    putVideo(db, follow.record).catch(() => {});
    follow = null;
  }

  if (state.videoId && !copying) {
    send(unwatchMessage(state.videoId));
  }

  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }

  els.video.pause();
  els.video.removeAttribute("src");
  els.video.load();

  stopVideoPoll();

  state.videoId = null;
  state.notice = "";
  paintedKey = null;
  layerNodes().box.classList.add("hidden");

  els.stage.hidden = true;
  els.home.hidden = false;
  // We left the stage — there is nothing to dim anymore, and the timer must
  // not outlive the exit.
  clearTimeout(idleTimer);
  idleTimer = null;
  els.stage.classList.remove("idle");
  render();

  // While the viewer was on the stage, the daemon could finish downloading the
  // picture, and the copy could be deleted in another tab or through DevTools.
  // So first we reread the device database — the floppy promises a copy — and
  // at the same time ask the daemon for fresh readiness. Without the daemon
  // there is no one to ask, and the message about that matters more than the list.
  openCache()
    .then(() => (daemonAlive ? loadLinks() : drawLinks(knownLinks, "")))
    .catch(() => {});
}

// --- Launch ---------------------------------------------------------------

/** Shell on the device: without it the page opens only with a live daemon. */
async function registerWorker() {
  if (!("serviceWorker" in navigator)) {
    workerMissing = true;

    return;
  }

  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch {
    // Registration fails on http without a certificate, in old browsers and
    // without the network. "Without the network" is not a verdict on the
    // browser: if the shell is already installed, the page opened offline
    // precisely from it, and one must not promise that it will not be saved.
    workerMissing = !navigator.serviceWorker.controller;
  }
}

/** Warning about the missing worker: once per launch and once per device. */
function workerNote() {
  if (!workerMissing) {
    return "";
  }

  if (!workerWarning && !store(WORKER_KEY)) {
    store(WORKER_KEY, "1");
    workerWarning = "the browser will not keep the page for offline — https and a modern browser are required";
  }

  // Remembered until the end of the launch: the list is redrawn (a daemon
  // answer, a return to the window), and the warning must not be lost every time.
  return workerWarning;
}

async function boot() {
  els.linkForm.addEventListener("submit", addLink);
  els.linksFilter.addEventListener("input", () => {
    linksQuery = els.linksFilter.value;
    // A new query means new matches: the earlier "collapsed" does not apply to them.
    searchClosed = new Set();
    drawLinks(knownLinks, "");
  });
  els.toggleTarget.addEventListener("click", () => flipLine("target"));
  els.toggleStudy.addEventListener("click", () => flipLine("study"));
  els.back.innerHTML = iconMarkup("back");
  // Same way as the browser button: the history step laid down on entering
  // the video is removed rather than left hanging behind.
  els.back.addEventListener("click", () => history.back());
  els.fullscreen.addEventListener("click", toggleFullscreen);
  els.cache.addEventListener("click", toggleCache);
  els.fontSize.addEventListener("change", pickFontSize);
  // The status bar and the title fade on rest and return on any touch, key or
  // mouse movement — where the player's own controls wake up. We listen on the
  // window: presses on the native controls do not reach the stage.
  for (const type of ["pointermove", "pointerdown", "keydown"]) {
    window.addEventListener(type, wakeStage, { passive: true, capture: true });
  }
  els.video.addEventListener("play", wakeStage);
  // Pause and seeking are also a reason to show the bar: on pause the controls are visible.
  els.video.addEventListener("pause", wakeStage);
  els.video.addEventListener("seeked", wakeStage);
  drawSwitches();
  drawFontSize();

  // The episode list and the stage are one address, so the browser's "back"
  // on its own would change nothing. Our own history step is laid down on
  // entering a video, and returning along it brings back the list.
  window.addEventListener("popstate", leaveStage);
  watchFullscreen();
  watchCopies();

  startClock();
  reportPlayhead();

  // The app is visible at once: the daemon has no key, there is nothing to ask.
  els.app.hidden = false;
  start();

  // Registration follows and delays neither the screen nor the saved data.
  await registerWorker();

  // The list of links could have been drawn before its answer: the warning
  // appears on top of what is already drawn rather than disappearing with the
  // registration.
  const warning = workerNote();

  if (warning) {
    drawLinks(knownLinks, warning);
  }
}

boot();
