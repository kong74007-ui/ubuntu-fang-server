const CONTROL_CHARACTERS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/u;
const SAFE_ID = /^[a-z][a-z0-9_]{0,95}$/u;

export function assertSafeText(value, {maxChars = 480, maxLines = 6} = {}) {
  if (typeof value !== "string") throw new Error("text_type_invalid");
  if (CONTROL_CHARACTERS.test(value)) throw new Error("text_control_forbidden");
  const normalized = value.replace(/\r\n?/gu, "\n").normalize("NFC");
  if ([...normalized].length > maxChars) throw new Error("text_length_exceeded");
  if (normalized.split("\n").length > maxLines) throw new Error("text_lines_exceeded");
  return normalized;
}

export function escapeAttribute(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

export function assertSafeId(value, field = "id") {
  if (typeof value !== "string" || !SAFE_ID.test(value)) throw new Error(`${field}_invalid`);
  return value;
}

export function seconds(milliseconds) {
  if (!Number.isInteger(milliseconds) || milliseconds < 0) throw new Error("time_invalid");
  return (milliseconds / 1000).toFixed(3).replace(/\.0+$/u, "").replace(/(\.\d*?)0+$/u, "$1");
}

export function mediaSlot({idPrefix, durationMs, kind = "placeholder"}) {
  const prefix = assertSafeId(idPrefix, "id_prefix");
  const duration = seconds(durationMs);
  return `<div id="${prefix}_media" class="hf-media clip hf-media-${kind}" data-start="0" data-duration="${duration}" data-track-index="0" aria-hidden="true"></div>`;
}

export function safeArea({idPrefix, durationMs, children = ""}) {
  const prefix = assertSafeId(idPrefix, "id_prefix");
  return `<div id="${prefix}_safe" class="hf-safe-area clip" data-start="0" data-duration="${seconds(durationMs)}" data-track-index="1">${children}</div>`;
}

export function compilePrimitiveLayout({idPrefix, durationMs, hasVideo = false, overlays = ""}) {
  const prefix = assertSafeId(idPrefix, "id_prefix");
  const duration = seconds(durationMs);
  return [
    `<div id="${prefix}_background" class="hf-background clip" data-start="0" data-duration="${duration}" data-track-index="0"></div>`,
    mediaSlot({idPrefix: prefix, durationMs, kind: hasVideo ? "video" : "placeholder"}),
    safeArea({idPrefix: prefix, durationMs, children: overlays}),
  ].join("");
}
