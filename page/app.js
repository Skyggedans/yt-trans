// Проводка страницы планшета: токен, сохранённые ссылки, видео с демона, сокет
// и слой субтитров. Чистая логика — в logic.js, здесь только DOM и сеть.

import {
  activeCueIndex, cueLines, filterLinks, flipSwitch, formatTime, isForVideo,
  languageText, parsePositions, pendingAt, positionFor, readFontSize, readSwitches, rememberPosition,
  sharedUrl, SPARE_BYTES, copyState, copyVerdict, formatBytes, mergeCues, percent,
  playSource,
  stageText, unwatchMessage, visibleLines, watchMessages,
} from "./logic.js";

import {
  askPersist, deleteVideo, downloadVideo, freeBytes, getVideo, listVideos, openDb, putVideo,
} from "./cache.js";

const TOKEN_KEY = "yt-trans-token";
const CLIENT_KEY = "yt-trans-client";
const POSITIONS_KEY = "yt-trans-positions";
const SWITCHES_KEY = "yt-trans-switches";
const FONT_KEY = "yt-trans-font";
const POLL_VIDEO_MS = 2000;
const RECONNECT_MS = 1000;
const PLAYHEAD_MS = 1000;
const LINKS_TIMEOUT_MS = 3000;

// Разметка не меняется за время жизни страницы, поэтому каждый элемент ищется
// один раз: в кадровом цикле слоя обращение к DOM не должно стоить поиска.
const els = {
  setup: document.getElementById("setup"),
  setupMessage: document.getElementById("setup-message"),
  tokenInput: document.getElementById("token-input"),
  tokenSave: document.getElementById("token-save"),
  app: document.getElementById("app"),
  home: document.getElementById("home"),
  links: document.getElementById("links"),
  linksFilter: document.getElementById("links-filter"),
  linkForm: document.getElementById("link-form"),
  linkUrl: document.getElementById("link-url"),
  episodes: document.getElementById("episodes"),
  episodesTitle: document.getElementById("episodes-title"),
  episodeList: document.getElementById("episode-list"),
  stage: document.getElementById("stage"),
  video: document.getElementById("video"),
  overlay: document.getElementById("overlay"),
  status: document.getElementById("status"),
  title: document.getElementById("title"),
  toggleTarget: document.getElementById("toggle-target"),
  toggleStudy: document.getElementById("toggle-study"),
  fontSize: document.getElementById("font-size"),
  fullscreen: document.getElementById("fullscreen"),
  cache: document.getElementById("cache"),
};

const state = {
  videoId: null,
  cues: [],
  pending: [],
  language: null,
  stage: "",
  waiting: undefined,
  error: null,
  retryable: false,
  total: 0,
  translated: 0,
  title: "",
  notice: "",
  // Из какой ссылки ролик и какой он длины: копия записывает это в базу, а
  // заполняет play(). Пока сцена не открыта, там пусто.
  link: { id: "", title: "" },
  duration: 0,
};

// Реплики приходят пачками и не по порядку: их вливают по номеру, а на экран
// отдают плотным списком, отсортированным по времени.
const cuesByIndex = new Map();

let token = "";
let socket = null;
let videoTimer = null;
let layer = null;
let paintedKey = null;
// Место просмотра — своё на устройство: планшет в другой комнате продолжает
// с того места, где его оставили, и не тянет за собой вкладки компьютера.
let positions = parsePositions(store(POSITIONS_KEY));
// Что сейчас в списке и что набрано в поиске: перерисовка по вводу не должна
// ходить на сервер за тем же списком.
let knownLinks = [];
let linksQuery = "";
// Открытая ссылка: перерисовать её эпизоды можно и не спрашивая демона заново.
let currentEpisodes = null;
// Какие строки показывать — тоже своё на устройство: планшет в другой комнате
// не должен менять то, что настроено на компьютере.
let switches = readSwitches(store(SWITCHES_KEY));
// Размер строк тоже запоминается на устройстве: на планшете в другой комнате
// он нужен другой, чем за компьютером.
let fontSize = readFontSize(store(FONT_KEY));

const clientId = makeClientId();

// Кеш на устройстве. База открывается один раз; savedVideos — то, что по ней
// уже известно, без самих mp4, чтобы список отвечал без чтения сотен мегабайт.
let db = null;
const savedVideos = new Map();
// Копия, которая сейчас качается: {videoId, controller, waiting, seen, total,
// cues, linkId, linkTitle, title, duration, language}. Одна за раз.
let copy = null;
// Ролик, чья копия не сохранилась: кнопка говорит об этом до следующей попытки.
let copyFailure = null;
// Адрес копии для <video>; отзывается при уходе со сцены и смене ролика.
let objectUrl = null;
// Офлайн — признак страницы, а не сети: ставится неудачей запроса к демону,
// снимается любым успешным ответом. navigator.onLine не спрашивается.
let offline = false;
let daemonAlive = true;
// Постоянное хранилище просим один раз за жизнь страницы, а не на каждую копию.
let persistAsked = false;
// Сохранённая копия, чьи титры ещё дописываются: {videoId, record, timer}.
let follow = null;

