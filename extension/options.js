/**
 * Настройки и диагностика связи с демоном.
 *
 * Диагностика здесь не украшение. Событие `error` у WebSocket намеренно лишено
 * подробностей, поэтому из background нельзя отличить «демон не запущен» от
 * «браузер заблокировал обращение к локальной сети». Обычный fetch, наоборот,
 * даёт текст ошибки, и только по нему эти случаи и различаются.
 */

const api = globalThis.browser ?? globalThis.chrome;

const DEFAULTS = { host: "127.0.0.1", port: 8756, token: "", targetLang: "ru" };

const fields = {
  token: document.querySelector("#token"),
  host: document.querySelector("#host"),
  port: document.querySelector("#port"),
};

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
      "Открой chrome://extensions → yt-trans → Подробности → Настройки сайта →\n" +
      "Доступ к локальной сети → Разрешить.\n\n" +
      "В Firefox то же самое даёт режим «Только HTTPS»: он молча повышает\n" +
      `${base} до https и связь не устанавливается. Отключи его или добавь исключение.`
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

  show(
    true,
    "Связь есть.\n\n" +
      `Распознавание: ${health.asr_model}\n` +
      `Перевод: ${health.translator_model}\n` +
      `Свободно на карте: ${gpu.free_mib} МиБ${gpu.busy ? " (занята пользователем)" : ""}\n` +
      `В кэше дорожек: ${(health.cache || {}).tracks ?? 0}`,
  );
}

async function load() {
  const stored = await api.storage.local.get(DEFAULTS);

  fields.token.value = stored.token || "";
  fields.host.value = stored.host || DEFAULTS.host;
  fields.port.value = stored.port || DEFAULTS.port;
}

document.querySelector("#check").addEventListener("click", check);
load();
