const IDS = ["card_match_cut", "directional_slide", "hard_cut", "light_flash", "soft_wipe"];
const TARGET = /^#[a-z][a-z0-9_]{0,95}$/u;

export const TRANSITION_CONTRACTS = Object.freeze(IDS.map((id) => Object.freeze({id, version: "1.0.0", finite: true})));

export function applyTransition({timeline, transition, outgoing, incoming, boundaryMs, sceneDurationMs, fps}) {
  if (!IDS.includes(transition)) throw new Error("transition_unknown");
  if (!TARGET.test(outgoing) || !TARGET.test(incoming)) throw new Error("transition_target_invalid");
  if (!Number.isInteger(boundaryMs) || !Number.isInteger(sceneDurationMs) || sceneDurationMs <= 0 || !Number.isFinite(fps) || fps <= 0) throw new Error("transition_bounds_invalid");
  const durationMs = transition === "hard_cut" ? Math.ceil(1000 / fps) : Math.min(420, Math.max(180, Math.floor(sceneDurationMs / 5)));
  const startMs = Math.max(0, Math.min(sceneDurationMs - durationMs, boundaryMs - Math.floor(durationMs / 2)));
  const audit = Object.freeze({transition, boundaryMs, startMs, endMs: startMs + durationMs, durationMs, identityRequired: transition === "card_match_cut"});
  applyCalls(timeline, audit, outgoing, incoming);
  return audit;
}

export function compileTransitionScript({transition, outgoing, incoming, startMs, durationMs}) {
  if (!IDS.includes(transition) || !TARGET.test(outgoing) || !TARGET.test(incoming)) throw new Error("transition_compile_invalid");
  if (transition === "hard_cut") return `tl.set(${JSON.stringify(incoming)},{autoAlpha:1},${startMs / 1000});`;
  const from = transition === "directional_slide" ? {xPercent: 8, autoAlpha: 0} : transition === "soft_wipe" ? {clipPath: "inset(0 100% 0 0)"} : {autoAlpha: 0};
  const to = transition === "soft_wipe" ? {clipPath: "inset(0 0% 0 0)"} : {xPercent: 0, autoAlpha: 1};
  return `tl.fromTo(${JSON.stringify(incoming)},${JSON.stringify(from)},${JSON.stringify({...to, duration: durationMs / 1000, ease: "power2.out"})},${startMs / 1000});`;
}

function applyCalls(timeline, audit, outgoing, incoming) {
  if (!timeline || typeof timeline.fromTo !== "function" || typeof timeline.set !== "function") throw new Error("transition_timeline_invalid");
  if (audit.transition === "hard_cut") timeline.set(incoming, {autoAlpha: 1}, audit.startMs / 1000);
  else if (audit.transition === "directional_slide") timeline.fromTo(incoming, {xPercent: 8, autoAlpha: 0}, {xPercent: 0, autoAlpha: 1, duration: audit.durationMs / 1000}, audit.startMs / 1000);
  else if (audit.transition === "soft_wipe") timeline.fromTo(incoming, {clipPath: "inset(0 100% 0 0)"}, {clipPath: "inset(0 0% 0 0)", duration: audit.durationMs / 1000}, audit.startMs / 1000);
  else timeline.fromTo(incoming, {autoAlpha: 0}, {autoAlpha: 1, duration: audit.durationMs / 1000}, audit.startMs / 1000);
  void outgoing;
}
