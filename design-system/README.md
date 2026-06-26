# 黄雀 AI · 设计系统（huangque-design-system）

React + TypeScript + Framer Motion + Three.js/@react-three/fiber。暗色"作战台 + 黄雀金"。
供 **Claude Design `/design-sync`** 同步：它读 `src/tokens.ts` 与 `src/components/*` 的 React 组件。

## 跑起来看
```bash
cd design-system
npm install
npm run dev      # http://localhost:5180  —— premium 效果 demo
```

## 同步给 Claude Design
```bash
cd design-system
claude
› /design-sync
```
完成后出现在你组织的「Design systems for everyone」里。

## 组件
| 组件 | 说明 |
|---|---|
| `tokens` | 颜色/字体/圆角/间距/阴影/渐变（与站点 shell.css 同源）|
| `Button` | gold / ghost / soft |
| `Card` / `GlassCard` | 面板卡 / **玻璃拟态**（backdrop-blur + 高光边）|
| `TiltCard` | **3D 倾斜卡**（Framer Motion，光标透视 + 高光跟随）|
| `Reveal` | **滚动入场**（Framer whileInView，含 reduced-motion）|
| `StatCard` | 指标卡（图标徽章 + 数值 + 涨跌）|
| `Chip` / `Tag` | 筛选 chip / 语义标签 |
| `ParticleField` | 金色粒子**星网**（canvas，鼠标交互，轻量）|
| `DataFlow` | **数据流**（金色光点沿正弦线流动）|
| `WebGLParticles` | **WebGL 3D 粒子球**（Three.js / R3F）|
| `GlowHero` | 首屏（WebGL 粒子背景 + 径向金光 + 标题/CTA）|

所有动效尊重 `prefers-reduced-motion`。
