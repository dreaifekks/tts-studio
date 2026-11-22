import { useState } from 'react'
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { AudioItem, Step } from '../api/types'
import { Metric, Modal } from '../components'
import { fmt } from '../store'

function StepDetail({ step }: { step: Step }) {
  return (
    <div className="step-detail">
      <div className="mono">第 {step.step} 步 · token {step.token} · 采样概率 {fmt(step.p, 4)} · 原始概率 {fmt(step.p_raw, 4)} · 熵 {fmt(step.entropy, 3)} bit</div>
      <div className="mono small">Top-5：{step.top5.map((t) => `${t.token} (${fmt(t.p, 3)})`).join('   ')}</div>
    </div>
  )
}

export function DebugPanel({ item, onClose }: { item: AudioItem; onClose: () => void }) {
  const segments = item.segments ?? []
  const [index, setIndex] = useState(0)
  const [hover, setHover] = useState<Step | null>(null)
  const [melError, setMelError] = useState(false)
  const seg = segments[index]
  const steps = seg?.steps ?? []
  return (
    <Modal title="Debug · 采样与频谱" onClose={onClose} wide>
      <div className="muted small mono selectable">{item.engine} · {item.id}</div>
      {!segments.length ? (
        <div className="empty"><div className="empty-title">没有采样轨迹</div><div className="muted">这条音频生成时没有记录采样信息。</div></div>
      ) : (
        <>
          <div className="row gap wrap">
            <span>文本分段</span>
            <div className="segmented">
              {segments.map((_, i) => (
                <button key={i} className={i === index ? 'active' : ''} onClick={() => { setIndex(i); setHover(null) }}>第 {i + 1} 段</button>
              ))}
            </div>
          </div>
          {seg && (
            <>
              <div className="metrics">
                <Metric title="EOS 位置" value={seg.eos_step === null || seg.eos_step === undefined ? '未出现' : String(seg.eos_step)} />
                <Metric title="每音素 token" value={fmt(seg.tokens_per_phone)} />
                <Metric title="相对参考" value={`${fmt(seg.ratio_vs_ref)}×`} />
                <Metric title="终止方式" value={seg.hit_max_len ? '达到上限' : seg.eos_by === 'sampled' ? '采样 EOS' : '最高概率 EOS'} />
              </div>
              {steps.length ? (
                <div className="chart" onMouseLeave={() => setHover(null)}>
                  <div className="axis-caption">熵（bit）</div>
                  <ResponsiveContainer width="100%" height={240}>
                    <LineChart data={steps} margin={{ top: 20, right: 16, bottom: 16, left: 0 }}
                      onMouseMove={(st) => {
                        const i = Number(st?.activeTooltipIndex)
                        setHover(Number.isFinite(i) ? steps[i] ?? null : null)
                      }}>
                      <CartesianGrid stroke="#3a3a3c" strokeDasharray="3 3" />
                      <XAxis dataKey="step" type="number" domain={['dataMin', 'dataMax']} stroke="#8e8e93"
                        label={{ value: '步数', position: 'insideBottomLeft', offset: -8, fill: '#8e8e93' }} />
                      <YAxis stroke="#8e8e93" width={44} />
                      <Tooltip
                        cursor={{ stroke: '#8e8e93' }}
                        content={({ active, payload }) => active && payload?.[0] ? (
                          <div className="chart-tip"><StepDetail step={payload[0].payload as Step} /></div>
                        ) : null} />
                      {seg.eos_step !== null && seg.eos_step !== undefined && (
                        <ReferenceLine x={seg.eos_step} stroke="#ff9f0a" strokeWidth={2} label={{ value: 'EOS', position: 'top', fill: '#ff9f0a' }} />
                      )}
                      <Line type="linear" dataKey="entropy" stroke="#3b82f6" strokeWidth={2} dot={false} isAnimationActive={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : <div className="muted">本段没有逐步数据。</div>}
              {hover ? <StepDetail step={hover} /> : <div className="muted">将鼠标移到曲线上（手机上点按曲线）查看 token、采样概率及 Top-5。</div>}
            </>
          )}
        </>
      )}
      <hr />
      {!item.mel_url ? <div className="muted">本条没有 mel 频谱。</div>
        : melError ? <div className="error-text">频谱图片加载失败。</div>
        : <div className="mel"><img src={item.mel_url} alt="mel 频谱" onError={() => setMelError(true)} /></div>}
    </Modal>
  )
}
