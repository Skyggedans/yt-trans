// The page shell without the daemon. While the computer is on, the page comes
// from it and updates at once; when it is off — from this cache.
//
// The version name changes together with the set of files: the old cache is
// then deleted on activation, and no mixture of two releases is left on the
// tablet.
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

  // The daemon's live data and the certificate are not the shell: they must
  // not be served from the cache, otherwise the page would show yesterday's
  // subtitles.
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);

  if (url.origin !== self.location.origin || url.pathname.startsWith("/v1/") || url.pathname === "/ca.crt") {
    return;
  }

  // Network first: the page must update at once, and the cache is only a
  // substitute for when the daemon is not there.
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
