/**
 * Оверлей с субтитрами поверх плеера YouTube.
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
  // Кнопка запуска и признак того, что зритель уже согласился на перевод
  // этого ролика. Без согласия демон не трогается вовсе: каждое задание
  // качает аудио и занимает карту, поэтому запускать его на всём подряд,
  // что открыто в браузере, нельзя.
  chip: null,
  // Последняя надпись на кнопке, не считая ошибок.
  lastLabel: null,
  // Оверлей подключён и принимает реплики. Это НЕ согласие: восстановление
  // после перезагрузки страницы тоже его включает.
  active: false,
  // Зритель нажал кнопку на ЭТОЙ странице. Только этот флаг даёт право
  // просить работу: качать аудио и занимать карту. Разведены они потому, что
  // раньше был один, и восстановление им же подписывало зрителя за него —
  // переподключение порта слало {op:"watch"} на вкладке, где никто не нажимал.
  consented: false,
  // Длительность ролика по данным плеера. Уходит в запрос восстановления:
  // по ней демон отсекает кэш от перезалитого под тем же идентификатором ролика,
  // не спрашивая метаданные у YouTube на каждой загрузке страницы.
  duration: 0,
  // Сколько реплик всего. Приходит один раз вместе со стадией перевода
  // и нужен, чтобы показывать прогресс по мере поступления реплик.
  total: 0,
  // Последняя стадия задания, какой её сообщил демон. По ней кнопка решает,
  // что показать после обрыва связи и можно ли повторить отказ, а опрос
  // позиции и переподключение порта — нужны ли они вообще.
  stage: null,
  // Отказ, если задание упало: текст причины и может ли он пройти сам.
  error: "",
  retryable: true,
  // Повтор нажат, пока порт переподключался, и уйдёт вместе с переподключением.
  pendingRetry: false,
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
      /* Реплика, которую не удалось перевести: на экране текст распознавания.
         Зритель должен видеть, что это не перевод, а запасной вариант. */
      .box.failed .line { color: #ffd9a0; font-style: italic; }
      /* Оригинал — вспомогательная строка, и читается он краем глаза.
         Мельче и тусклее, чтобы не спорить за внимание с переводом. */
      .line.original {
        font-size: calc(var(--font) * 0.78);
        color: #d8d8d8;
        background: rgba(8, 8, 8, 0.62);
      }
      /* Единственный элемент, принимающий мышь: всё остальное в оверлее
         сквозное, чтобы клики доходили до плеера. */
      .chip {
        position: absolute;
        top: 12px;
        right: 12px;
        pointer-events: auto;
        font: 500 13px/1 system-ui, sans-serif;
        color: #fff;
        background: rgba(8, 8, 8, 0.78);
        border: 1px solid rgba(255, 255, 255, 0.25);
        border-radius: 14px;
        padding: 7px 13px;
        cursor: pointer;
        user-select: none;
        transition: opacity 150ms;
        /* Причина отказа бывает длиной в абзац — это хвост вывода yt-dlp.
           Одной строкой с многоточием, иначе кнопка вылезала бы за край плеера;
           целиком текст лежит в подсказке. */
        box-sizing: border-box;
        max-width: min(calc(100% - 24px), 40em);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .chip:hover { background: rgba(30, 30, 30, 0.9); }
      .chip[data-mode="busy"] { cursor: default; opacity: 0.85; }
      .chip[data-mode="error"],
      .chip[data-mode="retry"] { border-color: rgba(255, 140, 140, 0.6); color: #ffc9c9; }
      /* Обрыв связи и отказ без повтора нажимать бесполезно — курсор не должен
         обещать обратное. */
      .chip[data-mode="error"] { cursor: default; }
      .chip[hidden] { display: none; }
    </style>
    <div class="chip" hidden></div>
    <div class="box" part="box"></div>
  `;

  player.appendChild(host);

  return {
    host,
    box: shadow.querySelector(".box"),
    chip: shadow.querySelector(".chip"),
  };
}

const CHIP_LABELS = {
  queued: "в очереди…",
  probing: "читаю ролик…",
  fetching: "качаю звук…",
  asr: "распознаю речь…",
  translating: "перевожу…",
  paused: "пауза: карта занята",
};

function setChip(text, mode = "idle", title = "") {
  const chip = state.chip;

  if (!chip) return;

  // Последнюю НЕошибочную надпись запоминаем: к ней надо вернуться, когда связь
  // восстановится. Сообщение об обрыве иначе переживало бы сам обрыв — работа
  // давно идёт дальше, а на кнопке всё ещё написано, что демона нет.
  if (mode !== "error" && mode !== "retry") state.lastLabel = { text, mode };

  chip.hidden = false;
  chip.textContent = text;
  chip.title = title;
  chip.dataset.mode = mode;
}

/** Задание закончено — удачно или нет. Больше демон по нему ничего не пришлёт. */
function jobFinished() {
  return state.stage === "done" || state.stage === "error";
}

/**
 * Запомнить стадию из сообщения демона.
 *
 * Зовётся раньше слияния реплик: снимок приносит и реплики, и стадию, и счётчик
 * «перевожу…» не должен лечь на кнопку, когда из того же снимка уже ясно,
 * что работа закончена.
 */
function trackStage(message) {
  if (message.type !== "state" && message.type !== "snapshot") return;
  if (!message.stage) return;

  state.stage = message.stage;

  if (message.stage === "error") {
    state.error = message.message || message.error || "";
    // Старый демон этого поля не присылает. Отказ без него считаем временным:
    // лишнее нажатие обойдётся одним сбросом, а отсутствующая кнопка повтора —
    // роликом без субтитров.
    state.retryable = message.retryable !== false;
  }
}

/**
 * Показать отказ задания.
 *
 * Приглашение повторить стоит в начале надписи: длинная причина обрезается
 * многоточием, и в конце его просто не было бы видно — кнопка выглядела бы
 * надписью, на которую незачем нажимать.
 */
function showFailure() {
  const full = state.error || "не получилось";
  // «ERROR:» — метка уровня из вывода yt-dlp. Зрителю она ничего не сообщает,
  // а место на кнопке занимает.
  const reason = full.replace(/\bERROR:\s*/g, "").replace(/\s+/g, " ").trim() || full;

  if (state.retryable) setChip(`↻ повторить — ${reason}`, "retry", full);
  else setChip(reason, "error", full);
}

/**
 * Начать задание по ролику заново: повтором отказа или сменой источника реплик.
 *
 * Реплики прежнего задания забываем. У нового свой журнал с нуля, а слияние идёт
 * по номеру реплики и ничего не удаляет: счётчик «перевожу…» начал бы со старого
 * числа, а реплики другой разметки — распознавание вместо дорожки автора, другой
 * прогон распознавания — легли бы на экран вперемешку с новыми.
 */
function restartJob(op, label) {
  state.active = true;
  state.consented = true;
  // Стадия прежнего задания больше ничего не значит. Сброс её закрывает
  // второе нажатие, пока идёт этот повтор, и возвращает переподключению
  // порта обычную подписку на работу.
  state.stage = null;
  state.cues = [];
  state.total = 0;
  state.repaint?.();
  setChip(label, "busy");

  if (state.port) {
    state.port.postMessage({ op, videoId: state.videoId });
  } else {
    // Порт как раз переподключается — фоновая страница уснула. Подписку
    // переподключение пошлёт само, а повтор надо запомнить: вместо него ушла бы
    // обычная подписка и принесла бы тот же отказ, пока он на демоне не остыл.
    state.pendingRetry = op === "retry";
  }
}

/** Отразить на кнопке то, что сообщает демон. */
function updateChip(message) {
  if (!state.chip || !state.active) return;

  if (message.type === "helper") {
    if (message.ok === false) {
      setChip(message.reason || "нет связи с демоном", "error");
    } else if (state.chip.dataset.mode === "error") {
      // Связь вернулась. Надпись собираем по последней стадии, а не берём
      // прежнюю как есть: прежняя бывает старше самой стадии — «перевожу…»
      // на законченном ролике или занятая кнопка поверх отказа. Новых событий
      // демон при этом мог и не прислать: перевод батча идёт десятками секунд,
      // и всё это время сказать ему нечего.
      if (state.stage === "done") state.chip.hidden = true;
      else if (state.stage === "error") showFailure();
      else if (state.lastLabel) setChip(state.lastLabel.text, state.lastLabel.mode);
    }

    return;
  }

  const stage = message.stage;

  if (message.type === "state" || message.type === "snapshot") {
    if (stage === "error") {
      showFailure();

      return;
    }

    if (stage === "done") {
      // Работа закончена, реплики уже на экране — кнопке там больше не место.
      state.chip.hidden = true;

      return;
    }

    // Без согласия пауза не снимется никогда: опрос такой вкладки задание
    // только читает. Кнопка поэтому зовёт продолжить, а не просто сообщает.
    if (stage === "paused" && !state.consented) {
      setChip("▶ продолжить — карта была занята");

      return;
    }

    if (CHIP_LABELS[stage]) {
      if (message.total) state.total = message.total;

      const progress =
        stage === "translating" && state.total
          ? ` ${state.cues.length}/${state.total}`
          : "";

      // Демон уточняет источник, когда качает не звук, а готовые субтитры автора.
      // Без этого кнопка врала бы «качаю звук» на пути, где звук вообще не нужен.
      // Так же он отличает ожидание видеокарты от обычной очереди: ждать её можно
      // минутами, и «в очереди…» всё это время ничего бы не объясняло.
      let label = CHIP_LABELS[stage];

      if (message.source) label = `качаю ${message.source}…`;
      else if (message.waiting === "gpu") label = "жду видеокарту…";

      // Демон сам повторяет временный сбой загрузки и сообщает номер попытки.
      // Без него кнопка молча топталась бы на одной стадии, и зависание от
      // повтора было бы не отличить.
      const retry = message.retry ? ` (повтор ${message.retry})` : "";

      setChip(label + progress + retry, "busy");
    }
  }
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

  // Вторая строка приходит готовой от демона: черногорский перевод в учебном
  // режиме, текст распознавания без него. Подставлять здесь cue.src нельзя —
  // оверлей не знает, включён ли режим, и на русском ролике такая подстановка
  // молча выдавала русский вместо черногорского: выглядело это не как пропуск
  // строки, а как сломавшийся перевод.
  //
  // Когда перевод не удался, на экране и так исходный текст: второй раз
  // его показывать незачем.
  const second = cue.study;

  if (settings.showOriginal && second && !cue.failed) {
    const original = makeLine(second, "line original");

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
  // Согласие относится к конкретному ролику и на следующий не переносится:
  // иначе автоплей запускал бы обработку всего подряд без спроса.
  state.chip = null;
  state.active = false;
  // Согласие относится к КОНКРЕТНОМУ ролику. Не сбрасывай его здесь — и переход
  // на следующий унёс бы разрешение с собой: переподключение порта отправило бы
  // watch по новому ролику, которого зритель не разрешал.
  state.consented = false;
  state.duration = 0;
  state.total = 0;
  state.stage = null;
  state.error = "";
  state.retryable = true;
  state.pendingRetry = false;
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
  // настройки иначе вступила бы в силу только после возобновления. Отметка
  // null, а не -1: -1 значит «на экране пусто», и реплика, которой больше нет
  // в списке, так на экране бы и осталась.
  state.repaint = () => {
    state.lastIndex = null;
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
    // Только после согласия: без задания позиция просмотра никому не нужна,
    // а запрос к демону вернул бы 404 на каждой секунде. По законченному
    // заданию тоже: двигать в нём уже нечего.
    if (state.active && state.port && state.videoId === videoId && !jobFinished()) {
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

  // Прогресс приходит именно репликами: событие о начале перевода демон шлёт
  // один раз и числа готовых в нём нет. Без этой строки счётчик на кнопке
  // застывал бы на нуле до самого конца работы. По законченному заданию
  // счётчик не ставим: работа уже не идёт, а надпись о ней стала бы последней
  // запомненной и потом выдавала бы себя за текущую.
  if (state.active && state.total && !jobFinished()) {
    setChip(`перевожу… ${state.cues.length}/${state.total}`, "busy");
  }
}

async function activate(videoId) {
  const player = findPlayer();
  const video = player && player.querySelector("video");

  if (!player || !video) return false;

  const { host, box, chip } = buildOverlay(player);

  state.chip = chip;
  hideNativeCaptions();
  startRenderLoop(video, host, box);
  startLiftTracking(player, host, box);
  startPlayheadReports(video, videoId);

  // Задание НЕ запускается само. Каждое качает аудио и занимает видеокарту
  // на минуты, а во вкладках может быть открыто что угодно.
  //
  // Подпись не называет языки намеренно. Она рисуется до первого разговора
  // с демоном, а что зритель увидит на экране, решают два флага сразу:
  // study_language в конфиге демона и showOriginal здесь, в браузере.
  // Любое обещание про языки было бы враньём в свежей установке.
  chip.addEventListener("click", () => {
    // Упавшее задание повторяется нажатием на ту же кнопку. Отказ на демоне
    // прилипает: окончательный — навсегда, временный — на минуту остывания,
    // чтобы опрос не устраивал шторм повторов. Обычная подписка сразу после
    // отказа принесла бы его же, а повтор сбрасывает задание и начинает новое
    // немедленно.
    if (state.stage === "error" && state.retryable) {
      restartJob("retry", "повторяю…");

      return;
    }

    // Вкладка, открытая перезагрузкой, подхватила идущее задание без согласия,
    // и задание встало на паузу. Сама пауза снимается только подпиской с правом
    // создания, то есть нажатием: возобновление — это снова захват карты.
    // Реплики при этом не трогаем — задание то же, и сделанное остаётся на экране.
    if (state.stage === "paused" && !state.consented) {
      state.consented = true;
      setChip("продолжаю…", "busy");
      // Порт мог как раз переподключаться. Тогда подписку пошлёт само
      // переподключение: согласие уже дано, а задание не закончено.
      state.port?.postMessage({ op: "watch", videoId });

      return;
    }

    if (state.active) return;

    state.active = true;
    state.consented = true;
    setChip("запускаю…", "busy");
    state.port.postMessage({ op: "watch", videoId });
  });

  setChip("Сделать субтитры");

  // Спрашиваем демон, не сделано ли уже. Это чистое чтение: если по ролику идёт
  // перевод — подхватим его прогресс, если он готов — поднимем из кэша, а если
  // ничего нет, кнопка так и останется ждать нажатия. Без этого F5 посреди
  // перевода выглядел как потеря всей работы: оверлей заново просил согласия,
  // хотя демон в это время продолжал переводить.
  state.duration = video.duration;
  state.port.postMessage({ op: "restore", videoId, duration: state.duration });

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

    // Демон нашёл готовое или идущее задание по этому ролику. Согласие на него
    // зритель дал раньше — до перезагрузки страницы, — и переспрашивать незачем:
    // ни сети, ни карты восстановление не тратит.
    if (message.type === "restored") state.active = true;

    // Задание на демоне исчезло — перезапуск, вытеснение из реестра. Пересоздавать
    // его молча нельзя: согласия на работу с этой страницы не давали. Возвращаем
    // кнопку, чтобы решал зритель.
    if (message.type === "gone" && !state.consented) {
      state.active = false;
      state.stage = null;
      setChip("Сделать субтитры");

      return;
    }

    trackStage(message);

    if (message.type === "cues" || message.type === "snapshot") {
      mergeCues(message.cues || []);
    }

    updateChip(message);
  });

  state.port.onDisconnect.addListener(() => {
    state.port = null;
    // Service worker засыпает — это норма. Переподключаемся и заново
    // объявляем, что смотрим, чтобы демон продолжил слать реплики.
    setTimeout(() => {
      if (!api.runtime?.id) return;

      connect();

      // По законченному заданию всё уже на экране, и просить нечего. watch мог
      // бы заново запустить работу, а restore перечитывал бы кэш при каждом
      // засыпании фоновой страницы — то есть раз в полминуты, пока открыта вкладка.
      if (jobFinished()) return;

      // Только если согласие уже дано. Иначе разрыв соединения запускал бы
      // обработку сам по себе, в обход кнопки.
      if (state.videoId && state.consented) {
        // Нажатый в окно переподключения повтор уходит сейчас, см. restartJob.
        const op = state.pendingRetry ? "retry" : "watch";

        state.pendingRetry = false;
        state.port.postMessage({ op, videoId: state.videoId });
      } else if (state.videoId) {
        // Восстановленная вкладка переспрашивает тем же безопасным путём.
        // Слать сюда watch нельзя: он создаёт задание, а нажатия не было.
        state.port.postMessage({
          op: "restore",
          videoId: state.videoId,
          duration: state.duration,
        });
      }
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

    // Смена источника реплик начинает разбор открытого ролика заново — так
    // обещают настройки. Идущее задание подхватит её само: опрос уносит
    // настройку на каждом витке. По законченному опроса уже нет, и без этого
    // на экране так и остался бы прежний результат. Только там, где нажимали:
    // восстановленная вкладка согласия на работу не давала.
    if (changes.useAuthorTrack && state.consented && jobFinished()) {
      restartJob("watch", "запускаю…");
    }
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
