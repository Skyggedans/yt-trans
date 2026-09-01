/**
 * Транспорт до локального демона.
 *
 * Соединение держит service worker, а не content script. Причина в том, что
 * запрос из content script уходит от имени https://www.youtube.com, то есть
 * получает и правила CORS страницы, и гарантированный запрос разрешения на
 * доступ к локальной сети — прямо на ютубе. Из service worker источником
 * выступает само расширение.
 *
 * WebSocket, а не SSE: в MV3 воркер умирает, если ответ fetch идёт дольше
 * 30 секунд, а обработка двадцатиминутного ролика занимает минуты. Трафик по
 * сокету, наоборот, сбрасывает таймер простоя, поэтому демон шлёт пинг раз
 * в 15 секунд и соединение поддерживает воркер живым само.
 */

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = { host: "127.0.0.1", port: 8756, token: "", targetLang: "ru" };
const RECONNECT_MS = 2000;

let socket = null;
let connecting = false;
let reconnectTimer = null;

/** Порты content script'ов по идентификатору вкладки. */
const tabs = new Map();

/** Кто какой ролик смотрит: нужно, чтобы переподключение восстановило подписки. */
const watching = new Map();

async function settings() {
  const stored = await api.storage.local.get(DEFAULTS);

  return { ...DEFAULTS, ...stored };
}

function broadcast(message) {
  for (const [tabId, port] of tabs) {
    if (watching.get(tabId) === message.videoId || !message.videoId) {
      try {
        port.postMessage(message);
      } catch {
        tabs.delete(tabId);
      }
    }
  }
}

async function connect() {
  if (socket || connecting) return;

  const { host, port, token } = await settings();

  if (!token) {
    broadcast({ type: "helper", ok: false, reason: "нет токена" });

    return;
  }

  connecting = true;

  try {
    const ws = new WebSocket(`ws://${host}:${port}/v1/ws?token=${encodeURIComponent(token)}`);

    ws.addEventListener("open", () => {
      socket = ws;
      connecting = false;
      broadcast({ type: "helper", ok: true });

      // Восстанавливаем подписки: демон о нас ничего не помнит между соединениями.
      for (const videoId of new Set(watching.values())) {
        ws.send(JSON.stringify({ op: "watch", videoId }));
      }
    });

    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);

      if (message.type === "ping") return;

      broadcast(message);
    });

    ws.addEventListener("close", () => {
      socket = null;
      connecting = false;
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      // Отличить отказ разрешения на локальную сеть от «демон не запущен»
      // из WebSocket нельзя: событие error намеренно лишено подробностей.
      // Диагностику даёт страница настроек — она пробует fetch и читает текст ошибки.
      broadcast({ type: "helper", ok: false, reason: "нет связи с демоном" });
    });
  } catch (error) {
    connecting = false;
    broadcast({ type: "helper", ok: false, reason: String(error) });
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectTimer || watching.size === 0) return;

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RECONNECT_MS);
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));

    return true;
  }

  connect();

  return false;
}

api.runtime.onConnect.addListener((port) => {
  if (port.name !== "yt-trans") return;

  const tabId = port.sender?.tab?.id;

  if (tabId === undefined) return;

  tabs.set(tabId, port);

  port.onMessage.addListener(async (message) => {
    if (message.op === "watch") {
      watching.set(tabId, message.videoId);

      const { targetLang } = await settings();

      // watch отправляется и при повторном подключении по тому же ролику:
      // на стороне демона это единственный путь к возобновлению задания,
      // приостановленного из-за того, что карту забрал пользователь.
      send({ op: "watch", videoId: message.videoId, targetLang });
    } else if (message.op === "playhead") {
      send({ op: "playhead", videoId: message.videoId, t: message.t });
    } else if (message.op === "unwatch") {
      watching.delete(tabId);

      // Отписываемся только если ролик не смотрит никто другой.
      if (![...watching.values()].includes(message.videoId)) {
        send({ op: "unwatch", videoId: message.videoId });
      }
    }
  });

  port.onDisconnect.addListener(() => {
    const videoId = watching.get(tabId);

    tabs.delete(tabId);
    watching.delete(tabId);

    if (videoId && ![...watching.values()].includes(videoId)) {
      send({ op: "unwatch", videoId });
    }
  });
});

// Пересоздаём соединение, если поменяли адрес или токен в настройках.
api.storage.onChanged.addListener((changes) => {
  if (changes.token || changes.host || changes.port) {
    if (socket) socket.close();

    socket = null;
    connect();
  }
});
