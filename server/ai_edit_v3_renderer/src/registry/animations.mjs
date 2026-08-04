const IDS = [
  "card_reveal", "count_up", "fade", "highlight_draw", "image_pan_zoom", "light_sweep", "rotate",
  "scale", "slide", "split_screen", "stagger", "stamp", "subtitle_pop", "wipe",
];
const TARGET = /^#[a-z][a-z0-9_]{0,95}$/u;
const OPERATION_VERSIONS = new Set(["1.0", "2.0"]);
const OPERATION_CONTEXTS = new WeakMap();

export const ANIMATION_CONTRACTS = Object.freeze(IDS.map((id) => Object.freeze({id, version: "1.0.0", finite: true})));

export function applyAnimation({timeline, preset, target, direction = "none", childTargets, operationVersion = "2.0", params = {}, sceneDurationMs, fps}) {
  if (!IDS.includes(preset)) throw new Error("animation_unknown");
  if (!TARGET.test(target)) throw new Error("animation_target_invalid");
  if (!timeline || typeof timeline.fromTo !== "function") throw new Error("animation_timeline_invalid");
  if (!Number.isInteger(sceneDurationMs) || sceneDurationMs <= 0 || !Number.isFinite(fps) || fps <= 0) throw new Error("animation_bounds_invalid");
  if (!OPERATION_VERSIONS.has(operationVersion)) throw new Error("animation_operation_version_invalid");
  const durationMs = clamp(integer(params.durationMs, 500), Math.ceil(1000 / fps), Math.min(1200, sceneDurationMs));
  const delayMs = clamp(integer(params.delayMs, 0), 0, Math.max(0, sceneDurationMs - durationMs));
  const operations = operationVersion === "2.0"
    ? representativeOperations({preset, target, direction, childTargets, params, durationMs, delayMs, sceneDurationMs})
    : null;
  if (operations) OPERATION_CONTEXTS.set(operations, Object.freeze({
    operationVersion, preset, target, allowedTargets: Object.freeze([target, ...(childTargets ?? [])]),
    durationMs, delayMs, sceneDurationMs,
  }));
  const audit = Object.freeze({
    operationVersion, preset, startMs: delayMs, endMs: delayMs + durationMs, durationMs, delayMs, fps,
    ...(operations ? {operations} : {}),
  });
  if (operations) {
    validateAnimationOperations(operations, {allowedTargets: new Set([target, ...(childTargets ?? [])]), sceneDurationMs, requireTrusted: true});
    applyNormalizedOperations(timeline, operations);
    return audit;
  }
  const [from, to] = valuesFor(preset);
  timeline.fromTo(target, from, {...to, duration: durationMs / 1000, ease: easingFor(preset)}, delayMs / 1000);
  return audit;
}

export function compileAnimationScript({preset, target, startMs, durationMs, delayMs = startMs, operationVersion = "1.0", windowStartMs, compositionDurationMs, operations}) {
  if (!IDS.includes(preset) || !TARGET.test(target)) throw new Error("animation_compile_invalid");
  if (!OPERATION_VERSIONS.has(operationVersion)) throw new Error("animation_operation_version_invalid");
  if (operationVersion === "1.0" && (windowStartMs !== undefined || compositionDurationMs !== undefined)) throw new Error("animation_operation_context_invalid");
  if (operationVersion === "2.0" && !operations) throw new Error("animation_operations_required");
  if (operations) {
    validateAnimationOperations(operations, {
      requireTrusted: true,
      compileContext: {preset, target, startMs, durationMs, delayMs, operationVersion, windowStartMs, compositionDurationMs},
    });
    const offsetMs = startMs - delayMs;
    return operations.map((operation) => compileNormalizedOperation(operation, offsetMs)).join("");
  }
  const [from, to] = valuesFor(preset);
  return `tl.fromTo(${JSON.stringify(target)},${JSON.stringify(from)},${JSON.stringify({...to, duration: durationMs / 1000, ease: easingFor(preset)})},${startMs / 1000});`;
}