function store(key, value) {
  try {
    if (value === undefined) {
      return localStorage.getItem(key) || "";
    }

    localStorage.setItem(key, value);

    return value;
  } catch {
    // Хранилище может быть закрыто настройками приватности: тогда ключ живёт
    // до перезагрузки, но страница работает.
    return "";
  }
}

function makeClientId() {
  const saved = store(CLIENT_KEY);

  if (saved) {
    return saved;
  }

  const fresh = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

  return store(CLIENT_KEY, fresh) || fresh;
}

/** Запрос к демону с токеном установки. 401 — значит токен не тот, и это видно. */
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };

  if (options.body) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, { ...options, headers }).catch((error) => {
    // Запрос не дошёл вовсе — демона нет. 401 ниже — наоборот, живой демон
    // с чужим токеном, и офлайн тут ни при чём.
    daemonAlive = false;

    throw error;
  });

  daemonAlive = true;

  if (response.status === 401) {
    showSetup("токен не подошёл");

    throw new Error("токен не подошёл");
  }

  const data = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(data.detail || `демон ответил ${response.status}`);
  }

  return data;
}

// --- Экран токена ---------------------------------------------------------

function showSetup(message = "") {
  els.setupMessage.textContent = message;
  els.setup.hidden = false;
  els.app.hidden = true;
}

function saveToken() {
  const value = els.tokenInput.value.trim();

  if (!value) {
    els.setupMessage.textContent = "нужен токен";

    return;
  }

  token = value;
  store(TOKEN_KEY, value);
  els.tokenInput.value = "";
  els.setup.hidden = true;
  els.app.hidden = false;
  start();
}

// --- Дом: сохранённые ссылки и эпизоды -----------------------------------

async function start() {
  await openCache();
  connectSocket();
  await loadLinks();
  consumeShare();
}

// Приложение зарегистрировано в share-диалоге Android, поэтому из YouTube его
// открывают адресом вида /?text=<ссылка>. Ссылка добавляется тем же путём, что
// и набранная руками: демон сам проверит её у YouTube и, если это один ролик,
// страница сразу откроет его.
async function consumeShare() {
  const shared = sharedUrl(new URLSearchParams(location.search));

  if (!shared) {
    return;
  }

  // Сначала чистим адрес, потом работаем с сетью: иначе перезагрузка страницы
  // повторила бы добавление, а «назад» из ролика вернул бы на тот же шаринг.
  history.replaceState(null, "", location.pathname);

  try {
    const data = await api("/v1/links", { method: "POST", body: JSON.stringify({ url: shared }) });
    await loadLinks();

    if (data.link && data.link.id) {
      await openLink(data.link.id);
    }
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "демона нет — ссылку из YouTube добавить нельзя");

      return;
    }

    drawLinks([], `не удалось открыть ссылку из YouTube: ${error.message}`);
  }
}

function row(text, classes = []) {
  const item = document.createElement("li");
  item.className = classes.join(" ");
  item.textContent = text;

  return item;
}

function button(text, onClick) {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = text;
  node.addEventListener("click", onClick);

  return node;
}

function drawLinks(items, message) {
  knownLinks = items;
  els.links.replaceChildren();

  // Искать нечего, пока нечего показывать: пустое поле над «пока пусто» —
  // только лишний вопрос.
  els.linksFilter.hidden = items.length === 0;

  if (message) {
    els.links.append(row(message, ["empty"]));
  }

  if (!items.length) {
    // Ссылок демон не дал, но сохранённое лежит на устройстве: оно и есть
    // список, искать среди него тоже можно.
    if (offline && savedVideos.size) {
      els.links.append(...savedRows([...savedVideos.values()]));

      return;
    }

    if (!message) {
      els.links.append(row(
        offline ? "демона нет, на устройстве ничего не сохранено" : "пока пусто, добавь ссылку",
        ["empty"],
      ));
    }

    return;
  }

  const visible = filterLinks(items, linksQuery);

  if (!visible.length) {
    els.links.append(row("ничего не нашлось", ["empty"]));
  }

  for (const link of visible) {
    const item = document.createElement("li");
    item.append(
      button(link.title || link.url, () => openLink(link.id)),
      button("Удалить", () => dropLink(link.id)),
    );

    // Сохранённые ролики встают под своей ссылкой: без демона список ссылок
    // сам по себе ничего не говорит о том, что можно смотреть.
    if (offline) {
      const saved = savedForLink(link.id);

      if (saved.length) {
        const more = document.createElement("ul");
        more.append(...savedRows(saved));
        item.append(more);
      }
    }

    els.links.append(item);
  }
}

async function loadLinks() {
  // Прошлый список с устройства важнее скорости ответа: пока демон думает,
  // экран уже не пустой, а молчащий демон оставляет этот список на месте.
  const last = db ? await getMeta(db, "links") : null;

  if (last && last.length) {
    offline = false;
    drawLinks(last, "");
  }

  let data;

  try {
    data = await api("/v1/links", { signal: AbortSignal.timeout(LINKS_TIMEOUT_MS) });
  } catch (error) {
    if (daemonAlive) {
      offline = false;
      drawLinks([], error.message);

      return;
    }

    offline = true;
    drawLinks(last || [], "");

    return;
  }

  offline = false;

  if (db) {
    await putMeta(db, "links", data.links || []).catch(() => {});
  }

  drawLinks(data.links || [], "");
}

