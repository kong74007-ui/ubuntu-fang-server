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
});

export function resolveTheme(theme) {
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
