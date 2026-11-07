// Чистая логика страницы: что показать в данную секунду. Ни document, ни fetch —
// это гоняет node --test, а app.js только зовёт.

/** Индекс реплики, где start <= t < end; -1 — сейчас молчание. */
export function activeCueIndex(cues, t) {
  for (let i = 0; i < cues.length; i += 1) {
    if (cues[i].start <= t && t < cues[i].end) {
      return i;
    }
  }

  return -1;
}

/** Попадает ли t в непереведённый интервал [start, end). */
export function pendingAt(pending, t) {
  for (const [start, end] of pending) {
    if (start <= t && t < end) {
      return true;
    }
  }

  return false;
}

const STAGE_WORDS = {
  probing: () => "читаю ролик…",
  fetching: () => "качаю звук…",
  asr: () => "распознаю речь…",
  paused: () => "пауза: не хватило видеопамяти",
};

/** Надпись о ходе работы; на done — пустая, экран не должен ничего говорить. */
export function stageText(stage, state) {
  const data = state || {};

  if (stage === "done") {
    return "";
  }

  if (stage === "error") {
    const text = data.error || "ошибка";

    return data.retryable ? `${text} — можно повторить` : text;
  }

  if (stage === "queued") {
    return data.waiting === "gpu"
      ? "ждём видеокарту: её занимает другая работа"
      : "в очереди…";
  }

  if (stage === "translating") {
    if (typeof data.translated === "number" && typeof data.total === "number") {
      return `перевожу… ${data.translated} из ${data.total}`;
    }

    return "перевожу…";
  }

  const words = STAGE_WORDS[stage];

  return words ? words() : "";
}

/** Строка о языке речи: решение и приписка демона; пустая, если решения нет. */
export function languageText(language) {
  if (!language || !language.effective) {
    return "";
  }

  const parts = [`язык речи: ${language.effective}`];

  if (language.reason === "track" && language.track) {
    parts.push(`дорожка автора ${language.track}`);
  } else if (language.reason === "pin") {
    parts.push("закреплено в настройках");
  } else if (language.reason === "detector" || language.reason === "floor") {
    parts.push(
      typeof language.confidence === "number"
        ? `по речи, ${Math.round(language.confidence * 100)}%`
        : "по речи",
    );
  }

  if (language.note) {
    parts.push(language.note);
  }

  return parts.join("; ");
}

/** Две строки реплики. Вторая гаснет, когда её нечем показать или она повторяет первую. */
export function cueLines(cue) {
  const target = (cue.lines || []).join("\n");
  const study = cue.study || "";
  const secondary = target && study && study !== target ? study : "";

  return { target, secondary, failed: Boolean(cue.failed) };
}

