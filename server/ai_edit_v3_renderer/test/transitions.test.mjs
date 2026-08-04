import assert from "node:assert/strict";
import test from "node:test";

import {applyTransition, compileTransitionScript, TRANSITION_CONTRACTS} from "../src/registry/transitions.mjs";

test("five transitions are finite and preserve the requested boundary", () => {
  assert.equal(TRANSITION_CONTRACTS.length, 5);
  for (const contract of TRANSITION_CONTRACTS) {
    const timeline = fakeTimeline();
    const audit = applyTransition({
      timeline, transition: contract.id, outgoing: "#outgoing", incoming: "#incoming",
      ...(contract.id === "light_flash" ? {flashTarget: "#transition_flash"} : {}),
      boundaryMs: 1500, sceneDurationMs: 3000, fps: 30,
    });
    assert.equal(audit.transition, contract.id);
    assert(audit.startMs >= 0 && audit.endMs <= 3000);
    assert.equal(audit.boundaryMs, 1500);
    assert.doesNotMatch(compileTransitionScript({...audit, outgoing: "#outgoing", incoming: "#incoming"}), /eval|Function|setTimeout|repeat\s*:/);
  }
});

test("card match cut retains subject identity and selectors are bounded", () => {
  const audit = applyTransition({timeline: fakeTimeline(), transition: "card_match_cut", outgoing: "#subject_a", incoming: "#subject_a_next", boundaryMs: 1000, sceneDurationMs: 2200, fps: 30});
  assert.equal(audit.identityRequired, true);
  assert.throws(() => applyTransition({timeline: fakeTimeline(), transition: "spin", outgoing: "#a", incoming: "#b", boundaryMs: 1000, sceneDurationMs: 2200, fps: 30}), /transition_unknown/);
});

test("V1 light flash preserves the legacy 180-420ms fade contract without a flash layer", () => {
  const timeline = fakeTimeline();
  const audit = applyTransition({
    timeline, transition: "light_flash", outgoing: "#outgoing", incoming: "#incoming",
    operationVersion: "1.0", boundaryMs: 1500, sceneDurationMs: 3000, fps: 30,
  });
  assert.equal(audit.durationMs, 420);
  assert.equal(audit.flashTarget, undefined);
  assert.equal(audit.operations, undefined);
  assert.deepEqual(timeline.calls, [[
    "fromTo", "#incoming", {autoAlpha: 0}, {autoAlpha: 1, duration: 0.42}, audit.startMs / 1000,
  ]]);
  assert.equal(
    compileTransitionScript({...audit, outgoing: "#outgoing", incoming: "#incoming"}),
    `tl.fromTo("#incoming",{"autoAlpha":0},{"xPercent":0,"autoAlpha":1,"duration":0.42,"ease":"power2.out"},${audit.startMs / 1000});`,
  );
});

function fakeTimeline() {
  const timeline = {calls: []};
  for (const method of ["fromTo", "to", "set"]) timeline[method] = (...args) => { timeline.calls.push([method, ...args]); return timeline; };
  return timeline;
}
