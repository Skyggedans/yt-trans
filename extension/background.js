/**
 * Transport to the local daemon: long polling over plain HTTP.
 *
 * WebSocket does not work here, and that cannot be fixed by a setting on our side.
 * The extension context counts as secure, so the browser upgrades ws://
 * to wss:// as mixed content. The local-address exemption, thanks to which
 * plain http:// goes through, does not extend to WebSocket. Measured with a trap
 * on a live browser: 13 TLS attempts against 10 successful
 * plain requests to the same port.
 *
 * Long polling cures this entirely: the same http:// that demonstrates
 * that things work, plus a side benefit — short repeated requests
 * reset the idle timer of Chrome's service worker, whereas one long
 * request, on the contrary, kills it after thirty seconds.
 */

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = {
  // Protocol to the daemon. A daemon with a certificate ([server] tls_cert in
  // config.toml) listens ONLY over https: a plain http request gets nothing from
  // such a port, and the viewer would see "the daemon does not respond" while it
  // is alive. Selected in the settings.
  scheme: "http",
  host: "127.0.0.1",
  port: 8756,
  targetLang: "ru",
  // A ready-made author track instead of recognition, if the video has one.
  useAuthorTrack: true,
};

// Pause after a failure. The daemon may not be running — there is no point
// hammering it more often.
const RETRY_MS = 3000;

// Liveness probe after a break: the first one immediately, then rarer and rarer,
// but no rarer than once in ten seconds — a restarted daemon must be picked up
// quickly.
const PROBE_MIN_MS = 1000;
const PROBE_MAX_MS = 10000;

// The probe request answers instantly. Three seconds of silence is no longer the
// daemon working but a hung process, or a port nobody listens on.
const PROBE_TIMEOUT_MS = 3000;

/** Content script ports by tab identifier. */
const tabs = new Map();

/** Which video the tab is watching. */
const watching = new Map();

/**
 * Running polls by video identifier: { controller, create, cursor, broken }.
 *
 * broken — a failure has been shown to the viewer. Needed to report RECOVERY
 * of the connection: without that message the break notice stayed on the button
 * forever — the poll resumed successfully, but the success was never reported,
 * and the viewer saw a break while the daemon was perfectly alive. It lives in
 * the poll itself rather than the loop because a failure may be shown before
 * the poll starts: the retry reports the missed recovery itself.
 */
const polls = new Map();

async function settings() {
  const stored = await api.storage.local.get(DEFAULTS);

  return { ...DEFAULTS, ...stored };
}

function baseUrl({ scheme, host, port }) {
  // Anything except an explicit https is http: the storage may hold anything,
  // and a request address must not be built from it.
  return `${scheme === "https" ? "https" : "http"}://${host}:${port}`;
}

/**
 * Which protocol to the daemon answers right now.
 *
 * The setting cannot be trusted: a temporary add-on loses its storage on every
 * browser restart, and a daemon with a certificate listens only over https. So
 * the protocol is chosen by the very first response — the live one is remembered
 * until the end of the session.
 */
let liveScheme = null;

/** Check order: the live protocol first, the other one as a fallback. */
function schemes(config) {
  const chosen = config.scheme === "https" ? "https" : "http";
  const other = chosen === "https" ? "http" : "https";

  return liveScheme === other ? [other, chosen] : [chosen, other];
}

/**
 * A request to the daemon over the live protocol.
 *
 * A network refusal on one protocol is not final: http to a TLS port is silent
 * just like a closed port, while the other protocol answers at the same time.
 * We never repeat a cancellation — it is our own unwatch, not a failure.
 */
async function call(config, path, init = {}) {
  let failure;

  for (const scheme of schemes(config)) {
    try {
      const response = await fetch(`${baseUrl({ ...config, scheme })}${path}`, init);

      liveScheme = scheme;

      return response;
    } catch (error) {
      if (error.name === "AbortError") throw error;

      failure = error;
    }
  }

  // Neither answered: the daemon may have restarted with a different certificate.
  liveScheme = null;

  throw failure;
}

function viewers(videoId) {
  return [...watching.entries()].filter(([, id]) => id === videoId).map(([tabId]) => tabId);
}