/** Сохранённые ролики этой ссылки, свежие первыми. */
function savedForLink(linkId) {
  return [...savedVideos.values()]
    .filter((record) => record.linkId === linkId)
    .sort((left, right) => (right.savedAt || 0) - (left.savedAt || 0));
}

/** Строки списка для сохранённых роликов: посмотреть и удалить. */
function savedRows(records) {
  return records.map((record) => {
    const item = document.createElement("li");
    item.className = "saved";
    item.append(
      button(
        `${episodeLabel(record)} · ${cacheLabel(record.size)}`,
        () => play(record, { id: record.linkId, title: record.linkTitle }),
      ),
      button("удалить", () => dropCopy(record.videoId)),
    );

    return item;
  });
}

/** Убрать копию с устройства: место на планшете кончается, и это должно быть можно. */
async function dropCopy(videoId) {
  if (follow && follow.videoId === videoId) {
    clearTimeout(follow.timer);
    follow = null;
  }

  if (db) {
    await deleteVideo(db, videoId).catch(() => {});
  }

  savedVideos.delete(videoId);

  if (currentEpisodes) {
    drawEpisodes(currentEpisodes.title, currentEpisodes.videos, currentEpisodes.linkId);
  }

  drawLinks(knownLinks, "");
}

async function addLink(event) {
  event.preventDefault();

  const url = els.linkUrl.value.trim();

  if (!url) {
    return;
  }

  try {
    await api("/v1/links", { method: "POST", body: JSON.stringify({ url }) });
    els.linkUrl.value = "";
    await loadLinks();
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "демона нет — ссылку добавить нельзя");

      return;
    }

    drawLinks([], `не удалось добавить: ${error.message}`);
  }
}

async function dropLink(id) {
  try {
    await api(`/v1/links/${encodeURIComponent(id)}`, { method: "DELETE" });
    await loadLinks();
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "демона нет — ссылку удалить нельзя");

      return;
    }

    drawLinks([], `не удалось удалить: ${error.message}`);
  }
}

// Пометка о картинке — у каждого ролика сразу, чтобы список отвечал на вопрос
// «что можно смотреть мгновенно».
const VIDEO_STATE_WORDS = {
  ready: "готово",
  preparing: "готовится",
  error: "не скачалось",
};

function episodeLabel(video) {
  const parts = [video.title || video.videoId];

  if (typeof video.duration === "number") {
    parts.push(formatTime(video.duration));
  }

  const word = VIDEO_STATE_WORDS[video.state];

  if (word) {
    parts.push(word);
  }

  return parts.join(" · ");
}

async function openLink(id) {
  let data;

  try {
    data = await api(`/v1/links/${encodeURIComponent(id)}/videos`);
  } catch (error) {
    if (!daemonAlive) {
      // Список эпизодов пришёл бы от демона, а его нет: зато ролики этой
      // ссылки могли уже лежать на устройстве.
      const saved = savedForLink(id);
      drawLinks(knownLinks, saved.length
        ? "демона нет — в списке сохранённые ролики этой ссылки"
        : "демона нет, роликов этой ссылки на устройстве нет");

      return;
    }

    drawLinks([], `не удалось прочитать ссылку: ${error.message}`);

    return;
  }

  // Одиночный ролик — это не список, а сразу просмотр: кнопка с одним пунктом
  // только заставляла бы нажимать дважды.
  if ((data.videos || []).length === 1) {
    play(data.videos[0], { id, title: data.title || "" });

    return;
  }

  drawEpisodes(data.title || "", data.videos || [], id);
}

function drawEpisodes(title, videos, linkId = "") {
  currentEpisodes = { title, videos, linkId };

  els.episodesTitle.textContent = title;
  els.episodeList.replaceChildren();

  if (!videos.length) {
    els.episodeList.append(row("в ссылке нет роликов", ["empty"]));
  }

  for (const video of videos) {
    const saved = savedVideos.get(video.videoId);
    const item = document.createElement("li");

    item.append(button(
      saved ? `${episodeLabel(video)} · ${cacheLabel(saved.size)}` : episodeLabel(video),
      () => play(video, { id: linkId, title }),
    ));

    if (saved) {
      item.append(button("удалить", () => dropCopy(video.videoId)));
    }

    els.episodeList.append(item);
  }

  els.episodes.hidden = false;
}

// --- Сцена ----------------------------------------------------------------

function play(video, link = {}) {
  // Без копии и без демона играть нечего: чёрный экран притворялся бы поломкой,
  // поэтому сцена не открывается, а домашняя страница говорит, в чём дело.
  if (playSource(savedVideos.has(video.videoId), daemonAlive) === "none") {
    drawLinks(knownLinks, "демона нет, этого ролика на устройстве нет");

    return;
  }

  const previous = state.videoId;

  Object.assign(state, {
    videoId: video.videoId,
    cues: [],
    pending: [],
    language: null,
    stage: "",
    waiting: undefined,
    error: null,
    retryable: false,
    total: 0,
    translated: 0,
    title: video.title || link.title || "",
    notice: "",
    link: { id: link.id || "", title: link.title || "" },
    duration: Number(video.duration) || 0,
  });

  cuesByIndex.clear();
  paintedKey = null;

  copyFailure = null;

  els.title.textContent = state.title;
  els.home.hidden = true;
  els.stage.hidden = false;
  drawCacheButton();
  render();

  history.pushState({ videoId: state.videoId }, "");

  createJob(state.videoId);
  watch(state.videoId, previous);
  prepareVideo();
}

