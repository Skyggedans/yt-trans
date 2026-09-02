/**
 * Транспорт до локального демона: длинные опросы поверх обычного HTTP.
 *
 * WebSocket здесь не работает, и это не чинится настройкой на нашей стороне.
 * Контекст расширения считается защищённым, поэтому браузер повышает ws://
 * до wss:// как смешанное содержимое. Исключение для локального адреса,
 * благодаря которому проходит обычный http://, на WebSocket не распространяется.
 * Замерено ловушкой на живом браузере: 13 попыток TLS против 10 успешных
 * обычных запросов к тому же порту.
 *
 * Длинный опрос это лечит целиком: тот же самый http://, который демонстрирует
 * работоспособность, плюс побочная выгода — короткие повторяющиеся запросы
 * сбрасывают таймер простоя service worker'а в Chrome, тогда как один долгий
 * его, наоборот, убивает после тридцати секунд.
 */

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = { host: "127.0.0.1", port: 8756, token: "", targetLang: "ru" };

// Пауза после сбоя. Демон может быть не запущен — долбиться в него чаще незачем.
const RETRY_MS = 3000;

/** Порты content script'ов по идентификатору вкладки. */
const tabs = new Map();

/** Какой ролик смотрит вкладка. */
const watching = new Map();

/** Идущие опросы по идентификатору ролика: { controller, stop }. */
const polls = new Map();

async function settings() {
  const stored = await api.storage.local.get(DEFAULTS);

  return { ...DEFAULTS, ...stored };
}

function baseUrl({ host, port }) {
  return `http://${host}:${port}`;
}

function viewers(videoId) {
  return [...watching.entries()].filter(([, id]) => id === videoId).map(([tabId]) => tabId);
}

function deliver(videoId, message) {
  for (const tabId of viewers(videoId)) {
    const port = tabs.get(tabId);

    if (!port) continue;

    try {
      // videoId проставляем здесь: демон о нём в событиях не пишет, а content
      // script обязан отбрасывать всё, что относится к предыдущему ролику.
      port.postMessage({ ...message, videoId });
    } catch {
      tabs.delete(tabId);
    }
  }
}

async function pollLoop(videoId) {
  let cursor = 0;

  while (polls.has(videoId)) {
    const config = await settings();

    if (!config.token) {
      deliver(videoId, { type: "helper", ok: false, reason: "нет токена" });
      await sleep(RETRY_MS);
      continue;
    }

    const controller = new AbortController();
    polls.get(videoId).controller = controller;

    const url =
      `${baseUrl(config)}/v1/jobs/${encodeURIComponent(videoId)}/events` +
      `?cursor=${cursor}&target=${encodeURIComponent(config.targetLang)}`;

    try {
      const response = await fetch(url, {
        headers: { Authorization: `Bearer ${config.token}` },
        signal: controller.signal,
        cache: "no-store",
      });

      if (!response.ok) {
        deliver(videoId, {
          type: "helper",
          ok: false,
          reason: response.status === 401 ? "неверный токен" : `демон ответил ${response.status}`,
        });
        await sleep(RETRY_MS);
        continue;
      }

      const data = await response.json();

      cursor = data.cursor ?? cursor;

      if (data.snapshot) deliver(videoId, data.snapshot);

      for (const event of data.events || []) deliver(videoId, event);
    } catch (error) {
      // Отмена — это наш собственный unwatch, а не сбой.
      if (error.name === "AbortError") return;

      deliver(videoId, { type: "helper", ok: false, reason: "нет связи с демоном" });
      await sleep(RETRY_MS);
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function startPolling(videoId) {
  if (polls.has(videoId)) return;

  polls.set(videoId, { controller: null });
  pollLoop(videoId);
}

function stopPolling(videoId) {
  const poll = polls.get(videoId);

  if (!poll) return;

  polls.delete(videoId);

  // Обрываем висящий запрос, иначе он додержит соединение до конца ожидания.
  if (poll.controller) poll.controller.abort();
}

async function sendPlayhead(videoId, t) {
  const config = await settings();

  if (!config.token) return;

  try {
    await fetch(`${baseUrl(config)}/v1/jobs/${encodeURIComponent(videoId)}/playhead`, {
      method: "POST",
      headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ t }),
    });
  } catch {
    // Позиция просмотра — подсказка приоритета, а не команда. Потеря её
    // ничего не ломает: следующая придёт через секунду.
  }
}

api.runtime.onConnect.addListener((port) => {
  if (port.name !== "yt-trans") return;

  const tabId = port.sender?.tab?.id;

  if (tabId === undefined) return;

  tabs.set(tabId, port);

  port.onMessage.addListener((message) => {
    if (message.op === "watch") {
      const previous = watching.get(tabId);

      if (previous && previous !== message.videoId && viewers(previous).length <= 1) {
        stopPolling(previous);
      }

      watching.set(tabId, message.videoId);
      startPolling(message.videoId);
    } else if (message.op === "playhead") {
      sendPlayhead(message.videoId, message.t);
    } else if (message.op === "unwatch") {
      watching.delete(tabId);

      if (viewers(message.videoId).length === 0) stopPolling(message.videoId);
    }
  });

  port.onDisconnect.addListener(() => {
    const videoId = watching.get(tabId);

    tabs.delete(tabId);
    watching.delete(tabId);

    if (videoId && viewers(videoId).length === 0) stopPolling(videoId);
  });
});

// Смена адреса или токена: обрываем опросы, они переподключатся с новыми
// настройками на следующем витке цикла.
api.storage.onChanged.addListener((changes) => {
  if (changes.token || changes.host || changes.port) {
    // По копии: stopPolling меняет polls, а менять коллекцию во время обхода нельзя.
    for (const videoId of [...polls.keys()]) stopPolling(videoId);

    for (const videoId of new Set(watching.values())) startPolling(videoId);
  }
});
