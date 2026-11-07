/**
 * Subtitle overlay on top of the YouTube player.
 *
 * Attached to #movie_player, not to the video container. This is the very element
 * YouTube passes to requestFullscreen() and moves into the miniplayer,
 * so fullscreen, theater and mini modes work by themselves —
 * without a single fullscreenchange handler.
 */

const api = globalThis.browser ?? globalThis.chrome;

const HOST_TAG = "yt-trans-overlay";
const NAV_POLL_MS = 500;
const PLAYHEAD_MS = 1000;

// The control bar jitters while the cursor moves along the progress bar. Without a
// dead zone the text would shake with every pixel of its trembling.
const LIFT_HYSTERESIS_PX = 4;

const settings = {
  showOriginal: false,
  // "above" — the original above the translation, "below" — under it.
  originalPosition: "below",
};

const state = {
  videoId: null,
  // Grows on every navigation. Everything that comes with an old number is dropped:
  // otherwise the daemon's responses for the previous video would be painted over
  // the new one.
  epoch: 0,
  cues: [],
  lastIndex: -1,
  lift: 0,
  port: null,
  // Set by the render loop while it is alive.
  repaint: null,
  // The start button and a mark that the viewer has already consented to
  // translating this video. Without consent the daemon is not touched at all:
  // every job downloads audio and occupies the GPU, so it must not be started on
  // everything that happens to be open in the browser.
  chip: null,
  // The last label on the button, errors excluded.
  lastLabel: null,
  // The overlay is connected and accepts cues. This is NOT consent: restoring
  // after a page reload enables it too.
  active: false,
  // The viewer pressed the button on THIS page. Only this flag gives the right
  // to ask for work: to download audio and occupy the GPU. They are separate
  // because there used to be one, and the restore signed the viewer up with it —
  // a port reconnect sent {op:"watch"} in a tab where nobody pressed anything.
  consented: false,
  // Video duration according to the player. Goes into the restore request:
  // by it the daemon tells the cache apart from a re-upload under the same video
  // identifier, without asking YouTube for metadata on every page load.
  duration: 0,
  // How many cues there are in total. Comes once together with the translation
  // stage and is needed to show progress as cues arrive.
  total: 0,
  // The last job stage as the daemon reported it. From it the button decides
  // what to show after a connection break and whether a failure can be retried, and
  // the playhead poll and the port reconnect decide whether they are needed at all.
  stage: null,
  // Failure, if the job failed: the reason text and whether it can pass by itself.
  error: "",
  retryable: true,
  // Retry pressed while the port was reconnecting, to go out with the reconnect.
  pendingRetry: false,
  teardown: [],
};

// ── utilities ───────────────────────────────────────────────────────────────

function videoIdFromLocation() {
  const url = new URL(location.href);
  const shorts = url.pathname.match(/^\/(shorts|live)\/([A-Za-z0-9_-]{11})/);

  return shorts ? shorts[2] : url.searchParams.get("v") || null;
}

function findPlayer() {
  // On Shorts, YouTube keeps a hidden preloaded #movie_player in the DOM.
  // Asking for it first would hang the overlay on an invisible player.
  if (location.pathname.startsWith("/shorts/")) {
    return document.querySelector("#shorts-player") || document.querySelector(".html5-video-player");
  }

  return document.querySelector("#movie_player") || document.querySelector(".html5-video-player");
}

/** Index of the cue active at time t. Binary search: there are often hundreds of cues. */
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

// ── overlay ─────────────────────────────────────────────────────────────────

