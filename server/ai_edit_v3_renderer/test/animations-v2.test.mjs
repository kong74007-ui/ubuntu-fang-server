import assert from "node:assert/strict";
import {mkdtemp, readFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {compileProject} from "../src/compile-project.mjs";
import {compileProjectV2} from "../src/compile-project-v2.mjs";
import {applyAnimation, compileAnimationScript} from "../src/registry/animations.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";
import {applyTransition, compileTransitionScript} from "../src/registry/transitions.mjs";

const REPRESENTATIVE_PRESETS = ["fade", "slide", "count_up", "stagger"];
const VALID_V2_FIXTURE = new URL("../../../tests/fixtures/ai_edit_v3/valid-render-manifest-v2.json", import.meta.url);

test("Task 8a baseline: test source and canonical fixture are strict UTF-8 without mojibake", async () => {
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const source = decoder.decode(await readFile(new URL(import.meta.url)));
  const fixture = decoder.decode(await readFile(VALID_V2_FIXTURE));
  assert.doesNotMatch(source, /\uFFFD/u);
  assert.doesNotMatch(fixture, /\uFFFD/u);
  assert.match(source, /真实方向测试/u);
  assert.match(fixture, /权威标题/u);
});

test("Task 8a baseline: canonical V2 fixture compiles repeated instances to unique real selectors", async () => {
  for (const ratio of ["16:9", "9:16"]) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-motion-baseline-${ratio.replace(":", "-")}-`)), "project");
    const manifest = await motionManifest(ratio, {animations: []});
    await compileProjectV2({manifest, outputRoot});
    const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
    for (const id of ["composition_01_headline_left_headline_block", "composition_01_headline_right_headline_block"]) {
      assert.equal((scene.match(new RegExp(`\\sid="${id}"`, "gu")) ?? []).length, 1);
    }
  }
});

test("Task 8a baseline: V1 keeps the legacy slide program independent of V2 direction", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-motion-v1-")), "project");
  const manifest = await legacyMotionManifest();
  await compileProject({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /tl\.set\("#composition_01_root",\{autoAlpha:1\},0\);/u);
  assert.match(scene, /tl\.fromTo\("#composition_01_headline_block",\{"x":-36,"autoAlpha":0\}/u);
  assert.doesNotMatch(scene, /"x":36|"opacity":[01]/u);
});

test("Task 8a RED: representative presets expose distinct normalized operation records", () => {
  const records = REPRESENTATIVE_PRESETS.map((preset) => {
    const audit = applyAnimation({
      timeline: fakeTimeline(), preset, target: "#scene_overlay", direction: preset === "slide" ? "left" : "in",
      childTargets: preset === "stagger" ? ["#scene_item_1", "#scene_item_2", "#scene_item_3"] : undefined,
      params: {durationMs: 600, delayMs: 80}, sceneDurationMs: 2400, fps: 30,
    });
    assertNormalizedOperations(audit.operations, preset);
    return canonical(audit.operations);
  });
  assert.equal(new Set(records).size, REPRESENTATIVE_PRESETS.length);
});

test("Task 8a RED: slide direction changes the normalized transform", () => {
  const left = animationAudit("slide", {direction: "left"});
  const right = animationAudit("slide", {direction: "right"});
  assertNormalizedOperations(left.operations, "slide-left");
  assertNormalizedOperations(right.operations, "slide-right");
  assert.notEqual(canonical(left.operations), canonical(right.operations));
  assert.match(canonical(left.operations), /(?:x|xPercent).*-/u);
  assert.match(canonical(right.operations), /(?:x|xPercent).*[^-][1-9]/u);
  for (const direction of ["none", "left", "right", "up", "down", "in", "out"]) {
    assertNormalizedOperations(animationAudit("slide", {direction}).operations, `slide-${direction}`);
  }
  assert.throws(() => animationAudit("slide", {direction: "diagonal"}), /animation_direction_invalid/);
});

test("Task 8a RED: count-up binds a deterministic numeric proxy and stagger targets every child", () => {
  const countUp = animationAudit("count_up", {direction: "in", params: {numericStart: 0, numericEnd: 42.5, numericPrecision: 1, numericPrefix: "¥", numericSuffix: "万"}});
  assertNormalizedOperations(countUp.operations, "count_up");
  assert.ok(countUp.operations.some((operation) => operation.kind === "numeric_proxy" && operation.update_binding === "text_content"
    && operation.from_value === 0 && operation.to_value === 42.5 && operation.precision === 1
    && operation.prefix === "¥" && operation.suffix === "万"));

  const children = ["#metric_1", "#metric_2", "#metric_3"];
  const stagger = animationAudit("stagger", {direction: "in", childTargets: children});
  assertNormalizedOperations(stagger.operations, "stagger");
  assert.deepEqual(stagger.operations.map((operation) => operation.target), children);
  assert.equal(new Set(stagger.operations.map((operation) => operation.start_ms)).size, children.length);
});

test("Task 8a RED: representative presets are seek-reentrant at two ratios and three frame rates", () => {
  for (const preset of REPRESENTATIVE_PRESETS) {
    for (const ratio of ["16:9", "9:16"]) {
      for (const fps of [24, 25, 30]) {
        const timeline = seekableTimeline();
        const audit = applyAnimation({
          timeline, preset, target: "#seek_target", direction: preset === "slide" ? "up" : "in",
          childTargets: preset === "stagger" ? ["#seek_child_1", "#seek_child_2"] : undefined,
          params: {durationMs: 800, delayMs: 100, numericStart: 0, numericEnd: 42}, sceneDurationMs: 3200, fps, ratio,
        });
        assertNormalizedOperations(audit.operations, `${preset}-${ratio}-${fps}`);
        const snapshots = [0.25, 0.5, 0.75, 0.25].map((progress) => timeline.seek((audit.durationMs / 1000) * progress).snapshot());
        assert.equal(snapshots[0], snapshots[3], `${preset} ${ratio} ${fps}fps must restore the same 25% state`);
        assert.notEqual(snapshots[0], snapshots[2], `${preset} ${ratio} ${fps}fps must make visible progress`);
      }
    }
  }
});

test("Task 8a RED: unproven card identity fails closed and missing identity audits only internal soft-wipe fallback", () => {
  for (const operationVersion of ["1.0", "2.0", "bogus"]) {
    assert.throws(() => applyTransition({
      timeline: fakeTimeline(), transition: "card_match_cut", outgoing: "#scene_a", incoming: "#scene_b",
      identity: {slot: "speaker", outgoing: "#scene_a_speaker", incoming: "#scene_b_speaker"}, operationVersion,
      boundaryMs: 1000, sceneDurationMs: 2400, fps: 30,
    }), /transition_identity_unproven/);
  }
  assert.throws(() => applyTransition({
    timeline: fakeTimeline(), transition: "hard_cut", outgoing: "#scene_a", incoming: "#scene_b",
    operationVersion: "bogus", boundaryMs: 1000, sceneDurationMs: 2400, fps: 30,
  }), /transition_operation_version_invalid/);
  const missing = applyTransition({
    timeline: fakeTimeline(), transition: "card_match_cut", outgoing: "#scene_a", incoming: "#scene_b",
    boundaryMs: 1000, sceneDurationMs: 2400, fps: 30,
  });
  assert.equal(missing.effectiveTransition, "soft_wipe");
  assert.equal(missing.fallbackReason, "identity_missing");
  assertNormalizedOperations(missing.operations, "card_match_cut-fallback");
  const directSoftWipe = applyTransition({
    timeline: fakeTimeline(), transition: "soft_wipe", outgoing: "#scene_a", incoming: "#scene_b",
    boundaryMs: 1000, sceneDurationMs: 2400, fps: 30,
  });
  assert.equal(directSoftWipe.operations, undefined);
});

test("Task 8a RED: real V2 number and list components compile dedicated public targets without destroying DOM", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-motion-semantic-")), "project");
  await compileProjectV2({manifest: await semanticMotionManifest(), outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  const metricId = "composition_01_number_01_number_proof_metric_value";
  for (const item of [1, 2, 3]) assert.match(scene, new RegExp(`tl\\.fromTo\\("#composition_01_bullets_01_bullet_list_item_${item}"`, "u"));
  assert.match(scene, new RegExp(`document\\.querySelector\\("#${metricId}"\\)`, "u"));
  assert.match(scene, /const sink=node\.querySelector\("span"\)\?\?node/u);
  assert.doesNotMatch(scene, /node\.textContent=String/u);

  const script = scene.match(new RegExp(`(\\(\\(\\)=>\\{const node=document\\.querySelector\\("#${metricId}"\\)[\\s\\S]*?\\}\\)\\(\\);)`, "u"))?.[1];
  assert.ok(script, "compiled count-up runtime must be executable in isolation");
  const span = {textContent: "42"};
  const unit = {textContent: "%"};
  const node = {marker: "number-proof-structure", querySelector: (selector) => selector === "span" ? span : null, get textContent() { return span.textContent; }};
  const timeline = numericRuntimeTimeline();
  new Function("document", "tl", script)({querySelector: () => node}, timeline);
  const states = [0.45, 0.15, 0.45].map((seconds) => {
    timeline.seek(seconds);
    return {value: span.textContent, marker: node.marker, unit: unit.textContent};
  });
  assert.deepEqual(states[0], states[2], "generated runtime must restore the exact 75% DOM-leaf state after seeking backwards");
  assert.notDeepEqual(states[0], states[1], "generated runtime must expose a different 25% state");
  assert.equal(node.marker, "number-proof-structure");
  assert.equal(unit.textContent, "%");
  timeline.seek(0.6);
  assert.equal(span.textContent, "42.5", "decimal terminal value must exactly equal the frozen numeric audit value");
});

test("Task 8a RED: real director step-indicator stagger compiles every public step child", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-motion-steps-")), "project");
  await compileProjectV2({manifest: await semanticStepMotionManifest(), outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  for (const item of [1, 2, 3]) assert.match(scene, new RegExp(`tl\\.fromTo\\("#composition_01_steps_01_step_indicator_step_${item}"`, "u"));
});

test("Task 8a RED: count-up rejects authority numbers that cannot round-trip exactly", async () => {
  const manifest = await semanticMotionManifest();
  manifest.compositions[0].authoritative_content.headline.text = "增长9007199254740993%";
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-motion-unsafe-number-")), "project");
  await assert.rejects(() => compileProjectV2({manifest, outputRoot}), /animation_numeric_target_invalid/);
});

test("Task 8a RED: exported normalized-operation compilers reject unsafe caller records", () => {
  const animationBase = {preset: "fade", target: "#safe_target", startMs: 0, durationMs: 500};
  const nullPrototypeAnimation = Object.assign(Object.create(null), {kind: "from_to", target: "#safe_target", start_ms: 0, duration_ms: 500, from: {opacity: 0}, to: {opacity: 1}, ease: "power2.out"});
  for (const operations of [
    [{kind: "from_to", target: "body > *", start_ms: 0, duration_ms: 500, from: {opacity: 0}, to: {opacity: 1}, ease: "power2.out"}],
    [{kind: "eval", target: "#safe_target", start_ms: 0, duration_ms: 500}],
    [{kind: "from_to", target: "#safe_target", start_ms: -1, duration_ms: 500, from: {opacity: 0}, to: {opacity: 1}, ease: "power2.out"}],
    [{kind: "from_to", target: "#safe_target", start_ms: 0, duration_ms: 500, from: {opacity: 0}, to: {opacity: 1}, ease: "javascript:alert(1)"}],
    [{kind: "from_to", target: "#safe_target", start_ms: 0, duration_ms: 500, from: {opacity: Number.NaN}, to: {opacity: 1}, ease: "power2.out"}],
    [{kind: "from_to", target: "#safe_target", start_ms: 0, duration_ms: 500, from: {opacity: 0}, to: {opacity: 1}, ease: "power2.out", extra: true}],
    [nullPrototypeAnimation],
  ]) assert.throws(() => compileAnimationScript({...animationBase, operations}), /animation_operation_invalid/);
  assert.throws(() => applyAnimation({timeline: fakeTimeline(), preset: "fade", target: "#safe_target", operationVersion: "bogus", sceneDurationMs: 1000, fps: 30}), /animation_operation_version_invalid/);

  const transitionBase = {transition: "hard_cut", outgoing: "#safe_out", incoming: "#safe_in", startMs: 0, durationMs: 34};
  const nullPrototypeTransition = Object.assign(Object.create(null), {kind: "set", target: "#safe_in", start_ms: 0, duration_ms: 34, from: {}, to: {opacity: 1}});
  for (const operations of [
    [{kind: "set", target: "body", start_ms: 0, duration_ms: 34, from: {}, to: {opacity: 1}}],
    [{kind: "exec", target: "#safe_in", start_ms: 0, duration_ms: 34, from: {}, to: {opacity: 1}}],
    [{kind: "set", target: "#safe_in", start_ms: -1, duration_ms: 34, from: {}, to: {opacity: 1}}],
    [{kind: "set", target: "#safe_in", start_ms: 0, duration_ms: 34, from: {}, to: {opacity: Number.NaN}}],
    [{kind: "set", target: "#safe_in", start_ms: 0, duration_ms: 34, from: {}, to: {opacity: 1}, extra: true}],
    [nullPrototypeTransition],
  ]) assert.throws(() => compileTransitionScript({...transitionBase, operations}), /transition_operation_invalid/);

  const mintedAnimation = applyAnimation({
    timeline: fakeTimeline(), preset: "fade", target: "#source", operationVersion: "2.0",
    params: {durationMs: 500, delayMs: 0}, sceneDurationMs: 1000, fps: 30,
  });
  for (const override of [
    {target: "#different"},
    {preset: "slide"},
    {startMs: -500, windowStartMs: -500},
    {startMs: 900, windowStartMs: 900},
  ]) assert.throws(() => compileAnimationScript({
    ...mintedAnimation, preset: "fade", target: "#source", startMs: 0, durationMs: 500,
    windowStartMs: 0, compositionDurationMs: 1000, ...override,
  }), /animation_operation_context_invalid/);

  const mintedTransition = applyTransition({
    timeline: fakeTimeline(), transition: "hard_cut", outgoing: "#safe_out", incoming: "#safe_in",
    operationVersion: "2.0", boundaryMs: 0, sceneDurationMs: 1000, fps: 30,
  });
  assert.throws(() => compileTransitionScript({...mintedTransition, transition: "card_match_cut", outgoing: "#safe_out", incoming: "#safe_in"}), /transition_operation_context_invalid/);

  for (const preset of REPRESENTATIVE_PRESETS) {
    assert.throws(() => compileAnimationScript({
      operationVersion: "2.0", preset, target: "#safe_target", startMs: 0, durationMs: 500,
      delayMs: 0, windowStartMs: 0, compositionDurationMs: 1000,
    }), /animation_operations_required/);
  }
  assert.throws(() => compileAnimationScript({operationVersion: "bogus", preset: "fade", target: "#safe_target", startMs: 0, durationMs: 500}), /animation_operation_version_invalid/);
  assert.throws(() => compileTransitionScript({operationVersion: "2.0", transition: "hard_cut", outgoing: "#safe_out", incoming: "#safe_in", startMs: 0, durationMs: 34}), /transition_operations_required/);
  assert.throws(() => compileTransitionScript({operationVersion: "2.0", transition: "card_match_cut", effectiveTransition: "soft_wipe", outgoing: "#safe_out", incoming: "#safe_in", startMs: 0, durationMs: 200}), /transition_operations_required/);
  assert.throws(() => compileTransitionScript({operationVersion: "1.0", transition: "hard_cut", effectiveTransition: "soft_wipe", outgoing: "#safe_out", incoming: "#safe_in", startMs: 0, durationMs: 34}), /transition_effective_invalid/);
  assert.throws(() => compileTransitionScript({operationVersion: "bogus", transition: "hard_cut", outgoing: "#safe_out", incoming: "#safe_in", startMs: 0, durationMs: 34}), /transition_operation_version_invalid/);
});

test("Task 8a RED: real V2 compilation preserves exact repeated-instance selectors and direction", async () => {
  for (const ratio of ["16:9", "9:16"]) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-motion-${ratio.replace(":", "-")}-`)), "project");
    await compileProjectV2({manifest: await motionManifest(ratio), outputRoot});
    const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
    const leftId = "composition_01_headline_left_headline_block";
    const rightId = "composition_01_headline_right_headline_block";
    assert.equal((scene.match(new RegExp(`\\sid="${leftId}"`, "gu")) ?? []).length, 1);
    assert.equal((scene.match(new RegExp(`\\sid="${rightId}"`, "gu")) ?? []).length, 1);
    assert.match(scene, new RegExp(`tl\\.fromTo\\("#${leftId}",[\\s\\S]*?"x":-`, "u"));
    assert.match(scene, new RegExp(`tl\\.fromTo\\("#${rightId}",[\\s\\S]*?"x":[1-9]`, "u"));
  }
});