function representativeOperations({preset, target, direction, childTargets, params, durationMs, delayMs, sceneDurationMs}) {
  if (preset === "fade") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {opacity: 0}, to: {opacity: 1}})]);
  if (preset === "slide") {
    const from = directionTransform(direction);
    return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {...from, opacity: 0}, to: {x: 0, y: 0, opacity: 1}})]);
  }
  if (preset === "count_up") {
    const fromValue = finiteNumber(params.numericStart, 0);
    const toValue = finiteNumber(params.numericEnd, 1);
    const precision = integer(params.numericPrecision, Math.max(decimalPlaces(fromValue), decimalPlaces(toValue)));
    const fromNumber = exactDecimal(params.numericStartToken ?? String(fromValue), precision);
    const toNumber = exactDecimal(params.numericEndToken ?? String(toValue), precision);
    const prefix = numericAffix(params.numericPrefix);
    const suffix = numericAffix(params.numericSuffix);
    if (precision < 0 || precision > 6) throw new Error("animation_numeric_format_invalid");
    return freezeOperations([{
      kind: "numeric_proxy", target, start_ms: delayMs, duration_ms: durationMs,
      from_value: fromNumber.value, to_value: toNumber.value,
      from_token: fromNumber.token, to_token: toNumber.token,
      from_scaled: fromNumber.scaled, to_scaled: toNumber.scaled, scale: 10 ** precision,
      precision, prefix, suffix,
      update_binding: "text_content", ease: easingFor(preset),
    }]);
  }
  if (preset === "stagger") {
    const targets = childTargets === undefined ? [target] : childTargets;
    if (!Array.isArray(targets) || targets.length === 0 || targets.some((item) => !TARGET.test(item)) || new Set(targets).size !== targets.length) {
      throw new Error("animation_child_targets_invalid");
    }
    const stepMs = targets.length === 1 ? 0 : Math.max(1, Math.min(90, Math.floor(durationMs / (targets.length * 4))));
    return freezeOperations(targets.map((item, index) => {
      const start = delayMs + (index * stepMs);
      const boundedDuration = Math.max(1, Math.min(durationMs - (index * stepMs), sceneDurationMs - start));
      return fromToOperation({target: item, startMs: start, durationMs: boundedDuration, from: {y: 14, opacity: 0}, to: {y: 0, opacity: 1}});
    }));
  }
  if (preset === "scale") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {scale: .82, opacity: 0}, to: {scale: 1, opacity: 1}})]);
  if (preset === "rotate") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {rotation: -10, opacity: 0}, to: {rotation: 0, opacity: 1}})]);
  if (preset === "wipe") {
    const [from, to] = wipeClip(direction);
    return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {clipPath: from}, to: {clipPath: to}})]);
  }
  if (preset === "image_pan_zoom") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {scale: 1.02, xPercent: -2}, to: {scale: 1.1, xPercent: 2}})]);
  if (preset === "card_reveal") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {y: 28, scale: .96, opacity: 0}, to: {y: 0, scale: 1, opacity: 1}})]);
  if (preset === "stamp") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {rotation: -8, scale: 1.24, opacity: 0}, to: {rotation: 0, scale: 1, opacity: 1}})]);
  if (preset === "light_sweep") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {xPercent: -120, opacity: 0}, to: {xPercent: 120, opacity: 1}})]);
  if (preset === "highlight_draw") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {scaleX: 0, opacity: .35}, to: {scaleX: 1, opacity: 1}})]);
  if (preset === "split_screen") {
    const offset = splitOffset(direction);
    return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {xPercent: offset, opacity: 0}, to: {xPercent: 0, opacity: 1}})]);
  }
  if (preset === "subtitle_pop") return freezeOperations([fromToOperation({target, startMs: delayMs, durationMs, from: {y: 22, scale: .92, opacity: 0}, to: {y: 0, scale: 1, opacity: 1}})]);
  return null;
}

function wipeClip(direction) {
  switch (direction) {
    case "right": case "out": return ["inset(0 0 0 100%)", "inset(0 0 0 0)"];
    case "up": return ["inset(0 0 100% 0)", "inset(0 0 0 0)"];
    case "down": return ["inset(100% 0 0 0)", "inset(0 0 0 0)"];
    case "left": case "in": case "none": return ["inset(0 100% 0 0)", "inset(0 0 0 0)"];
    default: throw new Error("animation_direction_invalid");
  }
}

function splitOffset(direction) {
  switch (direction) {
    case "right": case "out": return 50;
    case "up": return -35;
    case "down": return 35;
    case "left": case "in": case "none": return -50;
    default: throw new Error("animation_direction_invalid");
  }
}