function buildOverlay(player) {
  const host = document.createElement(HOST_TAG);

  Object.assign(host.style, {
    position: "absolute",
    inset: "0",
    // Transparent to the mouse: clicks must reach the player.
    pointerEvents: "none",
    // Above the native subtitle window but below YouTube's settings menu.
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
      /* A cue that could not be translated: the recognition text is on screen.
         The viewer must see that this is not a translation but a fallback. */
      .box.failed .line { color: #ffd9a0; font-style: italic; }
      /* The original is a secondary line, read out of the corner of the eye.
         Smaller and dimmer so as not to compete for attention with the translation. */
      .line.original {
        font-size: calc(var(--font) * 0.78);
        color: #d8d8d8;
        background: rgba(8, 8, 8, 0.62);
      }
      /* The only element that accepts the mouse: everything else in the overlay is
         transparent so that clicks reach the player. */
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
        /* A failure reason can be a paragraph long — it is the tail of yt-dlp's
           output. One line with an ellipsis, otherwise the button would stick out
           beyond the player's edge; the full text lives in the tooltip. */
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
      /* Breaking the connection and a failure without a retry are useless to press —
         the cursor must not promise otherwise. */
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
  queued: "queued…",
  probing: "reading the video…",
  fetching: "downloading audio…",
  asr: "recognizing speech…",
  translating: "translating…",
  paused: "paused: GPU busy",
};

function setChip(text, mode = "idle", title = "") {
  const chip = state.chip;

  if (!chip) return;

  // Remember the last NON-error label: we must return to it when the connection
  // recovers. Otherwise the break message would outlive the break itself — the work
  // has long moved on, but the button still says the daemon is gone.
  if (mode !== "error" && mode !== "retry") state.lastLabel = { text, mode };

  chip.hidden = false;
  chip.textContent = text;
  chip.title = title;
  chip.dataset.mode = mode;
}

/** The job is finished — successfully or not. The daemon will send nothing more for it. */
function jobFinished() {
  return state.stage === "done" || state.stage === "error";
}

/**
 * Remember the stage from the daemon message.
 *
 * Called before merging cues: a snapshot brings both cues and the stage, and the
 * "translating…" counter must not land on the button when the same snapshot already
 * makes it clear that the work is finished.
 */
function trackStage(message) {
  if (message.type !== "state" && message.type !== "snapshot") return;
  if (!message.stage) return;

  state.stage = message.stage;

  if (message.stage === "error") {
    state.error = message.message || message.error || "";
    // An old daemon does not send this field. We treat a failure without it as
    // temporary: an extra press costs one reset, while a missing retry button
    // costs a video without subtitles.
    state.retryable = message.retryable !== false;
  }
}

/**
 * Show a job failure.
 *
 * The invitation to retry stands at the start of the label: a long reason is cut
 * off with an ellipsis, and at the end it simply would not be visible — the button
 * would look like a label there is no point pressing.
 */
function showFailure() {
  const full = state.error || "didn't work";
  // "ERROR:" is a level marker from yt-dlp's output. It tells the viewer nothing
  // and takes up space on the button.
  const reason = full.replace(/\bERROR:\s*/g, "").replace(/\s+/g, " ").trim() || full;

  if (state.retryable) setChip(`↻ retry — ${reason}`, "retry", full);
  else setChip(reason, "error", full);
}

/**
 * Start a job for a video over: by retrying a failure or changing the cue source.
 *
 * We forget cues of the previous job. The new one has its own journal from zero,
 * while merging goes by cue number and deletes nothing: the "translating…" counter
 * would start from the old number, and cues with different markup — recognition
 * instead of the author track, another recognition run — would land on the screen
 * mixed with the new ones.
 */
function restartJob(op, label) {
  state.active = true;
  state.consented = true;
  // The previous job's stage means nothing anymore. Resetting it closes the
  // second press while this retry is running and returns the port reconnect to
  // an ordinary subscription to the work.
  state.stage = null;
  state.cues = [];
  state.total = 0;
  state.repaint?.();
  setChip(label, "busy");

  if (state.port) {
    state.port.postMessage({ op, videoId: state.videoId });
  } else {
    // The port is just reconnecting — the background page fell asleep. The
    // reconnect will send the subscription itself, but the retry must be
    // remembered: otherwise a usual subscription would go instead and bring the
    // same failure back while it is still cooling down on the daemon.
    state.pendingRetry = op === "retry";
  }
}

/** Reflect on the button what the daemon reports. */
function updateChip(message) {
  if (!state.chip || !state.active) return;

  if (message.type === "helper") {
    if (message.ok === false) {
      setChip(message.reason || "no connection to the daemon", "error");
    } else if (state.chip.dataset.mode === "error") {
      // The connection is back. We build the label from the last stage rather than
      // taking the previous one as is: the previous one may be older than the stage
      // itself — "translating…" on a finished video or a busy button on top of a
      // failure. And the daemon might not have sent new events: translating a batch
      // takes tens of seconds, and it has nothing to say all that time.
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
      // The work is finished, the cues are already on screen — the button has no
      // place there anymore.
      state.chip.hidden = true;

      return;
    }

    // Without consent the pause will never be lifted: a poll of such a tab only
    // reads the job. That is why the button calls to continue rather than merely
    // reporting.
    if (stage === "paused" && !state.consented) {
      setChip("▶ continue — the GPU was busy");

      return;
    }

    if (CHIP_LABELS[stage]) {
      if (message.total) state.total = message.total;

      const progress =
        stage === "translating" && state.total
          ? ` ${state.cues.length}/${state.total}`
          : "";

      // The daemon clarifies the source when it is downloading the author's ready
      // subtitles rather than audio. Without this the button would lie "downloading
      // audio" on a path where audio is not needed at all. Likewise it tells waiting
      // for the graphics card apart from an ordinary queue: that wait can last
      // minutes, and "queued…" would explain nothing all that time.
      let label = CHIP_LABELS[stage];

      if (message.source) label = `downloading ${message.source}…`;
      else if (message.waiting === "gpu") label = "waiting for the GPU…";

      // The daemon repeats a temporary download failure itself and reports the
      // attempt number. Without it the button would silently trample on one stage,
      // and a hang would be indistinguishable from a retry.
      const retry = message.retry ? ` (retry ${message.retry})` : "";

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

  // The second line comes ready-made from the daemon: the Montenegrin translation
  // in study mode, the recognition text without it. cue.src must not be substituted
  // here — the overlay does not know whether the mode is on, and on a Russian video
  // such a substitution would silently produce Russian instead of Montenegrin: it
  // would look not like a missing line but like a broken translation.
  //
  // When the translation failed, the source text is on screen anyway: there is no
  // point showing it a second time.
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
 * Lift the text above the control bar when it is shown.
 *
 * The height is computed from the player's geometry, not from the overlay's live
 * rectangle: `bottom` is animated, and reading it during a transition feeds the
 * previous lift back into the calculation, which makes the text oscillate.
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

  // We temporarily suppress the transition: otherwise a repaint after a player mode
  // change takes 120 ms, and the frame sits in the wrong place all that time.
  box.classList.add("notrans");
  host.style.setProperty("--lift", `${lift}px`);
  void box.offsetHeight;
  requestAnimationFrame(() => box.classList.remove("notrans"));
}

// ── lifecycle of one video ──────────────────────────────────────────────────

function teardown() {
  for (const undo of state.teardown.splice(0)) {
    try {
      undo();
    } catch {
      /* the element may already be gone together with the player */
    }
  }

  document.querySelectorAll(HOST_TAG).forEach((node) => node.remove());
  document.documentElement.classList.remove("yt-trans-active");
  state.cues = [];
  state.lastIndex = -1;
  state.lift = 0;
  // Consent belongs to a specific video and is not carried over to the next one:
  // otherwise autoplay would start processing everything without asking.
  state.chip = null;
  state.active = false;
  // Consent belongs to a SPECIFIC video. Do not reset it here — otherwise moving
  // to the next one would carry the permission along: the port reconnect would send
  // watch for a new video the viewer did not allow.
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

  // We dim with opacity, not display:none. Native subtitles hidden this way stay in
  // the DOM, and their text can be read if needed.
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

  // rVFC does not fire while frames are not changing, so seeking while paused would
  // leave the previous cue on screen. These events repaint explicitly.
  for (const name of ["seeked", "ratechange", "play", "pause", "loadedmetadata"]) {
    video.addEventListener(name, render);
    state.teardown.push(() => video.removeEventListener(name, render));
  }

  render();

  // Repaint on demand: the frame tick does not run while paused, so a settings
  // change would otherwise take effect only after resuming. The mark is null, not
  // -1: -1 means "the screen is empty", and a cue that is no longer in the list
  // would stay on screen.
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

  // The ytp-autohide class toggles continuously while the cursor moves along the
  // progress bar, so a burst of mutations collapses into one frame.
  const observer = new MutationObserver(schedule);
  observer.observe(player, { attributes: true, attributeFilter: ["class"] });

  // Changing the player mode and resizing the window do not touch the class but do
  // change the geometry.
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
    // Only after consent: without a job the playhead position is needed by nobody,
    // and a request to the daemon would return 404 every second. For a finished job
    // too: there is nothing left to move in it.
    if (state.active && state.port && state.videoId === videoId && !jobFinished()) {
      state.port.postMessage({ op: "playhead", videoId, t: video.currentTime });
    }
  }, PLAYHEAD_MS);

  state.teardown.push(() => clearInterval(timer));
}

function mergeCues(incoming) {
  // Cues come in batches and out of order: the window around the playhead is
  // translated first. We keep the array sorted, otherwise the binary search would lie.
  const byIndex = new Map(state.cues.map((cue) => [cue.i, cue]));

  for (const cue of incoming) byIndex.set(cue.i, cue);

  state.cues = [...byIndex.values()].sort((a, b) => a.start - b.start);
  state.lastIndex = -1;

  // Progress comes precisely with cues: the daemon sends the translation-start
  // event once and has no finished count in it. Without this line the counter on
  // the button would freeze at zero until the very end of the work. We do not set
  // the counter for a finished job: the work is no longer running, and the label
  // about it would become the last remembered one and later pass itself off as the
  // current one.
  if (state.active && state.total && !jobFinished()) {
    setChip(`translating… ${state.cues.length}/${state.total}`, "busy");
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

  // The job does NOT start by itself. Each one downloads audio and occupies the
  // graphics card for minutes, and anything at all may be open in the tabs.
  //
  // The label deliberately does not name languages. It is drawn before the first
  // conversation with the daemon, and what the viewer sees on screen is decided by
  // two flags at once: study_language in the daemon config and showOriginal here,
  // in the browser. Any promise about languages would be a lie in a fresh install.
  chip.addEventListener("click", () => {
    // A failed job is retried by pressing the same button. A refusal on the daemon
    // sticks: a final one forever, a temporary one for a minute of cooling down,
    // so that the poll does not cause a storm of retries. An ordinary subscription
    // right after a refusal would bring the same one, while a retry resets the job
    // and starts a new one immediately.
    if (state.stage === "error" && state.retryable) {
      restartJob("retry", "retrying…");

      return;
    }

    // A tab opened by a reload picked up a running job without consent, and the job
    // paused. The pause itself is lifted only by a subscription with the right to
    // create, that is, by a press: resuming means capturing the GPU again. We do not
    // touch the cues: the job is the same, and what is done stays on screen.
    if (state.stage === "paused" && !state.consented) {
      state.consented = true;
      setChip("continuing…", "busy");
      // The port could be reconnecting right now. Then the reconnect will send the
      // subscription itself: consent is already given, and the job is not finished.
      state.port?.postMessage({ op: "watch", videoId });

      return;
    }

    if (state.active) return;

    state.active = true;
    state.consented = true;
    setChip("starting…", "busy");
    state.port.postMessage({ op: "watch", videoId });
  });

  setChip("Make subtitles");

  // We ask the daemon whether it is already done. This is a pure read: if a
  // translation is running for the video — we pick up its progress, if it is ready
  // — we raise it from the cache, and if there is nothing, the button keeps waiting
  // for a press. Without this, F5 in the middle of a translation looked like losing
  // all the work: the overlay asked for consent again, while the daemon kept
  // translating all that time.
  state.duration = video.duration;
  state.port.postMessage({ op: "restore", videoId, duration: state.duration });

  return true;
}

// ── navigation ──────────────────────────────────────────────────────────────

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

  // The player does not appear in the DOM instantly; we wait for it, but not
  // forever.
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

    // The daemon found a ready or running job for this video. The viewer gave
    // consent for it earlier — before the page reload — and there is no need to ask
    // again: the restore spends neither network nor GPU.
    if (message.type === "restored") state.active = true;

    // The job disappeared on the daemon — a restart, eviction from the registry.
    // It cannot be recreated silently: no consent for work was given from this page.
    // We return the button so that the viewer decides.
    if (message.type === "gone" && !state.consented) {
      state.active = false;
      state.stage = null;
      setChip("Make subtitles");

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
    // The service worker falls asleep — that is normal. We reconnect and announce
    // again what we are watching so that the daemon keeps sending cues.
    setTimeout(() => {
      if (!api.runtime?.id) return;

      connect();

      // For a finished job everything is already on screen, and there is nothing to
      // ask for. watch could restart the work, while restore would re-read the cache
      // on every falling asleep of the background page — that is, once every half a
      // minute while the tab is open.
      if (jobFinished()) return;

      // Only if consent is already given. Otherwise a connection break would start
      // processing by itself, bypassing the button.
      if (state.videoId && state.consented) {
        // A retry pressed during the reconnect window goes out now, see restartJob.
        const op = state.pendingRetry ? "retry" : "watch";

        state.pendingRetry = false;
        state.port.postMessage({ op, videoId: state.videoId });
      } else if (state.videoId) {
        // A restored tab asks again in the same safe way. watch must not be sent
        // here: it creates a job, and there was no press.
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

    // Changing the cue source starts processing of the open video over — that is
    // what the settings promise. A running job will pick it up itself: the poll
    // carries the setting on every round. A finished job has no poll anymore, and
    // without this the previous result would stay on screen. Only where a press
    // happened: a restored tab gave no consent to work.
    if (changes.useAuthorTrack && state.consented && jobFinished()) {
      restartJob("watch", "starting…");
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
    // After an extension reload the old content script keeps living with a dead
    // runtime, and without self-destruction the overlay would hang on screen forever.
    if (!api.runtime?.id) {
      clearInterval(poll);
      teardown();

      return;
    }

    // Swipes in Shorts change the address faster than yt-navigate-finish fires,
    // and a restored tab may come back with the player already running.
    if (videoIdFromLocation() !== state.videoId) onNavigate();
  }, NAV_POLL_MS);
}

main();