function deliver(videoId, message) {
  for (const tabId of viewers(videoId)) {
    const port = tabs.get(tabId);

    if (!port) continue;

    try {
      // videoId is set here: the daemon does not write about it in events, and the
      // content script must discard everything that belongs to the previous video.
      port.postMessage({ ...message, videoId });
    } catch {
      tabs.delete(tabId);
    }
  }
}

/** The stage the job ended up at according to the poll response. */
function lastStage(data) {
  let stage = data.snapshot?.stage;

  for (const event of data.events || []) {
    if (event.type === "state") stage = event.stage;
  }

  return stage;
}

async function pollLoop(videoId, poll) {
  // Breaks in a row. A single one is almost always caused not by the daemon but by
  // the browser itself: on a network change — docker's interface brought back up,
  // waking from sleep — Firefox closes every connection that carried no byte for
  // five seconds, local ones too, and does not repeat such a request. Long polling
  // stays silent for up to twenty-five seconds and hits this every time. A fresh
  // connection after such a cleanup works at once, so the first break is not shown
  // to the viewer and we ask again without a pause. A second one in a row is
  // already a reason to check the daemon.
  let failures = 0;

  // Compare the object itself, not the presence of the key. A poll stopped and
  // immediately restarted for the same video is a different poll, and the old loop,
  // waking up after await, would otherwise keep running next to the new one.
  while (polls.get(videoId) === poll) {
    const config = await settings();

    // The poll could have been stopped while the settings were being read. A request
    // sent after that would have nobody left to abort it.
    if (polls.get(videoId) !== poll) return;

    const controller = new AbortController();
    poll.controller = controller;

    const path =
      `/v1/jobs/${encodeURIComponent(videoId)}/events` +
      `?cursor=${poll.cursor}&target=${encodeURIComponent(config.targetLang)}` +
      `&authorTrack=${config.useAuthorTrack === false ? "false" : "true"}` +
      `&create=${poll.create ? "true" : "false"}`;

    // Whether the request reached a response. Distinguishes a break from a response
    // that could not be parsed: the latter must not be retried without a pause, see
    // below.
    let answered = false;

    try {
      const response = await call(config, path, {
        signal: controller.signal,
        cache: "no-store",
      });

      // A response arrived — any response, but from a live process. The break count
      // is kept only for requests that did not arrive at all.
      answered = true;
      failures = 0;

      if (!response.ok) {
        deliver(videoId, {
          type: "helper",
          ok: false,
          reason: `the daemon answered ${response.status}`,
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

      // The job no longer exists on the daemon: it was restarted or the registry
      // evicted the finished one. We have no right to create a new one — consent
      // for that was never given.
      if (data.gone) {
        deliver(videoId, { type: "gone" });
        stopPolling(videoId);

        return;
      }

      poll.cursor = data.cursor ?? poll.cursor;

      if (data.snapshot) deliver(videoId, data.snapshot);

      for (const event of data.events || []) deliver(videoId, event);

      // A finished or failed job will send nothing more. Polling it is
      // a connection silent for twenty-five seconds, which the browser tears
      // on every network change, and a break on the screen on top of long
      // finished work. Only the viewer starts such a job again — with a retry or
      // a change of cue source — and the poll is raised again then.
      const stage = lastStage(data);

      if (stage === "done" || stage === "error") {
        stopPolling(videoId);

        return;
      }
    } catch (error) {
      // Cancellation is our own unwatch, not a failure.
      if (error.name === "AbortError") return;

      if (polls.get(videoId) !== poll) return;

      if (answered) {
        // A response arrived, but it could not be parsed: something other than
        // the daemon is listening on the port, or the body is of the wrong shape.
        // This does not count as a break — repeating without a pause would hammer
        // that server without a single breather, and the viewer would watch a
        // frozen button. We act as with an error response.
        console.warn(`yt-trans: could not parse the poll response ${videoId}`, error);
        deliver(videoId, { type: "helper", ok: false, reason: "unclear daemon response" });
        poll.broken = true;
        await sleep(RETRY_MS);
        continue;
      }

      console.warn(`yt-trans: poll ${videoId} broke off`, error);
      failures += 1;

      if (failures < 2) continue;

      poll.broken = true;
      deliver(videoId, { type: "helper", ok: false, reason: "the daemon is not responding, reconnecting…" });

      if (!(await waitForDaemon(videoId, poll))) return;

      // Report at once without waiting for the next poll: on a quiet job it stays
      // silent for up to twenty-five seconds, and all that time the button would
      // keep repeating a break that no longer exists.
      poll.broken = false;
      failures = 0;
      deliver(videoId, { type: "helper", ok: true });
    }
  }
}

/**
 * Wait until the daemon process answers again.
 *
 * We ask for something the daemon answers at once without creating anything: a job
 * snapshot or a 404 if there is no job. ANY HTTP response is good enough, even a
 * 403 — what matters is not what was said but that a live process said it.
 *
 * Returns false if the poll was stopped in the meantime.
 */
async function waitForDaemon(videoId, poll) {
  for (let delay = PROBE_MIN_MS; ; delay = Math.min(delay * 2, PROBE_MAX_MS)) {
    const config = await settings();

    if (polls.get(videoId) !== poll) return false;

    try {
      await call(
        config,
        `/v1/jobs/${encodeURIComponent(videoId)}`,
        { cache: "no-store", signal: AbortSignal.timeout(PROBE_TIMEOUT_MS) },
      );

      return polls.get(videoId) === poll;
    } catch {
      // The port is closed or the daemon did not have time to answer — wait and ask
      // again.
    }

    await sleep(delay);

    if (polls.get(videoId) !== poll) return false;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Ask the daemon whether something already exists for this video, and pick it up.
 *
 * Called on every page load, so it must be cheap and silent: the request is local,
 * starts no work, and if there is no daemon or the video is unfamiliar it simply
 * does nothing, and the viewer sees the usual start button.
 */
async function restore(tabId, videoId, duration) {
  const config = await settings();

  const path =
    `/v1/jobs/${encodeURIComponent(videoId)}/restore` +
    `?target=${encodeURIComponent(config.targetLang)}` +
    `&authorTrack=${config.useAuthorTrack === false ? "false" : "true"}` +
    // Zero means "the player has not read the metadata yet", not a duration. If it
    // went into the request as a real one, the cache check would fail, and the
    // restore would silently not work precisely on a fast reload.
    (Number.isFinite(duration) && duration > 0 ? `&duration=${duration}` : "");

  let data;

  try {
    const response = await call(config, path, { cache: "no-store" });

    if (!response.ok) return;

    data = await response.json();
  } catch {
    return;
  }

  if (!data.found) return;

  // Between the request and the response the viewer could have moved to another
  // video: the request goes through two awaits. Without this check the poll would
  // be raised for an abandoned video and live forever — there would be nobody left
  // to stop it.
  if (watching.get(tabId) !== videoId) return;

  // The order matters: first the restore marker, which enables the overlay,
  // and only then the snapshot. Otherwise cues would arrive into an inactive
  // overlay and be discarded.
  deliver(videoId, { type: "restored" });

  if (data.snapshot) deliver(videoId, data.snapshot);

  // There is nothing to wait for on a finished job — we do not raise the poll at
  // all. Otherwise it would forever poke the creating endpoint on every open tab.
  // A failed one is the same: a poll from the end of the journal learns nothing
  // new about it and cannot stop itself.
  if (data.stage === "done" || data.stage === "error") return;

  startPolling(videoId, { create: false, cursor: data.cursor ?? 0 });
}

/**
 * Raise the event poll.
 *
 * create distinguishes the two subscriptions. The usual one is a consequence of the
 * button press, and it has the right to create a job: subscribing to a video IS
 * consent to the work. A restored one carries no consent: the tab was merely
 * reloaded, and if there is no job on the daemon anymore, it must not be created —
 * otherwise F5 alone would start downloading audio and occupying the GPU. Rights
 * only increase: a button press on top of a restore makes the subscription full.
 */
function startPolling(videoId, { create = true, cursor = 0 } = {}) {
  const existing = polls.get(videoId);

  if (existing) {
    // The right to create was just granted — by a press in a tab opened by a
    // reload. The hanging request went out without it and will stay silent for up
    // to twenty-five seconds, and a paused job will not resume through it. We raise
    // the poll again from the same position so that consent arrives at once.
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

  // Abort the hanging request, otherwise it will hold the connection until the end
  // of the timeout.
  if (poll.controller) poll.controller.abort();
}

/**
 * Retry a failed job at the viewer's request.
 *
 * A refusal on the daemon sticks: a final one forever, a temporary one for a minute
 * of cooling down. Otherwise the poll would itself cause a storm of retries hitting
 * YouTube on every round. The viewer should not wait that minute, so a retry is a
 * reset of the job and a fresh subscription. Cursor from zero: a new job has its
 * own journal, and a position in the old one means nothing in it.
 */
async function retry(videoId) {
  stopPolling(videoId);

  const config = await settings();
  const job = `/v1/jobs/${encodeURIComponent(videoId)}`;
  let unreachable = false;

  try {
    // We reset only what is still lying failed. The retry button may be older than
    // the refusal itself: a neighbouring tab with the same video has already retried
    // it, and events of the new job do not reach this one if the background page
    // managed to fall asleep and forget who is watching what. A blind reset would
    // kill running work — even a recognition halfway through — and start it from
    // scratch. A running job is simply picked up by the subscription.
    const response = await call(config, job, { cache: "no-store" });
    const snapshot = response.ok ? await response.json() : null;

    if (snapshot?.stage === "error") {
      await call(config, `${job}/drop`, { method: "POST", cache: "no-store" });
    }
  } catch (error) {
    // The reset did not arrive. We raise the poll anyway: it knows how to wait for
    // the daemon itself, and if the refusal is still in place, the viewer will see
    // it again and press once more.
    console.warn(`yt-trans: reset ${videoId} did not arrive`, error);
    unreachable = true;
    deliver(videoId, { type: "helper", ok: false, reason: "the daemon is not responding, reconnecting…" });
  }

  // While the reset was going on, the viewer could have left the video. A job with
  // no viewer is needed by nobody.
  if (viewers(videoId).length === 0) return;

  startPolling(videoId, { create: true, cursor: 0 });

  // The viewer already knows about the break — so they must learn about the
  // recovery too.
  if (unreachable) polls.get(videoId).broken = true;
}

/** Mark which video the tab is watching, releasing the previous one if nobody needs it anymore. */
function follow(tabId, videoId) {
  const previous = watching.get(tabId);

  if (previous && previous !== videoId && viewers(previous).length <= 1) {
    stopPolling(previous);
  }

  watching.set(tabId, videoId);
}

async function sendPlayhead(videoId, t) {
  const config = await settings();

  try {
    await call(config, `/v1/jobs/${encodeURIComponent(videoId)}/playhead`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ t }),
    });
  } catch {
    // The playhead position is a priority hint, not a command. Losing it breaks
    // nothing: the next one will come in a second.
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
      // Mark the tab's video synchronously, before the request: it is needed both
      // for message delivery and as a sign that the tab is still here when the
      // response returns. This starts no work — only startPolling starts it.
      follow(tabId, message.videoId);
      restore(tabId, message.videoId, message.duration);
    } else if (message.op === "retry") {
      // A tab with a finished job does not announce itself after the port reconnects,
      // so we mark it again: without this there would be nobody to deliver events of
      // the new job to.
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

// Address or protocol change: we break the polls, they will reconnect
// with the new settings on the next round of the loop.
//
// We raise again only those that were running, each with its own right to create.
// A finished job holds no poll, and a subscription with the right to create on it
// would start work without a press: the daemon recreates a failed one after it
// cools down. The same with a restored tab — it gave no consent, and a settings
// change does not give it. Cursor from zero: a different daemon may answer at the
// new address, and a position in someone else's journal means nothing.
api.storage.onChanged.addListener((changes) => {
  if (changes.scheme || changes.host || changes.port) {
    // A new address — a new protocol choice: the previous one may have belonged
    // to another daemon.
    liveScheme = null;

    // On a copy: stopPolling changes polls, and the collection must not be changed
    // while iterating.
    const running = [...polls].map(([videoId, poll]) => [videoId, poll.create]);

    for (const [videoId] of running) stopPolling(videoId);

    for (const [videoId, create] of running) startPolling(videoId, { create });
  }
});
