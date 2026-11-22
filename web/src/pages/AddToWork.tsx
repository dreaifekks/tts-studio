import { useEffect, useState } from 'react'
import { getJSON } from '../api/client'
import type { AudioItem, Work } from '../api/types'
import { Modal, Segmented, Select } from '../components'
import { addCandidateToWork, characterName, loadWorks, useStore } from '../store'

type Mode = '新建一行' | '挂到已有行'

export function AddToWork({ item, onClose }: { item: AudioItem; onClose: () => void }) {
  const works = useStore((s) => s.works)
  const current = useStore((s) => s.work)
  const workBusy = useStore((s) => s.workBusy || s.workSaving)
  const [workId, setWorkId] = useState(current?.id ?? '')
  const [title, setTitle] = useState('新作品')
  const [mode, setMode] = useState<Mode>('新建一行')
  const [target, setTarget] = useState<Work | null>(null)
  const [lineId, setLineId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { loadWorks().catch((e) => setError(String(e.message ?? e))) }, [])
  useEffect(() => {
    setLineId('')
    setTarget(null)
    if (!workId) { setMode('新建一行'); return }
    let cancelled = false
    // 当前已打开的作品用包含未保存编辑的本地行列表
    const s = useStore.getState()
    const load = s.work?.id === workId ? Promise.resolve(s.work) : getJSON<Work>(`/works/${encodeURIComponent(workId)}`)
    load.then((w) => { if (!cancelled) { setTarget(w); setLineId(w.lines[0]?.id ?? '') } })
      .catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [workId])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await addCandidateToWork(item, workId || null, mode === '挂到已有行' ? lineId : null, title.trim())
      onClose()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <Modal title="加入作品" onClose={onClose} closeLabel={null}
      footer={<><span className="spacer" /><button onClick={onClose}>取消</button>
        <button className="primary" disabled={busy || workBusy || (mode === '挂到已有行' && !lineId)} onClick={() => void submit()}>
          {busy ? '加入中…' : '加入并选用'}
        </button></>}>
      <div className="stack">
        <div className="muted clamp3">{item.text}</div>
        <Select label="作品" value={workId}
          options={[{ value: '', label: '新建作品' }, ...works.map((w) => ({ value: w.id, label: w.title }))]} onChange={setWorkId} />
        {!workId && <label className="field"><span>新作品名称</span><input value={title} onChange={(e) => setTitle(e.target.value)} /></label>}
        <Segmented label="加到哪一行" value={mode} disabled={!workId} onChange={setMode}
          options={(['新建一行', '挂到已有行'] as Mode[]).map((m) => ({ value: m, label: m }))} />
        {mode === '挂到已有行' && workId ? (
          <>
            <Select label="目标行" value={lineId}
              options={[{ value: '', label: '请选择' }, ...(target?.lines ?? []).map((l, i) => ({ value: l.id, label: `${i + 1}. ${characterName(l.character)} · ${l.text.slice(0, 40)}` }))]}
              onChange={setLineId} />
            <div className="muted small">这条音频会加入该行候选并立即选用，行的台词与参数保持原样，字幕按行台词生成。</div>
          </>
        ) : <div className="muted small">直接沿用这条候选的台词、角色和参数，并选用此音频，无需重新生成。</div>}
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </Modal>
  )
}