/** Время как в плеере: часы появляются, только когда они есть. */
export function formatTime(seconds) {
  const whole = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = String(whole % 60).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${rest}`;
  }

  return `${minutes}:${rest}`;
}

/** Событие о другом ролике — не наше: сокет подписан на всё, что смотрит страница. */
export function isForVideo(message, videoId) {
  return (
    message.videoId === undefined
    || message.videoId === null
    || message.videoId === videoId
  );
}

/** Отписка от ролика: без неё старый эпизод продолжает слать сюда свои реплики. */
export function unwatchMessage(videoId) {
  return { op: "unwatch", videoId };
}

/** Что сказать сокету при подписке: отписка от прежнего, подписка, место просмотра. */
export function watchMessages(videoId, t, clientId, previous = null) {
  const messages = [];

  if (previous && previous !== videoId) {
    messages.push(unwatchMessage(previous));
  }

  messages.push({ op: "watch", videoId, targetLang: "ru", authorTrack: true });
  messages.push({
    op: "playhead",
    videoId,
    t: typeof t === "number" && Number.isFinite(t) && t > 0 ? t : 0,
    clientId,
  });

  return messages;
}

/** Карта мест просмотра из строки localStorage; битое и чужие поля — прочь. */
export function parsePositions(raw) {
  let data;

  try {
    data = JSON.parse(raw || "{}");
  } catch {
    return {};
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return {};
  }

  const positions = {};

  for (const [key, value] of Object.entries(data)) {
    if (typeof value === "number" && Number.isFinite(value) && value > 0) {
      positions[key] = value;
    }
  }

  return positions;
}

/** Где остановились: 0, если ролик ещё не смотрели. */
export function positionFor(positions, videoId) {
  const value = positions ? positions[videoId] : undefined;

  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

/** Запомнить место. Ноль и нечисло местом не считаются и запись стирают. */
export function rememberPosition(positions, videoId, seconds) {
  const next = { ...(positions || {}) };

  if (typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0) {
    next[videoId] = Math.floor(seconds);
  } else {
    delete next[videoId];
  }

  return next;
}

/** Строки реплики с учётом выключателей: зритель решает, что видеть. */
export function visibleLines(lines, show) {
  const switches = show || {};
  const target = switches.target === false ? "" : lines.target;
  const secondary = switches.study === false ? "" : lines.secondary;

  // Пометка «перевод не вышел» относится к русской строке: без неё ей нечего
  // подсвечивать.
  return { target, secondary, failed: target ? lines.failed : false };
}

/** Выключатели из localStorage. Всё непонятное — «оба включены». */
export function readSwitches(raw) {
  const both = { target: true, study: true };
  let data;

  try {
    data = JSON.parse(raw || "{}");
  } catch {
    return both;
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return both;
  }

  return { target: data.target !== false, study: data.study !== false };
}

/** Переключить одну строку, не трогая вторую. */
export function flipSwitch(switches, name) {
  const next = {
    target: (switches || {}).target !== false,
    study: (switches || {}).study !== false,
  };

  next[name] = !next[name];

  return next;
}

// --- Поиск по сохранённым ссылкам -------------------------------------

/**
 * Есть ли запрос в строке. Пустой запрос совпадает со всем: поле поиска,
 * которое ничего не ищет, не должно прятать список.
 */
export function matchesQuery(text, query) {
  const needle = String(query || "").trim().toLocaleLowerCase();

  if (!needle) {
    return true;
  }

  return String(text || "").toLocaleLowerCase().includes(needle);
}

/**
 * Ссылки, где запрос есть в названии плейлиста, адресе или в имени ролика.
 * Имена роликов отдаёт videosOf: у плейлиста их узнают у демона, у копии —
 * из её же записи на устройстве.
 */
export function filterLinks(items, query, videosOf = () => []) {
  const needle = String(query || "").trim();

  if (!needle) {
    return items;
  }

  return items.filter((link) => (
    matchesQuery(link.title, needle)
    || matchesQuery(link.url, needle)
    || (videosOf(link) || []).some((video) => matchesQuery(video.title, needle))
  ));
}

// --- Ссылка, пришедшая из share-диалога -------------------------------

// Android кладёт адрес во text (поля url у него в share-системе нет), иногда
// вместе с заголовком ролика, и обрамляет его знаками препинания, если ссылка
// стоит в скобках или в конце предложения. Берём адрес, а не всё поле: демон
// принимает только YouTube и по адресу строит ссылку.
const YOUTUBE_HOSTS = /^(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)$/i;
const URL_IN_TEXT = /https?:\/\/[^\s<>"'«»]+/g;
const TRAILING = /[.,;:!?)\]»"']+$/;

export function youTubeUrlIn(text) {
  if (typeof text !== "string") {
    return "";
  }

  for (const found of text.match(URL_IN_TEXT) || []) {
    const candidate = found.replace(TRAILING, "");

    try {
      if (YOUTUBE_HOSTS.test(new URL(candidate).hostname)) {
        return candidate;
      }
    } catch {
      // Не адрес — например, «http://» без хоста.
    }
  }

  return "";
}

/** Первая ссылка YouTube среди полей, которые прислал share-диалог. */
export function sharedUrl(params) {
  for (const name of ["url", "text", "title"]) {
    const value = params.get ? params.get(name) : params[name];
    const found = youTubeUrlIn(value);

    if (found) {
      return found;
    }
  }

  return "";
}

/** Размеры субтитров, которые предлагает страница; имя — то же, что в разметке. */
export const FONT_SIZES = ["tiny", "small", "normal", "large"];

/** Размер из хранилища; чужое или испорченное значение — обычный размер. */
export function readFontSize(raw) {
  return FONT_SIZES.includes(raw) ? raw : "normal";
}

// --- Кеш роликов на устройстве -----------------------------------------
//
// Числа копии — размер, проценты, приговор по свободному месту — считаются
// здесь, а не в app.js: их видит хозяин, их стоит проверять в node --test.

const BYTE_UNITS = ["КБ", "МБ", "ГБ", "ТБ"];

/** Байты так, как их читает человек: «400 МБ», «1.2 ГБ». */
export function formatBytes(bytes) {
  // Мусор и отрицательные — прочерк, а не «NaN МБ»: такое число не значит
  // ничего, и показывать его как размер нельзя.
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }

  if (bytes < 1024) {
    return `${Math.round(bytes)} Б`;
  }

  let value = bytes;
  let unit = "Б";

  for (const next of BYTE_UNITS) {
    value /= 1024;
    unit = next;

    if (value < 1024 || next === "ТБ") {
      break;
    }
  }

  // У мелких чисел оставляем десятые («1.5 ГБ»): целыми они были бы слишком
  // грубы. У крупных они уже шум, и там округление до целого.
  const shown = value < 10
    ? Math.round(value * 10) / 10
    : Math.round(value);

  return `${shown} ${unit}`;
}

/** Проценты скачанного: без деления на ноль и без выхода за сотню. */
export function percent(seen, total) {
  if (!(total > 0) || !(seen > 0)) {
    return 0;
  }

  return Math.min(100, Math.floor((seen / total) * 100));
}

// Запас на запись: базе и самому браузеру тоже нужно место, а квота, забитая
// под ноль, обрывает копирование на середине файла. 200 МиБ — около половины
// эпизода, который ещё и не влезает целиком.
export const SPARE_BYTES = 200 * 1024 * 1024;

/**
 * Начинать ли копирование: «ok» — да, «tight» — места меньше, чем нужно с
 * запасом, «unknown» — судить не по чему (размер или место неизвестны).
 * Неизвестность копирование не запрещает: пусть решит сама запись.
 */
export function copyVerdict(need, free, reserve = SPARE_BYTES) {
  if (!(need > 0) || !(free > 0)) {
    return "unknown";
  }

  return need + reserve <= free ? "ok" : "tight";
}

/**
 * Подпись к иконке кеша. Само действие называет иконка (скачать или галочка),
 * поэтому здесь остаётся только то, что меняется: ожидание, проценты, размер.
 */
export function copyState({ waiting, active, percent: done = 0, size = 0, failed } = {}) {
  if (failed) {
    return "ошибка";
  }

  if (waiting) {
    return "ждёт";
  }

  if (active) {
    return `${done}%`;
  }

  return size > 0 ? formatBytes(size) : "";
}

/** Пометка сохранённого ролика в списке ссылок. */
export function cacheLabel(size) {
  return size > 0 ? `на устройстве · ${formatBytes(size)}` : "на устройстве";
}

/**
 * Заголовок строки ссылки. Метраж единственной копии — это и есть метраж
 * ролика, поэтому он стоит рядом с названием, а строка копии остаётся одна.
 * Копий несколько (плейлист) — метража здесь нет: одна цифра соврала бы про
 * остальные, и он остаётся при каждой копии.
 */
export function linkTitle(link, copy = null) {
  const title = (link && (link.title || link.url)) || "";
  const seconds = copy && typeof copy.duration === "number" ? copy.duration : 0;

  return seconds > 0 ? `${title} · ${formatTime(seconds)}` : title;
}

// --- Раскрытые карточки списка ------------------------------------------
//
// Ключ карточки — «link:<id>» или «video:<id>». Раскрытие живёт только до
// перезагрузки: список открывается свёрнутым, а карточку раскрывают пальцем.

/** Раскрыть или свернуть одну карточку, не трогая соседние. */
export function toggleExpanded(keys, key) {
  const next = Array.isArray(keys) ? [...keys] : [];
  const at = next.indexOf(key);

  if (at >= 0) {
    next.splice(at, 1);
  } else {
    next.push(key);
  }

  return next;
}

/**
 * Слияние реплик по номеру: сохранённое лежит в базе, пришедшее приходит
 * сокетом и полнее, поэтому оно побеждает. Порядок — по времени, как рисует
 * слой.
 */
export function mergeCues(saved, incoming) {
  const byNumber = new Map();

  for (const cue of saved || []) {
    byNumber.set(cue.i, cue);
  }

  for (const cue of incoming || []) {
    byNumber.set(cue.i, cue);
  }

  return [...byNumber.values()].sort((a, b) => a.start - b.start);
}

/** Откуда играть: копия предпочтительнее демона и переживает его смерть. */
export function playSource(hasCopy, daemonAlive) {
  if (hasCopy) {
    return "copy";
  }

  return daemonAlive ? "daemon" : "none";
}