async function createJob(videoId) {
  try {
    await api("/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ videoId, targetLang: "ru", authorTrack: true }),
    });
  } catch (error) {
    // Демона нет, но копия на устройстве есть — заводить задание не для кого.
    note(!daemonAlive && savedVideos.has(videoId) ? "демона нет — играю сохранённое" : error.message);
  }
}

function note(message) {
  state.notice = message || "";
  render();
}

function stopVideoPoll() {
  if (videoTimer !== null) {
    clearTimeout(videoTimer);
    videoTimer = null;
  }
}

/** Спросить у демона готовый mp4 и ждать, пока он его склеит. */
async function prepareVideo() {
  const videoId = state.videoId;

  stopVideoPoll();

  // Копия на устройстве предпочтительнее демона: она не ждёт склейки и не
  // рвётся вместе со связью.
  if (playSource(savedVideos.has(videoId), daemonAlive) === "copy" && await playLocal(videoId)) {
    return;
  }

  let status;

  try {
    status = await api(`/v1/videos/${encodeURIComponent(videoId)}`, { method: "POST" });
  } catch (error) {
    note(playSource(false, daemonAlive) === "none"
      ? "демона нет, этого ролика на устройстве нет"
      : error.message);

    return;
  }

  const step = (current) => {
    // Пока ждали двухсекундного ответа, зритель мог уйти на другой ролик.
    if (videoId !== state.videoId) {
      return;
    }

    if (current.state === "ready") {
      note("");

      const resume = positionFor(positions, videoId);

      els.video.src = `/v1/videos/${encodeURIComponent(videoId)}/file?token=${encodeURIComponent(token)}`;
      els.video.addEventListener("loadedmetadata", () => {
        if (resume > 0) {
          els.video.currentTime = resume;
        }
      }, { once: true });
      els.video.play().catch(() => {
        // Браузер вправе отказать: запуск вне нажатия он считает автозапуском.
        // Кнопки у плеера свои, так что зритель начнёт сам — молчать тут честно.
      });

      return;
    }

    if (current.state === "error") {
      note(current.message || "видео не скачалось");

      return;
    }

    note("готовлю картинку…");
    videoTimer = setTimeout(async () => {
      try {
        step(await api(`/v1/videos/${encodeURIComponent(videoId)}`));
      } catch (error) {
        note(error.message);
      }
    }, POLL_VIDEO_MS);
  };

  step(status);
}

// --- Кеш роликов на устройстве -------------------------------------------

/** Сохранённые титры в память страницы: они не зависят ни от сокета, ни от демона. */
function seedCues(record) {
  if (!record.cues || !record.cues.length) {
    return;
  }

  cuesByIndex.clear();

  for (const cue of record.cues) {
    cuesByIndex.set(cue.i, cue);
  }

  state.cues = [...cuesByIndex.values()].sort((a, b) => a.start - b.start);
  state.language = record.language || state.language;
  state.translated = countTranslated();
  paintedKey = null;
  render();
  paint();
}

/** Открыть ролик из копии; false — копии нет или она не наша. */
async function playLocal(videoId) {
  if (!savedVideos.has(videoId) || !db) {
    return false;
  }

  const record = await getVideo(db, videoId);

  if (videoId !== state.videoId) {
    return false;
  }

  if (!record || !record.blob) {
    // Браузер вправе вытеснить Blob: запись в базе есть, играть нечего, и
    // строка списка не должна больше врать, что ролик на устройстве.
    savedVideos.delete(videoId);
    deleteVideo(db, videoId).catch(() => {});
    drawCacheButton();

    return false;
  }

  switchToCopy(record, { resume: positionFor(positions, videoId), autoplay: true });
  seedCues(record);

  if (!daemonAlive) {
    note("демона нет — играю сохранённое");
  }

  return true;
}

/**
 * Играть из копии, не потеряв место и паузу: адрес объекта подставляется в
 * <video>, а время возвращается по loadedmetadata. `resume` не задан — берём
 * то место, где ролик стоит сейчас.
 */
function switchToCopy(record, { resume = 0, autoplay = false } = {}) {
  const from = resume || els.video.currentTime;
  const wasPlaying = !els.video.paused || autoplay;

  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
  }

  objectUrl = URL.createObjectURL(record.blob);

  els.video.addEventListener("loadedmetadata", () => {
    if (from > 0) {
      els.video.currentTime = from;
    }

    if (wasPlaying) {
      els.video.play().catch(() => {
        // Вне нажатия браузер вправе отказать — тогда зритель начнёт сам.
      });
    }
  }, { once: true });

  els.video.src = objectUrl;
}

