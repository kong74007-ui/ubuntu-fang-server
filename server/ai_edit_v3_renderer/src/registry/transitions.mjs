const IDS = ["card_match_cut", "directional_slide", "hard_cut", "light_flash", "soft_wipe"];
const TARGET = /^#[a-z][a-z0-9_]{0,95}$/u;
const OPERATION_VERSIONS = new Set(["1.0", "2.0"]);
const OPERATION_CONTEXTS = new WeakMap();

export const TRANSITION_CONTRACTS = Object.freeze(IDS.map((id) => Object.freeze({
  id,
  version: "1.0.0",
  finite: true,
  identityRequired: id === "card_match_cut",
})));

export function applyTransition(options) {
  if (!plainRecord(options) || Object.prototype.hasOwnProperty.call(options, "identity")) throw new Error("transition_identity_unproven");
  return applyTransitionInternal(options);
}

function applyTransitionInternal({timeline, transition, outgoing, incoming, flashTarget, operationVersion = "2.0", boundaryMs, sceneDurationMs, fps}) {
  if (!IDS.includes(transition)) throw new Error("transition_unknown");
  if (!TARGET.test(outgoing) || !TARGET.test(incoming)) throw new Error("transition_target_invalid");
  if (!Number.isInteger(boundaryMs) || !Number.isInteger(sceneDurationMs) || sceneDurationMs <= 0 || !Number.isFinite(fps) || fps <= 0) throw new Error("transition_bounds_invalid");
  if (!OPERATION_VERSIONS.has(operationVersion)) throw new Error("transition_operation_version_invalid");
  if (operationVersion === "2.0" && transition === "light_flash" && !TARGET.test(flashTarget ?? "")) throw new Error("transition_flash_target_invalid");
  if (transition !== "light_flash" && flashTarget !== undefined) throw new Error("transition_flash_target_invalid");
  const durationMs = transition === "hard_cut" ? Math.ceil(1000 / fps)
    : operationVersion === "2.0" && transition === "light_flash" ? Math.min(240, Math.max(80, Math.floor(sceneDurationMs / 10)))
    : Math.min(420, Math.max(180, Math.floor(sceneDurationMs / 5)));
  const startMs = Math.max(0, Math.min(sceneDurationMs - durationMs, boundaryMs - Math.floor(durationMs / 2)));
  const internalCardFallback = operationVersion === "2.0" && transition === "card_match_cut";
  const effectiveTransition = internalCardFallback ? "soft_wipe" : transition;
  const fallbackReason = internalCardFallback ? "identity_missing" : null;
  const operations = operationVersion === "2.0"
    ? representativeOperations({transition, effectiveTransition, outgoing, incoming, flashTarget, startMs, durationMs})
    : null;
  if (operations) OPERATION_CONTEXTS.set(operations, Object.freeze({
    operationVersion, transition, effectiveTransition, outgoing, incoming, flashTarget, startMs, durationMs, sceneDurationMs,
  }));
  const audit = Object.freeze({
    operationVersion, transition, effectiveTransition, fallbackReason, boundaryMs, startMs, endMs: startMs + durationMs,
    durationMs, identityRequired: transition === "card_match_cut", ...(flashTarget ? {flashTarget} : {}), ...(operations ? {operations} : {}),
  });
  if (operations) validateTransitionOperations(operations, {allowedTargets: new Set([outgoing, incoming, ...(flashTarget ? [flashTarget] : [])]), sceneDurationMs, requireTrusted: true});
  applyCalls(timeline, audit, outgoing, incoming);
  return audit;
}

