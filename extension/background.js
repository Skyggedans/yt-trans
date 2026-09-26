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

const DEFAULTS = {
  host: "127.0.0.1",
  port: 8756,
  token: "",
  targetLang: "ru",
  // Готовая дорожка автора вместо распознавания, если она у ролика есть.
  useAuthorTrack: true,
};

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
  // Был ли сбой на прошлом витке. Нужен, чтобы сообщить о ВОССТАНОВЛЕНИИ связи:
  // без этого сообщения надпись об обрыве оставалась на кнопке навсегда — опрос
  // через три секунды успешно возобновлялся, но успех никому не сообщался,
  // и зритель видел «нет связи с демоном» при совершенно живом демоне.
  let broken = false;

  while (polls.has(videoId)) {
    const poll = polls.get(videoId);
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
      `?cursor=${poll.cursor}&target=${encodeURIComponent(config.targetLang)}` +
      `&authorTrack=${config.useAuthorTrack === false ? "false" : "true"}` +
      `&create=${poll.create ? "true" : "false"}`;

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
        broken = true;
        await sleep(RETRY_MS);
        continue;
      }

      const data = await response.json();

      if (broken) {
        broken = false;
        deliver(videoId, { type: "helper", ok: true });
      }

      // Задания на демоне больше нет: его перезапустили или реестр вытеснил
      // готовое. Создавать новое мы не вправе — согласия на это не давали.
      if (data.gone) {
        deliver(videoId, { type: "gone" });
        stopPolling(videoId);

        return;
      }

      poll.cursor = data.cursor ?? poll.cursor;

      if (data.snapshot) deliver(videoId, data.snapshot);

      for (const event of data.events || []) deliver(videoId, event);
    } catch (error) {
      // Отмена — это наш собственный unwatch, а не сбой.
      if (error.name === "AbortError") return;

      broken = true;
      deliver(videoId, { type: "helper", ok: false, reason: "нет связи с демоном" });
      await sleep(RETRY_MS);
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Спросить демон, есть ли уже что-то по этому ролику, и подхватить.
 *
 * Зовётся при каждой загрузке страницы, поэтому обязана быть дешёвой и молчаливой:
 * запрос локальный, работы не запускает, а если демона нет или ролик незнаком —
 * просто ничего не делает, и зритель видит обычную кнопку запуска.
 */
async function restore(tabId, videoId, duration) {
  const config = await settings();

  if (!config.token) return;

  const url =
    `${baseUrl(config)}/v1/jobs/${encodeURIComponent(videoId)}/restore` +
    `?target=${encodeURIComponent(config.targetLang)}` +
    `&authorTrack=${config.useAuthorTrack === false ? "false" : "true"}` +
    // Ноль — это «плеер ещё не прочитал метаданные», а не длительность. Уйди он
    // в запрос как настоящий, сверка с кэшем провалилась бы, и восстановление
    // молча не сработало бы именно на быстрой перезагрузке.
    (Number.isFinite(duration) && duration > 0 ? `&duration=${duration}` : "");

  let data;

  try {
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${config.token}` },
      cache: "no-store",
    });

    if (!response.ok) return;

    data = await response.json();
  } catch {
    return;
  }

  if (!data.found) return;

  // Между просьбой и ответом зритель мог уйти на другой ролик: запрос идёт через
  // два await. Без этой проверки опрос поднимался бы по покинутому ролику и жил
  // вечно — останавливать его было бы уже некому.
  if (watching.get(tabId) !== videoId) return;

  // Порядок важен: сперва отметка о восстановлении, она включает оверлей,
  // и только потом снимок. Иначе реплики придут в неактивный оверлей
  // и будут отброшены.
  deliver(videoId, { type: "restored" });

  if (data.snapshot) deliver(videoId, data.snapshot);

  // По готовому заданию ждать нечего — опрос не поднимаем вовсе. Раньше он шёл
  // и здесь, вечно дёргая создающий эндпоинт на каждой открытой вкладке.
  if (data.stage === "done") return;

  startPolling(videoId, { create: false, cursor: data.cursor ?? 0 });
}

/**
 * Поднять опрос событий.
 *
 * create различает две подписки. Обычная — следствие нажатия кнопки, и она вправе
 * создать задание: подписка на ролик и ЕСТЬ согласие на работу. Восстановленная
 * согласия не несёт: вкладку всего лишь перезагрузили, и если задания на демоне
 * уже нет, создавать его нельзя — иначе F5 сам начинал бы качать аудио и занимать
 * карту. Права только повышаются: нажатие на кнопку поверх восстановления делает
 * подписку полноценной.
 */
function startPolling(videoId, { create = true, cursor = 0 } = {}) {
  const existing = polls.get(videoId);

  if (existing) {
    if (create) existing.create = true;

    return;
  }

  polls.set(videoId, { controller: null, create, cursor });
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
    } else if (message.op === "restore") {
      // Ролик вкладки отмечаем синхронно, до запроса: он нужен и для доставки
      // сообщений, и как признак того, что вкладка всё ещё здесь, когда ответ
      // вернётся. Работы это не начинает — её начинает только startPolling.
      const previous = watching.get(tabId);

      if (previous && previous !== message.videoId && viewers(previous).length <= 1) {
        stopPolling(previous);
      }

      watching.set(tabId, message.videoId);
      restore(tabId, message.videoId, message.duration);
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