// Запись сохранённых титров стоит дорого (целый mp4 поверх), поэтому копим их
// и пишем не чаще раза в пять секунд — к концу перевода в базе всё равно всё.
const FOLLOW_MS = 5000;

const filePath = (videoId) =>
  `/v1/videos/${encodeURIComponent(videoId)}/file?token=${encodeURIComponent(token)}`;

/** Размер готового mp4: HEAD не отдаёт тело, но Content-Length у него есть. */
async function fileSize(path) {
  try {
    const response = await fetch(path, { method: "HEAD" });

    return Number(response.headers.get("Content-Length") || 0);
  } catch {
    // Размер не спросили — не повод отказывать: приговор будет «unknown».
    return 0;
  }
}

/** Ждёт готовый mp4, как ждёт его сцена; -1 — демон сказал, что не будет. */
async function waitForFile(videoId) {
  const mine = copy;

  for (;;) {
    if (!copy || copy !== mine) {
      return 0;
    }

    const status = await api(`/v1/videos/${encodeURIComponent(videoId)}`, { method: "POST" })
      .catch(() => null);

    if (copy !== mine) {
      return 0;
    }

    if (status && status.state === "error") {
      note(status.message || "видео не скачалось");

      return -1;
    }

    if (status && status.state === "ready") {
      return fileSize(filePath(videoId));
    }

    await new Promise((resolve) => setTimeout(resolve, POLL_VIDEO_MS));
  }
}

/** Открыть базу и вспомнить, что на устройстве уже лежит. Зовётся и из start(). */
async function openCache() {
  if (!db) {
    try {
      db = await openDb();
    } catch {
      db = null;
    }
  }

  if (!db) {
    return;
  }

  try {
    for (const record of await listVideos(db)) {
      savedVideos.set(record.videoId, record);
    }
  } catch {
    // Список не прочитался — копия всё равно попробует сохраниться.
  }
}

/** Надпись на переключателе: он же и рассказывает, что с копией. */
function drawCacheButton() {
  const mine = copy && copy.videoId === state.videoId ? copy : null;
  const saved = savedVideos.get(state.videoId);

  els.cache.textContent = copyState({
    waiting: Boolean(mine && mine.waiting),
    active: Boolean(mine),
    percent: mine ? percent(mine.seen, mine.total) : 0,
    size: saved ? saved.size : 0,
    failed: copyFailure === state.videoId,
  });
  els.cache.setAttribute("aria-pressed", mine || saved ? "true" : "false");
}

function toggleCache() {
  const videoId = state.videoId;

  if (!videoId) {
    return;
  }

  if (copy && copy.videoId === videoId) {
    cancelCopy();

    return;
  }

  // «Кеш ✓» само ничего не удаляет: дорогую копию нельзя снести неверным
  // касанием, для этого есть кнопка в списке.
  if (savedVideos.has(videoId)) {
    return;
  }

  if (copy) {
    cancelCopy();
  }

  startCopy();
}

async function startCopy() {
  const videoId = state.videoId;

  if (!db) {
    await openCache();
  }

  if (!db) {
    note("браузер не дал хранилище");

    return;
  }

  const mine = {
    videoId,
    controller: new AbortController(),
    waiting: true,
    seen: 0,
    total: 0,
    // Титры, пришедшие до начала копирования, уже лежат в памяти страницы —
    // сохраняется всё, что есть на этот момент.
    cues: new Map(cuesByIndex),
    linkId: state.link.id,
    linkTitle: state.link.title,
    title: state.title,
    duration: state.duration,
    language: state.language,
  };

  copy = mine;
  copyFailure = null;
  drawCacheButton();

  if (!persistAsked) {
    persistAsked = true;
    askPersist().catch(() => {});
  }

  const size = await waitForFile(videoId);

  if (copy !== mine) {
    return;
  }

  if (size < 0) {
    copy = null;
    drawCacheButton();

    return;
  }

  const free = await freeBytes();

  if (copy !== mine) {
    return;
  }

  if (copyVerdict(size, free) === "tight") {
    note(`не хватает места: нужно ещё ${formatBytes(size + SPARE_BYTES - free)}, свободно ${formatBytes(free)}`);
    copy = null;
    drawCacheButton();

    return;
  }

  mine.waiting = false;
  mine.total = size;
  drawCacheButton();

  let drawn = 0;

  try {
    const { blob } = await downloadVideo(filePath(videoId), {
      signal: mine.controller.signal,
      onProgress: (seen, total) => {
        mine.seen = seen;

        if (total > 0) {
          mine.total = total;
        }

        // Перерисовываем только когда процент сменился: иначе кнопка дёргалась
        // бы на каждом куске потока.
        const now = percent(mine.seen, mine.total);

        if (now !== drawn) {
          drawn = now;
          drawCacheButton();
        }
      },
    });

    await finishCopy(blob);
  } catch (error) {
    const cancelled = error.name === "AbortError";

    if (copy === mine) {
      copy = null;
    }

    if (!cancelled) {
      copyFailure = videoId;
      note(`копия не сохранилась: ${error.message}`);
    }

    drawCacheButton();

    if (state.videoId !== videoId) {
      send(unwatchMessage(videoId));
    }
  }
}

