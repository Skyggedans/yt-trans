import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeCueIndex, cueLines, formatTime, isForVideo, languageText, parsePositions,
  pendingAt, positionFor, rememberPosition, stageText, unwatchMessage, watchMessages,
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
