// Оболочка страницы без демона. Пока компьютер включён, страница приходит от
// него и обновляется сразу; когда выключен — из этого кеша.
//
// Имя версии меняется вместе с набором файлов: старый кеш тогда удаляется на
// активации, и на планшете не остаётся смеси из двух выпусков.
const VERSION = "yt-trans-1";
const SHELL = [
  "/", "/app.js", "/logic.js", "/cache.js", "/style.css",
  "/manifest.json", "/icon-192.png", "/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    await cache.addAll(SHELL);
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name !== VERSION) {
        await caches.delete(name);
      }
    }

    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Живые данные демона и сертификат — не оболочка: их нельзя отдавать
  // из кеша, иначе страница показывала бы вчерашние титры.
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);

  if (url.origin !== self.location.origin || url.pathname.startsWith("/v1/") || url.pathname === "/ca.crt") {
    return;
  }

  // Сеть первой: страница должна обновляться сразу, кеш — только замена
  // на случай, когда демона нет.
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      const cache = await caches.open(VERSION);
      cache.put(request, response.clone()).catch(() => {});

      return response;
    } catch {
      const cached = await caches.match(request);

      if (cached) {
        return cached;
      }

      if (request.mode === "navigate") {
        const shell = await caches.match("/");

        if (shell) {
          return shell;
        }
      }

      return new Response("", { status: 504 });
    }
  })());
});
