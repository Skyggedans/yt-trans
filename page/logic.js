// Чистая логика страницы: что показать в данную секунду. Ни document, ни fetch —
// это гоняет node --test, а app.js только зовёт.

/** Индекс реплики, где start <= t < end; -1 — сейчас молчание. */
export function activeCueIndex(cues, t) {
  for (let i = 0; i < cues.length; i += 1) {
    if (cues[i].start <= t && t < cues[i].end) {
      return i;
    }
  }

  return -1;
}

/** Попадает ли t в непереведённый интервал [start, end). */
export function pendingAt(pending, t) {
  for (const [start, end] of pending) {
    if (start <= t && t < end) {
      return true;
    }
  }

  return false;
}

const STAGE_WORDS = {
  probing: () => "читаю ролик…",
  fetching: () => "качаю звук…",
  asr: () => "распознаю речь…",
  paused: () => "пауза: не хватило видеопамяти",
};

/** Надпись о ходе работы; на done — пустая, экран не должен ничего говорить. */
export function stageText(stage, state) {
  const data = state || {};

  if (stage === "done") {
    return "";
  }

  if (stage === "error") {
    const text = data.error || "ошибка";

    return data.retryable ? `${text} — можно повторить` : text;
  }

  if (stage === "queued") {
    return data.waiting === "gpu"
      ? "ждём видеокарту: её занимает другая работа"
      : "в очереди…";
  }

  if (stage === "translating") {
    if (typeof data.translated === "number" && typeof data.total === "number") {
      return `перевожу… ${data.translated} из ${data.total}`;
    }

    return "перевожу…";
  }

  const words = STAGE_WORDS[stage];

  return words ? words() : "";
}

/** Строка о языке речи: решение и приписка демона; пустая, если решения нет. */
export function languageText(language) {
  if (!language || !language.effective) {
    return "";
  }

  const parts = [`язык речи: ${language.effective}`];

  if (language.reason === "track" && language.track) {
    parts.push(`дорожка автора ${language.track}`);
  } else if (language.reason === "pin") {
    parts.push("закреплено в настройках");
  } else if (language.reason === "detector" || language.reason === "floor") {
    parts.push(
      typeof language.confidence === "number"
        ? `по речи, ${Math.round(language.confidence * 100)}%`
        : "по речи",
    );
  }

  if (language.note) {
    parts.push(language.note);
  }

  return parts.join("; ");
}

/** Две строки реплики. Вторая гаснет, когда её нечем показать или она повторяет первую. */
export function cueLines(cue) {
  const target = (cue.lines || []).join("\n");
  const study = cue.study || "";
  const secondary = target && study && study !== target ? study : "";

  return { target, secondary, failed: Boolean(cue.failed) };
}

/** Время как в плеере: часы появляются, только когда они есть. */
export function formatTime(seconds) {
  const whole = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = String(whole % 60).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${rest}`;
  }

  return `${minutes}:${rest}`;
}

/** Событие о другом ролике — не наше: сокет подписан на всё, что смотрит страница. */
export function isForVideo(message, videoId) {
  return (
    message.videoId === undefined
    || message.videoId === null
    || message.videoId === videoId
  );
}

/** Отписка от ролика: без неё старый эпизод продолжает слать сюда свои реплики. */
export function unwatchMessage(videoId) {
  return { op: "unwatch", videoId };
}

/** Что сказать сокету при подписке: отписка от прежнего, подписка, место просмотра. */
export function watchMessages(videoId, t, clientId, previous = null) {
  const messages = [];

  if (previous && previous !== videoId) {
    messages.push(unwatchMessage(previous));
  }

  messages.push({ op: "watch", videoId, targetLang: "ru", authorTrack: true });
  messages.push({
    op: "playhead",
    videoId,
    t: typeof t === "number" && Number.isFinite(t) && t > 0 ? t : 0,
    clientId,
  });

  return messages;
}

/** Карта мест просмотра из строки localStorage; битое и чужие поля — прочь. */
export function parsePositions(raw) {
  let data;

  try {
    data = JSON.parse(raw || "{}");
  } catch {
    return {};
  }

  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return {};
  }

  const positions = {};

  for (const [key, value] of Object.entries(data)) {
    if (typeof value === "number" && Number.isFinite(value) && value > 0) {
      positions[key] = value;
    }
  }

  return positions;
}

/** Где остановились: 0, если ролик ещё не смотрели. */
export function positionFor(positions, videoId) {
  const value = positions ? positions[videoId] : undefined;

  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

/** Запомнить место. Ноль и нечисло местом не считаются и запись стирают. */
export function rememberPosition(positions, videoId, seconds) {
  const next = { ...(positions || {}) };

  if (typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0) {
    next[videoId] = Math.floor(seconds);
  } else {
    delete next[videoId];
  }

  return next;
}
