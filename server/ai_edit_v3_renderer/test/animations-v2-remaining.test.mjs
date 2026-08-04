import assert from "node:assert/strict";
import {mkdtemp, readFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {compileProjectV2} from "../src/compile-project-v2.mjs";
import {applyAnimation} from "../src/registry/animations.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";
import {applyTransition, compileTransitionScript} from "../src/registry/transitions.mjs";

const REMAINING_PRESETS = [
  "scale", "rotate", "wipe", "image_pan_zoom", "card_reveal",
  "stamp", "light_sweep", "highlight_draw", "split_screen", "subtitle_pop",
];
const VALID_V2_FIXTURE = new URL("../../../tests/fixtures/ai_edit_v3/valid-render-manifest-v2.json", import.meta.url);

test("Task 8b RED E2E: adjacent shared primary binding becomes a real matched-card host transition", async () => {
  const manifest = await adjacentCardManifest();
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-card-match-e2e-")), "project");
  await compileProjectV2({manifest, outputRoot});
  const index = await readFile(path.join(outputRoot, "index.html"), "utf8");
  const firstHost = "composition_01_host";
  const secondHost = "composition_02_host";
  assert.equal((index.match(new RegExp(`id="${firstHost}"`, "gu")) ?? []).length, 1);
  assert.equal((index.match(new RegExp(`id="${secondHost}"`, "gu")) ?? []).length, 1);
  assert.match(index, /data-card-identity="primary:asset_01"/u);
  assert.match(index, new RegExp(`tl\\.fromTo\\("#${firstHost}"`, "u"));
  assert.match(index, new RegExp(`tl\\.fromTo\\("#${secondHost}"`, "u"));
  assert.match(index, /data-transition-audit="card_match_cut:matched:primary:asset_01"/u);
  for (const [compositionId, slotId] of [["composition_01", "composition_01_primary"], ["composition_02", "composition_02_primary"]]) {
    const scene = await readFile(path.join(outputRoot, "compositions", `${compositionId}.html`), "utf8");
    assert.equal((scene.match(new RegExp(`id="${slotId}"`, "gu")) ?? []).length, 1, `${slotId} must identify one real slot DOM node`);
  }
});

test("Task 8b: missing or conflicting adjacent identity safely degrades to a branded soft wipe", async () => {
  const missing = await adjacentCardManifest();
  missing.assets.push({...missing.assets[0], id: "asset_02", path: "media/image-02.png"});
  missing.compositions = missing.compositions.map((item, index) => Object.freeze({
    ...item,
    layout_slot_bindings: Object.freeze([Object.freeze({slot_id: "primary", asset_id: index ? "asset_02" : "asset_01"})]),
    asset_ids: Object.freeze([index ? "asset_02" : "asset_01"]),
  }));
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-card-fallback-e2e-")), "project");
  await compileProjectV2({manifest: missing, outputRoot});
  const index = await readFile(path.join(outputRoot, "index.html"), "utf8");
  assert.doesNotMatch(index, /data-card-identity=/u);
  assert.match(index, /data-transition-audit="card_match_cut:fallback:identity_missing"/u);
  assert.match(index, /tl\.fromTo\("#composition_01_host",\{"clipPath"/u);
  assert.match(index, /tl\.fromTo\("#composition_02_host",\{"clipPath"/u);
});

test("Task 8b RED E2E: real V2 compile targets mask, sweep accent, and subtitle DOM with normalized programs", async () => {
  const manifest = await remainingAnimationManifest();
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-motion-remaining-e2e-")), "project");
  await compileProjectV2({manifest, outputRoot});
  const html = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  for (const selector of [
    "composition_01_wipe_01_headline_block",
    "composition_01_sweep_01_headline_block_underline",
    "composition_01_subtitle_01_emphasis_caption_caption",
  ]) assert.equal((html.match(new RegExp(`id="${selector}"`, "gu")) ?? []).length, 1, `${selector} must be a unique real DOM target`);
  assert.match(html, /tl\.fromTo\("#composition_01_wipe_01_headline_block",\{"clipPath":"inset\(0 0 0 100%\)"\}/u);
  assert.match(html, /tl\.fromTo\("#composition_01_sweep_01_headline_block_underline",\{"xPercent":-120,"opacity":0\}/u);
  assert.match(html, /tl\.fromTo\("#composition_01_subtitle_01_emphasis_caption_caption",\{"y":22,"scale":0\.92,"opacity":0\}/u);
  assert.doesNotMatch(html, /setTimeout|setInterval|requestAnimationFrame|https?:\/\//u);
});

test("Task 8b browser gate: compiled safe hosts never overlap overlay clips on the same track", async () => {
  const manifest = await adjacentCardManifest();
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-motion-track-gate-")), "project");
  await compileProjectV2({manifest, outputRoot});
  for (const composition of manifest.compositions) {
    const html = await readFile(path.join(outputRoot, "compositions", `${composition.id}.html`), "utf8");
    const clips = [...html.matchAll(/<[^>]+\bclass="[^"]*\bclip\b[^"]*"[^>]*>/gu)].map(([tag]) => ({
      id: tag.match(/\bid="([^"]+)"/u)?.[1], tag,
      start: Number(tag.match(/\bdata-start="([^"]+)"/u)?.[1]),
      duration: Number(tag.match(/\bdata-duration="([^"]+)"/u)?.[1]),
      track: Number(tag.match(/\bdata-track-index="([^"]+)"/u)?.[1]),
    })).filter((clip) => [clip.start, clip.duration, clip.track].every(Number.isFinite));
    for (const clip of clips) assert.ok(clip.id, `${composition.id} timeline clip needs a stable id: ${clip.tag}`);
    for (let left = 0; left < clips.length; left += 1) {
      for (let right = left + 1; right < clips.length; right += 1) {
        if (clips[left].track !== clips[right].track) continue;
        const overlap = clips[left].start < clips[right].start + clips[right].duration
          && clips[right].start < clips[left].start + clips[left].duration;
        assert.equal(overlap, false, `${composition.id} track ${clips[left].track}: ${clips[left].id} overlaps ${clips[right].id}`);
      }
    }
  }
});

test("Task 8b strict browser gate: split-copy timeline nodes have deterministic unique stable ids", async () => {
  const manifest = await adjacentCardManifest();
  manifest.compositions[0] = {...manifest.compositions[0], layout_variant: "split_copy"};
  const roots = await Promise.all(["a", "b"].map(async (suffix) => {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-split-copy-${suffix}-`)), "project");
    await compileProjectV2({manifest, outputRoot});
    return outputRoot;
  }));
  const html = await readFile(path.join(roots[0], "compositions", "composition_01.html"), "utf8");
  assert.equal(html, await readFile(path.join(roots[1], "compositions", "composition_01.html"), "utf8"));
  const timedTags = [...html.matchAll(/<[^>]+\bdata-(?:start|duration)="[^"]+"[^>]*>/gu)].map(([tag]) => tag);
  assert.ok(timedTags.length > 0);
  const ids = timedTags.map((tag) => tag.match(/\bid="([^"]+)"/u)?.[1]);
  assert.equal(ids.every(Boolean), true, `timeline-visible nodes need stable ids: ${timedTags.filter((_, index) => !ids[index]).join(" | ")}`);
  assert.equal(new Set(ids).size, ids.length, "timeline-visible node ids must be unique");
});

test("Task 8b RED: remaining presets publish ten distinct normalized programs", () => {
  const programs = REMAINING_PRESETS.map((preset) => {
    const audit = applyAnimation({
      timeline: seekableTimeline(), preset, target: "#scene_target",
      direction: ["wipe", "split_screen"].includes(preset) ? "left" : "in",
      operationVersion: "2.0", params: {durationMs: 800, delayMs: 100},
      sceneDurationMs: 3200, fps: 30,
    });
    assert.ok(Array.isArray(audit.operations) && audit.operations.length > 0, `${preset} must be normalized`);
    return canonical(audit.operations);
  });
  assert.equal(new Set(programs).size, REMAINING_PRESETS.length);
});

test("Task 8b RED: all remaining presets are seek-reentrant for both ratios and supported fps", () => {
  for (const preset of REMAINING_PRESETS) {
    for (const ratio of ["16:9", "9:16"]) {
      for (const fps of [24, 25, 30]) {
        const timeline = seekableTimeline();
        const audit = applyAnimation({
          timeline, preset, target: "#seek_target",
          direction: ["wipe", "split_screen"].includes(preset) ? "right" : "in",
          operationVersion: "2.0", params: {durationMs: 800, delayMs: 100},
          sceneDurationMs: 3200, fps, ratio,
        });
        assert.ok(Array.isArray(audit.operations) && audit.operations.length > 0, `${preset}/${ratio}/${fps} must be normalized`);
        const states = [0.25, 0.5, 0.75, 0.25].map((progress) => timeline.seek((audit.endMs / 1000) * progress).snapshot());
        assert.equal(states[0], states[3], `${preset}/${ratio}/${fps} must restore 25% state`);
        assert.notEqual(states[0], states[2], `${preset}/${ratio}/${fps} must make visible progress`);
      }
    }
  }
});

test("Task 8b: wipe and split-screen share one explicit directional contract", () => {
  const expected = {
    left: {wipe: "inset(0 100% 0 0)", split: -50},
    right: {wipe: "inset(0 0 0 100%)", split: 50},
    up: {wipe: "inset(0 0 100% 0)", split: -35},
    down: {wipe: "inset(100% 0 0 0)", split: 35},
    in: {wipe: "inset(0 100% 0 0)", split: -50},
    out: {wipe: "inset(0 0 0 100%)", split: 50},
  };
  for (const [direction, values] of Object.entries(expected)) {
    const wipe = animationProgram("wipe", direction)[0];
    const split = animationProgram("split_screen", direction)[0];
    assert.equal(wipe.from.clipPath, values.wipe);
    assert.equal(split.from.xPercent, values.split);
  }
});

test("Task 8b RED: direct V2 transitions are distinct finite two-scene programs", () => {
  const programs = ["soft_wipe", "directional_slide", "light_flash"].map((transition) => {
    const timeline = seekableTimeline();
    const audit = applyTransition({
      timeline, transition, outgoing: "#scene_previous", incoming: "#scene_current",
      ...(transition === "light_flash" ? {flashTarget: "#scene_flash"} : {}),
      operationVersion: "2.0", boundaryMs: 900, sceneDurationMs: 2400, fps: 30,
    });
    assert.ok(Array.isArray(audit.operations) && audit.operations.length >= 2, `${transition} must control previous and current scenes`);
    const expectedTargets = transition === "light_flash"
      ? new Set(["#scene_previous", "#scene_current", "#scene_flash"])
      : new Set(["#scene_previous", "#scene_current"]);
    assert.deepEqual(new Set(audit.operations.map((operation) => operation.target)), expectedTargets);
    assert.ok(audit.operations.every((operation) => operation.start_ms >= 0 && operation.start_ms + operation.duration_ms <= 2400));
    const states = [0.25, 0.5, 0.75, 0.25].map((progress) => timeline.seek((audit.startMs + (audit.durationMs * progress)) / 1000).snapshot());
    assert.equal(states[0], states[3]);
    if (transition !== "light_flash") assert.notEqual(states[0], states[2]);
    return canonical(audit.operations);
  });
  assert.equal(new Set(programs).size, 3);
});

test("Task 8b review RED: light flash pulses a real white layer inside the 80-240ms contract", async () => {
  const timeline = seekableTimeline();
  const audit = applyTransition({
    timeline, transition: "light_flash",
    outgoing: "#scene_previous", incoming: "#scene_current",
    flashTarget: "#scene_flash",
    operationVersion: "2.0", boundaryMs: 900, sceneDurationMs: 2400, fps: 30,
  });
  assert.ok(audit.durationMs >= 80 && audit.durationMs <= 240);
  assert.equal(audit.operations.filter((operation) => operation.target === "#scene_flash").length, 2);
  assert.ok(audit.operations.some((operation) => operation.target === "#scene_previous"));
  assert.ok(audit.operations.some((operation) => operation.target === "#scene_current"));
  const [start, peak, end] = [audit.startMs, audit.startMs + Math.floor(audit.durationMs / 2), audit.endMs]
    .map((milliseconds) => JSON.parse(timeline.seek(milliseconds / 1000).snapshot())["#scene_flash"]?.opacity ?? 0);
  assert.equal(start, 0); assert.equal(peak, 1); assert.equal(end, 0);
  for (const durationMs of [79, 241]) assert.throws(() => compileTransitionScript({
    transition: "light_flash", effectiveTransition: "light_flash",
    outgoing: "#scene_previous", incoming: "#scene_current", flashTarget: "#scene_flash",
    startMs: 0, durationMs, operationVersion: "2.0",
  }), /transition_duration_invalid/);

  const manifest = await adjacentCardManifest();
  manifest.compositions[1] = {...manifest.compositions[1], transition: "light_flash"};
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-light-flash-e2e-")), "project");
  await compileProjectV2({manifest, outputRoot});
  const index = await readFile(path.join(outputRoot, "index.html"), "utf8");
  assert.equal((index.match(/id="transition_flash_global"/gu) ?? []).length, 1);
  assert.match(index, /class="hf-transition-flash"[^>]*aria-hidden="true"/u);
  assert.match(index, /\.hf-transition-flash\{[^}]*background:#fff;opacity:0/u);
  for (const selector of ["#composition_01_host", "#composition_02_host", "#transition_flash_global"]) {
    assert.match(index, new RegExp(`tl\\.fromTo\\(${JSON.stringify(selector)}`, "u"));
  }
});

test("Task 8b review RED: multiple flash boundaries reuse one global white layer without timeline overlap", async () => {
  const manifest = await adjacentCardManifest();
  const second = structuredClone(manifest.compositions[1]);
  manifest.duration_ms = 6000;
  manifest.compositions[1] = {...second, transition: "light_flash"};
  manifest.compositions.push({
    ...structuredClone(second), id: "composition_03", scene_id: "scene_03", start_ms: 4000, end_ms: 6000,
    transition: "light_flash",
    overlay_instances: second.overlay_instances.map((item) => ({...item, instance_id: `${item.instance_id}_third`})),
  });
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-multi-light-flash-e2e-")), "project");
  await compileProjectV2({manifest, outputRoot});
  const index = await readFile(path.join(outputRoot, "index.html"), "utf8");
  assert.equal((index.match(/id="transition_flash_global"/gu) ?? []).length, 1);
  assert.doesNotMatch(index, /id="transition_flash_[12]"/u);
  for (const [outgoing, incoming] of [["composition_01", "composition_02"], ["composition_02", "composition_03"]]) {
    assert.match(index, new RegExp(`tl\\.fromTo\\("#${outgoing}_host"`, "u"));
    assert.match(index, new RegExp(`tl\\.fromTo\\("#${incoming}_host"`, "u"));
  }
  assert.equal((index.match(/tl\.fromTo\("#transition_flash_global"/gu) ?? []).length, 4);
  const starts = [...index.matchAll(/tl\.fromTo\("#transition_flash_global",[^;]+?,(\d+(?:\.\d+)?)\);/gu)].map((match) => Number(match[1]));
  assert.equal(starts.length, 4);
  assert.ok(Math.max(...starts.slice(0, 2)) < Math.min(...starts.slice(2)), "flash windows must not overlap across boundaries");
});

test("Task 8b review RED: public transition API cannot mint or replay a caller identity", async () => {
  const transitions = await import("../src/registry/transitions.mjs");
  assert.equal(transitions.deriveCardMatchIdentity, undefined, "identity minting must stay private to the adjacent-composition operation");
  const forged = Object.freeze({
    slot_id: "primary", asset_id: "attacker_asset",
    outgoing: "#attacker_a_host", incoming: "#attacker_b_host",
    outgoing_slot: "#attacker_a_primary", incoming_slot: "#attacker_b_primary",
  });
  assert.throws(() => applyTransition({
    timeline: seekableTimeline(), transition: "card_match_cut",
    outgoing: forged.outgoing, incoming: forged.incoming, identity: forged,
    operationVersion: "2.0", boundaryMs: 900, sceneDurationMs: 2400, fps: 30,
  }), /transition_identity_unproven/);
});

function animationProgram(preset, direction) {
  const audit = applyAnimation({
    timeline: seekableTimeline(), preset, target: "#direction_target", direction,
    operationVersion: "2.0", params: {durationMs: 600, delayMs: 0}, sceneDurationMs: 2000, fps: 30,
  });
  return audit.operations;
}

async function canonicalFixture() {
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const manifest = structuredClone(JSON.parse(decoder.decode(await readFile(VALID_V2_FIXTURE))));
  manifest.registry_sha256 = getRegistrySha256();
  manifest.compositions[0].authoritative_content = {
    headline: {text: "Authoritative headline", source_caption_ids: ["caption_01"]},
    highlight: {text: "Authoritative supporting copy", source_caption_ids: ["caption_01"]},
  };
  return manifest;
}

async function adjacentCardManifest() {
  const manifest = await canonicalFixture();
  const base = manifest.compositions[0];
  manifest.compositions = [
    {
      ...structuredClone(base), id: "composition_01", scene_id: "scene_01", start_ms: 0, end_ms: 2000,
      layout_id: "product_hero", layout_variant: "center_pedestal", transition: "hard_cut",
      animations: [], asset_ids: ["asset_01"], layout_slot_bindings: [{slot_id: "primary", asset_id: "asset_01"}],
    },
    {
      ...structuredClone(base), id: "composition_02", scene_id: "scene_02", start_ms: 2000, end_ms: 4000,
      layout_id: "product_hero", layout_variant: "detail_gallery", transition: "card_match_cut",
      overlay_instances: base.overlay_instances.map((item) => ({...item, instance_id: `${item.instance_id}_next`})),
      animations: [], asset_ids: ["asset_01"], layout_slot_bindings: [{slot_id: "primary", asset_id: "asset_01"}],
    },
  ];
  return manifest;
}

async function remainingAnimationManifest() {
  const manifest = await canonicalFixture();
  manifest.compositions[0] = {
    ...manifest.compositions[0],
    overlay_ids: ["headline_block", "headline_block", "emphasis_caption"],
    overlay_instances: [
      {instance_id: "wipe_01", component_id: "headline_block", content_ref: "headline", placement: "title_safe"},
      {instance_id: "sweep_01", component_id: "headline_block", content_ref: "highlight", placement: "title_safe"},
      {instance_id: "subtitle_01", component_id: "emphasis_caption", content_ref: "highlight", placement: "subtitle_safe"},
    ],
    animations: [
      {target: "wipe_01", preset: "wipe", direction: "right", duration_ms: 700, delay_ms: 0},
      {target: "sweep_01", preset: "light_sweep", direction: "right", duration_ms: 700, delay_ms: 100},
      {target: "subtitle_01", preset: "subtitle_pop", direction: "up", duration_ms: 600, delay_ms: 200},
    ],
  };
  return manifest;
}

function seekableTimeline() {
  const operations = [];
  const timeline = {
    fromTo(target, from, to, at) { operations.push({target, from, to, at}); return timeline; },
    set(target, to, at) { operations.push({target, from: to, to, at}); return timeline; },
    seek(seconds) {
      const state = {};
      operations.forEach((operation, index) => {
        if (seconds < operation.at) return;
        const duration = operation.to.duration ?? 0;
        const progress = duration === 0 ? Number(seconds >= operation.at) : Math.max(0, Math.min(1, (seconds - operation.at) / duration));
        const key = typeof operation.target === "string" ? operation.target : `proxy_${index}`;
        state[key] = interpolate(operation.from, operation.to, progress);
      });
      return {snapshot: () => canonical(state)};
    },
  };
  return timeline;
}

function interpolate(from, to, progress) {
  const output = {};
  for (const key of new Set([...Object.keys(from), ...Object.keys(to)])) {
    if (["duration", "ease", "immediateRender", "onUpdate"].includes(key)) continue;
    output[key] = typeof from[key] === "number" && typeof to[key] === "number"
      ? from[key] + ((to[key] - from[key]) * progress)
      : key === "clipPath" ? interpolateInset(from[key], to[key], progress)
      : progress < 1 ? from[key] : to[key];
  }
  return output;
}

function interpolateInset(from, to, progress) {
  const parse = (value) => value.match(/-?\d+(?:\.\d+)?/gu)?.map(Number);
  const start = parse(from); const end = parse(to);
  assert.equal(start?.length, 4); assert.equal(end?.length, 4);
  return `inset(${start.map((value, index) => `${value + ((end[index] - value) * progress)}%`).join(" ")})`;
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
