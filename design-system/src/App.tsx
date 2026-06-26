import { color, font } from './tokens'
import { Button } from './components/Button'
import { GlassCard } from './components/GlassCard'
import { TiltCard } from './components/TiltCard'
import { Reveal } from './components/Reveal'
import { StatCard } from './components/StatCard'
import { Chip } from './components/Chip'
import { Tag } from './components/Tag'
import { DataFlow } from './components/DataFlow'
import { GlowHero } from './components/GlowHero'

const sec: React.CSSProperties = { maxWidth: 1180, margin: '0 auto', padding: '64px 32px' }
const h2: React.CSSProperties = { fontFamily: font.sans, fontSize: 30, fontWeight: 850, color: color.txt, margin: '0 0 8px' }
const sub: React.CSSProperties = { color: color.txtDim, margin: '0 0 30px', fontSize: 15 }

export default function App() {
  return (
    <div style={{ background: color.bg, color: color.txt, fontFamily: font.sans, minHeight: '100vh' }}>
      {/* 1) WebGL 粒子 Hero */}
      <GlowHero
        eyebrow="黄雀 AI · 设计系统"
        title={<>评论区获客，<span style={{ color: color.gold }}>AI 内容</span>成交。</>}
        subtitle="WebGL 粒子 · 玻璃拟态 · 3D 倾斜 · 滚动入场 · 数据流——黄雀这套高级暗色质感，全是可复用组件。"
      >
        <Button variant="gold" size="lg">进入工作台 →</Button>
        <Button variant="ghost" size="lg">看演示</Button>
      </GlowHero>

      {/* 2) 玻璃拟态 + 数据流 */}
      <div style={{ position: 'relative', overflow: 'hidden', background: `linear-gradient(180deg, ${color.bg}, ${color.panel})` }}>
        <DataFlow lines={6} perLine={3} />
        <div style={sec}>
          <Reveal><h2 style={h2}>玻璃拟态 · 数据流</h2></Reveal>
          <Reveal delay={0.08}><p style={sub}>半透明玻璃卡叠在流动的金色数据线上，科技感拉满。</p></Reveal>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 18 }}>
            {['关键词获客', 'AI 作图', '数字人口播'].map((t, i) => (
              <Reveal key={t} delay={i * 0.1} from="up">
                <GlassCard>
                  <Tag tone="gold">{['意图过滤', 'gpt-image-2', 'Seedance'][i]}</Tag>
                  <div style={{ fontSize: 19, fontWeight: 800, margin: '12px 0 6px' }}>{t}</div>
                  <div style={{ fontSize: 13.5, color: color.txtDim }}>评论区扒精准客户 / 一键出图 / 真人对口型，结果即资产。</div>
                </GlassCard>
              </Reveal>
            ))}
          </div>
        </div>
      </div>

      {/* 3) 3D 倾斜卡 */}
      <div style={sec}>
        <Reveal><h2 style={h2}>3D 倾斜卡</h2></Reveal>
        <Reveal delay={0.08}><p style={sub}>鼠标在卡上滑动，卡片随光标做透视倾斜，金色高光跟随。</p></Reveal>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 18 }}>
          {[0, 1, 2].map((i) => (
            <TiltCard key={i}>
              <Tag tone={(['cyan', 'gold', 'green'] as const)[i]}>{['美业', '电商', '品牌'][i]}</Tag>
              <div style={{ fontSize: 18, fontWeight: 800, margin: '12px 0 6px' }}>{['科技焕肤主视觉', '矩阵分发图', '品牌升级 VI'][i]}</div>
              <div style={{ fontSize: 13, color: color.txtDim }}>悬停我，感受 3D。</div>
              <div style={{ marginTop: 16 }}><Button variant="soft" size="sm">做同款</Button></div>
            </TiltCard>
          ))}
        </div>
      </div>

      {/* 4) 指标卡 + chips */}
      <div style={{ ...sec, paddingTop: 0 }}>
        <Reveal><h2 style={h2}>指标卡 · 筛选 chip</h2></Reveal>
        <Reveal delay={0.08}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 22 }}>
            {['全部', '美业', '数字人', '电商', '节日', '品牌'].map((c, i) => (
              <Chip key={c} active={i === 0}>{c}</Chip>
            ))}
          </div>
        </Reveal>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14 }}>
          <Reveal from="up"><StatCard icon="💬" iconTone="cyan" value="1,268" label="客户群消息" change="▲ 较昨日 19.8%" /></Reveal>
          <Reveal from="up" delay={0.06}><StatCard icon="📋" iconTone="blue" value="32" label="今日任务" change="▲ 较昨日 12.5%" /></Reveal>
          <Reveal from="up" delay={0.12}><StatCard icon="💰" iconTone="gold" accent value="¥1,280" label="今日成本" change="▼ 较昨日 8.2%" /></Reveal>
          <Reveal from="up" delay={0.18}><StatCard icon="⚠️" iconTone="red" value={<>3.2<span style={{ fontSize: 15, color: color.gold }}>%</span></>} label="失败率" change="▲ 较昨日 1.4%" changeBad /></Reveal>
        </div>
      </div>

      <footer style={{ borderTop: `1px solid ${color.lineSoft}`, padding: '28px 32px', textAlign: 'center', color: color.txtFaint, fontSize: 12.5 }}>
        黄雀 AI 设计系统 · React + TS + Framer Motion + Three.js/R3F · /design-sync 可同步给 Claude Design
      </footer>
    </div>
  )
}
