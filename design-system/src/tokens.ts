/**
 * 黄雀 AI · 设计 token
 * 暗色"作战台" + 黄雀金。与站点 shell.css / DESIGN.md 同源。
 * Claude Design /design-sync 读取这里的 token。
 */
export const color = {
  bg: '#070b13',
  panel: '#0c1220',
  panel2: '#111a2b',
  surface: '#101827',
  surface2: '#0d1422',
  line: '#1d2a3e',
  lineSoft: '#15202f',
  lineStrong: '#27374e',
  txt: '#eaf1fa',
  txtDim: '#94a4bb',
  txtFaint: '#586a82',
  gold: '#e7b24c',
  goldSoft: '#f2c869',
  cyan: '#2dd4bf',
  blue: '#46b4ff',
  green: '#2bd576',
  warn: '#e0a93c',
  red: '#ff5566',
} as const

export const font = {
  sans: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
  mono: 'ui-monospace, "SF Mono", Menlo, "JetBrains Mono", Consolas, monospace',
} as const

export const radius = { sm: 8, md: 11, lg: 14, xl: 18, xxl: 22, pill: 999 } as const

export const space = { xs: 6, sm: 10, md: 14, lg: 18, xl: 24, xxl: 32 } as const

export const shadow = {
  card: '0 14px 36px rgba(0,0,0,.45)',
  gold: '0 8px 24px rgba(231,178,76,.26)',
  pop: '0 16px 44px rgba(0,0,0,.5)',
} as const

export const gradient = {
  gold: `linear-gradient(135deg, ${color.goldSoft}, ${color.gold})`,
  goldGlow: 'linear-gradient(120deg, rgba(231,178,76,.16), rgba(45,212,191,.07))',
} as const

export const tokens = { color, font, radius, space, shadow, gradient } as const
export type Tokens = typeof tokens
