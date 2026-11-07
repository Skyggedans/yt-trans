// Pure page logic: what to show at a given second. No document, no fetch —
// node --test runs it, and app.js only calls it.

/** Index of the cue where start <= t < end; -1 means silence right now. */
export function activeCueIndex(cues, t) {
  for (let i = 0; i < cues.length; i += 1) {
    if (cues[i].start <= t && t < cues[i].end) {
      return i;
    }
  }

  return -1;
}

/** Whether t falls into an untranslated interval [start, end). */
export function pendingAt(pending, t) {
  for (const [start, end] of pending) {
    if (start <= t && t < end) {
      return true;
    }
  }

  return false;
}

const STAGE_WORDS = {
  probing: () => "reading the video…",
  fetching: () => "downloading the audio…",
  asr: () => "recognizing speech…",
  paused: () => "paused: not enough video memory",
};

/** Progress caption; on done it is empty, the screen should say nothing. */
export function stageText(stage, state) {
  const data = state || {};

  if (stage === "done") {
    return "";
  }

  if (stage === "error") {
    const text = data.error || "error";

    return data.retryable ? `${text} — you can retry` : text;
  }

  if (stage === "queued") {
    return data.waiting === "gpu"
      ? "waiting for the GPU: another job is using it"
      : "queued…";
  }

  if (stage === "translating") {
    if (typeof data.translated === "number" && typeof data.total === "number") {
      return `translating… ${data.translated} of ${data.total}`;
    }

    return "translating…";
  }

  const words = STAGE_WORDS[stage];

  return words ? words() : "";
}

/** Line about the speech language: the verdict and the daemon's note; empty when there is no verdict. */
export function languageText(language) {
  if (!language || !language.effective) {
    return "";
  }

  const parts = [`speech language: ${language.effective}`];

  if (language.reason === "track" && language.track) {
    parts.push(`author's track ${language.track}`);
  } else if (language.reason === "pin") {
    parts.push("pinned in the settings");
  } else if (language.reason === "detector" || language.reason === "floor") {
    parts.push(
      typeof language.confidence === "number"
        ? `by speech, ${Math.round(language.confidence * 100)}%`
        : "by speech",
    );
  }

  if (language.note) {
    parts.push(language.note);
  }

  return parts.join("; ");
}

/** Two lines of a cue. The second goes dark when there is nothing to show or it repeats the first. */
export function cueLines(cue) {
  const target = (cue.lines || []).join("\n");
  const study = cue.study || "";
  const secondary = target && study && study !== target ? study : "";

  return { target, secondary, failed: Boolean(cue.failed) };
}

