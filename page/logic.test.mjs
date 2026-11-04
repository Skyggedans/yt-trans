import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeCueIndex, cueLines, filterLinks, flipSwitch, formatTime, isForVideo,
  languageText, parsePositions, readFontSize, pendingAt, positionFor, readSwitches, rememberPosition, sharedUrl,
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
  assert.equal(stageText("queued", { waiting: "gpu" }), "ждём видеокарту: её занимает другая работа");
  assert.equal(stageText("queued", {}), "в очереди…");
  assert.equal(stageText("probing", {}), "читаю ролик…");
  assert.equal(stageText("fetching", {}), "качаю звук…");
  assert.equal(stageText("asr", {}), "распознаю речь…");
  assert.equal(stageText("translating", { total: 10, translated: 3 }), "перевожу… 3 из 10");
  assert.equal(stageText("paused", {}), "пауза: не хватило видеопамяти");
  assert.equal(stageText("done", {}), "");
  assert.equal(stageText("error", { error: "нет связи", retryable: true }), "нет связи — можно повторить");
  assert.equal(stageText("error", { error: "нет связи", retryable: false }), "нет связи");
});

test("the language line names the verdict and repeats the daemon's note", () => {
  assert.equal(languageText(null), "");
  assert.equal(
    languageText({
      effective: "sr", reason: "detector", confidence: 0.97,
      note: "YouTube указал en, по речи sr",
    }),
    "язык речи: sr; по речи, 97%; YouTube указал en, по речи sr",
  );
  assert.equal(
    languageText({ effective: "en", reason: "hint", confidence: null, note: null }),
    "язык речи: en",
  );
  assert.equal(
    languageText({
      effective: "sr", reason: "track", track: "sr",
      note: "YouTube указал en, дорожка автора sr",
    }),
    "язык речи: sr; дорожка автора sr; YouTube указал en, дорожка автора sr",
  );
});

test("a cue gives two lines, and no study line without a translation", () => {
  assert.deepEqual(cueLines(CUES[0]), { target: "Раз", secondary: "Jedan", failed: false });

  assert.deepEqual(
    cueLines({ lines: ["A", "B"], study: "", failed: true }),
    { target: "A\nB", secondary: "", failed: true },
  );
  // Непереведённая реплика: study без перевода на экран не идёт.
  assert.deepEqual(
    cueLines({ lines: [], study: "Jedan", failed: false }),
    { target: "", secondary: "", failed: false },
  );
  // Вторая строка совпала с первой — её не показываем.
  assert.deepEqual(
    cueLines({ lines: ["Jedan"], study: "Jedan", failed: false }),
    { target: "Jedan", secondary: "", failed: false },
  );
});

test("a failed Russian line does not take the Montenegrin line away", () => {
  // Правило спеки: вторая строка гаснет только когда её язык совпал с первой,
  // а не когда не удался перевод. Иначе неудача русского отнимает ровно то,
  // ради чего всё затеяно.
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
  // У сокета нет поля videoId в событиях задания: он подписан на всё, что
  // смотрит. Пока страница не отписалась от прежнего эпизода, его реплики
  // приходят сюда же — и по номерам совпадают с новыми.
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
  // Тот же ролик заново — отписываться не от чего.
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

test("the subtitle size is remembered, and rubbish means the usual one", () => {
  assert.equal(readFontSize("tiny"), "tiny");
  assert.equal(readFontSize("small"), "small");
  assert.equal(readFontSize("large"), "large");
  // «Огромный» убран: сохранённый им размер сводится к обычному.
  assert.equal(readFontSize("huge"), "normal");
  assert.equal(readFontSize(null), "normal");
  assert.equal(readFontSize(""), "normal");
  assert.equal(readFontSize("громадный"), "normal");
  assert.equal(readFontSize("[large]"), "normal");
});
