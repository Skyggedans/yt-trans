import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeCueIndex, cueLines, filterLinks, flipSwitch, formatTime, isForVideo,
  languageText, matchesQuery, parsePositions, readFontSize, pendingAt, positionFor, readSwitches, rememberPosition, sharedUrl,
  stageText, unwatchMessage, visibleLines, watchMessages,
} from "./logic.js";

const CUES = [
  { i: 0, start: 0, end: 2, lines: ["Раз"], study: "Jedan", failed: false },
  { i: 1, start: 2, end: 4, lines: ["Два"], study: "", failed: false },
];

test("replica is active from its start up to, but not including, its end", () => {
  assert.equal(activeCueIndex(CUES, -1), -1);
  assert.equal(activeCueIndex(CUES, 0), 0);
  assert.equal(activeCueIndex(CUES, 1.999), 0);
  assert.equal(activeCueIndex(CUES, 2), 1);
  assert.equal(activeCueIndex(CUES, 4), -1);
  assert.equal(activeCueIndex([], 1), -1);
});

test("a gap is where the translation has not arrived yet", () => {
  const pending = [[0, 2], [4, 6]];

  assert.equal(pendingAt(pending, 1), true);
  assert.equal(pendingAt(pending, 2), false);
  assert.equal(pendingAt(pending, 5), true);
  assert.equal(pendingAt(pending, 6), false);
  assert.equal(pendingAt([], 1), false);
});

test("each stage has its own words, and a done job says nothing", () => {
  assert.equal(stageText("queued", { waiting: "gpu" }), "waiting for the GPU: another job is using it");
  assert.equal(stageText("queued", {}), "queued…");
  assert.equal(stageText("probing", {}), "reading the video…");
  assert.equal(stageText("fetching", {}), "downloading the audio…");
  assert.equal(stageText("asr", {}), "recognizing speech…");
  assert.equal(stageText("translating", { total: 10, translated: 3 }), "translating… 3 of 10");
  assert.equal(stageText("paused", {}), "paused: not enough video memory");
  assert.equal(stageText("done", {}), "");
  assert.equal(stageText("error", { error: "нет связи", retryable: true }), "нет связи — you can retry");
  assert.equal(stageText("error", { error: "нет связи", retryable: false }), "нет связи");
});

test("the language line names the verdict and repeats the daemon's note", () => {
  assert.equal(languageText(null), "");
  assert.equal(
    languageText({
      effective: "sr", reason: "detector", confidence: 0.97,
      note: "YouTube указал en, по речи sr",
    }),
    "speech language: sr; by speech, 97%; YouTube указал en, по речи sr",
  );
  assert.equal(
    languageText({ effective: "en", reason: "hint", confidence: null, note: null }),
    "speech language: en",
  );
  assert.equal(
    languageText({
      effective: "sr", reason: "track", track: "sr",
      note: "YouTube указал en, дорожка автора sr",
    }),
    "speech language: sr; author's track sr; YouTube указал en, дорожка автора sr",
  );
});

test("a cue gives two lines, and no study line without a translation", () => {
  assert.deepEqual(cueLines(CUES[0]), { target: "Раз", secondary: "Jedan", failed: false });

  assert.deepEqual(
    cueLines({ lines: ["A", "B"], study: "", failed: true }),
    { target: "A\nB", secondary: "", failed: true },
  );
  // An untranslated cue: study without a translation does not go on screen.
  assert.deepEqual(
    cueLines({ lines: [], study: "Jedan", failed: false }),
    { target: "", secondary: "", failed: false },
  );
  // The second line matched the first — we do not show it.
  assert.deepEqual(
    cueLines({ lines: ["Jedan"], study: "Jedan", failed: false }),
    { target: "Jedan", secondary: "", failed: false },
  );
});

test("a failed Russian line does not take the Montenegrin line away", () => {
  // The spec rule: the second line goes dark only when its language matched
  // the first, not when the translation failed. Otherwise a Russian failure
  // takes away exactly what all this was started for.
  assert.deepEqual(
    cueLines({ lines: ["Zdravo svima."], study: "Zdravo svima, narode.", failed: true }),
    { target: "Zdravo svima.", secondary: "Zdravo svima, narode.", failed: true },
  );
});

test("time is read the way a player shows it", () => {
  assert.equal(formatTime(0), "0:00");
  assert.equal(formatTime(65), "1:05");
  assert.equal(formatTime(3723), "1:02:03");
});

