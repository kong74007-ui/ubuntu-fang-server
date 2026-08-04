import {createDeterministicRandom, parseVariationSeed} from "./deterministic-random.mjs";

const PALETTES = Object.freeze({
  midnight_gold: Object.freeze({
    "--hf-bg": "#07111f",
    "--hf-surface": "#101d2f",
    "--hf-surface-strong": "#172a42",
    "--hf-text": "#f7f5ef",
    "--hf-muted": "#b8c0cc",
    "--hf-accent": "#d9a441",
    "--hf-accent-soft": "#f3d38b",
    "--hf-border": "rgba(217,164,65,.42)",
  }),
});
const TYPOGRAPHY = Object.freeze({editorial_sans: Object.freeze({"--hf-font": '"Noto Sans SC", sans-serif'})});
const DENSITY = Object.freeze({
  airy: Object.freeze({"--hf-gap": "36px", "--hf-pad": "72px"}),
  balanced: Object.freeze({"--hf-gap": "26px", "--hf-pad": "56px"}),
  dense: Object.freeze({"--hf-gap": "18px", "--hf-pad": "42px"}),
});
const MOTION = Object.freeze({
  low: Object.freeze({"--hf-motion-distance": "18px"}),
  medium: Object.freeze({"--hf-motion-distance": "36px"}),
  high: Object.freeze({"--hf-motion-distance": "54px"}),
});
const IMAGE_FIT = Object.freeze({contain: "contain", cover: "cover", smart_crop: "cover"});
const PROFILES = Object.freeze({
  editorial_clean: Object.freeze({bg: "#f7f4ed", surface: "#ffffff", text: "#17212b", accent: "#315b8a", border: "rgba(49,91,138,.28)", shadow: "0 18px 48px rgba(23,33,43,.14)", texture: "none"}),
  commercial_energy: Object.freeze({bg: "#10122a", surface: "#1e2360", text: "#ffffff", accent: "#ff6b35", border: "rgba(255,107,53,.52)", shadow: "0 22px 64px rgba(255,107,53,.22)", texture: "grain_subtle"}),
  premium_dark: Object.freeze({bg: "#07111f", surface: "#101d2f", text: "#f7f5ef", accent: "#d9a441", border: "rgba(217,164,65,.42)", shadow: "0 24px 80px rgba(0,0,0,.30)", texture: "none"}),
  warm_lifestyle: Object.freeze({bg: "#fff5e8", surface: "#fffdfa", text: "#49352c", accent: "#b9603d", border: "rgba(185,96,61,.30)", shadow: "0 16px 44px rgba(106,65,45,.16)", texture: "paper_subtle"}),
});

export const THEME_CONTRACT = Object.freeze({
  palette_id: Object.freeze(Object.keys(PALETTES)),
  typography_id: Object.freeze(Object.keys(TYPOGRAPHY)),
  density: Object.freeze(Object.keys(DENSITY)),
  motion_energy: Object.freeze(Object.keys(MOTION)),
  image_fit: Object.freeze(Object.keys(IMAGE_FIT)),
  radius: Object.freeze(["soft"]),
  border: Object.freeze(["subtle"]),
  shadow: Object.freeze(["editorial"]),
  spacing: Object.freeze(["responsive"]),
  texture: Object.freeze(["none"]),
  theme_profile_id: Object.freeze(Object.keys(PROFILES)),
});

function resolveLegacyTheme(theme) {
  if (!theme || typeof theme !== "object" || Array.isArray(theme)) throw new Error("theme_invalid");
  const required = ["palette_id", "typography_id", "density", "motion_energy", "image_fit"];
  if (Object.keys(theme).some((key) => !required.includes(key)) || required.some((key) => typeof theme[key] !== "string")) {
    throw new Error("theme_token_unknown");
  }
  const palette = PALETTES[theme.palette_id];
  const typography = TYPOGRAPHY[theme.typography_id];
  const density = DENSITY[theme.density];
  const motion = MOTION[theme.motion_energy];
  const imageFit = IMAGE_FIT[theme.image_fit];
  if (!palette || !typography || !density || !motion || !imageFit) throw new Error("theme_token_unknown");
  return Object.freeze({
    ...palette,
    ...typography,
    ...density,
    ...motion,
    "--hf-image-fit": imageFit,
    "--hf-radius": "28px",
    "--hf-shadow": "0 24px 80px rgba(0,0,0,.30)",
  });
}

function resolveProfileTheme({profileId, intent, variationSeed}) {
  if (!intent || typeof intent !== "object" || Array.isArray(intent)) throw new Error("theme_intent_invalid");
  const profile = PROFILES[profileId];
  if (!profile) throw new Error("theme_profile_unknown");
  parseVariationSeed(variationSeed);
  const density = {minimal: "airy", balanced: "balanced", dense: "dense"}[intent.density];
  const motionDistance = {low: "18px", medium: "36px", high: "54px"}[intent.motion_energy];
  const imageFit = IMAGE_FIT[intent.image_fit];
  if (!density || !motionDistance || !imageFit || !["low", "medium", "high"].includes(intent.decoration_intensity)) throw new Error("theme_intent_invalid");
  const random = createDeterministicRandom(variationSeed);
  const typeScale = ["0.960", "1.000", "1.040"][random.nextUint32() % 3];
  const gap = {airy: ["34px", "38px"], balanced: ["24px", "28px"], dense: ["16px", "20px"]}[density][random.nextUint32() % 2];
  const radius = ["18px", "22px", "26px"][random.nextUint32() % 3];
  return Object.freeze({
    "--hf-theme-profile": profileId, "--hf-bg": profile.bg, "--hf-surface": profile.surface,
    "--hf-text": profile.text, "--hf-accent": profile.accent, "--hf-font": '"Noto Sans SC", sans-serif',
    "--hf-type-scale": typeScale, "--hf-gap": gap, "--hf-radius": radius, "--hf-border": profile.border,
    "--hf-shadow": profile.shadow, "--hf-texture": profile.texture, "--hf-density": density,
    "--hf-motion-distance": motionDistance, "--hf-image-fit": imageFit,
  });
}

export function resolveTheme(theme) {
  if (theme && typeof theme === "object" && "profileId" in theme) return resolveProfileTheme(theme);
  return resolveLegacyTheme(theme);
}
