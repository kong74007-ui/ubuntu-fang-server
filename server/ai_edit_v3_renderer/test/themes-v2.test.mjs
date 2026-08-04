import assert from "node:assert/strict";
import test from "node:test";

import {resolveTheme} from "../src/registry/index.mjs";

const INTENT = Object.freeze({
  density: "balanced",
  motion_energy: "medium",
  image_fit: "cover",
  decoration_intensity: "medium",
});
const SEED = "0123456789abcdef";
const PROFILE_IDS = ["editorial_clean", "commercial_energy", "premium_dark", "warm_lifestyle"];

test("theme profiles resolve distinct deterministic frozen bounded design tokens", () => {
  const tokens = PROFILE_IDS.map((profileId) => resolveTheme({profileId, intent: INTENT, variationSeed: SEED}));

  assert.equal(new Set(tokens.map((value) => JSON.stringify(value))).size, PROFILE_IDS.length);
  for (const value of tokens) {
    assert(Object.isFrozen(value));
    assert.deepEqual(value, resolveTheme({profileId: value["--hf-theme-profile"], intent: INTENT, variationSeed: SEED}));
    for (const requiredToken of [
      "--hf-bg", "--hf-font", "--hf-type-scale", "--hf-gap", "--hf-radius", "--hf-border",
      "--hf-shadow", "--hf-texture", "--hf-density", "--hf-motion-distance", "--hf-image-fit",
    ]) assert.equal(typeof value[requiredToken], "string", `${requiredToken} is frozen`);
    for (const token of Object.values(value)) {
      assert.doesNotMatch(token, /(?:https?:)?\/\/|@font-face|animation/i);
    }
  }
});

test("theme profiles reject variation seeds that are not 16 lowercase hexadecimal characters", () => {
  for (const variationSeed of ["0123456789abcde", "0123456789abcdef0", "0123456789ABCDEf", "0123456789abcdeg", 42]) {
    assert.throws(
      () => resolveTheme({profileId: "editorial_clean", intent: INTENT, variationSeed}),
      /variation_seed_invalid/,
    );
  }
});
