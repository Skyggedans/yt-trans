import assert from "node:assert/strict";
import { test } from "node:test";

import { diagnostics } from "./diagnostics.js";

const FULL = {
  ok: true,
  models: {
    asr: { model: "large-v3", compute_type: "float16", language: "auto", vram_mib: 6144 },
    translator: { model: "qwen3.8:27b", vram_mib: 17800, study_language: "cnr" },
  },
  gpu: { free_mib: 20231, busy: false },
  cache: { tracks: 5 },
};

test("the full answer is spelled out line by line", () => {
  const text = diagnostics(FULL, "");

  assert.match(text, /Recognition: large-v3 \(float16, ~6144 MiB\)/);
  assert.match(text, /Translation: qwen3\.8:27b \(~17800 MiB\)/);
  assert.match(text, /Source language: auto/);
  assert.match(text, /Second line: cnr/);
  assert.match(text, /Free on the GPU: 20231 MiB/);
  assert.match(text, /Tracks in cache: 5/);
});

test("a silent second line reads as off", () => {
  const translator = { model: "qwen3.8:27b", vram_mib: 17800 };
  const text = diagnostics({ ...FULL, models: { ...FULL.models, translator } }, "");

  assert.match(text, /Second line: off/);
});

test("a bare alive is explained, not printed as undefined", () => {
  // A daemon on all interfaces hides the models from clients outside the machine
  // and answers {"ok": true} alone. The page used to show "Recognition: undefined".
  const text = diagnostics({ ok: true }, "");

  assert.doesNotMatch(text, /undefined/);
  assert.doesNotMatch(text, /Recognition:/);
  assert.match(text, /alive/);
  assert.match(text, /config\.toml/);
});

test("the note about the switched protocol stays in front", () => {
  const switched = "The daemon answered over https, not http — switched the setting.\n\n";

  assert.ok(diagnostics(FULL, switched).startsWith(switched));
  assert.ok(diagnostics({ ok: true }, switched).startsWith(switched));
});
