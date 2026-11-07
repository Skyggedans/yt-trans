/**
 * Настройки и диагностика связи с демоном.
 *
 * Проверяется ровно тот транспорт, которым идёт работа, — обычный HTTP или
 * HTTPS. WebSocket отсюда убран не для простоты: браузер повышает ws:// до
 * wss:// при обращении из контекста расширения, поэтому проверять его смысла
 * нет, а рабочий поток событий везут длинные опросы поверх того же протокола,
 * который эта диагностика и щупает.
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

/** Распознать причину отказа по тексту ошибки fetch. */
function explain(error, base, scheme) {
  const text = String(error && error.message ? error.message : error).toLowerCase();

  if (text.includes("local network") || text.includes("address space") || text.includes("loopback")) {
    return (
      "Браузер заблокировал обращение к локальной сети.\n\n" +
      "Chrome: chrome://extensions → yt-trans → Подробности →\n" +
      "Настройки сайта → Доступ к локальной сети → Разрешить.\n\n" +
      "Firefox и форки: ограничение включено по умолчанию с версии 151,\n" +
      `настраивается в разрешениях для ${base}.`
    );
  }

  const other = scheme === "https" ? "http" : "https";

  // curl не читает хранилище сертификатов браузера, поэтому свой CA ему
  // указывается файлом — иначе ручная проверка падала бы на недоверии.
  const manual =
    scheme === "https"
      ? `curl --cacert ~/.config/yt-trans/tls/ca.crt ${base}/v1/health`
      : `curl ${base}/v1/health`;

  let protocolHint =
    `Демон с сертификатом ([server] tls_cert в config.toml) слушает только https,\n` +
    `без сертификата — только http. Сейчас выбрано ${scheme}; возможно, нужно ${other}.`;

  if (scheme === "https") {
    protocolHint +=
      "\n\nСертификату демона должен доверять браузер: ca.crt из\n" +
      "~/.config/yt-trans/tls/ ставится в хранилище сертификатов браузера —\n" +
      "Chrome: chrome://settings/certificates → «Центры сертификации»;\n" +
      "Firefox и Zen: Настройки → Приватность и защита → Сертификаты →\n" +
      "«Просмотр сертификатов» → «Центры сертификации» → «Импорт».";
  }

  return (
    `Демон не отвечает по адресу ${base}.\n\n` +
    "Запусти его:  python -m helper.app\n" +
    "Проверь вручную:  " + manual + "\n\n" +
    protocolHint
  );
}

/** Запрос здоровья по одному протоколу. Отказ сети — исключение, как у fetch. */
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
    // Демон с сертификатом слушает только https, и наоборот: выбранный протокол
    // молчит, а второй отвечает. Это единичная проверка по нажатию кнопки, а не
    // догадка на горячем пути; рабочий протокол запоминается, и длинные опросы
    // дальше идут уже им.
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
    switched = `Демон ответил по ${other}, а не по ${tried} — переключил настройку.\n\n`;
  }

  const base = found.base;
  const response = found.response;
  let health;

  if (!response.ok) {
    show(false, `Демон ответил кодом ${response.status} (${base}).`);

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
      "Связь есть.\n\n" +
      `Распознавание: ${asr.model} (${asr.compute_type}, ~${asr.vram_mib} МиБ)\n` +
      `Перевод: ${translator.model} (~${translator.vram_mib} МиБ)\n` +
      `Язык источника: ${asr.language}\n` +
      `Вторая строка: ${translator.study_language || "выключена"}\n` +
      `Свободно на карте: ${gpu.free_mib} МиБ${gpu.busy ? " (занята пользователем)" : ""}\n` +
      `В кэше дорожек: ${(health.cache || {}).tracks ?? 0}\n\n` +
      "Модели задаются в конфиге демона, не здесь: расход видеопамяти\n" +
      "неотделим от имени модели, и менять его из браузера нельзя.",
  );
}

function syncPositionRow() {
  // Выбор места бессмыслен, пока сам оригинал выключен.
  positionRow.hidden = !showOriginal.checked;
}

/**
 * Настройки показа сохраняются сразу, а не по кнопке.
 *
 * Кнопка проверяет связь с демоном — к тому, как рисуется текст, она отношения
 * не имеет. Content script слушает изменения хранилища и перерисовывает текущую
 * реплику на месте, так что действие видно на открытом ролике мгновенно.
 */
async function saveDisplay() {
  syncPositionRow();

  await api.storage.local.set({
    showOriginal: showOriginal.checked,
    originalPosition: originalPosition.value,
  });
}

/**
 * Источник реплик. В отличие от настроек показа, это меняет работу демона,
 * поэтому задание для открытого ролика начинается заново — иначе переключатель
 * выглядел бы сломанным: готовый результат никуда бы не делся. Оба варианта
 * лежат в кэше под разными ключами, так что возврат к прежнему мгновенный.
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
