/**
 * Настройки и диагностика связи с демоном.
 *
 * Проверяется ровно тот транспорт, которым идёт работа, — обычный HTTP.
 * WebSocket отсюда убран не для простоты: браузер повышает ws:// до wss://
 * при обращении из контекста расширения, поэтому проверять его смысла нет,
 * а рабочий поток событий везут длинные опросы поверх того же http://,
 * который эта диагностика и щупает.
 */

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = {
  host: "127.0.0.1",
  port: 8756,
  token: "",
  targetLang: "ru",
  showOriginal: false,
  originalPosition: "below",
  useAuthorTrack: true,
};

const fields = {
  token: document.querySelector("#token"),
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
function explain(error, base) {
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

  return (
    `Демон не отвечает по адресу ${base}.\n\n` +
    "Запусти его:  python -m helper.app\n" +
    "Проверь вручную:  curl " + base + "/v1/health"
  );
}

async function check() {
  const values = {
    token: fields.token.value.trim(),
    host: fields.host.value.trim() || DEFAULTS.host,
    port: Number(fields.port.value) || DEFAULTS.port,
  };

  await api.storage.local.set(values);

  const base = `http://${values.host}:${values.port}`;
  let health;

  try {
    const response = await fetch(`${base}/v1/health`, { cache: "no-store" });

    if (!response.ok) {
      show(false, `Демон ответил кодом ${response.status}.`);

      return;
    }

    health = await response.json();
  } catch (error) {
    show(false, explain(error, base));

    return;
  }

  if (!values.token) {
    show(false, "Демон отвечает, но токен не заполнен.\nПоказать его: cat " + health.token_path);

    return;
  }

  // Токен проверяем отдельным запросом: /v1/health намеренно открыт, чтобы
  // расширение могло обнаружить демон до того, как настроен доступ.
  try {
    const probe = await fetch(`${base}/v1/jobs/00000000000`, {
      headers: { Authorization: `Bearer ${values.token}` },
    });

    if (probe.status === 401) {
      show(false, "Демон отвечает, но токен неверный.\nПоказать его: cat " + health.token_path);

      return;
    }
  } catch (error) {
    show(false, explain(error, base));

    return;
  }

  const gpu = health.gpu || {};
  const models = health.models || {};
  const asr = models.asr || {};
  const translator = models.translator || {};

  show(
    true,
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

  fields.token.value = stored.token || "";
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
