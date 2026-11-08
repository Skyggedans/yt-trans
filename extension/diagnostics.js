/**
 * The text of the connection check, from the daemon's /v1/health answer.
 *
 * Pure on purpose: the settings page has no other place to learn what the daemon
 * is running, and the shapes of the answer differ. On the loopback of the machine
 * that runs it, health spells out models, video memory and cache. To any other
 * client it says only {"ok": true}: model names and the amount of video memory are
 * a map of someone else's machine. Printing the second answer as if it were the
 * first is what gave the page "Recognition: undefined (undefined, ~undefined MiB)".
 */

export function diagnostics(health, switched = "") {
  const models = health.models;

  if (!models) {
    return (
      switched +
      "Connection works.\n\n" +
      "The daemon answered only that it is alive: models, video memory and the cache\n" +
      "are shown to a client on the machine itself, not to the whole home network.\n\n" +
      "Models are set in the [asr] and [translator] sections of config.toml\n" +
      "on that machine."
    );
  }

  const asr = models.asr || {};
  const translator = models.translator || {};
  const gpu = health.gpu || {};
  const cache = health.cache || {};

  return (
    switched +
    "Connection works.\n\n" +
    `Recognition: ${asr.model} (${asr.compute_type}, ~${asr.vram_mib} MiB)\n` +
    `Translation: ${translator.model} (~${translator.vram_mib} MiB)\n` +
    `Source language: ${asr.language}\n` +
    `Second line: ${translator.study_language || "off"}\n` +
    `Free on the GPU: ${gpu.free_mib} MiB${gpu.busy ? " (busy with a user)" : ""}\n` +
    `Tracks in cache: ${cache.tracks ?? 0}\n\n` +
    "Models are set in the daemon config, not here: video memory usage\n" +
    "is inseparable from the model name, and it cannot be changed from the browser."
  );
}