test("an event about another video is not this video's event", () => {
  // The socket has no videoId in job events: it is subscribed to everything
  // the page watches. Until the page unsubscribes from the previous episode,
  // its cues arrive here too — and match the new ones by number.
  assert.equal(isForVideo({ type: "cues" }, "abc"), true);
  assert.equal(isForVideo({ type: "snapshot", videoId: "abc" }, "abc"), true);
  assert.equal(isForVideo({ type: "cues", videoId: "xyz" }, "abc"), false);
  assert.equal(isForVideo({ type: "state", videoId: "xyz" }, null), false);
});

test("subscribing lets go of the previous episode before taking the next", () => {
  const first = watchMessages("abc", 0, "client-1");

  assert.deepEqual(first, [
    { op: "watch", videoId: "abc", targetLang: "ru", authorTrack: true },
    { op: "playhead", videoId: "abc", t: 0, clientId: "client-1" },
  ]);

  const second = watchMessages("xyz", 40, "client-1", "abc");

  assert.deepEqual(second, [
    { op: "unwatch", videoId: "abc" },
    { op: "watch", videoId: "xyz", targetLang: "ru", authorTrack: true },
    { op: "playhead", videoId: "xyz", t: 40, clientId: "client-1" },
  ]);
  // The same video again — nothing to unsubscribe from.
  assert.equal(watchMessages("abc", 0, "c", "abc").length, 2);
  assert.deepEqual(unwatchMessage("abc"), { op: "unwatch", videoId: "abc" });
});

test("the place watched is remembered per video, and rubbish is not a place", () => {
  assert.deepEqual(parsePositions(""), {});
  assert.deepEqual(parsePositions("{не json"), {});
  assert.deepEqual(parsePositions("[1,2]"), {});
  assert.deepEqual(
    parsePositions('{"abc": 12.7, "xyz": "да", "q": -3}'),
    { abc: 12.7 },
  );

  assert.equal(positionFor({ abc: 12.7 }, "abc"), 12.7);
  assert.equal(positionFor({}, "abc"), 0);
  assert.equal(positionFor(null, "abc"), 0);

  assert.deepEqual(rememberPosition({}, "abc", 12.7), { abc: 12 });
  assert.deepEqual(rememberPosition({ abc: 12 }, "abc", 0), {});
  assert.deepEqual(rememberPosition({ abc: 12 }, "xyz", NaN), { abc: 12 });
});

test("a switched-off line leaves the other one alone", () => {
  const lines = { target: "русский", secondary: "crnogorski", failed: false };

  assert.deepEqual(visibleLines(lines, { target: false, study: true }), {
    target: "", secondary: "crnogorski", failed: false,
  });
  assert.deepEqual(visibleLines(lines, { target: true, study: false }), {
    target: "русский", secondary: "", failed: false,
  });
});

test("switching the Russian line off also takes away its failed look", () => {
  const failed = { target: "перевод не вышел", secondary: "crnogorski", failed: true };

  assert.deepEqual(visibleLines(failed, { target: false, study: true }), {
    target: "", secondary: "crnogorski", failed: false,
  });
});

test("two switched-off lines leave the layer nothing to say", () => {
  assert.deepEqual(
    visibleLines({ target: "a", secondary: "b", failed: true }, { target: false, study: false }),
    { target: "", secondary: "", failed: false },
  );
});

test("the switches are remembered, and rubbish means both on", () => {
  assert.deepEqual(readSwitches(""), { target: true, study: true });
  assert.deepEqual(readSwitches("не json"), { target: true, study: true });
  assert.deepEqual(readSwitches("[false]"), { target: true, study: true });
  assert.deepEqual(readSwitches('{"target":false,"study":true}'), { target: false, study: true });
  assert.deepEqual(readSwitches('{"target":"нет"}'), { target: true, study: true });
});

test("a switch flips, and the other one does not move", () => {
  assert.deepEqual(flipSwitch({ target: true, study: true }, "study"), { target: true, study: false });
  assert.deepEqual(flipSwitch({ target: true, study: false }, "study"), { target: true, study: true });
  assert.deepEqual(flipSwitch({ target: true, study: true }, "target"), { target: false, study: true });
});

test("a shared link is found in the text Android puts it in", () => {
  const params = new URLSearchParams(
    "title=Neki+snimak&text=Neki+snimak+https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ%3Fsi%3Dabc&url=",
  );

  assert.equal(sharedUrl(params), "https://youtu.be/dQw4w9WgXcQ?si=abc");
});

test("the separate url field wins over the text", () => {
  const params = new URLSearchParams(
    "url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Daaa&text=https%3A%2F%2Fyoutu.be%2Fbbb",
  );

  assert.equal(sharedUrl(params), "https://www.youtube.com/watch?v=aaa");
});

