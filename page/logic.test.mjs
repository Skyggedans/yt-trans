import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeCueIndex, cueLines, formatTime, languageText, pendingAt, stageText,
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
