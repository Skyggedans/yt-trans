/**
 * Оверлей с русскими субтитрами поверх плеера YouTube.
 *
 * Крепится к #movie_player, а не к контейнеру видео. Это тот самый элемент,
 * который YouTube передаёт в requestFullscreen() и переносит в miniplayer,
 * поэтому полноэкранный, театральный и мини-режимы работают сами собой —
 * без единого обработчика fullscreenchange.
 */

const api = globalThis.browser ?? globalThis.chrome;

const HOST_TAG = "yt-trans-overlay";
const NAV_POLL_MS = 500;
const PLAYHEAD_MS = 1000;

// Панель управления дёргается, пока курсор едет по прогресс-бару. Без зоны
// нечувствительности текст ходил бы ходуном на каждый пиксель её дрожания.
const LIFT_HYSTERESIS_PX = 4;

const settings = {
  showOriginal: false,
  // "above" — оригинал над переводом, "below" — под ним.
  originalPosition: "below",
};

const state = {
  videoId: null,
  // Растёт на каждой навигации. Всё, что приходит со старым номером, выбрасывается:
  // ответы демона по предыдущему ролику иначе дорисовались бы поверх нового.
  epoch: 0,
  cues: [],
  lastIndex: -1,
  lift: 0,
  port: null,
  // Ставится циклом отрисовки, пока он жив.
  repaint: null,
  teardown: [],
};

// ── утилиты ─────────────────────────────────────────────────────────────────

function videoIdFromLocation() {
  const url = new URL(location.href);
  const shorts = url.pathname.match(/^\/(shorts|live)\/([A-Za-z0-9_-]{11})/);

  return shorts ? shorts[2] : url.searchParams.get("v") || null;
}

function findPlayer() {
  // На Shorts YouTube держит в DOM скрытый предзагруженный #movie_player.
  // Спросив его первым, мы повесили бы оверлей на невидимый плеер.
  if (location.pathname.startsWith("/shorts/")) {
    return document.querySelector("#shorts-player") || document.querySelector(".html5-video-player");
  }

  return document.querySelector("#movie_player") || document.querySelector(".html5-video-player");
}

/** Индекс реплики, активной в момент t. Двоичный поиск: реплик бывают сотни. */
function activeCueIndex(cues, t) {
  let low = 0;
  let high = cues.length - 1;

  while (low <= high) {
    const mid = (low + high) >> 1;
    const cue = cues[mid];

    if (t < cue.start) high = mid - 1;
    else if (t > cue.end) low = mid + 1;
    else return mid;
  }

  return -1;
}

// ── оверлей ─────────────────────────────────────────────────────────────────

function buildOverlay(player) {
  const host = document.createElement(HOST_TAG);

  Object.assign(host.style, {
    position: "absolute",
    inset: "0",
    // Насквозь для мыши: клики обязаны доходить до плеера.
    pointerEvents: "none",
    // Выше окна родных субтитров, но ниже меню настроек YouTube.
    zIndex: "60",
  });

  const shadow = host.attachShadow({ mode: "open" });

  shadow.innerHTML = `
    <style>
      :host { --font: 22px; --lift: 0px; }
      .box {
        position: absolute;
        left: 50%;
        bottom: calc(8% + var(--lift));
        transform: translateX(-50%);
        max-width: 88%;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 2px;
        transition: bottom 120ms ease-out;
      }
      .box.notrans { transition: none; }
      .line {
        font: 500 var(--font) / 1.25 "YouTube Noto", Roboto, Arial, sans-serif;
        color: #fff;
        background: rgba(8, 8, 8, 0.78);
        padding: 0.1em 0.4em;
        border-radius: 3px;
        text-align: center;
        white-space: pre-wrap;
        text-shadow: 0 0 2px rgba(0, 0, 0, 0.9);
      }
      /* Реплика, которую не удалось перевести: на экране исходный сербский.
         Зритель должен видеть, что это не перевод, а запасной вариант. */
      .box.failed .line { color: #ffd9a0; font-style: italic; }
      /* Оригинал — вспомогательная строка, и читается он краем глаза.
         Мельче и тусклее, чтобы не спорить за внимание с переводом. */
      .line.original {
        font-size: calc(var(--font) * 0.78);
        color: #d8d8d8;
        background: rgba(8, 8, 8, 0.62);
      }
    </style>
    <div class="box" part="box"></div>
  `;

  player.appendChild(host);

  return { host, box: shadow.querySelector(".box") };
}