function fromToOperation({target, startMs, durationMs, from, to}) {
  return {kind: "from_to", target, start_ms: startMs, duration_ms: durationMs, from, to, ease: "power2.out"};
}

function directionTransform(direction) {
  switch (direction) {
    case "up": return {x: 0, y: -36};
    case "down": return {x: 0, y: 36};
    case "out": case "right": return {x: 36, y: 0};
    case "left": case "in": case "none": return {x: -36, y: 0};
    default: throw new Error("animation_direction_invalid");
  }
}

function freezeOperations(operations) {
  const frozen = Object.freeze(operations.map((operation) => Object.freeze({
    ...operation,
    ...(operation.from ? {from: Object.freeze({...operation.from})} : {}),
    ...(operation.to ? {to: Object.freeze({...operation.to})} : {}),
  })));
  return frozen;
}

function applyNormalizedOperations(timeline, operations) {
  for (const operation of operations) {
    if (operation.kind === "from_to") {
      timeline.fromTo(operation.target, operation.from, {...operation.to, duration: operation.duration_ms / 1000, ease: operation.ease}, operation.start_ms / 1000);
    } else if (operation.kind === "numeric_proxy") {
      const proxy = {value: operation.from_scaled};
      timeline.fromTo(proxy, {value: operation.from_scaled}, {value: operation.to_scaled, duration: operation.duration_ms / 1000, ease: operation.ease}, operation.start_ms / 1000);
    }
  }
}

