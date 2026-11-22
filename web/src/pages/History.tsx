import { useEffect, useState } from 'react'
import { getJSON } from '../api/client'
import type { AudioItem, AudioPage } from '../api/types'
import { CandidateCard, EngineSelect, Modal, PageHeading, Select, SortControl } from '../components'
import { useSortPref } from '../hooks'
import { characterName, perform, reuseAudioParams, showError, useStore } from '../store'

const PAGE = 30
const historySorts = [
  { value: 'created', label: '生成时间' }, { value: 'score', label: '总分' }, { value: 'duration', label: '时长' },
  { value: 'character', label: '角色' }, { value: 'engine', label: '引擎' }, { value: 'rating', label: '评分' },
  { value: 'reviewed_at', label: '评价时间' },
]
const sortKeys = (v: unknown): unknown =>
  Array.isArray(v) ? v.map(sortKeys)
    : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys((v as Record<string, unknown>)[k])]))
      : v

export function HistoryPanel({ embedded = false, onAddToWork }: { embedded?: boolean; onAddToWork?: (item: AudioItem) => void }) {
  const models = useStore((s) => s.models)
  const connected = useStore((s) => s.connected)
  const history = useStore((s) => s.history)
  const workBusy = useStore((s) => s.workBusy || s.workSaving)
  const [character, setCharacter] = useState('')
  const [engine, setEngine] = useState('')
  const [reviewed, setReviewed] = useState('')
  const [minRating, setMinRating] = useState(0)
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useSortPref('history', { sort: 'created', order: 'desc' })
  const [loadedKey, setLoadedKey] = useState('')
  const [meta, setMeta] = useState<AudioItem | null>(null)
  const slugs = models.map((m) => m.slug)

  const queryKey = `${character}|${engine}|${reviewed}|${minRating}|${search}|${offset}|${sort.sort}|${sort.order}`
  const loading = !!connected && loadedKey !== queryKey

  const filter = <T,>(setter: (v: T) => void) => (v: T) => { setter(v); setOffset(0) }

  useEffect(() => {
    if (!connected) return
    const ctrl = new AbortController()
    const key = queryKey
    const t = setTimeout(() => {
      const query: Record<string, string | number> = { limit: PAGE, offset, sort: sort.sort, order: sort.order }
      if (character) query.character = character
      if (engine) query.engine = engine
      if (search) query.q = search
      if (minRating > 0) query.min_rating = minRating
      else if (reviewed) query.reviewed = reviewed
      getJSON<AudioPage>(minRating > 0 ? '/reviews' : '/history', { query, signal: ctrl.signal })
        .then((page) => { if (!ctrl.signal.aborted) { useStore.setState({ history: page }); setLoadedKey(key) } })
        .catch((e) => { if (!ctrl.signal.aborted) { showError(e); setLoadedKey(key) } })
    }, 200)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [connected, queryKey, character, engine, reviewed, minRating, search, offset, sort])

  return (
    <div className={embedded ? 'stack history-embedded' : 'page'}>
      {!embedded && <PageHeading>找回生成过的声音，补充评价，复查每次采样参数。</PageHeading>}
      <div className="toolbar wrap">
        <Select label="角色" value={character} options={[{ value: '', label: '全部' }, ...slugs.map((v) => ({ value: v, label: characterName(v) }))]} onChange={filter(setCharacter)} />
        <EngineSelect label="引擎" value={engine} allowAll onChange={filter(setEngine)} />
        <Select label="评价" value={reviewed} disabled={minRating > 0}
          options={[{ value: '', label: '全部' }, { value: 'true', label: '已评价' }, { value: 'false', label: '未评价' }]} onChange={filter(setReviewed)} />
        <Select label="最低分" value={minRating}
          options={[{ value: 0, label: '不限' }, ...[1, 2, 3, 4, 5].map((v) => ({ value: v, label: `${v} 分` }))]} onChange={filter(setMinRating)} />
        <SortControl value={sort} onChange={(v) => { setSort(v); setOffset(0) }} options={historySorts} />
      </div>
      <input className="full" placeholder="搜索台词" aria-label="搜索台词" value={search} onChange={(e) => { setSearch(e.target.value); setOffset(0) }} />
      <div className="scroll-list">
        {history.items.map((item) => (
          <CandidateCard key={item.id} item={item} showDownload onReuse={() => reuseAudioParams(item)}
            extra={(
              <div className="row gap">
                {onAddToWork && (
                  <button className="small-btn" disabled={workBusy} onClick={(e) => { e.stopPropagation(); onAddToWork(item) }}>加入作品</button>
                )}
                <button className="small-btn" onClick={(e) => { e.stopPropagation(); void perform(async () => setMeta(await getJSON<AudioItem>(`/audio/${encodeURIComponent(item.id)}/meta`))) }}>全部参数</button>
              </div>
            )} />
        ))}
        {!history.items.length && !loading && <div className="empty compact"><div className="empty-title">暂无记录</div><div className="muted">调整筛选，或先生成一组候选。</div></div>}
      </div>
      <div className="row between pager">
        <span className="muted small">共 {history.total} 条 · {history.total ? offset + 1 : 0}–{Math.min(offset + PAGE, history.total)} {loading && '· 加载中…'}</span>
        <div className="row gap">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
          <button disabled={offset + PAGE >= history.total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
        </div>
      </div>
      {meta && (
        <Modal title="生成参数与元数据" onClose={() => setMeta(null)} wide>
          <pre className="json selectable">{JSON.stringify(sortKeys(meta), null, 2)}</pre>
        </Modal>
      )}
    </div>
  )
}

export function HistoryPage() {
  return <HistoryPanel />
}