/** Time the way a player shows it: hours appear only when they exist. */
export function formatTime(seconds) {
  const whole = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = String(whole % 60).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${rest}`;
  }

  return `${minutes}:${rest}`;
}

/** An event about another video is not ours: the socket is subscribed to whatever the page watches. */
export function isForVideo(message, videoId) {
  return (
    message.videoId === undefined
    || message.videoId === null
    || message.videoId === videoId
  );
}

/** Unsubscribing from a video: without it the old episode keeps sending its cues here. */
export function unwatchMessage(videoId) {
  return { op: "unwatch", videoId };
}

/** What to tell the socket on subscribe: unsubscribe from the previous one, subscribe, watch position. */
export function watchMessages(videoId, t, clientId, previous = null) {
  const messages = [];

  if (previous && previous !== videoId) {
    messages.push(unwatchMessage(previous));
  }

  messages.push({ op: "watch", videoId, targetLang: "ru", authorTrack: true });
  messages.push({
    op: "playhead",
    videoId,
    t: typeof t === "number" && Number.isFinite(t) && t > 0 ? t : 0,
    clientId,
  });

  return messages;
}

/** Watch positions from a localStorage string; broken and foreign fields are dropped. */
export function parsePositions(raw) {
  let data;

  try {
    data = JSON.parse(raw || "{}");
  } catch {
    return {};
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return {};
  }

  const positions = {};

  for (const [key, value] of Object.entries(data)) {
    if (typeof value === "number" && Number.isFinite(value) && value > 0) {
      positions[key] = value;
    }
  }

  return positions;
}

/** Where we stopped: 0 if the video has not been watched yet. */
export function positionFor(positions, videoId) {
  const value = positions ? positions[videoId] : undefined;

  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

/** Remember the position. Zero and non-numbers do not count as a position and erase the record. */
export function rememberPosition(positions, videoId, seconds) {
  const next = { ...(positions || {}) };

  if (typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0) {
    next[videoId] = Math.floor(seconds);
  } else {
    delete next[videoId];
  }

  return next;
}

/** Cue lines respecting the switches: the viewer decides what to see. */
export function visibleLines(lines, show) {
  const switches = show || {};
  const target = switches.target === false ? "" : lines.target;
  const secondary = switches.study === false ? "" : lines.secondary;

  // The "translation failed" mark belongs to the Russian line: without it
  // there is nothing to highlight.
  return { target, secondary, failed: target ? lines.failed : false };
}

/** Switches from localStorage. Anything unclear means "both on". */
export function readSwitches(raw) {
  const both = { target: true, study: true };
  let data;

  try {
    data = JSON.parse(raw || "{}");
  } catch {
    return both;
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return both;
  }

  return { target: data.target !== false, study: data.study !== false };
}

/** Flip one line without touching the other. */
export function flipSwitch(switches, name) {
  const next = {
    target: (switches || {}).target !== false,
    study: (switches || {}).study !== false,
  };

  next[name] = !next[name];

  return next;
}

// --- Search across saved links -----------------------------------------

/**
 * Whether the query appears in the text. An empty query matches everything:
 * a search field that finds nothing should not hide the list.
 */
export function matchesQuery(text, query) {
  const needle = String(query || "").trim().toLocaleLowerCase();

  if (!needle) {
    return true;
  }

  return String(text || "").toLocaleLowerCase().includes(needle);
}

/**
 * Links where the query appears in the playlist title, the address or a video
 * name. Video names come from videosOf: for a playlist they are learned from
 * the daemon, for a copy — from its own record on the device.
 */
export function filterLinks(items, query, videosOf = () => []) {
  const needle = String(query || "").trim();

  if (!needle) {
    return items;
  }

  return items.filter((link) => (
    matchesQuery(link.title, needle)
    || matchesQuery(link.url, needle)
    || (videosOf(link) || []).some((video) => matchesQuery(video.title, needle))
  ));
}

// --- A link that came from the share dialog ---------------------------

// Android puts the address into text (its share system has no url field),
// sometimes together with the video title, and wraps it in punctuation if the
// link stands in brackets or at the end of a sentence. We take the address,
// not the whole field: the daemon accepts YouTube only and builds the link
// from the address.
const YOUTUBE_HOSTS = /^(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)$/i;
const URL_IN_TEXT = /https?:\/\/[^\s<>"'«»]+/g;
const TRAILING = /[.,;:!?)\]»"']+$/;

export function youTubeUrlIn(text) {
  if (typeof text !== "string") {
    return "";
  }

  for (const found of text.match(URL_IN_TEXT) || []) {
    const candidate = found.replace(TRAILING, "");

    try {
      if (YOUTUBE_HOSTS.test(new URL(candidate).hostname)) {
        return candidate;
      }
    } catch {
      // Not an address — for example, "http://" without a host.
    }
  }

  return "";
}

/** The first YouTube link among the fields the share dialog sent. */
export function sharedUrl(params) {
  for (const name of ["url", "text", "title"]) {
    const value = params.get ? params.get(name) : params[name];
    const found = youTubeUrlIn(value);

    if (found) {
      return found;
    }
  }

  return "";
}

/** Subtitle sizes the page offers; the name is the same as in the markup. */
export const FONT_SIZES = ["tiny", "small", "normal", "large"];

/** The size from storage; a foreign or broken value means the usual size. */
export function readFontSize(raw) {
  return FONT_SIZES.includes(raw) ? raw : "normal";
}

// --- Video cache on the device -----------------------------------------
//
// Copy numbers — size, percentages, the verdict on free space — are computed
// here, not in app.js: the owner sees them, so they are worth checking in
// node --test.

const BYTE_UNITS = ["KB", "MB", "GB", "TB"];

/** Bytes the way a person reads them: "400 MB", "1.2 GB". */
export function formatBytes(bytes) {
  // Garbage and negative values are a dash, not "NaN MB": such a number means
  // nothing, and it must not be shown as a size.
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }

  if (bytes < 1024) {
    return `${Math.round(bytes)} B`;
  }

  let value = bytes;
  let unit = "B";

  for (const next of BYTE_UNITS) {
    value /= 1024;
    unit = next;

    if (value < 1024 || next === "TB") {
      break;
    }
  }

  // For small numbers we keep tenths ("1.5 GB"): whole ones would be too
  // coarse. For large ones they are already noise, and there we round to a
  // whole.
  const shown = value < 10
    ? Math.round(value * 10) / 10
    : Math.round(value);

  return `${shown} ${unit}`;
}

/** Percent downloaded: no division by zero and no going past a hundred. */
export function percent(seen, total) {
  if (!(total > 0) || !(seen > 0)) {
    return 0;
  }

  return Math.min(100, Math.floor((seen / total) * 100));
}

// Reserve for writing: the database and the browser itself need space too,
// and a quota filled to zero breaks the copy in the middle of the file.
// 200 MiB — about half an episode, which does not even fit whole.
export const SPARE_BYTES = 200 * 1024 * 1024;

/**
 * Whether to start copying: "ok" — yes, "tight" — there is less space than
 * needed with the reserve, "unknown" — nothing to judge by (size or space is
 * unknown). Uncertainty does not forbid copying: let the write itself decide.
 */
export function copyVerdict(need, free, reserve = SPARE_BYTES) {
  if (!(need > 0) || !(free > 0)) {
    return "unknown";
  }

  return need + reserve <= free ? "ok" : "tight";
}

/**
 * Caption for the cache icon. The action itself is named by the icon
 * (download, floppy or cancel), so here only what changes is left: waiting
 * and percentages.
 */
export function copyState({ waiting, active, percent: done = 0, failed } = {}) {
  if (failed) {
    return "error";
  }

  if (waiting) {
    return "waiting";
  }

  if (active) {
    return `${done}%`;
  }

  return "";
}

/** Mark of a saved video in the link list. */
export function cacheLabel(size) {
  return size > 0 ? `on the device · ${formatBytes(size)}` : "on the device";
}

/**
 * Title of a link row. The length of the only copy is the length of the video,
 * so it stands next to the title, and the copy row stays one. There are
 * several copies (a playlist) — no length here: one number would lie about
 * the others, and it stays with each copy. Without a copy the length comes
 * from the link: the daemon remembers it when it is added.
 */
export function linkTitle(link, copy = null) {
  const title = (link && (link.title || link.url)) || "";
  const fromCopy = copy && typeof copy.duration === "number" ? copy.duration : 0;
  const fromLink = link && typeof link.duration === "number" ? link.duration : 0;
  const seconds = fromCopy > 0 ? fromCopy : fromLink;

  return seconds > 0 ? `${title} · ${formatTime(seconds)}` : title;
}

// --- Expanded cards in the list ----------------------------------------
//
// A card key is "link:<id>" or "video:<id>". Expansion lives only until a
// reload: the list opens collapsed, and a card is expanded with a finger.

/** Expand or collapse one card without touching its neighbours. */
export function toggleExpanded(keys, key) {
  const next = Array.isArray(keys) ? [...keys] : [];
  const at = next.indexOf(key);

  if (at >= 0) {
    next.splice(at, 1);
  } else {
    next.push(key);
  }

  return next;
}

/**
 * Merge cues by number: the saved ones lie in the database, the incoming ones
 * arrive over the socket and are fuller, so they win. Order — by time, the
 * way the layer draws it.
 */
export function mergeCues(saved, incoming) {
  const byNumber = new Map();

  for (const cue of saved || []) {
    byNumber.set(cue.i, cue);
  }

  for (const cue of incoming || []) {
    byNumber.set(cue.i, cue);
  }

  return [...byNumber.values()].sort((a, b) => a.start - b.start);
}

/** Where to play from: a copy beats the daemon and survives its death. */
export function playSource(hasCopy, daemonAlive) {
  if (hasCopy) {
    return "copy";
  }

  return daemonAlive ? "daemon" : "none";
}
