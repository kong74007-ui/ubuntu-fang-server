const VARIATION_SEED = /^[0-9a-f]{16}$/;

export function parseVariationSeed(variationSeed) {
  if (typeof variationSeed !== "string" || !VARIATION_SEED.test(variationSeed)) throw new Error("variation_seed_invalid");
  return Object.freeze([
    Number.parseInt(variationSeed.slice(0, 8), 16) >>> 0,
    Number.parseInt(variationSeed.slice(8), 16) >>> 0,
  ]);
}

export function createDeterministicRandom(variationSeed) {
  let [left, right] = parseVariationSeed(variationSeed);
  if ((left | right) === 0) right = 0x9e3779b9;
  return Object.freeze({
    nextUint32() {
      left ^= left << 13; left >>>= 0;
      left ^= left >>> 17; left >>>= 0;
      left ^= left << 5; left >>>= 0;
      right = (right + 0x9e3779b9) >>> 0;
      return (left ^ right) >>> 0;
    },
  });
}
