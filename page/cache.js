// Storage of copies on the device and downloading a video whole. The module
// does not touch the DOM and does not import logic.js: it knows only its own
// database and the byte stream, so node --test runs it whole.

export const DB_NAME = "yt-trans";
export const DB_VERSION = 1;
export const VIDEOS = "videos";
export const META = "meta";

const abortError = () => new DOMException("copy cancelled", "AbortError");

// --- Downloading -------------------------------------------------------

/** The whole response body. An interrupted body is an error, not an empty answer. */
export async function readJson(response) {
  const text = await response.text();

  return text ? JSON.parse(text) : {};
}

/** Reading a chunk that does not hang after cancellation: a race of read and signal. */
function readOrAbort(reader, signal) {
  if (!signal) {
    return reader.read();
  }

  if (signal.aborted) {
    return Promise.reject(abortError());
  }

  return Promise.race([
    reader.read(),
    new Promise((_, fail) => signal.addEventListener("abort", () => fail(abortError()), { once: true })),
  ]);
}

/**
 * The body as a stream: chunks go on as they are read instead of piling up in
 * an array. The page does not keep its own mp4 in memory — a Blob takes it.
 */
export function collectBody(reader, { onProgress = () => {}, signal = null, total = 0 } = {}) {
  let seen = 0;

  return new ReadableStream({
    async pull(controller) {
      try {
        const { done, value } = await readOrAbort(reader, signal);

        if (done) {
          controller.close();

          return;
        }

        seen += value.length;
        onProgress(seen, total);
        controller.enqueue(value);
      } catch (error) {
        controller.error(error);
      }
    },
    cancel() {
      return reader.cancel().catch(() => {});
    },
  });
}

/**
 * Download a video. The copy reaches the database only when it returns from
 * here: an interrupted download leaves nothing on the device.
 */
export async function downloadVideo(url, { onProgress = () => {}, signal = null } = {}) {
  const response = await fetch(url, { signal });

  if (!response.ok) {
    throw new Error(`daemon answered ${response.status}`);
  }

  const total = Number(response.headers.get("Content-Length") || 0);
  const body = collectBody(response.body.getReader(), { onProgress, signal, total });
  const blob = await new Response(body, {
    headers: { "Content-Type": "video/mp4" },
  }).blob();

  return { blob, size: blob.size };
}

// --- Database ----------------------------------------------------------

function ask(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** Write completion is `tx.oncomplete`: `onsuccess` means "accepted", not "stored". */
function done(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

export function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = () => {
      const db = request.result;

      // videos — one record per video, together with the mp4 itself and its cues.
      if (!db.objectStoreNames.contains(VIDEOS)) {
        db.createObjectStore(VIDEOS, { keyPath: "videoId" });
      }

      // meta — small things by key: the last list of links.
      if (!db.objectStoreNames.contains(META)) {
        db.createObjectStore(META, { keyPath: "key" });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function putVideo(db, record) {
  const tx = db.transaction(VIDEOS, "readwrite");

  tx.objectStore(VIDEOS).put(record);

  await done(tx);
}

export function getVideo(db, videoId) {
  const tx = db.transaction(VIDEOS, "readonly");

  return ask(tx.objectStore(VIDEOS).get(videoId));
}

/** A list for the home screen: the mp4 itself is not needed, and pulling 400 MB into memory for a row is not either. */
export async function listVideos(db) {
  const tx = db.transaction(VIDEOS, "readonly");
  const records = await ask(tx.objectStore(VIDEOS).getAll());

  return records.map(({ blob, ...rest }) => rest);
}

export async function deleteVideo(db, videoId) {
  const tx = db.transaction(VIDEOS, "readwrite");

  tx.objectStore(VIDEOS).delete(videoId);

  await done(tx);
}

export async function getMeta(db, key) {
  const tx = db.transaction(META, "readonly");
  const row = await ask(tx.objectStore(META).get(key));

  return row ? row.value : null;
}

export async function putMeta(db, key, value) {
  const tx = db.transaction(META, "readwrite");

  tx.objectStore(META).put({ key, value });

  await done(tx);
}

// --- Space on the device -----------------------------------------------

/** How much still fits; 0 — the browser did not say, and the verdict will be "unknown". */
export async function freeBytes() {
  try {
    const { quota = 0, usage = 0 } = await navigator.storage.estimate();

    return Math.max(0, quota - usage);
  } catch {
    return 0;
  }
}

/** Persistent storage: otherwise the browser may evict the copy when space runs short. */
export async function askPersist() {
  try {
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}
