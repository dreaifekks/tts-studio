import { useEffect, useMemo, useRef, useState } from 'react'
import { getJSON, request } from '../api/client'
import type { Ref, RefList } from '../api/types'
import { Modal, PageHeading, PlayButton, Select, SortControl } from '../components'
import { useSortPref } from '../hooks'
import {
  characterName, fmt, goToGenerate, isValidPrimaryRefDuration, loadCharacterPrefs, navigate, patchCharacterPrefs,
  perform, playReference, queueTag, setActiveCharacter, setStatus, setTagShortcuts, useStore,
} from '../store'

export function ReferencesPage({ character: propChar, embedded = false }: { character?: string; embedded?: boolean }) {
  const s = useStore()
  const models = s.models
  const connected = s.connected
  const character = propChar ?? s.params.character
  const [search, setSearch] = useState('')
  const [emotion, setEmotion] = useState('')
  const [minimum, setMinimum] = useState(0)
  const [maximum, setMaximum] = useState(15)
  const [refs, setRefs] = useState<Ref[]>([])
  const refsRef = useRef<Ref[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [savingDefaultId, setSavingDefaultId] = useState<string | null>(null)
  const [sort, setSort] = useSortPref('refs', { sort: '', order: 'asc' })
  const listRef = useRef<HTMLDivElement>(null)
  const slugs = models.map((m) => m.slug)

  const charInfo = models.find((m) => m.slug === character)
  const defaultRefId = s.characterPrefs[character]?.default_ref ?? charInfo?.prefs?.default_ref ?? charInfo?.default_ref ?? null

  const mapping = useMemo(() => {
    const keys = s.tagKeys.split(',').map((k) => k.trim().toLowerCase())
    const names = s.tagNames.split(',').map((n) => n.trim())
    return keys.map((k, i) => [k, names[i]] as const).filter(([k, n]) => k && n)
  }, [s.tagKeys, s.tagNames])

  const filtered = useMemo(() => refs.filter((r) =>
    (!search || r.text.toLowerCase().includes(search.toLowerCase()) || r.id.includes(search))
    && (!emotion || r.tags.includes(emotion)) && r.duration >= minimum && r.duration <= maximum), [refs, search, emotion, minimum, maximum])

  const reloadSeq = useRef(0)
  const reload = async (keepSelection = false) => {
    const n = ++reloadSeq.current
    const list = await getJSON<RefList>('/refs', { query: { character, sort: sort.sort, order: sort.order } })
    if (n !== reloadSeq.current) return
    refsRef.current = list.refs
    setRefs(list.refs)
    if (!keepSelection) setSelected(list.refs[0]?.id ?? null)
  }
  useEffect(() => {
    if (!connected || !character) return
    void perform(() => reload())
    void loadCharacterPrefs(character, { applyToComposer: false }).catch(() => {})
  }, [character, connected, sort]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selected) return
    listRef.current?.querySelector(`[data-id="${CSS.escape(selected)}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [selected])

  const choose = (id: string | null) => {
    setSelected(id)
    const ref = refsRef.current.find((r) => r.id === id)
    if (ref) playReference(ref)
  }
  const setRef = (updated: Ref) => {
    refsRef.current = refsRef.current.map((r) => (r.id === updated.id ? updated : r))
    setRefs(refsRef.current)
  }
  const applyTag = (tag: string) => {
    const ids = filtered.map((r) => r.id)
    const i = ids.indexOf(selected ?? '')
    if (i < 0) return
    const ref = filtered[i]
    choose(ids[i + 1] ?? null)
    void queueTag(() => refsRef.current, setRef, ref, tag)
  }
  const onKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest('input, textarea, select, button')) return
    if (e.metaKey || e.ctrlKey || e.altKey) return
    const m = mapping.find(([k]) => k === e.key.toLowerCase())
    if (m) { applyTag(m[1]); e.preventDefault(); return }
    const ids = filtered.map((r) => r.id)
    const i = ids.indexOf(selected ?? '')
    if (e.key === 'ArrowDown' && i < ids.length - 1) { choose(ids[i + 1]); e.preventDefault() }
    if (e.key === 'ArrowUp' && i > 0) { choose(ids[i - 1]); e.preventDefault() }
  }

  const makeDefaultRef = (ref: Ref) => {
    setSavingDefaultId(ref.id)
    void perform(async () => {
      await patchCharacterPrefs(character, { default_ref: ref.id })
      setStatus(`已将参考设为 ${characterName(character)} 的默认参考`)
    }).finally(() => setSavingDefaultId(null))
  }

  return (
    <div className={embedded ? 'stack' : 'page'}>
      {!embedded && <PageHeading>用标签整理情绪；选中一条后，自动试听，按快捷键打标并试听下一条。</PageHeading>}
      <div className="toolbar wrap">
        {!embedded && (
          <Select label="角色" value={character} options={slugs.map((v) => ({ value: v, label: characterName(v) }))} onChange={setActiveCharacter} />
        )}
        <input className="grow" placeholder="搜索原文或 ID" aria-label="搜索原文或 ID" value={search} onChange={(e) => setSearch(e.target.value)} />
        <input className="w110" placeholder="情绪筛选" aria-label="情绪筛选" value={emotion} onChange={(e) => setEmotion(e.target.value)} />
        <div className="row gap">
          <input className="w70" type="number" inputMode="decimal" aria-label="最短秒" value={minimum} onChange={(e) => setMinimum(Number(e.target.value))} />
          <span>–</span>
          <input className="w70" type="number" inputMode="decimal" aria-label="最长秒" value={maximum} onChange={(e) => setMaximum(Number(e.target.value))} />
          <span className="muted">秒</span>
        </div>
        <SortControl value={sort} onChange={setSort} options={[
          { value: '', label: '索引顺序' }, { value: 'id', label: 'ID' }, { value: 'duration', label: '时长' },
          { value: 'text', label: '原文' }, { value: 'character', label: '角色' },
        ]} />
        <button onClick={() => setUploading(true)}>⇪ 上传参考</button>
      </div>
      <div className="row between wrap tag-keys">
        <div className="row gap wrap">
          {mapping.map(([k, n]) => (
            <button key={k} className="tag-key" title="添加标签并选择下一条" onClick={() => { applyTag(n); listRef.current?.focus({ preventScroll: true }) }}>
              <span className="desktop-only kbd">{k.toUpperCase()}</span> {n}
            </button>
          ))}
        </div>
        <span className="muted">{filtered.length.toLocaleString()} 条</span>
      </div>
      <div className="ref-list" ref={listRef} tabIndex={0} onKeyDown={onKey} aria-label="参考列表">
        {filtered.map((r) => {
          const validPrimary = isValidPrimaryRefDuration(r.duration)
          const isDef = defaultRefId === r.id
          return (
            <div key={r.id} data-id={r.id} className={`ref-row ${r.id === selected ? 'selected' : ''}`}
              onClick={() => { choose(r.id); listRef.current?.focus({ preventScroll: true }) }}>
              <PlayButton id={'ref-' + r.id} label={`试听参考 ${r.id}`}
                onClick={() => { if (selected === r.id) playReference(r, false); else choose(r.id) }} />
              <div className="ref-main">
                <div className="clamp2">{r.text}</div>
                <div className="row gap wrap muted small">
                  <span className="mono">{r.id} · {fmt(r.duration, 1)} 秒 · {r.lang}</span>
                  {isDef && <span className="badge acc">默认参考</span>}
                  {!validPrimary && <span className="warn">超出 3–10 秒，不可作主参考</span>}
                </div>
              </div>
              <div className="ref-tags">
                {r.tags.map((t) => (
                  <button key={t} className="chip" aria-label={`移除标签 ${t}`}
                    onClick={(e) => { e.stopPropagation(); void queueTag(() => refsRef.current, setRef, r, t, true) }}>
                    ⊗ {t}
                  </button>
                ))}
              </div>
              <div className="row gap ref-actions">
                <button className="small-btn" disabled={!validPrimary || isDef || savingDefaultId === r.id}
                  title={!validPrimary ? '仅 3–10 秒参考可设为默认' : undefined}
                  onClick={(e) => { e.stopPropagation(); makeDefaultRef(r) }}>
                  {isDef ? '已为默认' : savingDefaultId === r.id ? '保存中…' : '设为默认'}
                </button>
                <button className="small-btn primary" disabled={!validPrimary}
                  title={!validPrimary ? '仅 3–10 秒参考可作为主参考合成' : undefined}
                  onClick={(e) => { e.stopPropagation(); void perform(() => goToGenerate({ character, refId: r.id })) }}>
                  去合成
                </button>
              </div>
            </div>
          )
        })}
        {!filtered.length && <div className="empty muted">没有符合条件的参考。</div>}
      </div>
      <details className="disclosure">
        <summary>自定义快捷键（与设置同步）</summary>
        <div className="row gap wrap">
          <label className="field grow"><span>按键（逗号分隔）</span><input value={s.tagKeys} onChange={(e) => setTagShortcuts(e.target.value, s.tagNames)} /></label>
          <label className="field grow"><span>对应标签（逗号分隔）</span><input value={s.tagNames} onChange={(e) => setTagShortcuts(s.tagKeys, e.target.value)} /></label>
          <button className="ghost small-btn" onClick={() => navigate('settings', 'general')}>前往通用设置</button>
        </div>
      </details>
      {uploading && <UploadRef character={character} onClose={() => setUploading(false)} onDone={() => perform(() => reload(true))} />}
    </div>
  )
}

function UploadRef({ character, onClose, onDone }: { character: string; onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [text, setText] = useState('')
  const [lang, setLang] = useState('ja')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    const form = new FormData()
    form.set('audio', file)
    form.set('text', text)
    form.set('lang', lang)
    form.set('character', character)
    try {
      const res = await request('/refs/upload', { method: 'POST', form })
      const ref = (await res.json()) as Ref
      setStatus(`参考已上传：${ref.id}`)
      onDone()
      onClose()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  return (
    <Modal title="上传参考音频" onClose={onClose} closeLabel={null}
      footer={<><span className="spacer" /><button onClick={onClose}>取消</button><button className="primary" disabled={busy || !file || !text} onClick={() => void submit()}>{busy ? '上传中…' : '上传'}</button></>}>
      <div className="stack">
        <label className="field"><span>音频文件</span><input type="file" accept="audio/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
        <label className="field"><span>音频中的原文</span><textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} /></label>
        <Select label="语言" value={lang} options={[{ value: 'ja', label: '日语' }, { value: 'zh', label: '中文' }, { value: 'en', label: '英语' }]} onChange={setLang} />
        <div className="muted small">角色：{characterName(character)}</div>
        {error && <div className="error-text" role="alert">{error}</div>}
      </div>
    </Modal>
  )
}
