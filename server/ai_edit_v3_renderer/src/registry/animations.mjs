const IDS = [
  "card_reveal", "count_up", "fade", "highlight_draw", "image_pan_zoom", "light_sweep", "rotate",
  "scale", "slide", "split_screen", "stagger", "stamp", "subtitle_pop", "wipe",
];
const TARGET = /^#[a-z][a-z0-9_]{0,95}$/u;

export const ANIMATION_CONTRACTS = Object.freeze(IDS.map((id) => Object.freeze({id, version: "1.0.0", finite: true})));

export function applyAnimation({timeline, preset, target, params = {}, sceneDurationMs, fps}) {
  if (!IDS.includes(preset)) throw new Error("animation_unknown");
  if (!TARGET.test(target)) throw new Error("animation_target_invalid");
  if (!timeline || typeof timeline.fromTo !== "function") throw new Error("animation_timeline_invalid");
  if (!Number.isInteger(sceneDurationMs) || sceneDurationMs <= 0 || !Number.isFinite(fps) || fps <= 0) throw new Error("animation_bounds_invalid");
  const durationMs = clamp(integer(params.durationMs, 500), Math.ceil(1000 / fps), Math.min(1200, sceneDurationMs));
  const delayMs = clamp(integer(params.delayMs, 0), 0, Math.max(0, sceneDurationMs - durationMs));
  const audit = Object.freeze({preset, startMs: delayMs, endMs: delayMs + durationMs, durationMs, delayMs, fps});
  const [from, to] = valuesFor(preset);
  timeline.fromTo(target, from, {...to, duration: durationMs / 1000, ease: easingFor(preset)}, delayMs / 1000);
  return audit;
}

export function compileAnimationScript({preset, target, startMs, durationMs}) {
  if (!IDS.includes(preset) || !TARGET.test(target)) throw new Error("animation_compile_invalid");
  const [from, to] = valuesFor(preset);
  return `tl.fromTo(${JSON.stringify(target)},${JSON.stringify(from)},${JSON.stringify({...to, duration: durationMs / 1000, ease: easingFor(preset)})},${startMs / 1000});`;
}

function valuesFor(preset) {
  switch (preset) {
    case "slide": return [{x: -36, autoAlpha: 0}, {x: 0, autoAlpha: 1}];
    case "scale": case "card_reveal": return [{scale: .88, autoAlpha: 0}, {scale: 1, autoAlpha: 1}];
    case "rotate": case "stamp": return [{rotation: -6, scale: .9, autoAlpha: 0}, {rotation: 0, scale: 1, autoAlpha: 1}];
    case "wipe": case "highlight_draw": return [{clipPath: "inset(0 100% 0 0)"}, {clipPath: "inset(0 0% 0 0)"}];
    case "image_pan_zoom": return [{scale: 1.02, xPercent: -1}, {scale: 1.09, xPercent: 1}];
    case "light_sweep": return [{filter: "brightness(1)"}, {filter: "brightness(1.18)"}];
    case "split_screen": return [{xPercent: 10, autoAlpha: 0}, {xPercent: 0, autoAlpha: 1}];
    case "subtitle_pop": return [{y: 18, scale: .94, autoAlpha: 0}, {y: 0, scale: 1, autoAlpha: 1}];
    case "count_up": case "stagger": return [{y: 14, autoAlpha: 0}, {y: 0, autoAlpha: 1}];
    default: return [{autoAlpha: 0}, {autoAlpha: 1}];
  }
}

function easingFor(preset) {
  return ["stamp", "subtitle_pop"].includes(preset) ? "back.out(1.4)" : "power2.out";
}

function integer(value, fallback) { return Number.isInteger(value) ? value : fallback; }
function clamp(value, minimum, maximum) { return Math.max(minimum, Math.min(maximum, value)); }