function animationAudit(preset, {direction, childTargets, params} = {}) {
  return applyAnimation({
    timeline: fakeTimeline(), preset, target: "#scene_overlay", direction, childTargets,
    params: {durationMs: 600, delayMs: 80, ...params}, sceneDurationMs: 2400, fps: 30,
  });
}

function assertNormalizedOperations(operations, label) {
  assert.ok(Array.isArray(operations) && operations.length > 0, `${label} must expose normalized operation records`);
  for (const operation of operations) {
    assert.equal(Object.getPrototypeOf(operation), Object.prototype);
    assert.equal(typeof operation.kind, "string");
    assert.match(operation.target, /^#[a-z][a-z0-9_]{0,95}$/u);
    assert.ok(Number.isInteger(operation.start_ms) && Number.isInteger(operation.duration_ms));
  }
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function seekableTimeline() {
  const operations = [];
  const timeline = {
    fromTo(target, from, to, at) { operations.push({kind: "fromTo", target, from, to, at}); return timeline; },
    to(target, to, at) { operations.push({kind: "to", target, from: {}, to, at}); return timeline; },
    set(target, to, at) { operations.push({kind: "set", target, from: to, to, at}); return timeline; },
    seek(seconds) {
      const state = {};
      operations.forEach((operation, index) => {
        const duration = operation.to.duration ?? 0;
        const progress = duration === 0 ? Number(seconds >= operation.at) : Math.max(0, Math.min(1, (seconds - operation.at) / duration));
        const key = typeof operation.target === "string" ? operation.target : `proxy_${index}`;
        state[key] = interpolateState(operation.from, operation.to, progress);
        if (typeof operation.target === "object") Object.assign(operation.target, state[key]);
        operation.to.onUpdate?.();
      });
      return {snapshot: () => canonical(state)};
    },
  };
  return timeline;
}

function interpolateState(from, to, progress) {
  const state = {};
  for (const key of new Set([...Object.keys(from), ...Object.keys(to)])) {
    if (["duration", "ease", "onUpdate"].includes(key)) continue;
    const start = from[key]; const end = to[key];
    state[key] = typeof start === "number" && typeof end === "number" ? start + ((end - start) * progress) : progress < 1 ? start : end;
  }
  return state;
}

function fakeTimeline() {
  const timeline = {calls: []};
  for (const method of ["fromTo", "to", "set"]) timeline[method] = (...args) => { timeline.calls.push([method, ...args]); return timeline; };
  return timeline;
}

function numericRuntimeTimeline() {
  let operation;
  return {
    fromTo(target, from, to, at) { operation = {target, from, to, at}; return this; },
    seek(seconds) {
      assert.ok(operation, "numeric runtime must register a timeline operation");
      const progress = Math.max(0, Math.min(1, (seconds - operation.at) / operation.to.duration));
      operation.target.value = operation.from.value + ((operation.to.value - operation.from.value) * progress);
      operation.to.onUpdate();
      return this;
    },
  };
}

async function motionManifest(ratio, {animations} = {}) {
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const manifest = structuredClone(JSON.parse(decoder.decode(await readFile(VALID_V2_FIXTURE))));
  const [width, height] = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
  manifest.registry_sha256 = getRegistrySha256();
  manifest.output_spec = {...manifest.output_spec, ratio, width, height};
  manifest.compositions[0] = {
    ...manifest.compositions[0],
    overlay_ids: ["headline_block", "headline_block"],
    overlay_instances: [
      {instance_id: "headline_left", component_id: "headline_block", content_ref: "headline", placement: "title_safe"},
      {instance_id: "headline_right", component_id: "headline_block", content_ref: "highlight", placement: "title_safe"},
    ],
    animations: animations ?? [
      {target: "headline_left", preset: "slide", direction: "left", duration_ms: 500, delay_ms: 0},
      {target: "headline_right", preset: "slide", direction: "right", duration_ms: 500, delay_ms: 0},
    ],
    transition: "hard_cut",
  };
  return manifest;
}

async function legacyMotionManifest() {
  const manifest = await motionManifest("16:9");
  manifest.version = "1.0";
  delete manifest.theme_profile_id;
  delete manifest.design_intent;
  delete manifest.variation_seed;
  delete manifest.design_tokens;
  const composition = manifest.compositions[0];
  composition.overlay_ids = ["headline_block"];
  delete composition.overlay_instances;
  delete composition.authoritative_content;
  delete composition.layout_slot_bindings;
  composition.animations = [{target: "headline_block", preset: "slide", direction: "right", duration_ms: 500, delay_ms: 0}];
  return manifest;
}

async function semanticMotionManifest() {
  const manifest = await motionManifest("16:9", {animations: []});
  manifest.compositions[0] = {
    ...manifest.compositions[0],
    overlay_ids: ["number_proof", "bullet_list"],
    overlay_instances: [
      {instance_id: "number_01", component_id: "number_proof", content_ref: "headline", placement: "center"},
      {instance_id: "bullets_01", component_id: "bullet_list", content_ref: "highlight", placement: "right_panel"},
    ],
    animations: [
      {target: "number_01", preset: "count_up", direction: "in", duration_ms: 600, delay_ms: 0},
      {target: "bullets_01", preset: "stagger", direction: "in", duration_ms: 900, delay_ms: 100},
    ],
  };
  manifest.compositions[0].authoritative_content = {
    headline: {text: "增长42.5%", source_caption_ids: ["caption_01"]},
    highlight: {text: "第一项。第二项。第三项。", source_caption_ids: ["caption_01"]},
  };
  return manifest;
}

async function semanticStepMotionManifest() {
  const manifest = await motionManifest("16:9", {animations: []});
  manifest.compositions[0] = {
    ...manifest.compositions[0],
    overlay_ids: ["step_indicator"],
    overlay_instances: [{instance_id: "steps_01", component_id: "step_indicator", content_ref: "headline", placement: "left_panel"}],
    animations: [{target: "steps_01", preset: "stagger", direction: "up", duration_ms: 900, delay_ms: 0}],
  };
  manifest.compositions[0].authoritative_content = {
    ...manifest.compositions[0].authoritative_content,
    headline: {text: "第一步。第二步。第三步。", source_caption_ids: ["caption_01"]},
  };
  return manifest;
}