test("a title alone can carry the link", () => {
  assert.equal(
    sharedUrl(new URLSearchParams("title=https%3A%2F%2Fyoutu.be%2Fccc")),
    "https://youtu.be/ccc",
  );
});

test("a stranger's link is not taken for a video", () => {
  assert.equal(sharedUrl(new URLSearchParams("text=смотри+https%3A%2F%2Fexample.com%2Fvideo")), "");
});

test("nothing shared means nothing to open", () => {
  assert.equal(sharedUrl(new URLSearchParams("")), "");
  assert.equal(sharedUrl(new URLSearchParams("text=просто+текст")), "");
});

test("punctuation right after the link does not stick to it", () => {
  assert.equal(
    sharedUrl(new URLSearchParams("text=(https%3A%2F%2Fyoutu.be%2Fddd).")),
    "https://youtu.be/ddd",
  );
});

const LINKS = [
  { id: "1", title: "Kako se pravi rakija", url: "https://youtu.be/aaa" },
  { id: "2", title: "Историја Црне Горе", url: "https://youtu.be/bbb" },
  { id: "3", title: "Без названия", url: "https://www.youtube.com/playlist?list=ccc" },
];

test("an empty search leaves the whole list", () => {
  assert.equal(filterLinks(LINKS, "").length, 3);
  assert.equal(filterLinks(LINKS, "   ").length, 3);
});

test("the search finds a title by part of a word, regardless of case", () => {
  assert.deepEqual(filterLinks(LINKS, "RAKI").map((link) => link.id), ["1"]);
  assert.deepEqual(filterLinks(LINKS, "горе").map((link) => link.id), ["2"]);
});

test("the address is searched too", () => {
  assert.deepEqual(filterLinks(LINKS, "playlist").map((link) => link.id), ["3"]);
});

test("nothing found is an empty list, not the whole one", () => {
  assert.deepEqual(filterLinks(LINKS, "ничего такого"), []);
});

test("a link without a title does not break the search", () => {
  assert.deepEqual(filterLinks([{ id: "4", url: "https://youtu.be/ddd" }], "ddd"), [{ id: "4", url: "https://youtu.be/ddd" }]);
});

test("the search reaches the names of the videos under a link", () => {
  // The playlist title says nothing about what is inside, and people usually
  // search for a video: a link with a matching video must stay in the list.
  const videos = {
    3: [
      { videoId: "v1", title: "Kako se kalio čelik" },
      { videoId: "v2", title: "Istorija Balkana" },
    ],
  };
  const named = (link) => videos[link.id] || [];

  assert.deepEqual(filterLinks(LINKS, "kalio", named).map((link) => link.id), ["3"]);
  assert.deepEqual(filterLinks(LINKS, "balkan", named).map((link) => link.id), ["3"]);
  // A match on a video name does not replace the search by the link's own title.
  assert.deepEqual(filterLinks(LINKS, "RAKI", named).map((link) => link.id), ["1"]);
});

test("a name matches by part of a word, and an empty query matches everything", () => {
  assert.equal(matchesQuery("Kako se kalio čelik", "KALIO"), true);
  assert.equal(matchesQuery("Kako se kalio čelik", "  čelik "), true);
  assert.equal(matchesQuery("Kako se kalio čelik", "bakar"), false);
  assert.equal(matchesQuery(undefined, ""), true);
});

test("the subtitle size is remembered, and rubbish means the usual one", () => {
  assert.equal(readFontSize("tiny"), "tiny");
  assert.equal(readFontSize("small"), "small");
  assert.equal(readFontSize("large"), "large");
  // "huge" was removed: a size saved as it falls back to the usual one.
  assert.equal(readFontSize("huge"), "normal");
  assert.equal(readFontSize(null), "normal");
  assert.equal(readFontSize(""), "normal");
  assert.equal(readFontSize("громадный"), "normal");
  assert.equal(readFontSize("[large]"), "normal");
});

// --- Video cache on the device -----------------------------------------

import {
  cacheLabel, copyState, copyVerdict, formatBytes, linkTitle, mergeCues,
  percent, playSource, toggleExpanded,
} from "./logic.js";

test("bytes are shown the way a person reads them", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(999), "999 B");
  assert.equal(formatBytes(1024), "1 KB");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(419430400), "400 MB");
  assert.equal(formatBytes(1288490188), "1.2 GB");
  // Garbage and negative values — not "-1 B" and not "NaN MB".
  assert.equal(formatBytes(-1), "—");
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes("400"), "—");
});

test("the progress bar never divides by zero and never passes a hundred", () => {
  assert.equal(percent(0, 100), 0);
  assert.equal(percent(50, 100), 50);
  assert.equal(percent(120, 100), 100);
  assert.equal(percent(1, 0), 0);
  assert.equal(percent(-5, 100), 0);
});

