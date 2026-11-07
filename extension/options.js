/**
 * Settings and connection diagnostics for the daemon.
 *
 * We check exactly the transport the work goes over — plain HTTP or
 * HTTPS. WebSocket was removed from here not for simplicity: the browser upgrades
 * ws:// to wss:// when called from an extension context, so there is no point
 * checking it, and the working event stream is carried by long polling over the
 * same protocol this diagnostic probes.
 */

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = {
  scheme: "http",
  host: "127.0.0.1",
  port: 8756,
  targetLang: "ru",
  showOriginal: false,
  originalPosition: "below",
  useAuthorTrack: true,
};

const fields = {
  scheme: document.querySelector("#scheme"),
  host: document.querySelector("#host"),
  port: document.querySelector("#port"),
};

const useAuthorTrack = document.querySelector("#useAuthorTrack");
const showOriginal = document.querySelector("#showOriginal");
const originalPosition = document.querySelector("#originalPosition");
const positionRow = document.querySelector("#positionRow");

const status = document.querySelector("#status");

function show(ok, text) {
  status.hidden = false;
  status.className = ok ? "ok" : "bad";
  status.textContent = text;
}

/** Recognise the cause of a failure from the fetch error text. */
function explain(error, base, scheme) {
  const text = String(error && error.message ? error.message : error).toLowerCase();

  if (text.includes("local network") || text.includes("address space") || text.includes("loopback")) {
    return (
      "The browser blocked access to the local network.\n\n" +
      "Chrome: chrome://extensions → yt-trans → Details →\n" +
      "Site settings → Local network access → Allow.\n\n" +
      "Firefox and forks: the restriction is on by default since version 151,\n" +
      `and is configured in the permissions for ${base}.`
    );
  }

  const other = scheme === "https" ? "http" : "https";

  // curl does not read the browser's certificate store, so its own CA is given
  // to it as a file — otherwise a manual check would fail on distrust.
  const manual =
    scheme === "https"
      ? `curl --cacert ~/.config/yt-trans/tls/ca.crt ${base}/v1/health`
      : `curl ${base}/v1/health`;

  let protocolHint =
    `A daemon with a certificate ([server] tls_cert in config.toml) listens only over https,\n` +
    `without a certificate — only over http. Currently selected: ${scheme}; you may need ${other}.`;

  if (scheme === "https") {
    protocolHint +=
      "\n\nThe browser must trust the daemon's certificate: ca.crt from\n" +
      "~/.config/yt-trans/tls/ is installed into the browser's certificate store —\n" +
      "Chrome: chrome://settings/certificates → \"Authorities\";\n" +
      "Firefox and Zen: Settings → Privacy & Security → Certificates →\n" +
      "\"View Certificates\" → \"Authorities\" → \"Import\".";
  }

  return (
    `The daemon is not responding at ${base}.\n\n` +
    "Start it:  python -m helper.app\n" +
    "Check manually:  " + manual + "\n\n" +
    protocolHint
  );
}

/** A health request over one protocol. A network refusal is an exception, like with fetch. */
async function healthAt(scheme, values) {
  const base = `${scheme}://${values.host}:${values.port}`;
  const response = await fetch(`${base}/v1/health`, { cache: "no-store" });

  return { base, response };
}

async function check() {
  const values = {
    scheme: fields.scheme.value === "https" ? "https" : "http",
    host: fields.host.value.trim() || DEFAULTS.host,
    port: Number(fields.port.value) || DEFAULTS.port,
  };

  await api.storage.local.set(values);

  const tried = values.scheme;
  let found;
  let switched = "";

  try {
    found = await healthAt(values.scheme, values);
  } catch (error) {
    // A daemon with a certificate listens only over https, and vice versa: the
    // chosen protocol is silent while the other one answers. This is a one-off
    // check on a button press, not a guess on the hot path; the working protocol
    // is remembered, and long polls then go over it.
    const other = values.scheme === "https" ? "http" : "https";

    try {
      found = await healthAt(other, values);
    } catch {
      show(false, explain(error, `${tried}://${values.host}:${values.port}`, tried));

      return;
    }

    values.scheme = other;
    fields.scheme.value = other;
    await api.storage.local.set({ scheme: other });
    switched = `The daemon answered over ${other}, not ${tried} — switched the setting.\n\n`;
  }

  const base = found.base;
  const response = found.response;
  let health;

  if (!response.ok) {
    show(false, `The daemon answered with code ${response.status} (${base}).`);

    return;
  }

  try {
    health = await response.json();
  } catch (error) {
    show(false, explain(error, base, values.scheme));

    return;
  }

  const gpu = health.gpu || {};
  const models = health.models || {};
  const asr = models.asr || {};
  const translator = models.translator || {};

  show(
    true,
    switched +
      "Connection works.\n\n" +
      `Recognition: ${asr.model} (${asr.compute_type}, ~${asr.vram_mib} MiB)\n` +
      `Translation: ${translator.model} (~${translator.vram_mib} MiB)\n` +
      `Source language: ${asr.language}\n` +
      `Second line: ${translator.study_language || "off"}\n` +
      `Free on the GPU: ${gpu.free_mib} MiB${gpu.busy ? " (busy with a user)" : ""}\n` +
      `Tracks in cache: ${(health.cache || {}).tracks ?? 0}\n\n` +
      "Models are set in the daemon config, not here: video memory usage\n" +
      "is inseparable from the model name, and it cannot be changed from the browser.",
  );
}

function syncPositionRow() {
  // Choosing the place is meaningless while the original itself is off.
  positionRow.hidden = !showOriginal.checked;
}

/**
 * Display settings are saved at once, not on a button press.
 *
 * The button checks the connection to the daemon — it has nothing to do with how
 * the text is drawn. The content script listens for storage changes and repaints
 * the current cue in place, so the action is visible on an open video instantly.
 */
async function saveDisplay() {
  syncPositionRow();

  await api.storage.local.set({
    showOriginal: showOriginal.checked,
    originalPosition: originalPosition.value,
  });
}

/**
 * Cue source. Unlike the display settings, this changes the daemon's work,
 * so the job for an open video starts over — otherwise the switch would look
 * broken: the finished result would stay. Both variants are cached under
 * different keys, so going back to the previous one is instant.
 */
async function saveSource() {
  await api.storage.local.set({ useAuthorTrack: useAuthorTrack.checked });
}

async function load() {
  const stored = await api.storage.local.get(DEFAULTS);

  fields.scheme.value = stored.scheme === "https" ? "https" : "http";
  fields.host.value = stored.host || DEFAULTS.host;
  fields.port.value = stored.port || DEFAULTS.port;
  useAuthorTrack.checked = stored.useAuthorTrack !== false;
  showOriginal.checked = Boolean(stored.showOriginal);
  originalPosition.value = stored.originalPosition || DEFAULTS.originalPosition;

  syncPositionRow();
}

document.querySelector("#check").addEventListener("click", check);
useAuthorTrack.addEventListener("change", saveSource);
showOriginal.addEventListener("change", saveDisplay);
originalPosition.addEventListener("change", saveDisplay);
load();