function paint(box, cue) {
  if (!cue) {
    box.replaceChildren();
    box.classList.remove("failed");

    return;
  }

  const makeLine = (text, className) => {
    const line = document.createElement("div");
    line.className = className;
    line.textContent = text;

    return line;
  };

  const nodes = cue.lines.map((text) => makeLine(text, "line"));

  // Когда перевод не удался, на экране и так исходный сербский — второй раз
  // его показывать незачем.
  if (settings.showOriginal && cue.src && !cue.failed) {
    const original = makeLine(cue.src, "line original");

    if (settings.originalPosition === "above") nodes.unshift(original);
    else nodes.push(original);
  }

  box.classList.toggle("failed", Boolean(cue.failed));
  box.replaceChildren(...nodes);
}

/**
 * Поднять текст над панелью управления, когда она показана.
 *
 * Высота вычисляется из геометрии плеера, а не из живого прямоугольника оверлея:
 * `bottom` анимирован, и чтение его во время перехода скармливает предыдущий
 * подъём обратно в расчёт, из-за чего текст начинает колебаться.
 */
function measureLift(player, box) {
  const bar = player.querySelector(".ytp-chrome-bottom");

  if (!bar || player.classList.contains("ytp-autohide") || bar.offsetParent === null) {
    return 0;
  }

  const playerRect = player.getBoundingClientRect();
  const barRect = bar.getBoundingClientRect();
  const unlifted = playerRect.bottom - 0.08 * playerRect.height;
  const overlap = unlifted - barRect.top;

  return overlap > 0 ? Math.round(overlap) : 0;
}

function applyLift(host, box, lift) {
  if (Math.abs(lift - state.lift) < LIFT_HYSTERESIS_PX) return;

  state.lift = lift;

  // Переход временно гасим: иначе перерисовка после смены режима плеера
  // едет 120 мс, и всё это время рамка стоит не там, где должна.
  box.classList.add("notrans");
  host.style.setProperty("--lift", `${lift}px`);
  void box.offsetHeight;
  requestAnimationFrame(() => box.classList.remove("notrans"));
}

// ── жизненный цикл одного ролика ────────────────────────────────────────────

function teardown() {
  for (const undo of state.teardown.splice(0)) {
    try {
      undo();
    } catch {
      /* элемент мог уже исчезнуть вместе с плеером */
    }
  }

  document.querySelectorAll(HOST_TAG).forEach((node) => node.remove());
  document.documentElement.classList.remove("yt-trans-active");
  state.cues = [];
  state.lastIndex = -1;
  state.lift = 0;
}

function hideNativeCaptions() {
  const style = document.createElement("style");

  // Гасим прозрачностью, а не display:none. Скрытые таким образом родные
  // субтитры остаются в DOM, и их текст при необходимости можно прочитать.
  style.textContent = `
    .yt-trans-active .ytp-caption-window-container,
    .yt-trans-active .caption-window { opacity: 0 !important; pointer-events: none !important; }
  `;
  document.head.appendChild(style);
  document.documentElement.classList.add("yt-trans-active");
  state.teardown.push(() => style.remove());
}

function startRenderLoop(video, host, box) {
  let handle = null;
  let running = true;

  const render = () => {
    const index = activeCueIndex(state.cues, video.currentTime);

    if (index !== state.lastIndex) {
      state.lastIndex = index;
      paint(box, index < 0 ? null : state.cues[index]);
    }
  };

  const step = () => {
    if (!running) return;

    render();
    handle = video.requestVideoFrameCallback(step);
  };

  if ("requestVideoFrameCallback" in video) {
    handle = video.requestVideoFrameCallback(step);
  } else {
    const loop = () => {
      if (!running) return;

      render();
      handle = requestAnimationFrame(loop);
    };
    handle = requestAnimationFrame(loop);
  }

  // rVFC не срабатывает, пока кадры не сменяются, поэтому перемотка на паузе
  // оставила бы на экране предыдущую реплику. Эти события перерисовывают явно.
  for (const name of ["seeked", "ratechange", "play", "pause", "loadedmetadata"]) {
    video.addEventListener(name, render);
    state.teardown.push(() => video.removeEventListener(name, render));
  }

  render();

  // Перерисовка по требованию: тик по кадрам на паузе не идёт, поэтому смена
  // настройки иначе вступила бы в силу только после возобновления.
  state.repaint = () => {
    state.lastIndex = -1;
    render();
  };

  state.teardown.push(() => {
    running = false;
    state.repaint = null;

    if (handle && "cancelVideoFrameCallback" in video) video.cancelVideoFrameCallback(handle);
    else if (handle) cancelAnimationFrame(handle);
  });
}

