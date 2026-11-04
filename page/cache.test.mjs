// Скачивание копии проверяется настоящим Response и настоящими потоками:
// подделанная сеть здесь — только `fetch`, всё остальное живёт по-настоящему.
import test from "node:test";
import assert from "node:assert/strict";

import { downloadVideo } from "./cache.js";

const bytes = (text) => new TextEncoder().encode(text);

test("a finished copy is the whole file and its size", async () => {
  globalThis.fetch = async () => new Response(bytes("0123456789"), {
    headers: { "Content-Length": "10" },
  });

  const seen = [];
  const copy = await downloadVideo("/v1/videos/x/file", {
    onProgress: (loaded, total) => seen.push([loaded, total]),
  });

  assert.equal(copy.size, 10);
  assert.equal(await copy.blob.text(), "0123456789");
  assert.deepEqual(seen.at(-1), [10, 10]);
});

test("progress reaches every chunk, not only the last", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes("abc"));
      controller.enqueue(bytes("de"));
      controller.close();
    },
  });
  globalThis.fetch = async () => new Response(stream, { headers: { "Content-Length": "5" } });

  const seen = [];
  await downloadVideo("/v1/videos/x/file", {
    onProgress: (loaded, total) => seen.push([loaded, total]),
  });

  assert.deepEqual(seen, [[3, 5], [5, 5]]);
});

test("a file without a length is still a copy", async () => {
  globalThis.fetch = async () => new Response(bytes("abc"));

  const copy = await downloadVideo("/v1/videos/x/file");

  assert.equal(copy.size, 3);
});

test("a daemon that refuses does not become a copy", async () => {
  globalThis.fetch = async () => new Response("нет", { status: 404 });

  await assert.rejects(downloadVideo("/v1/videos/x/file"), /демон ответил 404/);
});

test("cancelling stops the copy and leaves nothing behind", async () => {
  // Поток, который сам никогда не кончится: остановить его может только отмена.
  const stream = new ReadableStream({
    pull(controller) {
      controller.enqueue(bytes("abcd"));
    },
  });
  globalThis.fetch = async () => new Response(stream);

  const controller = new AbortController();
  const copy = downloadVideo("/v1/videos/x/file", { signal: controller.signal });
  controller.abort();

  await assert.rejects(copy, (error) => error.name === "AbortError");
});
