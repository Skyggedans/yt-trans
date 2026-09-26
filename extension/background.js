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

// Проверка живости после обрыва: первая сразу, дальше всё реже, но не реже раза
// в десять секунд — поднятый заново демон должен подхватываться быстро.
const PROBE_MIN_MS = 1000;
const PROBE_MAX_MS = 10000;

// Проверочный запрос отвечает мгновенно. Три секунды тишины — это уже не работа
// демона, а зависший процесс или порт, который никто не слушает.
const PROBE_TIMEOUT_MS = 3000;

/** Порты content script'ов по идентификатору вкладки. */
const tabs = new Map();

/** Какой ролик смотрит вкладка. */
const watching = new Map();

/**
 * Идущие опросы по идентификатору ролика: { controller, create, cursor, broken }.
 *
 * broken — зрителю показан сбой. Нужен, чтобы сообщить о ВОССТАНОВЛЕНИИ связи:
 * без этого сообщения надпись об обрыве оставалась на кнопке навсегда — опрос
 * успешно возобновлялся, но успех никому не сообщался, и зритель видел обрыв
 * при совершенно живом демоне. Живёт в самом опросе, а не в цикле, потому что
 * сбой бывает показан и до его начала: повтор сообщает о недошедшем сбросе сам.
 */
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

/** Стадия, на которой задание оказалось по итогам ответа опроса. */
function lastStage(data) {
  let stage = data.snapshot?.stage;

  for (const event of data.events || []) {
    if (event.type === "state") stage = event.stage;
  }

  return stage;
}

