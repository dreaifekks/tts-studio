import { useEffect, useRef, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getJSON } from '../api/client'
import type { ReviewStats } from '../api/types'
import { Metric, PageHeading, Segmented, Select } from '../components'
import { scoreHelp } from '../display'
import { characterName, fmt, perform, showError, useStore } from '../store'

const labels: Record<string, string> = { engine: '引擎', character: '角色', ref_id: '参考', weights: '权重组合', temperature: '温度', top_k: 'Top K' }
const pct = (v: number) => `${Math.round(v * 100)}%`
const tooltipStyle = { background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)' }

export function StatsPage({ character: propChar, embedded = false }: { character?: string; embedded?: boolean }) {
  const models = useStore((s) => s.models)
  const connected = useStore((s) => s.connected)
  const storeChar = useStore((s) => s.params.character)
  const activeChar = propChar ?? storeChar
  const [scope, setScope] = useState<'current' | 'all'>('current')
  const [manualChar, setManualChar] = useState('')
  const [dimension, setDimension] = useState('engine')
  const [statsState, setStatsState] = useState<{ query: string; data: ReviewStats } | null>(null)
  const [failedQuery, setFailedQuery] = useState<string | null>(null)
  const seq = useRef(0)
  const slugs = models.map((m) => m.slug)

  const queryCharacter = embedded ? (scope === 'current' ? activeChar : '') : manualChar
  const stats = statsState?.query === queryCharacter ? statsState.data : null
  const failed = failedQuery === queryCharacter
  const load = (signal?: AbortSignal) => {
    setStatsState((cur) => (cur?.query === queryCharacter ? null : cur))
    setFailedQuery((cur) => (cur === queryCharacter ? null : cur))
    return perform(async () => {
      const n = ++seq.current
      try {
        const res = await getJSON<ReviewStats>('/reviews/stats', { query: { character: queryCharacter }, signal })
        if (n === seq.current && !signal?.aborted) {
          setStatsState({ query: queryCharacter, data: res })
          setFailedQuery(null)
        }
      } catch (e) {
        if (n === seq.current && !signal?.aborted) setFailedQuery(queryCharacter)
        throw e
      }
    })
  }
  useEffect(() => {
    if (!connected) return
    const ctrl = new AbortController()
    const n = ++seq.current
    getJSON<ReviewStats>('/reviews/stats', { query: { character: queryCharacter }, signal: ctrl.signal })
      .then((res) => {
        if (n === seq.current && !ctrl.signal.aborted) {
          setStatsState({ query: queryCharacter, data: res })
          setFailedQuery(null)
        }
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) {
          if (n === seq.current) setFailedQuery(queryCharacter)
          showError(e)
        }
      })
    return () => ctrl.abort()
  }, [connected, queryCharacter])

  const groups = stats?.dimensions.find((d) => d.dimension === dimension)?.groups ?? []
  const overall = stats?.overall
  const tags = Object.entries(overall?.tag_ratios ?? {}).sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => ({ key, value: Number(value) }))

  return (
    <div className={embedded ? 'stack' : 'page'}>
      {!embedded && <PageHeading>从已评价的声音里，找出更容易得到好结果的设置。</PageHeading>}
      <div className="toolbar wrap">
        {embedded ? (
          <Segmented label="统计范围" value={scope} onChange={setScope}
            options={[{ value: 'current', label: `当前角色（${characterName(activeChar)}）` }, { value: 'all', label: '全部角色' }]} />
        ) : (
          <Select label="角色" value={manualChar} options={[{ value: '', label: '全部' }, ...slugs.map((v) => ({ value: v, label: characterName(v) }))]} onChange={setManualChar} />
        )}
        <Select label="比较维度" value={dimension}
          options={(stats?.dimensions ?? [{ dimension: 'engine' }]).map((d) => ({ value: d.dimension, label: labels[d.dimension] ?? d.dimension }))}
          onChange={setDimension} />
        <span className="spacer" />
        <button onClick={() => void load()}>↻ 刷新</button>
      </div>
      {!stats ? (
        <div className="muted">{failed ? '统计数据加载失败，请点击刷新重试。' : '正在读取统计…'}</div>
      ) : (
      <>
        {overall && (
          <div className="group metrics summary">
            <Metric title="已评价" value={String(overall.n)} />
            <Metric title="平均评分" value={fmt(overall.avg_rating)} />
            <Metric title="好评占比" value={`${fmt(overall.good_ratio * 100, 1)}%`} />
            <Metric title="自动评分均值" value={fmt(overall.avg_score, 1)} help={scoreHelp(overall.avg_score)} />
          </div>
        )}
        <div className="chart">
          <div className="axis-caption">好评占比</div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={groups} margin={{ top: 16, right: 8, left: 0, bottom: 8 }}>
              <defs>
                <linearGradient id="good" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor="#4f9dff" /><stop offset="1" stopColor="#2f6fe8" />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#8e8e9340" strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="key" stroke="#8e8e93" interval={0} tick={{ fontSize: 12 }} />
              <YAxis domain={[0, 1]} tickFormatter={pct} stroke="#8e8e93" width={44} />
              <Tooltip formatter={(v) => pct(Number(v))} contentStyle={tooltipStyle} cursor={{ fill: '#8e8e9320' }} />
              <Bar dataKey="good_ratio" name="好评占比" fill="url(#good)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>设置</th><th className="num">条数</th><th className="num">平均评分</th><th className="num">好评占比</th><th className="num">自动均分</th></tr></thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.key}>
                  <td className="break">{g.key}</td><td className="num">{g.n}</td><td className="num">{fmt(g.avg_rating)}</td>
                  <td className="num">{pct(g.good_ratio)}</td><td className="num">{fmt(g.avg_score)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <h3>问题标签占比</h3>
        {tags.length ? (
          <div className="chart">
            <ResponsiveContainer width="100%" height={Math.max(120, tags.length * 34 + 30)}>
              <BarChart data={tags} layout="vertical" margin={{ top: 4, right: 16, left: 8, bottom: 4 }}>
                <CartesianGrid stroke="#8e8e9340" strokeDasharray="3 3" horizontal={false} />
                <XAxis type="number" domain={[0, 1]} tickFormatter={pct} stroke="#8e8e93" />
                <YAxis type="category" dataKey="key" stroke="#8e8e93" width={72} />
                <Tooltip formatter={(v) => pct(Number(v))} contentStyle={tooltipStyle} cursor={{ fill: '#8e8e9320' }} />
                <Bar dataKey="value" name="占比" fill="#ff9f0a" radius={[0, 4, 4, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : <div className="muted">还没有问题标签；给候选添加标签后会显示占比。</div>}
      </>
      )}
    </div>
  )
}