function cancelCopy() {
  const mine = copy;

  if (!mine) {
    return;
  }

  copy = null;
  mine.controller.abort();

  if (state.videoId !== mine.videoId) {
    send(unwatchMessage(mine.videoId));
  }

  drawCacheButton();
}

async function finishCopy(blob) {
  const mine = copy;

  if (!mine) {
    return;
  }

  const record = {
    videoId: mine.videoId,
    linkId: mine.linkId,
    linkTitle: mine.linkTitle,
    title: mine.title,
    duration: mine.duration,
    // Размер — у самого файла: HEAD мог не ответить, а blob уже весь здесь.
    size: blob.size,
    savedAt: Date.now(),
    language: mine.language,
    cues: mergeCues([], [...mine.cues.values()]),
    blob,
  };

  try {
    await putVideo(db, record);
  } catch (error) {
    copy = null;
    copyFailure = mine.videoId;
    note(`копия не сохранилась: ${error.message}`);
    drawCacheButton();

    return;
  }

  copy = null;
  savedVideos.set(record.videoId, record);
  drawCacheButton();
  drawLinks(knownLinks, "");

  if (state.videoId === record.videoId) {
    note("сохранено на устройстве");
    // Копия готова посреди просмотра — подхватываем её без потери места.
    switchToCopy(record);
    // Перевод идёт дальше: сохранённые титры дописываем, пока зритель тут.
    follow = { videoId: record.videoId, record, timer: null };
  } else {
    send(unwatchMessage(record.videoId));
  }
}

/** Реплики ложатся и в недокачанную копию, и в уже сохранённую. */
function rememberCues(message) {
  if (message.videoId === undefined || message.videoId === null) {
    return;   // событие задания: к ролику не относится
  }

  const list = message.type === "snapshot" || message.type === "cues" ? message.cues || [] : [];

  if (copy && message.videoId === copy.videoId) {
    for (const cue of list) {
      copy.cues.set(cue.i, cue);
    }

    copy.language = message.language || copy.language;
  }

  if (follow && message.videoId === follow.videoId) {
    follow.record.cues = mergeCues(follow.record.cues, list);
    follow.record.language = message.language || follow.record.language;
    followSoon();
  }
}

function followSoon() {
  if (!follow || follow.timer !== null) {
    return;
  }

  const mine = follow;

  mine.timer = setTimeout(() => {
    mine.timer = null;
    putVideo(db, mine.record).catch(() => {});
  }, FOLLOW_MS);
}

// --- Сокет ----------------------------------------------------------------