async function pollLoop(videoId, poll) {
  // Обрывы подряд. Одиночный почти всегда устраивает не демон, а сам браузер:
  // при смене сети — переподнятый интерфейс docker'а, выход из сна — Firefox
  // закрывает каждое соединение, по которому пять секунд не шло ни байта,
  // локальные тоже, и такой запрос не повторяет. Длинный опрос молчит до
  // двадцати пяти секунд и попадает под это всякий раз. Свежее соединение после
  // такой чистки работает сразу, поэтому первый обрыв зрителю не показываем
  // и спрашиваем снова без паузы. Второй подряд — уже повод проверить демон.
  let failures = 0;

  // Сверяем сам объект, а не наличие ключа. Остановленный и тут же поднятый
  // заново опрос по тому же ролику — это уже другой опрос, и старый цикл,
  // проснувшись после await, иначе продолжил бы работать рядом с новым.
  while (polls.get(videoId) === poll) {
    const config = await settings();

    // Опрос могли остановить, пока читались настройки. Запрос, ушедший после
    // этого, оборвать было бы уже некому.
    if (polls.get(videoId) !== poll) return;

    if (!config.token) {
      deliver(videoId, { type: "helper", ok: false, reason: "нет токена" });
      await sleep(RETRY_MS);
      continue;
    }

    const controller = new AbortController();
    poll.controller = controller;

    const url =
      `${baseUrl(config)}/v1/jobs/${encodeURIComponent(videoId)}/events` +
      `?cursor=${poll.cursor}&target=${encodeURIComponent(config.targetLang)}` +
      `&authorTrack=${config.useAuthorTrack === false ? "false" : "true"}` +
      `&create=${poll.create ? "true" : "false"}`;

    // Дошёл ли запрос до ответа. Отличает обрыв от ответа, который не удалось
    // разобрать: второй повторять без паузы нельзя, см. ниже.
    let answered = false;

    try {
      const response = await fetch(url, {
        headers: { Authorization: `Bearer ${config.token}` },
        signal: controller.signal,
        cache: "no-store",
      });

      // Ответ пришёл — какой угодно, но от живого процесса. Счёт обрывов
      // ведётся только по запросам, которые не дошли вовсе.
      answered = true;
      failures = 0;

      if (!response.ok) {
        deliver(videoId, {
          type: "helper",
          ok: false,
          reason: response.status === 401 ? "неверный токен" : `демон ответил ${response.status}`,
        });
        poll.broken = true;
        await sleep(RETRY_MS);
        continue;
      }

      const data = await response.json();

      if (polls.get(videoId) !== poll) return;

      if (poll.broken) {
        poll.broken = false;
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

      // Готовое или упавшее задание больше ничего не пришлёт. Опрос по нему —
      // это соединение, молчащее по двадцать пять секунд, которое браузер рвёт
      // на каждой смене сети, и обрыв на экране поверх давно законченной работы.
      // Заново такое задание начинает только зритель — повтором или сменой
      // источника реплик, — и опрос тогда поднимается снова.
      const stage = lastStage(data);

      if (stage === "done" || stage === "error") {
        stopPolling(videoId);

        return;
      }
    } catch (error) {
      // Отмена — это наш собственный unwatch, а не сбой.
      if (error.name === "AbortError") return;

      if (polls.get(videoId) !== poll) return;

      if (answered) {
        // Ответ пришёл, но разобрать его не вышло: на порту слушает не демон,
        // а другой сервер, или тело не того вида. Обрывом это не считается —
        // повтор без паузы долбил бы тот сервер без единой передышки, а зритель
        // смотрел бы на застывшую кнопку. Поступаем как с ответом-ошибкой.
        console.warn(`yt-trans: ответ на опрос ${videoId} не разобран`, error);
        deliver(videoId, { type: "helper", ok: false, reason: "непонятный ответ демона" });
        poll.broken = true;
        await sleep(RETRY_MS);
        continue;
      }

      console.warn(`yt-trans: опрос ${videoId} оборвался`, error);
      failures += 1;

      if (failures < 2) continue;

      poll.broken = true;
      deliver(videoId, { type: "helper", ok: false, reason: "демон не отвечает, переподключаюсь…" });

      if (!(await waitForDaemon(videoId, poll))) return;

      // Сообщаем сразу, не дожидаясь следующего опроса: на тихом задании тот
      // молчит до двадцати пяти секунд, и всё это время кнопка твердила бы
      // про обрыв, которого уже нет.
      poll.broken = false;
      failures = 0;
      deliver(videoId, { type: "helper", ok: true });
    }
  }
}

/**
 * Дождаться, пока процесс демона снова ответит.
 *
 * Спрашиваем то, на что демон отвечает сразу, ничего не создавая: снимок задания
 * или 404, если задания нет. Годится ЛЮБОЙ HTTP-ответ, даже 401 — важно не что
 * сказано, а что сказал живой процесс. С неверным токеном разберётся уже опрос
 * и покажет ровно эту причину.
 *
 * Возвращает false, если опрос за это время остановили.
 */
async function waitForDaemon(videoId, poll) {
  for (let delay = PROBE_MIN_MS; ; delay = Math.min(delay * 2, PROBE_MAX_MS)) {
    const config = await settings();

    if (polls.get(videoId) !== poll) return false;

    try {
      await fetch(`${baseUrl(config)}/v1/jobs/${encodeURIComponent(videoId)}`, {
        headers: { Authorization: `Bearer ${config.token}` },
        cache: "no-store",
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      });

      return polls.get(videoId) === poll;
    } catch {
      // Порт закрыт или демон не успел ответить — ждём и спрашиваем снова.
    }

    await sleep(delay);

    if (polls.get(videoId) !== poll) return false;
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

  // По законченному заданию ждать нечего — опрос не поднимаем вовсе. Иначе он
  // вечно дёргал бы создающий эндпоинт на каждой открытой вкладке. Упавшее
  // сюда же: опрос с курсора в конце журнала о нём ничего нового не узнает
  // и остановить себя не сможет.
  if (data.stage === "done" || data.stage === "error") return;

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
    // Право создавать только что дано — нажатием во вкладке, открытой
    // перезагрузкой. Висящий запрос ушёл без него и промолчит до двадцати пяти
    // секунд, а приостановленное задание по нему так и не возобновится.
    // Поднимаем опрос заново с той же позиции, чтобы согласие дошло сразу.
    if (create && !existing.create) {
      stopPolling(videoId);
      startPolling(videoId, { create: true, cursor: existing.cursor });
      polls.get(videoId).broken = existing.broken;
    }

    return;
  }

  const poll = { controller: null, create, cursor, broken: false };

  polls.set(videoId, poll);
  pollLoop(videoId, poll);
}

function stopPolling(videoId) {
  const poll = polls.get(videoId);

  if (!poll) return;

  polls.delete(videoId);

  // Обрываем висящий запрос, иначе он додержит соединение до конца ожидания.
  if (poll.controller) poll.controller.abort();
}

/**
 * Повторить упавшее задание по просьбе зрителя.
 *
 * Отказ на демоне прилипает: окончательный — навсегда, временный — на минуту
 * остывания. Иначе опрос сам устраивал бы шторм повторов с обращением к YouTube
 * на каждом витке. Зритель этой минуты ждать не должен, поэтому повтор — это
 * сброс задания и подписка заново. Курсор с нуля: у нового задания свой журнал,
 * и позиция в старом ничего в нём не значит.
 */
async function retry(videoId) {
  stopPolling(videoId);

  const config = await settings();
  const job = `${baseUrl(config)}/v1/jobs/${encodeURIComponent(videoId)}`;
  const headers = { Authorization: `Bearer ${config.token}` };
  let unreachable = false;

  try {
    // Сбрасываем только то, что всё ещё лежит упавшим. Кнопка повтора бывает
    // старше самого отказа: соседняя вкладка с тем же роликом его уже повторила,
    // а до этой события нового задания не доходят, если фоновая страница
    // успела уснуть и забыть, кто что смотрит. Сброс вслепую убил бы идущую
    // работу — вплоть до распознавания на середине — и начал бы её с нуля.
    // Идущее задание подписка просто подхватит.
    const response = await fetch(job, { headers, cache: "no-store" });
    const snapshot = response.ok ? await response.json() : null;

    if (snapshot?.stage === "error") {
      await fetch(`${job}/drop`, { method: "POST", headers, cache: "no-store" });
    }
  } catch (error) {
    // Сброс не дошёл. Опрос поднимаем всё равно: дожидаться демона он умеет сам,
    // а если отказ так и остался на месте, зритель увидит его снова и нажмёт ещё раз.
    console.warn(`yt-trans: сброс ${videoId} не дошёл`, error);
    unreachable = true;
    deliver(videoId, { type: "helper", ok: false, reason: "демон не отвечает, переподключаюсь…" });
  }

  // Пока шёл сброс, зритель мог уйти с ролика. Задание без зрителя не нужно никому.
  if (viewers(videoId).length === 0) return;

  startPolling(videoId, { create: true, cursor: 0 });

  // Об обрыве зритель уже знает — значит, и о восстановлении должен узнать.
  if (unreachable) polls.get(videoId).broken = true;
}

/** Отметить, какой ролик смотрит вкладка, отпустив прежний, если он больше никому не нужен. */
function follow(tabId, videoId) {
  const previous = watching.get(tabId);

  if (previous && previous !== videoId && viewers(previous).length <= 1) {
    stopPolling(previous);
  }

  watching.set(tabId, videoId);
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
      follow(tabId, message.videoId);
      startPolling(message.videoId);
    } else if (message.op === "restore") {
      // Ролик вкладки отмечаем синхронно, до запроса: он нужен и для доставки
      // сообщений, и как признак того, что вкладка всё ещё здесь, когда ответ
      // вернётся. Работы это не начинает — её начинает только startPolling.
      follow(tabId, message.videoId);
      restore(tabId, message.videoId, message.duration);
    } else if (message.op === "retry") {
      // Вкладка с законченным заданием после переподключения порта о себе
      // не напоминает, поэтому отмечаем её заново: без этого новому заданию
      // некому было бы доставлять события.
      follow(tabId, message.videoId);
      retry(message.videoId);
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
//
// Поднимаем заново только те, что шли, и каждый с его же правом создавать.
// Законченное задание опроса не держит, и подписка с правом создания по нему
// начала бы работу без нажатия: упавшее, остыв, демон пересоздаёт. То же
// с восстановленной вкладкой — согласия она не давала, и смена настроек его
// не даёт. Курсор с нуля: по новому адресу может отвечать другой демон,
// и позиция в чужом журнале ничего не значит.
api.storage.onChanged.addListener((changes) => {
  if (changes.token || changes.host || changes.port) {
    // По копии: stopPolling меняет polls, а менять коллекцию во время обхода нельзя.
    const running = [...polls].map(([videoId, poll]) => [videoId, poll.create]);

    for (const [videoId] of running) stopPolling(videoId);

    for (const [videoId, create] of running) startPolling(videoId, { create });
  }
});