function startLiftTracking(player, host, box) {
  let scheduled = false;

  const schedule = () => {
    if (scheduled) return;

    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      applyLift(host, box, measureLift(player, box));
    });
  };

  // Класс ytp-autohide переключается непрерывно, пока курсор едет по прогресс-бару,
  // поэтому всплеск мутаций схлопывается в один кадр.
  const observer = new MutationObserver(schedule);
  observer.observe(player, { attributes: true, attributeFilter: ["class"] });

  // Смена режима плеера и изменение окна класс не трогают, но геометрию меняют.
  const resize = new ResizeObserver(() => {
    const height = player.getBoundingClientRect().height;
    host.style.setProperty("--font", `${Math.min(Math.max(16, height * 0.04), 56)}px`);
    schedule();
  });
  resize.observe(player);

  window.addEventListener("resize", schedule);

  state.teardown.push(() => {
    observer.disconnect();
    resize.disconnect();
    window.removeEventListener("resize", schedule);
  });

  schedule();
}

function startPlayheadReports(video, videoId) {
  const timer = setInterval(() => {
    if (state.port && state.videoId === videoId) {
      state.port.postMessage({ op: "playhead", videoId, t: video.currentTime });
    }
  }, PLAYHEAD_MS);

  state.teardown.push(() => clearInterval(timer));
}

function mergeCues(incoming) {
  // Реплики приходят батчами и не по порядку: первым переводится окно вокруг
  // плейхеда. Держим массив отсортированным, иначе двоичный поиск соврёт.
  const byIndex = new Map(state.cues.map((cue) => [cue.i, cue]));

  for (const cue of incoming) byIndex.set(cue.i, cue);

  state.cues = [...byIndex.values()].sort((a, b) => a.start - b.start);
  state.lastIndex = -1;
}

async function activate(videoId) {
  const player = findPlayer();
  const video = player && player.querySelector("video");

  if (!player || !video) return false;

  const { host, box } = buildOverlay(player);

  hideNativeCaptions();
  startRenderLoop(video, host, box);
  startLiftTracking(player, host, box);
  startPlayheadReports(video, videoId);

  state.port.postMessage({ op: "watch", videoId });

  return true;
}

// ── навигация ───────────────────────────────────────────────────────────────

async function onNavigate() {
  const videoId = videoIdFromLocation();

  if (videoId === state.videoId) return;

  if (state.videoId && state.port) {
    state.port.postMessage({ op: "unwatch", videoId: state.videoId });
  }

  teardown();
  state.videoId = videoId;
  state.epoch += 1;

  if (!videoId) return;

  const epoch = state.epoch;

  // Плеер появляется в DOM не мгновенно; ждём его, но не бесконечно.
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (state.epoch !== epoch) return;
    if (await activate(videoId)) return;

    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function connect() {
  state.port = api.runtime.connect({ name: "yt-trans" });

  state.port.onMessage.addListener((message) => {
    if (message.videoId && message.videoId !== state.videoId) return;

    if (message.type === "cues" || message.type === "snapshot") {
      mergeCues(message.cues || []);
    }
  });

  state.port.onDisconnect.addListener(() => {
    state.port = null;
    // Service worker засыпает — это норма. Переподключаемся и заново
    // объявляем, что смотрим, чтобы демон продолжил слать реплики.
    setTimeout(() => {
      if (!api.runtime?.id) return;

      connect();

      if (state.videoId) state.port.postMessage({ op: "watch", videoId: state.videoId });
    }, 1000);
  });
}

async function loadSettings() {
  const stored = await api.storage.local.get(settings);

  Object.assign(settings, stored);
}

function watchSettings() {
  api.storage.onChanged.addListener((changes) => {
    let touched = false;

    for (const key of Object.keys(settings)) {
      if (changes[key]) {
        settings[key] = changes[key].newValue;
        touched = true;
      }
    }

    if (touched && state.repaint) state.repaint();
  });
}

async function main() {
  await loadSettings();
  watchSettings();
  connect();
  onNavigate();

  window.addEventListener("yt-navigate-finish", onNavigate, true);

  const poll = setInterval(() => {
    // После перезагрузки расширения старый content script продолжает жить
    // с мёртвым runtime, и без самоуничтожения оверлей висел бы на экране вечно.
    if (!api.runtime?.id) {
      clearInterval(poll);
      teardown();

      return;
    }

    // Свайпы в Shorts меняют адрес быстрее, чем срабатывает yt-navigate-finish,
    // а восстановленная вкладка может вернуться с уже запущенным плеером.
    if (videoIdFromLocation() !== state.videoId) onNavigate();
  }, NAV_POLL_MS);
}

main();