function connectSocket() {
  if (!token) {
    return;
  }

  if (socket) {
    try {
      socket.close();
    } catch {
      // Сокет уже мог закрыться сам; новому это не мешает.
    }
  }

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/v1/ws?token=${encodeURIComponent(token)}`;
  const opening = new WebSocket(url);

  socket = opening;

  opening.addEventListener("open", () => {
    daemonAlive = true;

    if (state.videoId) {
      watch(state.videoId);
    }
  });

  opening.addEventListener("message", (event) => {
    let message;

    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    handleMessage(message);
  });

  opening.addEventListener("close", () => {
    // Демон перезапустился или связь пропала — переподключаемся молча:
    // снимок вернёт всё состояние целиком, терять нечего.
    if (socket === opening) {
      socket = null;
      daemonAlive = false;
      setTimeout(connectSocket, RECONNECT_MS);
    }
  });
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return false;
  }

  socket.send(JSON.stringify(message));

  return true;
}

function watch(videoId, previous = null) {
  // Подписка уйдёт из обработчика open, как только соединение поднимется:
  // сокета ещё нет, а запоминать её тут нечем — он подпишется по state.videoId.
  for (const message of watchMessages(
    videoId, positionFor(positions, videoId), clientId, previous,
  )) {
    send(message);
  }
}

function handleMessage(message) {
  // Копия ловит реплики раньше проверки на свой ролик: равенство по videoId
  // честнее, потому что событие без videoId к копии не относится вовсе.
  rememberCues(message);

  // События задания не несут videoId, а сокет помнит все подписки: без этой
  // проверки реплики прежнего эпизода ложились бы на номера новых.
  if (!isForVideo(message, state.videoId)) {
    return;
  }

  switch (message.type) {
    case "snapshot":
      takeSnapshot(message);
      break;
    case "state":
      state.stage = message.stage || "";
      state.waiting = message.waiting;
      state.error = message.message || null;
      state.retryable = Boolean(message.retryable);
      break;
    case "language":
      state.language = message.language || null;
      break;
    case "segmented":
      state.total = message.total || 0;
      state.pending = message.pending || [];
      break;
    case "cues":
      takeCues(message.cues || []);
      state.pending = message.pending || state.pending;
      state.translated = countTranslated();
      break;
    default:
      // ping держит соединение; показывать в нём нечего.
      return;
  }

  render();
  // На паузе кадров нет, timeupdate молчит — новый перевод иначе не дошёл бы
  // до экрана до самого возобновления. paint() дешёвый: одинаковая строка
  // не трогает DOM.
  paint();
}

function takeSnapshot(snapshot) {
  cuesByIndex.clear();
  paintedKey = null;

  Object.assign(state, {
    videoId: snapshot.videoId || state.videoId,
    stage: snapshot.stage || "",
    waiting: snapshot.waiting,
    error: snapshot.error || null,
    retryable: Boolean(snapshot.retryable),
    language: snapshot.language || null,
    total: snapshot.total || 0,
    pending: snapshot.pending || [],
    title: snapshot.title || state.title,
    notice: "",
  });

  takeCues(snapshot.cues || []);
  state.translated = typeof snapshot.translated === "number"
    ? snapshot.translated
    : countTranslated();
}

function takeCues(list) {
  for (const cue of list) {
    cuesByIndex.set(cue.i, cue);
  }

  state.cues = [...cuesByIndex.values()].sort((a, b) => a.start - b.start);
}

/** Переведённая реплика — та, у которой есть строки: так её считает и демон. */
function countTranslated() {
  return state.cues.filter((cue) => (cue.lines || []).length > 0).length;
}

// --- Надписи --------------------------------------------------------------

function render() {
  const lines = [];
  const stage = stageText(state.stage, state);

  if (stage) {
    lines.push(stage);
  }

  if (state.notice) {
    lines.push(state.notice);
  }

  const language = languageText(state.language);

  if (language) {
    lines.push(language);
  }

  els.status.replaceChildren(...lines.map((text) => {
    const line = document.createElement("div");
    line.textContent = text;

    return line;
  }));
  els.status.hidden = lines.length === 0;
}

// --- Слой субтитров -------------------------------------------------------

// Стили слоя живут в его теневом дереве, а не в style.css: теневая граница
// не пропускает внешние правила внутрь. Вид — тот же, что в extension/content.js;
// --font и --overlay-scale наследуются сквозь границу, поэтому крупный шрифт
// широкого экрана и выбранный зрителем размер приходят сюда сами.
const LAYER_CSS = `
  .box {
    max-width: 88%;
    padding: 6px 12px;
    border-radius: 8px;
    background: rgba(8, 8, 8, .78);
    text-align: center;
  }
  .line {
    margin: 0;
    color: #fff;
    /* --overlay-scale — выбранный на сцене размер; множитель, а не замена:
       на широком экране обычный уже крупнее. */
    font-size: calc(var(--font, 22px) * var(--overlay-scale, 1));
    line-height: 1.3;
    text-shadow: 0 1px 2px rgba(0, 0, 0, .9);
    white-space: pre-wrap;
  }
  .line + .line {
    margin-top: 4px;
  }
  .line[hidden] {
    display: none;
  }
  .box.failed .line {
    color: #ffd9a0;
    font-style: italic;
  }
  .box.hidden {
    display: none;
  }
`;

function layerNodes() {
  if (layer) {
    return layer;
  }

  const root = els.overlay.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  const box = document.createElement("div");
  const target = document.createElement("div");
  const secondary = document.createElement("div");

  style.textContent = LAYER_CSS;
  box.className = "box hidden";
  target.className = "line";
  secondary.className = "line";
  box.append(target, secondary);
  root.append(style, box);

  layer = { box, target, secondary };

  return layer;
}

function paint() {
  const nodes = layerNodes();
  const index = activeCueIndex(state.cues, els.video.currentTime);
  let lines = null;

  if (index >= 0) {
    const found = cueLines(state.cues[index]);

    // Реплика без перевода на экран не идёт: её место занимает надпись о том,
    // что перевода ещё нет, если она попадает в непереведённый кусок.
    if (found.target) {
      lines = found;
    }
  }

  // «Перевод ещё не готов…» стоит на месте русской строки: выключил её — значит
  // и ждать нечего показывать, черногорская приходит вместе с ней.
  if (!lines && switches.target && pendingAt(state.pending, els.video.currentTime)) {
    lines = { target: "перевод ещё не готов…", secondary: "", failed: true };
  }

  if (lines) {
    lines = visibleLines(lines, switches);
  }

  // Обе строки выключены — слой молчит целиком, вместе с надписью об ожидании.
  if (!lines || (!lines.target && !lines.secondary)) {
    paintedKey = null;
    nodes.box.classList.add("hidden");

    return;
  }

  const key = `${lines.target}\u0000${lines.secondary}\u0000${lines.failed}`;

  if (key === paintedKey) {
    return;
  }

  paintedKey = key;
  nodes.target.textContent = lines.target;
  nodes.target.hidden = !lines.target;
  nodes.secondary.textContent = lines.secondary;
  nodes.secondary.hidden = !lines.secondary;
  // failed меняет вид только первой строки: вторая гаснет лишь тогда, когда
  // пуста или повторяет первую (это уже решил cueLines).
  nodes.box.classList.toggle("failed", lines.failed);
  nodes.box.classList.remove("hidden");
}

// Родная кнопка плеера разворачивает сам <video>, а слой строк — соседний
// элемент, и в этом режиме его не видно. Подменить элемент «по факту» браузер
// не даёт: полноэкранный режим требует нажатия, и запрос из обработчика
// fullscreenchange отклоняется — видно, как экран разворачивается и сразу
// возвращается. Поэтому у плеера его кнопка спрятана (controlslist), а сцену
// разворачивает своя — по нажатию, когда браузер разрешает.
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
    return;
  }

  els.stage.requestFullscreen().catch(() => {});
}

// Там, где спрятать родную кнопку нечем (iOS), остаётся перехват: если
// полноэкранным стал ролик, просим сцену. Отказ больше не выкидывает из
// режима — иначе получался бы тот же прыжок туда и обратно.
function watchFullscreen() {
  document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement !== els.video || !els.stage.requestFullscreen) {
      return;
    }

    els.stage.requestFullscreen().catch(() => {});
  });
}

function startClock() {
  // Кадровый колбэк точнее timeupdate: тот срабатывает четыре раза в секунду
  // и оставляет субтитр отставать от картинки на четверть секунды.
  if (typeof els.video.requestVideoFrameCallback === "function") {
    const tick = () => {
      paint();
      els.video.requestVideoFrameCallback(tick);
    };

    els.video.requestVideoFrameCallback(tick);

    return;
  }

  els.video.addEventListener("timeupdate", paint);
}

function reportPlayhead() {
  setInterval(() => {
    if (!state.videoId) {
      return;
    }

    // Место запоминаем и на паузе: именно тогда зритель уходит, а вернувшись,
    // должен попасть туда же, а не в начало.
    savePosition();

    if (els.video.paused) {
      return;
    }

    send({
      op: "playhead", videoId: state.videoId, t: els.video.currentTime, clientId,
    });
  }, PLAYHEAD_MS);
}

function savePosition() {
  if (!state.videoId) {
    return;
  }

  positions = rememberPosition(positions, state.videoId, els.video.currentTime);
  store(POSITIONS_KEY, JSON.stringify(positions));
}

// --- Выключатели строк ----------------------------------------------------

function drawSwitches() {
  els.toggleTarget.setAttribute("aria-pressed", String(switches.target));
  els.toggleStudy.setAttribute("aria-pressed", String(switches.study));
}

function flipLine(name) {
  switches = flipSwitch(switches, name);
  store(SWITCHES_KEY, JSON.stringify(switches));
  drawSwitches();
  // Перерисовываем сразу: на паузе кадров нет, и слой сам не обновился бы до
  // возобновления.
  paintedKey = null;
  paint();
}

// --- Размер строк ---------------------------------------------------------

function drawFontSize() {
  els.fontSize.value = fontSize;
  // Слой живёт в shadow root, но признак стоит на сцене: правило для .line
  // в LAYER_CSS находит его через унаследованную переменную.
  els.stage.dataset.font = fontSize;
}

function pickFontSize() {
  fontSize = readFontSize(els.fontSize.value);
  store(FONT_KEY, fontSize);
  drawFontSize();
}

// --- Уход со сцены --------------------------------------------------------

/** Вернуться к списку: браузерная кнопка «назад» зовёт то же самое. */
function leaveStage() {
  if (!state.videoId) {
    return;
  }

  // Уходим — запоминаем место и отпускаем ролик. Исключение — идущее на нём
  // копирование: без подписки сохранённые титры оборвались бы на моменте
  // ухода, поэтому их сбор продолжается до конца копии.
  savePosition();

  const copying = Boolean(copy && copy.videoId === state.videoId);

  if (follow && follow.videoId === state.videoId) {
    clearTimeout(follow.timer);
    follow.timer = null;
    putVideo(db, follow.record).catch(() => {});
    follow = null;
  }

  if (state.videoId && !copying) {
    send(unwatchMessage(state.videoId));
  }

  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }

  els.video.pause();
  els.video.removeAttribute("src");
  els.video.load();

  stopVideoPoll();

  state.videoId = null;
  state.notice = "";
  paintedKey = null;
  layerNodes().box.classList.add("hidden");

  els.stage.hidden = true;
  els.home.hidden = false;
  render();
}

// --- Запуск ---------------------------------------------------------------

function boot() {
  token = store(TOKEN_KEY);

  els.tokenSave.addEventListener("click", saveToken);
  els.linkForm.addEventListener("submit", addLink);
  els.linksFilter.addEventListener("input", () => {
    linksQuery = els.linksFilter.value;
    drawLinks(knownLinks, "");
  });
  els.toggleTarget.addEventListener("click", () => flipLine("target"));
  els.toggleStudy.addEventListener("click", () => flipLine("study"));
  els.fullscreen.addEventListener("click", toggleFullscreen);
  els.cache.addEventListener("click", toggleCache);
  els.fontSize.addEventListener("change", pickFontSize);
  drawSwitches();
  drawFontSize();

  // Список эпизодов и сцена — один адрес, поэтому «назад» в браузере сам по себе
  // ничего не менял бы. Свой шаг истории кладётся при входе в ролик, а возврат
  // по нему возвращает к списку.
  window.addEventListener("popstate", leaveStage);
  watchFullscreen();

  startClock();
  reportPlayhead();

  if (token) {
    els.setup.hidden = true;
    els.app.hidden = false;
    start();
  } else {
    showSetup();
  }
}

boot();