function compileNormalizedOperation(operation, offsetMs) {
  const at = (operation.start_ms + offsetMs) / 1000;
  if (operation.kind === "from_to") {
    return `tl.fromTo(${JSON.stringify(operation.target)},${JSON.stringify(operation.from)},${JSON.stringify({...operation.to, duration: operation.duration_ms / 1000, ease: operation.ease})},${at});`;
  }
  if (operation.kind === "numeric_proxy") {
    const selector = JSON.stringify(operation.target);
    const formatted = operation.precision === 0 ? "String(Math.round(proxy.value))" : `(Math.round(proxy.value)/${operation.scale}).toFixed(${operation.precision})`;
    return `(()=>{const node=document.querySelector(${selector});if(!node)return;const sink=node.querySelector("span")??node;const proxy={value:${operation.from_scaled}};tl.fromTo(proxy,{value:${operation.from_scaled}},{value:${operation.to_scaled},duration:${operation.duration_ms / 1000},ease:${JSON.stringify(operation.ease)},onUpdate:()=>{sink.textContent=${JSON.stringify(operation.prefix)}+${formatted}+${JSON.stringify(operation.suffix)};}},${at});})();`;
  }
  throw new Error("animation_operation_invalid");
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
function finiteNumber(value, fallback) { return Number.isFinite(value) ? value : fallback; }
function decimalPlaces(value) {
  const text = String(value).toLowerCase();
  if (text.includes("e")) {
    const [coefficient, exponentText] = text.split("e");
    return Math.max(0, (coefficient.split(".")[1]?.length ?? 0) - Number(exponentText));
  }
  return text.split(".")[1]?.length ?? 0;
}
function numericAffix(value) {
  if (value === undefined) return "";
  if (typeof value !== "string" || value.length > 16 || /[\u0000-\u001f\u007f]/u.test(value)) throw new Error("animation_numeric_format_invalid");
  return value;
}
function exactDecimal(token, precision) {
  if (!Number.isInteger(precision) || precision < 0 || precision > 6 || typeof token !== "string" || !/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/u.test(token) || token === "-0") throw new Error("animation_numeric_format_invalid");
  const [whole, fraction = ""] = token.split(".");
  if (fraction.length > precision) throw new Error("animation_numeric_format_invalid");
  const negative = whole.startsWith("-");
  const digits = `${negative ? whole.slice(1) : whole}${fraction.padEnd(precision, "0")}`;
  const scaled = Number(`${negative ? "-" : ""}${digits}`);
  if (!Number.isSafeInteger(scaled)) throw new Error("animation_numeric_format_invalid");
  const canonical = precision === 0 ? String(scaled) : `${negative ? "-" : ""}${Math.floor(Math.abs(scaled) / (10 ** precision))}.${String(Math.abs(scaled) % (10 ** precision)).padStart(precision, "0")}`;
  return Object.freeze({token: canonical, scaled, value: scaled / (10 ** precision)});
}
function validateAnimationOperations(operations, {allowedTargets, sceneDurationMs, requireTrusted = false, compileContext} = {}) {
  const trustedContext = OPERATION_CONTEXTS.get(operations);
  if (!Array.isArray(operations) || operations.length === 0 || operations.length > 32 || (requireTrusted && !trustedContext)) throw new Error("animation_operation_invalid");
  if (compileContext && !matchesAnimationCompileContext(trustedContext, compileContext)) throw new Error("animation_operation_context_invalid");
  for (const operation of operations) {
    if (!plainRecord(operation) || !TARGET.test(operation.target) || !Number.isInteger(operation.start_ms) || operation.start_ms < 0 || !Number.isInteger(operation.duration_ms) || operation.duration_ms <= 0 || (sceneDurationMs !== undefined && operation.start_ms + operation.duration_ms > sceneDurationMs) || (allowedTargets && !allowedTargets.has(operation.target))) throw new Error("animation_operation_invalid");
    if (operation.kind === "from_to") {
      exactKeys(operation, ["kind", "target", "start_ms", "duration_ms", "from", "to", "ease"], "animation_operation_invalid");
      validateStyle(operation.from); validateStyle(operation.to);
      if (operation.ease !== "power2.out") throw new Error("animation_operation_invalid");
    } else if (operation.kind === "numeric_proxy") {
      exactKeys(operation, ["kind", "target", "start_ms", "duration_ms", "from_value", "to_value", "from_token", "to_token", "from_scaled", "to_scaled", "scale", "precision", "prefix", "suffix", "update_binding", "ease"], "animation_operation_invalid");
      if (![operation.from_value, operation.to_value].every(Number.isFinite) || ![operation.from_scaled, operation.to_scaled, operation.scale].every(Number.isSafeInteger) || operation.scale !== 10 ** operation.precision || !Number.isInteger(operation.precision) || operation.precision < 0 || operation.precision > 6 || operation.update_binding !== "text_content" || operation.ease !== "power2.out") throw new Error("animation_operation_invalid");
      const from = exactDecimal(operation.from_token, operation.precision); const to = exactDecimal(operation.to_token, operation.precision);
      if (from.scaled !== operation.from_scaled || to.scaled !== operation.to_scaled || from.value !== operation.from_value || to.value !== operation.to_value) throw new Error("animation_operation_invalid");
      numericAffix(operation.prefix); numericAffix(operation.suffix);
    } else throw new Error("animation_operation_invalid");
  }
}
function matchesAnimationCompileContext(context, value) {
  if (!context || !plainRecord(value) || value.operationVersion !== context.operationVersion || value.preset !== context.preset || value.target !== context.target || value.durationMs !== context.durationMs || value.delayMs !== context.delayMs) return false;
  if (!Number.isInteger(value.windowStartMs) || value.windowStartMs < 0 || !Number.isInteger(value.compositionDurationMs) || value.compositionDurationMs <= 0) return false;
  if (value.startMs !== value.windowStartMs + context.delayMs || value.windowStartMs + context.sceneDurationMs > value.compositionDurationMs) return false;
  return true;
}
function validateStyle(style) {
  const numeric = new Set(["opacity", "x", "y", "scale", "scaleX", "rotation", "xPercent"]);
  const allowed = new Set([...numeric, "clipPath"]);
  if (!plainRecord(style) || Object.keys(style).some((key) => !allowed.has(key))) throw new Error("animation_operation_invalid");
  for (const [key, value] of Object.entries(style)) {
    if (numeric.has(key) && !Number.isFinite(value)) throw new Error("animation_operation_invalid");
    if (key === "clipPath" && (typeof value !== "string" || !/^inset\((?:0|0%|100%) (?:0|0%|100%) (?:0|0%|100%) (?:0|0%|100%)\)$/u.test(value))) throw new Error("animation_operation_invalid");
  }
}
function exactKeys(value, keys, code) {
  const actual = Object.keys(value).sort(); const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) throw new Error(code);
}
function plainRecord(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function clamp(value, minimum, maximum) { return Math.max(minimum, Math.min(maximum, value)); }