export function compileTransitionScript({transition, effectiveTransition, outgoing, incoming, flashTarget, startMs, durationMs, operationVersion = "1.0", operations}) {
  if (!IDS.includes(transition) || !TARGET.test(outgoing) || !TARGET.test(incoming)) throw new Error("transition_compile_invalid");
  if (!OPERATION_VERSIONS.has(operationVersion)) throw new Error("transition_operation_version_invalid");
  if (operationVersion === "2.0" && transition === "light_flash" && (!Number.isInteger(durationMs) || durationMs < 80 || durationMs > 240)) throw new Error("transition_duration_invalid");
  if (operationVersion === "2.0" && transition === "light_flash" && !TARGET.test(flashTarget ?? "")) throw new Error("transition_flash_target_invalid");
  if (operationVersion === "2.0" && ["hard_cut", "card_match_cut"].includes(transition) && !operations) throw new Error("transition_operations_required");
  if (!operations && effectiveTransition !== undefined && effectiveTransition !== transition) throw new Error("transition_effective_invalid");
  if (operations) {
    validateTransitionOperations(operations, {
      allowedTargets: new Set([outgoing, incoming, ...(flashTarget ? [flashTarget] : [])]), requireTrusted: true,
      compileContext: {operationVersion, transition, effectiveTransition, outgoing, incoming, flashTarget, startMs, durationMs},
    });
    return operations.map(compileOperation).join("");
  }
  transition = effectiveTransition ?? transition;
  if (transition === "hard_cut") return `tl.set(${JSON.stringify(incoming)},{autoAlpha:1},${startMs / 1000});`;
  const from = transition === "directional_slide" ? {xPercent: 8, autoAlpha: 0} : transition === "soft_wipe" ? {clipPath: "inset(0 100% 0 0)"} : {autoAlpha: 0};
  const to = transition === "soft_wipe" ? {clipPath: "inset(0 0% 0 0)"} : {xPercent: 0, autoAlpha: 1};
  return `tl.fromTo(${JSON.stringify(incoming)},${JSON.stringify(from)},${JSON.stringify({...to, duration: durationMs / 1000, ease: "power2.out"})},${startMs / 1000});`;
}

function applyCalls(timeline, audit, outgoing, incoming) {
  if (!timeline || typeof timeline.fromTo !== "function" || typeof timeline.set !== "function") throw new Error("transition_timeline_invalid");
  if (audit.operations) {
    for (const operation of audit.operations) {
      if (operation.kind === "set") timeline.set(operation.target, operation.to, operation.start_ms / 1000);
      else timeline.fromTo(operation.target, operation.from, {...operation.to, duration: operation.duration_ms / 1000, ease: operation.ease, ...(operation.immediate_render === false ? {immediateRender: false} : {})}, operation.start_ms / 1000);
    }
    return;
  }
  if (audit.transition === "hard_cut") timeline.set(incoming, {autoAlpha: 1}, audit.startMs / 1000);
  else if (audit.transition === "directional_slide") timeline.fromTo(incoming, {xPercent: 8, autoAlpha: 0}, {xPercent: 0, autoAlpha: 1, duration: audit.durationMs / 1000}, audit.startMs / 1000);
  else if (audit.transition === "soft_wipe") timeline.fromTo(incoming, {clipPath: "inset(0 100% 0 0)"}, {clipPath: "inset(0 0% 0 0)", duration: audit.durationMs / 1000}, audit.startMs / 1000);
  else timeline.fromTo(incoming, {autoAlpha: 0}, {autoAlpha: 1, duration: audit.durationMs / 1000}, audit.startMs / 1000);
  void outgoing;
}

function representativeOperations({transition, effectiveTransition, outgoing, incoming, flashTarget, startMs, durationMs}) {
  if (effectiveTransition === "hard_cut") {
    return freezeOperations([{kind: "set", target: incoming, start_ms: startMs, duration_ms: durationMs, from: {}, to: {opacity: 1}}]);
  }
  if (transition === "card_match_cut" && effectiveTransition === "soft_wipe") {
    return freezeOperations([
      {kind: "from_to", target: outgoing, start_ms: startMs, duration_ms: durationMs, from: {clipPath: "inset(0 0% 0 0)"}, to: {clipPath: "inset(0 0 0 100%)"}, ease: "power2.out"},
      {kind: "from_to", target: incoming, start_ms: startMs, duration_ms: durationMs, from: {clipPath: "inset(0 100% 0 0)"}, to: {clipPath: "inset(0 0% 0 0)"}, ease: "power2.out"},
    ]);
  }
  if (effectiveTransition === "soft_wipe") return freezeOperations([
    {kind: "from_to", target: outgoing, start_ms: startMs, duration_ms: durationMs, from: {clipPath: "inset(0 0% 0 0)"}, to: {clipPath: "inset(0 0 0 100%)"}, ease: "power2.out"},
    {kind: "from_to", target: incoming, start_ms: startMs, duration_ms: durationMs, from: {clipPath: "inset(0 100% 0 0)"}, to: {clipPath: "inset(0 0% 0 0)"}, ease: "power2.out"},
  ]);
  if (effectiveTransition === "directional_slide") return freezeOperations([
    {kind: "from_to", target: outgoing, start_ms: startMs, duration_ms: durationMs, from: {xPercent: 0, opacity: 1}, to: {xPercent: -12, opacity: 0}, ease: "power2.out"},
    {kind: "from_to", target: incoming, start_ms: startMs, duration_ms: durationMs, from: {xPercent: 12, opacity: 0}, to: {xPercent: 0, opacity: 1}, ease: "power2.out"},
  ]);
  if (effectiveTransition === "light_flash") return freezeOperations([
    {kind: "from_to", target: outgoing, start_ms: startMs, duration_ms: durationMs, from: {opacity: 1}, to: {opacity: .7}, ease: "power2.out"},
    {kind: "from_to", target: incoming, start_ms: startMs, duration_ms: durationMs, from: {opacity: .7}, to: {opacity: 1}, ease: "power2.out"},
    {kind: "from_to", target: flashTarget, start_ms: startMs, duration_ms: Math.floor(durationMs / 2), from: {opacity: 0}, to: {opacity: 1}, ease: "power2.out"},
    {kind: "from_to", target: flashTarget, start_ms: startMs + Math.floor(durationMs / 2), duration_ms: durationMs - Math.floor(durationMs / 2), from: {opacity: 1}, to: {opacity: 0}, ease: "power2.out", immediate_render: false},
  ]);
  return null;
}

