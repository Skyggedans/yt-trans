// Хранилище копий на устройстве и скачивание ролика целиком. Модуль не трогает
// DOM и не импортирует logic.js: он знает только свою базу и поток байт,
// поэтому его целиком гоняет node --test.

export const DB_NAME = "yt-trans";
export const DB_VERSION = 1;
export const VIDEOS = "videos";
export const META = "meta";

const abortError = () => new DOMException("копирование отменено", "AbortError");

// --- Скачивание --------------------------------------------------------

/** Чтение куска, которое не зависает после отмены: гонка чтения и сигнала. */
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
 * Скачать ролик в память целиком. Копия попадает в базу только по возврату
 * отсюда: прерванное скачивание не оставляет на устройстве ничего.
 */
export async function downloadVideo(url, { onProgress = () => {}, signal = null } = {}) {
  const response = await fetch(url, { signal });

  if (!response.ok) {
    throw new Error(`демон ответил ${response.status}`);
  }

  const total = Number(response.headers.get("Content-Length") || 0);
  const reader = response.body.getReader();
  const chunks = [];
  let seen = 0;

  try {
    for (;;) {
      // Поток читается вручную, а не `new Response(stream).blob()`: отмена
      // должна срабатывать, пока чтение висит, а не после конца файла.
      const { done, value } = await readOrAbort(reader, signal);

      if (done) {
        break;
      }

      chunks.push(value);
      seen += value.length;
      onProgress(seen, total);
    }
  } finally {
    reader.cancel().catch(() => {});
  }

  const blob = new Blob(chunks, { type: "video/mp4" });

  return { blob, size: blob.size };
}

// --- База --------------------------------------------------------------

function ask(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** Сделанность записи — по `tx.oncomplete`: `onsuccess` значит «принято», а не «легло». */
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

      // videos — по ролику на запись, вместе с самим mp4 и его титрами.
      if (!db.objectStoreNames.contains(VIDEOS)) {
        db.createObjectStore(VIDEOS, { keyPath: "videoId" });
      }

      // meta — мелкие вещи по ключу: последний список ссылок.
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

/** Список для главной: сам mp4 не нужен, а тянуть 400 МБ в память ради строки — нет. */
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

// --- Место на устройстве -----------------------------------------------

/** Сколько ещё влезет; 0 — браузер не сказал, и приговор будет «unknown». */
export async function freeBytes() {
  try {
    const { quota = 0, usage = 0 } = await navigator.storage.estimate();

    return Math.max(0, quota - usage);
  } catch {
    return 0;
  }
}

/** Постоянное хранилище: иначе браузер вправе вытеснить копию при нехватке места. */
export async function askPersist() {
  try {
    return await navigator.storage.persist();
  } catch {
    return false;
  }
}
