// Проводка страницы планшета: сохранённые ссылки, видео с демона, сокет
// и слой субтитров. Чистая логика — в logic.js, здесь только DOM и сеть.

import {
  activeCueIndex, cacheLabel, cueLines, filterLinks, flipSwitch, formatTime, isForVideo,
  languageText, linkTitle, matchesQuery, parsePositions, pendingAt, positionFor, readFontSize,
  readSwitches, rememberPosition, sharedUrl, SPARE_BYTES, copyState, copyVerdict, formatBytes,
  mergeCues, percent, playSource, toggleExpanded, stageText, unwatchMessage, visibleLines,
  watchMessages,
} from "./logic.js";

import {
  askPersist, deleteVideo, downloadVideo, freeBytes, getMeta, getVideo, listVideos,
  openDb, putMeta, putVideo, readJson,
} from "./cache.js";

const CLIENT_KEY = "yt-trans-client";
const POSITIONS_KEY = "yt-trans-positions";
const SWITCHES_KEY = "yt-trans-switches";
const FONT_KEY = "yt-trans-font";
const POLL_VIDEO_MS = 2000;
const RECONNECT_MS = 1000;
const PLAYHEAD_MS = 1000;
const LINKS_TIMEOUT_MS = 3000;
const WORKER_KEY = "yt-trans-no-worker";

// Разметка не меняется за время жизни страницы, поэтому каждый элемент ищется
// один раз: в кадровом цикле слоя обращение к DOM не должно стоить поиска.
const els = {
  app: document.getElementById("app"),
  home: document.getElementById("home"),
  links: document.getElementById("links"),
  linksFilter: document.getElementById("links-filter"),
  linkForm: document.getElementById("link-form"),
  linkUrl: document.getElementById("link-url"),
  stage: document.getElementById("stage"),
  video: document.getElementById("video"),
  overlay: document.getElementById("overlay"),
  status: document.getElementById("status"),
  title: document.getElementById("title"),
  toggleTarget: document.getElementById("toggle-target"),
  toggleStudy: document.getElementById("toggle-study"),
  fontSize: document.getElementById("font-size"),
  back: document.getElementById("back"),
  fullscreen: document.getElementById("fullscreen"),
  cache: document.getElementById("cache"),
};

// Иконки рисуются вместе с кнопкой: страница не тянет шрифтов и картинок из
// сети, а без сети они и не пришли бы. Контур наследует цвет кнопки.
const ICONS = {
  back: '<path d="M15 5l-7 7 7 7"/>',
  download: '<path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14"/>',
  check: '<path d="M4 12.5l5 5.5L20 6.5"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13M10 11v6M14 11v6"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  // «Телевизор» YouTube: скруглённый кадр с треугольником внутри.
  video: '<rect x="2.5" y="5" width="19" height="14" rx="4"/><path d="M10 9.2l5.5 2.8-5.5 2.8z" fill="currentColor"/>',
  // Список с треугольником — как у плейлиста на самом YouTube.
  playlist: '<path d="M3.5 7h11M3.5 12h11M3.5 17h7"/><path d="M15.5 13.5l5 3.5-5 3.5z" fill="currentColor"/>',
};