test("a copy is refused only when the space is really short", () => {
  const fourHundred = 419430400;
  const gib = 1073741824;

  assert.equal(copyVerdict(fourHundred, gib), "ok");
  // 500 MiB minus the 200 MiB reserve is less than 400 MiB: the write would
  // stop halfway.
  assert.equal(copyVerdict(fourHundred, 524288000), "tight");
  // The size was not asked for or the space is unknown: nothing to judge by,
  // but nothing to forbid with either.
  assert.equal(copyVerdict(0, gib), "unknown");
  assert.equal(copyVerdict(fourHundred, 0), "unknown");
  assert.equal(copyVerdict(fourHundred, 524288000, 0), "ok");
});

test("the cache switch says what it is doing", () => {
  // The button became an icon: the word "cache" went into it, and only what
  // changes stays on the button — percentages, waiting and failure. The size
  // of a finished copy is not written: the floppy has already said that the
  // video is on the device.
  assert.equal(copyState({}), "");
  assert.equal(copyState({ waiting: true }), "waiting");
  assert.equal(copyState({ active: true, percent: 37 }), "37%");
  assert.equal(copyState({ size: 419430400 }), "");
  assert.equal(copyState({ failed: true }), "error");
  // Copying is in progress — percentages matter more than the old size; an
  // error matters most of all.
  assert.equal(copyState({ active: true, percent: 5, size: 1000 }), "5%");
  assert.equal(copyState({ failed: true, size: 1000 }), "error");
});

test("a saved episode is marked with its size", () => {
  assert.equal(cacheLabel(419430400), "on the device · 400 MB");
  assert.equal(cacheLabel(0), "on the device");
});

test("a link row carries the length of its only copy", () => {
  // The length of the only copy is the video's length, and it must stand next
  // to the title, not as a separate row.
  assert.equal(linkTitle({ title: "Ролик" }, { duration: 960 }), "Ролик · 16:00");
  // No copy — the row stays one, without an invented length.
  assert.equal(linkTitle({ title: "Ролик" }), "Ролик");
  assert.equal(linkTitle({ title: "Ролик" }, { duration: 0 }), "Ролик");
  // The title did not arrive — show the address, not an empty string.
  assert.equal(linkTitle({ url: "https://youtu.be/x" }), "https://youtu.be/x");
});

test("a link row carries the length the daemon remembered", () => {
  // The daemon remembers the length when a link is added: without a copy on
  // the device the row used to say nothing about the duration.
  assert.equal(linkTitle({ title: "Ролик", duration: 960 }), "Ролик · 16:00");
  // A copy without a length must not erase the server one.
  assert.equal(linkTitle({ title: "Ролик", duration: 960 }, { duration: 0 }), "Ролик · 16:00");
  // The server does not know the length — nothing to invent.
  assert.equal(linkTitle({ title: "Ролик", duration: null }), "Ролик");
});

test("cards are toggled one at a time and nothing is open at start", () => {
  // Expansion lives only until a reload: the list opens collapsed, and a card
  // is expanded with a finger, one place at a time.
  assert.deepEqual(toggleExpanded([], "link:a"), ["link:a"]);
  assert.deepEqual(toggleExpanded(["link:a"], "link:b"), ["link:a", "link:b"]);
  assert.deepEqual(toggleExpanded(["link:a", "link:b"], "link:a"), ["link:b"]);
  assert.deepEqual(toggleExpanded(null, "link:a"), ["link:a"]);
});

test("saved and arrived cues are merged by their number", () => {
  const saved = [
    { i: 2, start: 4, lines: ["b"] },
    { i: 1, start: 0, lines: ["a"] },
  ];
  const incoming = [
    { i: 2, start: 4, lines: ["b", "c"] },
    { i: 3, start: 8, lines: ["d"] },
  ];

  assert.deepEqual(mergeCues([], incoming), [
    { i: 2, start: 4, lines: ["b", "c"] },
    { i: 3, start: 8, lines: ["d"] },
  ]);
  assert.deepEqual(mergeCues(saved, incoming), [
    { i: 1, start: 0, lines: ["a"] },
    { i: 2, start: 4, lines: ["b", "c"] },
    { i: 3, start: 8, lines: ["d"] },
  ]);
  assert.deepEqual(mergeCues(undefined, []), []);
});

test("the copy wins over the daemon, and without either there is nothing", () => {
  assert.equal(playSource(true, true), "copy");
  assert.equal(playSource(true, false), "copy");
  assert.equal(playSource(false, true), "daemon");
  assert.equal(playSource(false, false), "none");
});
