import assert from "node:assert/strict";
import test from "node:test";

import {ANIMATION_CONTRACTS, applyAnimation, compileAnimationScript} from "../src/registry/animations.mjs";

test("fourteen finite deterministic animation presets stay inside scene bounds", () => {
  assert.equal(ANIMATION_CONTRACTS.length, 14);
  for (const contract of ANIMATION_CONTRACTS) {
    const timeline = fakeTimeline();
    const audit = applyAnimation({
      timeline, preset: contract.id, target: "#scene_overlay", params: {durationMs: 900, delayMs: 100},
      sceneDurationMs: 2000, fps: 30,
    });
    assert.equal(audit.preset, contract.id);
    assert(audit.startMs >= 0 && audit.endMs <= 2000 && audit.endMs > audit.startMs);
    assert(timeline.calls.length >= 1);
    assert.doesNotMatch(JSON.stringify(timeline.calls), /repeat|random|yoyo/i);
    assert.doesNotMatch(compileAnimationScript({
      ...audit, target: "#scene_overlay",
      ...(audit.operations ? {windowStartMs: 0, compositionDurationMs: 2000} : {}),
    }), /eval|Function|setTimeout|repeat\s*:/);
  }
});

test("unknown animation and unsafe target fail closed", () => {
  assert.throws(() => applyAnimation({timeline: fakeTimeline(), preset: "bounce", target: "#x", params: {}, sceneDurationMs: 2000, fps: 30}), /animation_unknown/);
  assert.throws(() => applyAnimation({timeline: fakeTimeline(), preset: "fade", target: "body > *", params: {}, sceneDurationMs: 2000, fps: 30}), /animation_target_invalid/);
});

function fakeTimeline() {
  const timeline = {calls: []};
  for (const method of ["fromTo", "to", "set"]) timeline[method] = (...args) => { timeline.calls.push([method, ...args]); return timeline; };
  return timeline;
}