function freezeOperations(operations) {
  const frozen = Object.freeze(operations.map((operation) => Object.freeze({
    ...operation, from: Object.freeze({...operation.from}), to: Object.freeze({...operation.to}),
  })));
  return frozen;
}

function compileOperation(operation) {
  if (operation.kind === "set") return `tl.set(${JSON.stringify(operation.target)},${JSON.stringify(operation.to)},${operation.start_ms / 1000});`;
  if (operation.kind === "from_to") return `tl.fromTo(${JSON.stringify(operation.target)},${JSON.stringify(operation.from)},${JSON.stringify({...operation.to, duration: operation.duration_ms / 1000, ease: operation.ease, ...(operation.immediate_render === false ? {immediateRender: false} : {})})},${operation.start_ms / 1000});`;
  throw new Error("transition_operation_invalid");
}

function validateTransitionOperations(operations, {allowedTargets, sceneDurationMs, requireTrusted = false, compileContext} = {}) {
  const trustedContext = OPERATION_CONTEXTS.get(operations);
  if (!Array.isArray(operations) || operations.length === 0 || operations.length > 8 || (requireTrusted && !trustedContext)) throw new Error("transition_operation_invalid");
  if (compileContext && !matchesTransitionCompileContext(trustedContext, compileContext)) throw new Error("transition_operation_context_invalid");
  for (const operation of operations) {
    if (!plainRecord(operation) || !TARGET.test(operation.target) || !allowedTargets.has(operation.target) || !Number.isInteger(operation.start_ms) || operation.start_ms < 0 || !Number.isInteger(operation.duration_ms) || operation.duration_ms <= 0 || (sceneDurationMs !== undefined && operation.start_ms + operation.duration_ms > sceneDurationMs)) throw new Error("transition_operation_invalid");
    if (operation.kind === "set") {
      exactKeys(operation, ["kind", "target", "start_ms", "duration_ms", "from", "to"], "transition_operation_invalid");
    } else if (operation.kind === "from_to") {
      exactKeys(operation, ["kind", "target", "start_ms", "duration_ms", "from", "to", "ease", ...(operation.immediate_render === false ? ["immediate_render"] : [])], "transition_operation_invalid");
      if (operation.ease !== "power2.out") throw new Error("transition_operation_invalid");
      if (Object.prototype.hasOwnProperty.call(operation, "immediate_render") && operation.immediate_render !== false) throw new Error("transition_operation_invalid");
    } else throw new Error("transition_operation_invalid");
    validateTransitionStyle(operation.from); validateTransitionStyle(operation.to);
  }
}
function matchesTransitionCompileContext(context, value) {
  return Boolean(context && plainRecord(value)
    && value.operationVersion === context.operationVersion
    && value.transition === context.transition
    && value.effectiveTransition === context.effectiveTransition
    && value.outgoing === context.outgoing
    && value.incoming === context.incoming
    && value.flashTarget === context.flashTarget
    && value.startMs === context.startMs
    && value.durationMs === context.durationMs);
}
function validateTransitionStyle(style) {
  const allowed = new Set(["opacity", "clipPath", "xPercent", "scale"]);
  if (!plainRecord(style) || Object.keys(style).some((key) => !allowed.has(key))) throw new Error("transition_operation_invalid");
  for (const [key, value] of Object.entries(style)) {
    if (["opacity", "xPercent", "scale"].includes(key) && !Number.isFinite(value)) throw new Error("transition_operation_invalid");
    if (key === "clipPath" && !["inset(0 100% 0 0)", "inset(0 0% 0 0)", "inset(0 0 0 100%)"].includes(value)) throw new Error("transition_operation_invalid");
  }
}
function exactKeys(value, keys, code) {
  const actual = Object.keys(value).sort(); const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) throw new Error(code);
}
function plainRecord(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