function iconMarkup(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round"
    stroke-linejoin="round">${ICONS[name]}</svg>`;
}

const ICON_DOWNLOAD = iconMarkup("download");
const ICON_CHECK = iconMarkup("check");
const ICON_TRASH = iconMarkup("trash");
const ICON_CHEVRON = iconMarkup("chevron");

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
// Раскрытые карточки: «link:<id>» у ссылки и «video:<id>» у ролика, под которым
// лежит копия. Всё по умолчанию свёрнуто, а раскрытие живёт до перезагрузки:
// это действие на этот просмотр, а не настройка устройства.
let expandedKeys = [];
// Карточки, которые поиск раскрыл сам, а зритель свернул обратно. Живёт до
// смены запроса: иначе шеврон на найденной карточке ничего бы не делал.
let searchClosed = new Set();
// Ролики плейлистов: спрашиваются у демона при раскрытии и живут до перезагрузки.
// Ошибка запроса тоже запоминается — иначе карточка молчала бы о ней.
const linkVideos = new Map();
const loadingVideos = new Set();
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
// Сервис-воркера нет: страница откроется только с живым демоном, и об этом
// стоит сказать — но один раз, а не при каждом запуске.
let workerMissing = false;
// Сказано ли уже в этом запуске, что оболочки не будет: строку списка
// перерисовка не должна затирать.
let workerWarning = "";
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

/** Запрос к демону. Ключа демон не спрашивает: пускает по Origin. */
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };

  if (options.body) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, { ...options, headers }).catch((error) => {
    // Запрос не дошёл вовсе — демона нет.
    daemonAlive = false;

    throw error;
  });

  daemonAlive = true;

  let data = {};

  try {
    data = await readJson(response);
  } catch (error) {
    if (!response.ok) {
      throw new Error(`демон ответил ${response.status}`);
    }

    // Тело оборвалось посреди ответа — демон умер или вышел срок. Это отказ,
    // а не пустой ответ: иначе сохранённый список затёрся бы пустым.
    daemonAlive = false;

    throw error;
  }

  if (!response.ok) {
    throw new Error(data.detail || `демон ответил ${response.status}`);
  }

  return data;
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

  if (!daemonAlive) {
    drawLinks(knownLinks, "демона нет — ссылку из YouTube добавить нельзя");

    return;
  }

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

    drawLinks(knownLinks, `не удалось открыть ссылку из YouTube: ${error.message}`);
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

/**
 * Кнопка заголовка: иконка типа перед названием. Иконка — часть кнопки, но не
 * её имени: читалке с экрана остаётся только название.
 */
function titledButton(icon, text, onClick) {
  const node = button("", onClick);
  const mark = document.createElement("span");
  mark.className = "title-icon";
  mark.innerHTML = iconMarkup(icon);
  mark.setAttribute("aria-hidden", "true");
  node.append(mark, document.createTextNode(text));

  return node;
}

/** Кнопка-иконка: подписи на экране нет, поэтому имя нужно для пальца и для слуха. */
function iconButton(markup, label, onClick) {
  const node = button("", onClick);
  node.classList.add("icon-only");
  node.innerHTML = markup;
  node.setAttribute("aria-label", label);
  node.title = label;

  return node;
}

/**
 * Имена роликов, известные без сети: прочитанный плейлист и копии на
 * устройстве. Поиск идёт и по ним — название плейлиста не говорит, что внутри.
 */
function videosFor(link) {
  const known = linkVideos.get(link.id);
  const list = known && known.videos ? known.videos : [];
  const seen = new Set(list.map((video) => video.videoId));
  const extra = savedForLink(link.id)
    .filter((record) => !seen.has(record.videoId))
    .map((record) => ({ videoId: record.videoId, title: record.title }));

  return [...list, ...extra];
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
    // список, искать среди него тоже можно. Карточки здесь одинокие — своей
    // ссылки у них на экране нет, поэтому метраж остаётся при копии.
    if (savedVideos.size) {
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

  const visible = filterLinks(items, linksQuery, videosFor);
  // Роликам плейлиста узнать имена неоткуда, пока их не прочитали: на время
  // поиска спрашиваем, иначе совпадение внутри плейлиста так и осталось бы
  // ненайденным. Ответ приходит один раз — дальше имя уже в памяти страницы.
  const searching = linksQuery.trim() !== "";

  if (!visible.length) {
    // Плейлист без совпадения по названию уже отфильтрован, и ролики ещё
    // читаются: «ничего не нашлось» здесь было бы неправдой.
    const pending = searching && daemonAlive && items.some((link) => (
      link.kind === "playlist" && (loadingVideos.has(link.id) || !linkVideos.has(link.id))
    ));

    els.links.append(row(pending ? "ищу в роликах…" : "ничего не нашлось", ["empty"]));
  }

  for (const link of visible) {
    els.links.append(linkCard(link));
  }

  // Обход по всем ссылкам, а не по видимым: иначе поиск по имени ролика
  // отбросил бы плейлист раньше, чем узнал его ролики.
  for (const link of items) {
    if (link.kind !== "playlist") {
      continue;
    }

    if (expandedKeys.includes(linkKey(link.id)) || searching) {
      // Запрос после отрисовки: loadVideos сам перерисует список, и звать его
      // прямо из обхода значило бы вкладывать отрисовку в отрисовку.
      queueMicrotask(() => loadVideos(link));
    }
  }
}

/**
 * Раскрыта ли карточка самим поиском: совпал не её заголовок, а имя ролика.
 * Такую карточку шеврон закрывает, не трогая раскрытий, сделанных зрителем.
 */
function openedBySearch(link) {
  const key = linkKey(link.id);

  if (expandedKeys.includes(key) || searchClosed.has(key) || linksQuery.trim() === "") {
    return false;
  }

  const byTitle = matchesQuery(link.title, linksQuery) || matchesQuery(link.url, linksQuery);

  return !byTitle && videosFor(link).some((video) => matchesQuery(video.title, linksQuery));
}

/**
 * Одна ссылка — одна карточка: название, под ним дети. У плейлиста дети —
 * ролики, у ролика — его копия. Отдельными плашками они читались как разные
 * пункты списка, и было непонятно, что нижняя строка относится к строке выше.
 */
function linkCard(link) {
  const item = document.createElement("li");
  item.className = "card";

  // Копии этой ссылки живут под ней и при живом демоне: иначе их нельзя было
  // бы ни увидеть, ни удалить, пока компьютер работает.
  const saved = savedForLink(link.id);
  const playlist = link.kind === "playlist";
  // Совпадение по названию ссылки видно в самом заголовке, и раскрывать нечего.
  // Совпадение по имени ролика — наоборот: без раскрытия найденное осталось бы
  // спрятанным под свёрнутой строкой.
  const byTitle = matchesQuery(link.title, linksQuery) || matchesQuery(link.url, linksQuery);
  const key = linkKey(link.id);
  const searchOpened = openedBySearch(link);
  const open = expandedKeys.includes(key) || searchOpened;
  // Метраж переезжает в заголовок, только если копия одна: у плейлиста за
  // одной ссылкой их много, и одна цифра соврала бы про остальные.
  const only = !playlist && saved.length === 1 ? saved[0] : null;

  const line = document.createElement("div");
  line.className = "link-line";
  const openButton = titledButton(
    playlist ? "playlist" : "video",
    linkTitle(link, only),
    () => openCard(link),
  );
  // Готовность картинки знает только демон и только про ролик: в сохранённом
  // списке она могла устареть, а у плейлиста своей картинки нет.
  const mark = playlist || !daemonAlive ? null : stateMark(link.state);

  if (mark) {
    openButton.append(mark);
  }

  line.append(openButton);
  line.append(iconButton(ICON_TRASH, "удалить ссылку", () => dropLink(link)));

  // Шеврон — последняя кнопка заголовка: раскрытие не должно стоять между
  // пальцем и удалением, иначе по нему будут промахиваться в обе стороны.
  if (playlist || saved.length) {
    line.append(chevronButton(open, () => toggleCard(
      key,
      playlist ? () => loadVideos(link) : null,
      searchOpened,
    )));
  }

  item.append(line);

  if (!open) {
    return item;
  }

  if (playlist) {
    item.append(playlistChildren(link, byTitle));

    return item;
  }

  const kids = document.createElement("div");
  kids.className = "children";
  // При поиске под карточкой остаются только совпавшие копии; совпадение по
  // названию ссылки показывает их все.
  const records = byTitle
    ? saved
    : saved.filter((record) => matchesQuery(record.title, linksQuery));

  for (const record of records) {
    kids.append(copyLine(record, !only));
  }

  item.append(kids);

  return item;
}

/** Строка копии в карточке: открыть с устройства и убрать её. */
function copyLine(record, withDuration) {
  const line = document.createElement("div");
  line.className = "saved-line";
  line.append(
    button(
      withDuration ? savedLabel(record) : cacheLabel(record.size),
      () => play(record, { id: record.linkId, title: record.linkTitle }),
    ),
    iconButton(ICON_TRASH, "удалить копию", () => dropCopy(record)),
  );

  return line;
}

/**
 * Дети плейлиста. Ролики приходят от демона: без них раскрытая карточка всё
 * равно должна показать то, что уже лежит на устройстве, — копии.
 */
function playlistChildren(link, all) {
  const kids = document.createElement("div");
  kids.className = "children";
  const known = linkVideos.get(link.id);

  if (known && known.error) {
    kids.append(cardNote(`ролики плейлиста не прочитались: ${known.error}`));
  } else if (known) {
    // При поиске по имени ролика остаются только совпавшие: остальные к
    // запросу не относятся. Совпадение по названию плейлиста — все ролики.
    const videos = all
      ? known.videos
      : known.videos.filter((video) => matchesQuery(video.title, linksQuery));

    if (!videos.length) {
      kids.append(cardNote(all ? "в ссылке нет роликов" : "совпадений в плейлисте нет"));
    }

    for (const video of videos) {
      kids.append(videoCard(video, link));
    }

    return kids;
  } else if (loadingVideos.has(link.id)) {
    kids.append(cardNote("читаю ролики…"));

    return kids;
  } else if (!daemonAlive) {
    kids.append(cardNote("демона нет — роликов плейлиста нет на устройстве"));
  }

  // Ролики плейлиста могут быть неизвестны и при живом демоне: тогда под
  // карточкой остаются хотя бы копии, которые уже есть.
  if (!known) {
    const records = all
      ? savedForLink(link.id)
      : savedForLink(link.id).filter((item) => matchesQuery(item.title, linksQuery));

    for (const record of records) {
      kids.append(copyLine(record, true));
    }
  }

  return kids;
}

/** Ролик внутри плейлиста: копия под ним — его ребёнок. */
function videoCard(video, link) {
  const saved = savedVideos.get(video.videoId);
  const open = expandedKeys.includes(videoKey(video.videoId));

  const item = document.createElement("div");
  item.className = "card-row";

  const line = document.createElement("div");
  line.className = "video-line";
  const playButton = titledButton(
    "video",
    episodeLabel(video),
    () => play(video, { id: link.id, title: link.title }),
  );
  const mark = stateMark(video.state);

  if (mark) {
    playButton.append(mark);
  }

  line.append(playButton);

  if (saved) {
    line.append(chevronButton(open, () => toggleCard(videoKey(video.videoId))));
  }

  item.append(line);

  if (saved && open) {
    const kids = document.createElement("div");
    kids.className = "children";
    // Метраж уже стоит в подписи ролика: копии остаётся сказать, где она.
    kids.append(copyLine(saved, false));
    item.append(kids);
  }

  return item;
}

/** Шеврон конца заголовка: раскрывает и сворачивает детей карточки. */
function chevronButton(open, onClick) {
  const node = iconButton(ICON_CHEVRON, open ? "свернуть" : "развернуть", onClick);
  node.classList.add("chevron");
  node.setAttribute("aria-expanded", open ? "true" : "false");

  return node;
}

const linkKey = (id) => `link:${id}`;
const videoKey = (id) => `video:${id}`;

/**
 * Раскрыть или свернуть карточку. Состояние переживает перерисовку, но не
 * перезагрузку: иначе каждая новая копия схлопывала бы список под пальцем.
 * `bySearch` — карточку раскрыл поиск, а не зритель: её шеврон сворачивает
 * найденное, не трогая раскрытие, сделанное раньше.
 */
function toggleCard(key, onOpen = null, bySearch = false) {
  if (bySearch && !expandedKeys.includes(key)) {
    searchClosed.add(key);
  } else {
    searchClosed.delete(key);
    expandedKeys = toggleExpanded(expandedKeys, key);
  }

  drawLinks(knownLinks, "");

  if (onOpen && expandedKeys.includes(key)) {
    onOpen();
  }
}

/** Заголовок карточки: ролик играет сразу, плейлист раскрывается. */
function openCard(link) {
  if (link.kind === "playlist") {
    toggleCard(linkKey(link.id), () => loadVideos(link), openedBySearch(link));

    return;
  }

  openLink(link.id);
}

/**
 * Ролики плейлиста спрашиваются при раскрытии, а не при каждой отрисовке:
 * за ответом демон ходит на YouTube, и список не должен этого делать зря.
 */
async function loadVideos(link) {
  if (linkVideos.has(link.id) || loadingVideos.has(link.id) || !daemonAlive) {
    return;
  }

  loadingVideos.add(link.id);
  drawLinks(knownLinks, "");

  try {
    const data = await api(`/v1/links/${encodeURIComponent(link.id)}/videos`);
    linkVideos.set(link.id, { videos: Array.isArray(data.videos) ? data.videos : [] });
  } catch (error) {
    linkVideos.set(link.id, { error: daemonAlive ? error.message : "демона нет" });
  } finally {
    loadingVideos.delete(link.id);
    drawLinks(knownLinks, "");
  }
}

/** Строка-подпись внутри карточки: div, а не li — вложенного списка здесь нет. */
function cardNote(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;

  return node;
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
    offline = !daemonAlive;

    // Сбой запроса не стирает то, что уже на экране. Ссылки пришли с устройства
    // или прошлым ответом, и строка об ошибке встаёт над ними; иначе один
    // неудавшийся запрос оставлял от дома одни сохранённые копии, и строки
    // роликов пропадали, хотя сами ссылки никуда не делись.
    drawLinks(last && last.length ? last : knownLinks, daemonAlive ? error.message : "");

    return;
  }

  offline = false;

  // Список ссылок — массив. Ответ без него (демон ответил не тем) не должен
  // ни затирать сохранённое, ни притворяться пустым списком.
  if (!Array.isArray(data.links)) {
    drawLinks(last && last.length ? last : knownLinks, "демон ответил без списка ссылок");

    return;
  }

  const links = data.links;

  if (db) {
    await putMeta(db, "links", links).catch(() => {});
  }

  drawLinks(links, workerNote());
}

/** Сохранённые ролики этой ссылки, свежие первыми. */
function savedForLink(linkId) {
  return [...savedVideos.values()]
    .filter((record) => record.linkId === linkId)
    .sort((left, right) => (right.savedAt || 0) - (left.savedAt || 0));
}

/**
 * Копии без своих ссылок: карточка на каждую, метраж внутри — снаружи его
 * взять неоткуда.
 */
function savedRows(records) {
  return records.map((record) => {
    const item = document.createElement("li");
    item.className = "card saved";
    item.append(copyLine(record, true));

    return item;
  });
}

/**
 * Подпись копии под её ссылкой. Заголовок здесь не повторяется — он уже
 * нарисован строкой выше; остаётся то, чего там нет: метраж, состояние и место
 * на устройстве.
 */
function savedLabel(record) {
  const parts = [];

  if (typeof record.duration === "number" && record.duration > 0) {
    parts.push(formatTime(record.duration));
  }

  parts.push(cacheLabel(record.size));

  return parts.join(" · ");
}

/** Забыть копию: база, список и хвост титров, который ещё дописывался. */
async function forgetCopy(videoId) {
  if (follow && follow.videoId === videoId) {
    clearTimeout(follow.timer);
    follow = null;
  }

  if (db) {
    await deleteVideo(db, videoId).catch(() => {});
  }

  savedVideos.delete(videoId);
}

async function dropLink(link) {
  const saved = savedForLink(link.id);
  const title = link.title || link.url;
  const question = saved.length
    ? `Удалить «${title}» вместе с сохранёнными копиями (${saved.length})?`
    : `Удалить «${title}»?`;

  if (!confirm(question)) {
    return;
  }

  if (!daemonAlive) {
    drawLinks(knownLinks, "демона нет — ссылку удалить нельзя");

    return;
  }

  try {
    await api(`/v1/links/${encodeURIComponent(link.id)}`, { method: "DELETE" });
  } catch (error) {
    if (!daemonAlive) {
      drawLinks(knownLinks, "демона нет — ссылку удалить нельзя");

      return;
    }

    drawLinks(knownLinks, `не удалось удалить: ${error.message}`);

    return;
  }

  // Ссылка ушла — уходит и всё, что лежало под ней. Иначе ролики плейлиста
  // остались бы на устройстве без строки, из которой их видно.
  linkVideos.delete(link.id);

  for (const record of saved) {
    await forgetCopy(record.videoId);
  }

  expandedKeys = expandedKeys.filter((key) => key !== linkKey(link.id));

  // Список правится на месте: если демон умер сразу после удаления, запрос за
  // списком вернул бы удалённую ссылку обратно.
  const left = knownLinks.filter((item) => item.id !== link.id);

  if (db) {
    await putMeta(db, "links", left).catch(() => {});
  }

  drawCacheButton();
  drawLinks(left, "");
}

/** Убрать копию с устройства: место на планшете кончается, и это должно быть можно. */
async function dropCopy(record) {
  const title = record.title || record.videoId;

  // Копия — это сотни мегабайт, которые потом придётся качать заново: одно
  // неверное касание не должно их уносить.
  if (!confirm(`Удалить копию «${title}» с устройства?`)) {
    return;
  }

  await forgetCopy(record.videoId);
  // Переключатель на сцене живёт своей надписью и без этого продолжал бы
  // показывать размер удалённой копии.
  drawCacheButton();
  drawLinks(knownLinks, "");
}

async function addLink(event) {
  event.preventDefault();

  const url = els.linkUrl.value.trim();

  if (!url) {
    return;
  }

  if (!daemonAlive) {
    drawLinks(knownLinks, "демона нет — ссылку добавить нельзя");

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

    drawLinks(knownLinks, `не удалось добавить: ${error.message}`);
  }
}

// Пометка о картинке — у каждого ролика сразу, чтобы список отвечал на вопрос
// «что можно смотреть мгновенно». Готовая картинка — галочка: слово «готово»
// повторялось бы в каждой строке и говорило бы ровно то же.
const VIDEO_STATE_ICONS = { ready: "check" };
const VIDEO_STATE_WORDS = {
  preparing: "готовится",
  error: "не скачалось",
};

/**
 * Значок состояния после названия: галочка у готовой картинки, слово — у
 * остальных. Готовность видно, но она не спорит с названием.
 */
function stateMark(stateName) {
  if (VIDEO_STATE_ICONS[stateName]) {
    const mark = document.createElement("span");
    mark.className = "state-mark";
    // Читалке с экрана галочка ничего не скажет, поэтому у неё есть имя.
    mark.setAttribute("role", "img");
    mark.setAttribute("aria-label", "готово");
    mark.innerHTML = iconMarkup(VIDEO_STATE_ICONS[stateName]);

    return mark;
  }

  const word = VIDEO_STATE_WORDS[stateName];

  if (!word) {
    return null;
  }

  const mark = document.createElement("span");
  mark.className = "state-word";
  mark.textContent = word;

  return mark;
}

function episodeLabel(video) {
  const parts = [video.title || video.videoId];

  if (typeof video.duration === "number") {
    parts.push(formatTime(video.duration));
  }

  return parts.join(" · ");
}

async function openLink(id) {
  if (!daemonAlive) {
    // Ролики идут от демона, а его нет: зато копии этой ссылки могли уже
    // лежать на устройстве, и список показывает их.
    const saved = savedForLink(id);

    drawLinks(knownLinks, saved.length
      ? "демона нет — в списке сохранённые ролики этой ссылки"
      : "демона нет, роликов этой ссылки на устройстве нет");

    return;
  }

  let data;

  try {
    data = await api(`/v1/links/${encodeURIComponent(id)}/videos`);
  } catch (error) {
    if (!daemonAlive) {
      const saved = savedForLink(id);
      drawLinks(knownLinks, saved.length
        ? "демона нет — в списке сохранённые ролики этой ссылки"
        : "демона нет, роликов этой ссылки на устройстве нет");

      return;
    }

    drawLinks(knownLinks, `не удалось прочитать ссылку: ${error.message}`);

    return;
  }

  const videos = Array.isArray(data.videos) ? data.videos : [];
  linkVideos.set(id, { videos });

  // Одиночный ролик — это не список, а сразу просмотр: кнопка с одним пунктом
  // только заставляла бы нажимать дважды.
  if (videos.length === 1) {
    play(videos[0], { id, title: data.title || "" });

    return;
  }

  // Похоже, под «роликом» оказался плейлист: раскрываем его карточку, а не
  // открываем отдельный экран — список и есть дерево.
  if (!expandedKeys.includes(linkKey(id))) {
    expandedKeys = toggleExpanded(expandedKeys, linkKey(id));
  }

  drawLinks(knownLinks, "");
}

// --- Сцена ----------------------------------------------------------------

async function play(video, link = {}) {
  // Устройство — единственный источник правды о копиях: удалённую в другом окне
  // страница иначе приняла бы за сохранённую и открыла пустую сцену.
  await openCache();

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
    if (playSource(false, daemonAlive) === "none") {
      // Копия числилась, но её вытеснил браузер, а демона нет: сцена
      // открылась бы пустой, поэтому сообщение остаётся на домашней.
      leaveStage();
      drawLinks(knownLinks, "демона нет, этого ролика на устройстве нет");

      return;
    }

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

      const resume = positionFor(positions, videoId);

      els.video.src = `/v1/videos/${encodeURIComponent(videoId)}/file`;
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
    // Строка списка обещала «на устройстве» — теперь это уже неправда.
    drawLinks(knownLinks, "");

    return false;
  }

  switchToCopy(record, { resume: positionFor(positions, videoId), autoplay: true });
  seedCues(record);

  if (!daemonAlive) {
    note("демона нет — играю сохранённое");
  } else {
    // Копия могла быть снята раньше, чем перевод закончился. Запись должна
    // расти и при обычном просмотре: иначе офлайн останутся только те титры,
    // что успели к копированию, а новые пропадут вместе с этим просмотром.
    follow = { videoId, record, timer: null };

    // Снимок подписки мог дойти раньше, чем копия открылась: его реплики уже
    // в памяти страницы, и запись стоит ими догнать.
    const merged = mergeCues(record.cues, state.cues);

    if (merged.length !== (record.cues || []).length) {
      record.cues = merged;
      followSoon();
    }
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

const filePath = (videoId) => `/v1/videos/${encodeURIComponent(videoId)}/file`;

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
    const records = await listVideos(db);

    // Не доливка, а пересборка: копию могли удалить в другом окне, и в памяти
    // этого окна осталась бы строка, обещающая то, чего на устройстве нет.
    // Пересобираем только по удавшемуся чтению: иначе сбой стирал бы список.
    savedVideos.clear();

    for (const record of records) {
      savedVideos.set(record.videoId, record);
    }
  } catch {
    // Список не прочитался — копия всё равно попробует сохраниться.
  }
}

/**
 * Кнопка кеша: иконка скачивания, а у готовой копии — галочка и размер.
 * Слово «кеш» здесь больше не пишется: на сцене оно спорило с кадром.
 */
function drawCacheButton() {
  const mine = copy && copy.videoId === state.videoId ? copy : null;
  const saved = savedVideos.get(state.videoId);

  const label = copyState({
    waiting: Boolean(mine && mine.waiting),
    active: Boolean(mine),
    percent: mine ? percent(mine.seen, mine.total) : 0,
    size: saved ? saved.size : 0,
    failed: copyFailure === state.videoId,
  });

  els.cache.innerHTML = `${saved ? ICON_CHECK : ICON_DOWNLOAD}<span class="cache-label"></span>`;

  // Подпись отдельным узлом, а не innerHTML: пустая строка не должна оставлять
  // после себя пробелов и лишних отступов на кнопке.
  if (label) {
    els.cache.querySelector(".cache-label").textContent = label;
  }

  // Иконка без слова: что произойдёт по нажатию, должно быть слышно и видно
  // при наведении. У готовой копии нажатие ничего не делает — так и говорим.
  const hint = mine ? "отменить копирование" : saved ? "копия на устройстве" : "копировать ролик на устройство";

  els.cache.setAttribute("aria-label", hint);
  els.cache.title = hint;
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
  if (socket) {
    try {
      socket.close();
    } catch {
      // Сокет уже мог закрыться сам; новому это не мешает.
    }
  }

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/v1/ws`;
  const opening = new WebSocket(url);

  socket = opening;

  opening.addEventListener("open", () => {
    daemonAlive = true;

    // Копия и её титры переживают уход со сцены: после переподключения
    // подписка нужна и им, иначе запись оборвётся на переподключении.
    for (const videoId of new Set([state.videoId, copy && copy.videoId, follow && follow.videoId])) {
      if (videoId) {
        watch(videoId);
      }
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

  // События задания несут ролик, но демон постарее его не подписывал: такое
  // событие считаем своим, иначе титры пропали бы вместе с обновлением
  // страницы. Чужое не наше — иначе реплики прежнего эпизода легли бы на
  // номера нового.
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

// Окно прячут и возвращаются к нему: пока его не было, копию могли удалить в
// соседнем (установленное приложение и вкладка браузера — разные экземпляры
// страницы). Вернувшись, список спрашивает устройство, а не свою память.
function watchCopies() {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      return;
    }

    openCache().then(() => {
      drawCacheButton();
      drawLinks(knownLinks, "");
    });
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

  // Пока зритель был на сцене, демон мог докачать картинку. Вернувшись, список
  // показывает свежую готовность, а не ту, что была при открытии приложения.
  // Без демона спрашивать некого, и сообщение об этом важнее списка.
  if (daemonAlive) {
    loadLinks().catch(() => {});
  }
}

// --- Запуск ---------------------------------------------------------------

/** Оболочка на устройстве: без неё страница открывается только с живым демоном. */
async function registerWorker() {
  if (!("serviceWorker" in navigator)) {
    workerMissing = true;

    return;
  }

  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch {
    // Регистрация падает на http без сертификата, в старых браузерах и без сети.
    // «Без сети» — не приговор браузеру: если оболочка уже стоит, страница
    // офлайн открылась именно из неё, и обещать, что её не сохранят, нельзя.
    workerMissing = !navigator.serviceWorker.controller;
  }
}

/** Предупреждение о пропавшем воркере: один раз за запуск и один раз за устройство. */
function workerNote() {
  if (!workerMissing) {
    return "";
  }

  if (!workerWarning && !store(WORKER_KEY)) {
    store(WORKER_KEY, "1");
    workerWarning = "браузер не сохранит страницу для офлайна — нужен https и современный браузер";
  }

  // Запоминается до конца запуска: список перерисовывается (ответ демона,
  // возвращение в окно), и каждый раз терять предупреждение нельзя.
  return workerWarning;
}

async function boot() {
  els.linkForm.addEventListener("submit", addLink);
  els.linksFilter.addEventListener("input", () => {
    linksQuery = els.linksFilter.value;
    // Новый запрос — новые совпадения: прежнее «свернул» к ним не относится.
    searchClosed = new Set();
    drawLinks(knownLinks, "");
  });
  els.toggleTarget.addEventListener("click", () => flipLine("target"));
  els.toggleStudy.addEventListener("click", () => flipLine("study"));
  els.back.innerHTML = iconMarkup("back");
  // Тем же путём, что и браузерная кнопка: шаг истории, положенный при входе
  // в ролик, снимается, а не остаётся висеть за спиной.
  els.back.addEventListener("click", () => history.back());
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
  watchCopies();

  startClock();
  reportPlayhead();

  // Приложение видно сразу: ключа у демона нет, спрашивать нечего.
  els.app.hidden = false;
  start();

  // Регистрация идёт следом и не задерживает ни экран, ни сохранённое.
  await registerWorker();

  // Список ссылок мог быть нарисован раньше её ответа: предупреждение
  // появляется поверх уже нарисованного, а не пропадает вместе с регистрацией.
  const warning = workerNote();

  if (warning) {
    drawLinks(knownLinks, warning);
  }
}

boot();
