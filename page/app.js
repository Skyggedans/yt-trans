// Проводка страницы планшета: токен, сохранённые ссылки, видео с демона, сокет
// и слой субтитров. Чистая логика — в logic.js, здесь только DOM и сеть.

import {
  activeCueIndex, cueLines, formatTime, languageText, pendingAt, stageText,
} from "./logic.js";

const TOKEN_KEY = "yt-trans-token";
const CLIENT_KEY = "yt-trans-client";
const POLL_VIDEO_MS = 2000;
const RECONNECT_MS = 1000;
const PLAYHEAD_MS = 1000;

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
  fullscreen: document.getElementById("fullscreen"),
  back: document.getElementById("back"),
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
};

// Реплики приходят пачками и не по порядку: их вливают по номеру, а на экран
// отдают плотным списком, отсортированным по времени.
const cuesByIndex = new Map();

let token = "";
let socket = null;
let videoTimer = null;
let layer = null;
let paintedKey = null;

const clientId = makeClientId();

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

  const response = await fetch(path, { ...options, headers });

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

function start() {
  connectSocket();
  loadLinks();
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
  els.links.replaceChildren();

  if (message) {
    els.links.append(row(message, ["empty"]));
  }

  if (!items.length) {
    if (!message) {
      els.links.append(row("пока пусто, добавь ссылку", ["empty"]));
    }

    return;
  }

  for (const link of items) {
    const item = document.createElement("li");
    item.append(
      button(link.title || link.url, () => openLink(link.id)),
      button("Удалить", () => dropLink(link.id)),
    );
    els.links.append(item);
  }
}

async function loadLinks() {
  let data;

  try {
    data = await api("/v1/links");
  } catch (error) {
    drawLinks([], error.message);

    return;
  }

  drawLinks(data.links || [], "");
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
    drawLinks([], `не удалось добавить: ${error.message}`);
  }
}

async function dropLink(id) {
  try {
    await api(`/v1/links/${encodeURIComponent(id)}`, { method: "DELETE" });
    await loadLinks();
  } catch (error) {
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
    drawLinks([], `не удалось прочитать ссылку: ${error.message}`);

    return;
  }

  const title = data.title || "";
  const videos = data.videos || [];

  els.episodesTitle.textContent = title;

  // Одиночный ролик — это не список, а сразу просмотр: кнопка с одним пунктом
  // только заставляла бы нажимать дважды.
  if (videos.length === 1) {
    play(videos[0].videoId, videos[0].title || title);

    return;
  }

  els.episodeList.replaceChildren();

  if (!videos.length) {
    els.episodeList.append(row("в ссылке нет роликов", ["empty"]));
  }

  for (const video of videos) {
    const item = document.createElement("li");
    item.append(button(episodeLabel(video), () => play(video.videoId, video.title || title)));
    els.episodeList.append(item);
  }

  els.episodes.hidden = false;
}

// --- Сцена ----------------------------------------------------------------

function play(videoId, title) {
  Object.assign(state, {
    videoId,
    cues: [],
    pending: [],
    language: null,
    stage: "",
    waiting: undefined,
    error: null,
    retryable: false,
    total: 0,
    translated: 0,
    title: title || "",
    notice: "",
  });

  cuesByIndex.clear();
  paintedKey = null;

  els.title.textContent = state.title;
  els.home.hidden = true;
  els.stage.hidden = false;
  render();

  createJob(videoId);
  watch(videoId);
  prepareVideo();
}

async function createJob(videoId) {
  try {
    await api("/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ videoId, targetLang: "ru", authorTrack: true }),
    });
  } catch (error) {
    note(error.message);
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

  let status;

  try {
    status = await api(`/v1/videos/${encodeURIComponent(videoId)}`, { method: "POST" });
  } catch (error) {
    note(error.message);

    return;
  }

  const step = (current) => {
    // Пока ждали двухсекундного ответа, зритель мог уйти на другой ролик.
    if (videoId !== state.videoId) {
      return;
    }

    if (current.state === "ready") {
      note("");
      els.video.src = `/v1/videos/${encodeURIComponent(videoId)}/file?token=${encodeURIComponent(token)}`;
      els.video.play().catch(() => {});

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
      setTimeout(connectSocket, RECONNECT_MS);
    }
  });
}

function watch(videoId) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    // Подписка уйдёт из обработчика open, как только соединение поднимется.
    return;
  }

  socket.send(JSON.stringify({
    op: "watch", videoId, targetLang: "ru", authorTrack: true,
  }));
  // Нулевая позиция говорит демону, что смотреть будут с начала: перевод пойдёт
  // от первой реплики, а не от чужой середины.
  socket.send(JSON.stringify({
    op: "playhead", videoId, t: 0, clientId,
  }));
}

function handleMessage(message) {
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
// --font наследуется сквозь границу, поэтому крупный шрифт широкого экрана
// приходит сюда сам.
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
    font-size: var(--font, 22px);
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

  if (!lines && pendingAt(state.pending, els.video.currentTime)) {
    lines = { target: "перевод ещё не готов…", secondary: "", failed: true };
  }

  if (!lines) {
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
    if (!state.videoId || els.video.paused) {
      return;
    }

    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }

    socket.send(JSON.stringify({
      op: "playhead", videoId: state.videoId, t: els.video.currentTime, clientId,
    }));
  }, PLAYHEAD_MS);
}

// --- Кнопки сцены ---------------------------------------------------------

function fullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});

    return;
  }

  if (els.stage.requestFullscreen) {
    els.stage.requestFullscreen().catch(() => {});
  }
}

function back() {
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
  els.fullscreen.addEventListener("click", fullscreen);
  els.back.addEventListener("click", back);

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
