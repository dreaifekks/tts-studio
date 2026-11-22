import { useEffect, useMemo, useRef, useState } from 'react'
import {
  DndContext, KeyboardSensor, PointerSensor, TouchSensor, closestCenter, useSensor, useSensors, type DragEndEvent,
} from '@dnd-kit/core'
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { getJSON, isActive, request } from '../api/client'
import type { Engine, ModelVersion, Ref, RefList, WorkLine } from '../api/types'
import {
  CandidateCard, Confirm, EngineSelect, JobProgress, Modal, PageHeading, PlayButton, Segmented, Select, SortControl,
} from '../components'
import { displaySeconds, versionOption } from '../display'
import {
  addLine, canPick, changeLine, characterName, createWork, deleteWork, engineList, engineSupportsAuxRefs, exportFile,
  fetchVersions, fmt, generateWork, isValidPrimaryRefDuration, loadWorks, needsRegeneration, perform, pick, playAudio,
  playReference, preferredEngine, regenerateLine, removeLine, renderWork, saveWork, setLines, setWorkTitle,
  setWorksSort, showError, stopWork, switchWork, toggleLineSelected, useStore,
} from '../store'

export function WorksPage() {
  const s = useStore()
  const { work, works, workDirty } = s
  const workBusy = s.workBusy || s.workSaving
  const [newTitle, setNewTitle] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [srt, setSrt] = useState<string | null>(null)
  const [versionsByChar, setVersionsByChar] = useState<Record<string, ModelVersion[]>>({})
  const [refsByChar, setRefsByChar] = useState<Record<string, Ref[]>>({})
  const fetchedVersions = useRef(new Set<string>())
  const fetchedRefs = useRef(new Set<string>())

  useEffect(() => { if (s.connected) void perform(loadWorks) }, [s.connected])

  // 为当前作品涉及的角色（以及当前活跃角色）按需缓存模型版本与参考列表
  const neededChars = useMemo(() => {
    const set = new Set<string>()
    if (s.params.character) set.add(s.params.character)
    for (const l of work?.lines ?? []) set.add(l.character)
    return [...set]
  }, [s.params.character, work?.lines])

  useEffect(() => {
    if (!s.connected) return
    for (const slug of neededChars) {
      if (!fetchedVersions.current.has(slug)) {
        fetchedVersions.current.add(slug)
        fetchVersions(slug)
          .then((rows) => setVersionsByChar((m) => ({ ...m, [slug]: rows })))
          .catch(() => { fetchedVersions.current.delete(slug) })
      }
      if (!fetchedRefs.current.has(slug)) {
        fetchedRefs.current.add(slug)
        getJSON<RefList>('/refs', { query: { character: slug } })
          .then((res) => setRefsByChar((m) => ({ ...m, [slug]: res.refs })))
          .catch(() => { fetchedRefs.current.delete(slug) })
      }
    }
  }, [s.connected, neededChars])

  // 尚未拉回版本时返回 undefined（允许 addLine 复用合成页缓存），确认返回空列表时返回 null 走 preferredEngine 兜底
  const defaultVersionFor = (slug: string): ModelVersion | null | undefined => {
    if (!(slug in versionsByChar)) return undefined
    const rows = versionsByChar[slug]
    return rows.find((v) => v.recommended) ?? rows[0] ?? null
  }

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 150, tolerance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )
  const onDragEnd = (e: DragEndEvent) => {
    if (!work || !e.over || e.active.id === e.over.id) return
    const from = work.lines.findIndex((l) => l.id === e.active.id)
    const to = work.lines.findIndex((l) => l.id === e.over!.id)
    setLines(arrayMove(work.lines, from, to))
  }

  const blocked = work ? work.lines.some((l) => needsRegeneration(s, l)) : true
  const render = work?.render
  const showRender = render && !workDirty && !blocked
  const jobs = s.jobs.filter((j) => isActive(j) && j.target === work?.id)

  const handleAddLine = () => addLine(defaultVersionFor(s.params.character))

  return (
    <div className="page">
      <PageHeading>把台词排成一段对话，逐行挑选声音，再渲染成整轨。</PageHeading>
      <div className="split works">
        <div className="col work-list">
          <div className="row gap">
            <input className="grow" placeholder="新作品名称" aria-label="新作品名称" value={newTitle} disabled={workBusy} onChange={(e) => setNewTitle(e.target.value)} />
            <button aria-label="新建作品" disabled={workBusy}
              onClick={() => void perform(async () => { await createWork(newTitle.trim() || '未命名作品'); setNewTitle('') })}>＋</button>
          </div>
          <SortControl value={s.worksSort} onChange={(v) => void perform(() => setWorksSort(v))} options={[
            { value: 'created', label: '创建时间' }, { value: 'updated', label: '修改时间' },
            { value: 'title', label: '标题' }, { value: 'n_lines', label: '行数' },
          ]} />
          <div className="list">
            {works.map((w) => (
              <button key={w.id} disabled={workBusy} className={`list-item ${work?.id === w.id ? 'selected' : ''}`}
                onClick={() => { if (work?.id !== w.id) void perform(() => switchWork(w.id)) }}>
                <div className="strong">{w.title}</div>
                <div className="muted small">{w.n_lines} 行 · {w.n_picked} 行已选</div>
              </button>
            ))}
          </div>
        </div>
        <div className="col editor">
          {!work ? (
            <div className="empty"><div className="empty-title">做一段对话</div><div className="muted">新建作品，然后添加台词。</div></div>
          ) : (
            <>
              <div className="row gap wrap">
                <input className="title-input grow" aria-label="作品名称" value={work.title} disabled={workBusy} onChange={(e) => setWorkTitle(e.target.value)} />
                {workDirty && <span className="warn small">未保存</span>}
                <button disabled={workBusy} onClick={() => void perform(saveWork)}>保存</button>
                <button className="danger" aria-label="删除作品" disabled={workBusy} onClick={() => setConfirmDelete(true)}>🗑</button>
              </div>
              <div className="row gap wrap">
                <button disabled={workBusy} onClick={handleAddLine}>＋ 添加行</button>
                <span className="spacer" />
                <button disabled={workBusy || !s.selectedLines.length} onClick={() => void generateWork([...s.selectedLines])}>生成选中行</button>
                <button disabled={workBusy} onClick={() => void generateWork(null)}>生成全部</button>
                <button className="primary" disabled={workBusy || blocked || workDirty} onClick={() => void renderWork()}>渲染</button>
              </div>
              <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
                <SortableContext items={work.lines.map((l) => l.id)} strategy={verticalListSortingStrategy}>
                  <div className="lines">
                    {work.lines.map((line) => (
                      <LineRow key={line.id} line={line}
                        versions={versionsByChar[line.character] ?? []}
                        refs={refsByChar[line.character] ?? []}
                        defaultVersionFor={defaultVersionFor} />
                    ))}
                    {!work.lines.length && <button className="big" onClick={handleAddLine}>＋ 添加第一行</button>}
                  </div>
                </SortableContext>
              </DndContext>
              {workBusy && <button className="full" onClick={stopWork}>取消任务 / 停止等待</button>}
              {jobs.map((j) => <JobProgress key={j.id} job={j} />)}
              {showRender && (
                <div className="render-bar">
                  <div className="row gap wrap">
                    <button className="primary" onClick={() => playAudio(render.wav_url, render.wav_url, '正在播放整轨')}>▶ 播放整轨</button>
                    <span className="muted mono">{fmt(render.duration)} 秒</span>
                    <button onClick={() => void perform(async () => setSrt(await (await request(render.srt_url)).text()))}>查看 SRT</button>
                    <span className="spacer" />
                    <ExportMenu urls={{ wav: render.wav_url, mp3: render.mp3_url, srt: render.srt_url, lrc: render.lrc_url }} />
                  </div>
                </div>
              )}
              <div className="muted small">拖动行左侧手柄可排序。生成全部会为每行重新生成候选；选中行可单独生成。</div>
            </>
          )}
        </div>
      </div>
      {confirmDelete && <Confirm message="删除这个作品？" confirmLabel="删除作品" onClose={() => setConfirmDelete(false)} onConfirm={() => void perform(deleteWork)} />}
      {srt !== null && <Modal title="SRT 字幕" onClose={() => setSrt(null)} wide><pre className="json selectable">{srt}</pre></Modal>}
    </div>
  )
}

function ExportMenu({ urls }: { urls: Record<'wav' | 'mp3' | 'srt' | 'lrc', string> }) {
  const ref = useRef<HTMLDetailsElement>(null)
  return (
    <details className="menu up" ref={ref}>
      <summary>导出 ▾</summary>
      <div className="menu-list">
        {(['wav', 'mp3', 'srt', 'lrc'] as const).map((ext) => (
          <button key={ext} onClick={() => { if (ref.current) ref.current.open = false; void perform(() => exportFile(urls[ext], ext)) }}>
            {ext.toUpperCase()}…
          </button>
        ))}
      </div>
    </details>
  )
}

function LineRow({ line, versions, refs, defaultVersionFor }: {
  line: WorkLine
  versions: ModelVersion[]
  refs: Ref[]
  defaultVersionFor: (slug: string) => ModelVersion | null | undefined
}) {
  const s = useStore()
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id: line.id, disabled: s.workBusy || s.workSaving })
  const [open, setOpen] = useState(false)
  const [pickingRef, setPickingRef] = useState(false)
  const slugs = s.models.map((m) => m.slug)
  const selected = s.selectedLines.includes(line.id)
  const stale = !!line.picked && needsRegeneration(s, line)
  const update = (patch: Partial<WorkLine>) => changeLine({ ...line, ...patch })
  const disabled = s.workBusy || s.workSaving
  const bestId = line.candidates.find((c) => canPick(s, c, line))?.id

  const matchedRef = line.ref_id ? refs.find((r) => r.id === line.ref_id) : null
  const auxIds = line.aux_ref_ids ?? []
  const baseRefSummary = line.ref_id
    ? `参考：${matchedRef ? matchedRef.text.slice(0, 18) : line.ref_id}`
    : line.emotion
      ? `按情绪随机：${line.emotion}`
      : '默认参考'
  const refSummary = auxIds.length ? `${baseRefSummary} + ${auxIds.length} 辅参` : baseRefSummary

  const matchedVersionId = line.version
    ?? versions.find((v) => v.engine === line.engine && v.gpt === line.gpt && v.sovits === line.sovits)?.id
    ?? ''

  return (
    <div ref={setNodeRef} className={`card line ${isDragging ? 'dragging' : ''}`}
      style={{ transform: CSS.Transform.toString(transform), transition }}>
      <div className="line-row">
        <button ref={setActivatorNodeRef} className="handle" aria-label="拖动排序" {...attributes} {...listeners}>☰</button>
        <input type="checkbox" className="line-check" aria-label="选中行" checked={selected} disabled={disabled}
          onChange={(e) => toggleLineSelected(line.id, e.target.checked)} />
        <Select label="" value={line.character} disabled={disabled} className="w-char"
          options={slugs.map((v) => ({ value: v, label: characterName(v) }))}
          onChange={(v) => {
            const rawDef = defaultVersionFor(v)
            const def = rawDef !== undefined ? rawDef : (s.composerVersions.character === v
              ? s.composerVersions.rows.find((r) => r.recommended) ?? s.composerVersions.rows[0] ?? null
              : null)
            update({
              character: v,
              engine: (def?.engine ?? preferredEngine(s.commonModels, engineList(s))) as Engine,
              version: def?.id ?? null,
              gpt: def?.gpt ?? null,
              sovits: def?.sovits ?? null,
              ref_id: null,
              emotion: null,
              aux_ref_ids: [],
            })
          }} />
        <textarea className="line-text" rows={1} placeholder="台词" aria-label="台词" value={line.text} disabled={disabled}
          onChange={(e) => update({ text: e.target.value })} />
        {versions.length > 0 ? (
          <Select value={matchedVersionId} disabled={disabled} className="w-version"
            options={[
              ...(!matchedVersionId ? [{ value: '', label: `自定 · ${line.engine}` }] : []),
              ...versions.map((v) => ({ value: v.id, label: versionOption(v) })),
            ]}
            onChange={(vid) => {
              const v = versions.find((x) => x.id === vid)
              if (v) {
                const nextEngine = v.engine as Engine
                update({
                  version: v.id,
                  engine: nextEngine,
                  gpt: v.gpt,
                  sovits: v.sovits,
                  ...(engineSupportsAuxRefs(s, nextEngine) ? {} : { aux_ref_ids: [] }),
                })
              }
            }} />
        ) : (
          <EngineSelect value={line.engine} disabled={disabled} className="w-engine"
            onChange={(v) => {
              const nextEngine = v as WorkLine['engine']
              update({
                engine: nextEngine,
                version: null,
                gpt: null,
                sovits: null,
                ...(engineSupportsAuxRefs(s, nextEngine) ? {} : { aux_ref_ids: [] }),
              })
            }} />
        )}
        <button className="icon" aria-label="删除行" disabled={disabled} onClick={() => removeLine(line.id)}>⊖</button>
      </div>
      <div className="line-row second wrap">
        <button className="work-ref-trigger" disabled={disabled} onClick={() => setPickingRef(true)} aria-label="选择参考音频">
          <span>◇ {refSummary}</span>
          <span className="muted small">更改</span>
        </button>
        <label className="row gap nowrap">停顿
          <input className="w70" type="number" inputMode="decimal" step="0.1" aria-label="停顿秒数" value={line.pause_after} disabled={disabled}
            onChange={(e) => update({ pause_after: Number(e.target.value) })} />
          <span className="muted">秒</span>
        </label>
        <span className="spacer" />
        <button disabled={line.id.startsWith('draft-') || !line.text || disabled} onClick={() => void regenerateLine(line)}>重新生成</button>
      </div>
      {stale && <div className="warn small">台词或声音设置已修改，请重新生成此行后再渲染。</div>}
      <button className="disclosure-btn" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '▾' : '▸'} 候选（{line.candidates.length}）</button>
      {open && (
        <div className="stack">
          {line.candidates.map((item) => {
            const ok = canPick(s, item, line)
            return (
              <div key={item.id} className="stack tight">
                <CandidateCard item={item} best={bestId === item.id} selected={line.picked === item.id}
                  extra={<button disabled={line.picked === item.id || s.workDirty || disabled || !ok}
                    onClick={(e) => { e.stopPropagation(); void perform(() => pick(item, line)) }}>{line.picked === item.id ? '已选' : '选用'}</button>} />
                {!ok && <div className="muted small">旧台词或声音设置，仅供试听</div>}
              </div>
            )
          })}
        </div>
      )}
      {pickingRef && (
        <WorkRefPicker character={line.character} initialRefs={refs} currentRefId={line.ref_id ?? null} currentEmotion={line.emotion ?? null}
          currentAuxRefIds={auxIds} supportsAuxRefs={engineSupportsAuxRefs(s, line.engine)}
          onClose={() => setPickingRef(false)}
          onUpdateAux={(nextAux) => update({ aux_ref_ids: nextAux })}
          onPick={(next) => {
            const cleanedAux = next.ref_id ? auxIds.filter((id) => id !== next.ref_id) : auxIds
            update({ ...next, aux_ref_ids: cleanedAux })
            setPickingRef(false)
          }} />
      )}
    </div>
  )
}

function WorkRefPicker({ character, initialRefs, currentRefId, currentEmotion, currentAuxRefIds, supportsAuxRefs, onClose, onUpdateAux, onPick }: {
  character: string
  initialRefs: Ref[]
  currentRefId: string | null
  currentEmotion: string | null
  currentAuxRefIds: string[]
  supportsAuxRefs: boolean
  onClose: () => void
  onUpdateAux: (nextAux: string[]) => void
  onPick: (patch: { ref_id: string | null; emotion: string | null }) => void
}) {
  const [refs, setRefs] = useState<Ref[]>(initialRefs)
  const [mode, setMode] = useState<'default' | 'emotion' | 'single'>(() =>
    currentRefId ? 'single' : currentEmotion ? 'emotion' : 'default')
  const [search, setSearch] = useState('')

  useEffect(() => {
    let stale = false
    getJSON<RefList>('/refs', { query: { character } })
      .then((res) => { if (!stale) setRefs(res.refs) })
      .catch(showError)
    return () => { stale = true }
  }, [character])

  // 情绪随机标签仅从合格 3–10s 主参考中提取
  const emotions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const r of refs) {
      if (!isValidPrimaryRefDuration(r.duration)) continue
      for (const t of r.tags) counts.set(t, (counts.get(t) ?? 0) + 1)
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [refs])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return refs
    return refs.filter((r) => r.text.toLowerCase().includes(q) || r.id.toLowerCase().includes(q) || r.tags.some((t) => t.toLowerCase().includes(q)))
  }, [refs, search])

  return (
    <Modal title={`选择参考音频 · ${characterName(character)}`} onClose={onClose} closeLabel="关闭" wide>
      <Segmented label="参考选择方式" value={mode} onChange={setMode}
        options={[
          { value: 'default', label: '默认参考' },
          { value: 'emotion', label: '按情绪随机' },
          { value: 'single', label: '指定参考' },
        ]} />

      {currentAuxRefIds.length > 0 && (
        <div className="aux-section">
          <div className="row between wrap gap">
            <span className="small muted">
              本行辅助参考（{currentAuxRefIds.length}）{!supportsAuxRefs && ' · 当前引擎不支持，建议移除'}
            </span>
            <button className="ghost small-btn" onClick={() => onUpdateAux([])}>清空辅参</button>
          </div>
          <div className="chips aux-pills">
            {currentAuxRefIds.map((id) => {
              const ref = refs.find((r) => r.id === id)
              return (
                <span key={id} className={`aux-pill ${!supportsAuxRefs ? 'dim' : ''}`}>
                  <span className="aux-pill-text">{ref ? ref.text.slice(0, 18) : id}</span>
                  <button className="icon aux-remove" aria-label={`移除本行辅助参考 ${id}`}
                    onClick={() => onUpdateAux(currentAuxRefIds.filter((x) => x !== id))}>✕</button>
                </span>
              )
            })}
          </div>
        </div>
      )}

      {mode === 'default' && (
        <div className="stack">
          <div className="muted">使用 {characterName(character)} 的默认参考音频生成这一行。</div>
          <div>
            <button className="primary" onClick={() => onPick({ ref_id: null, emotion: null })}>使用默认参考</button>
          </div>
        </div>
      )}

      {mode === 'emotion' && (
        <div className="stack">
          <div className="muted small">生成时从带所选情绪标签的合格参考（3–10 秒）中随机抽取一条。</div>
          {emotions.length ? (
            <div className="chips">
              {emotions.map(([tag, count]) => (
                <button key={tag} className={`chip-btn ${currentEmotion === tag ? 'on' : ''}`}
                  onClick={() => onPick({ ref_id: null, emotion: tag })}>
                  {tag}（{count}）
                </button>
              ))}
            </div>
          ) : (
            <div className="muted small">这个角色还没有已打标的合格情绪参考（需 3–10 秒），请先在「角色 → 参考音频」里打标。</div>
          )}
        </div>
      )}

      {mode === 'single' && (
        <div className="stack">
          <input placeholder="搜索台词、标签或编号" aria-label="搜索参考音频" value={search} onChange={(e) => setSearch(e.target.value)} />
          <div className="work-ref-list">
            {filtered.map((r) => {
              const ok = isValidPrimaryRefDuration(r.duration)
              const isAux = currentAuxRefIds.includes(r.id)
              return (
                <div key={r.id} className={`gen-ref-item ${currentRefId === r.id ? 'selected' : ''} ${!ok ? 'invalid-primary' : ''}`}
                  onClick={() => { if (ok) onPick({ ref_id: r.id, emotion: null }) }}>
                  <PlayButton id={'ref-' + r.id} label={`试听参考 ${r.id}`} onClick={() => playReference(r, false)} />
                  <div className="gen-ref-body">
                    <div className="clamp2 small">{r.text}</div>
                    <div className="row gap wrap muted tiny">
                      <span className="mono">{displaySeconds(r.duration)} 秒</span>
                      <span>{r.lang}</span>
                      {r.tags.map((t) => <span key={t} className="badge">{t}</span>)}
                    </div>
                    {!ok && <div className="warn tiny">超出 3–10 秒，不可作主参考</div>}
                  </div>
                  {supportsAuxRefs && (
                    <button className={`aux-btn ${isAux ? 'on' : ''}`} disabled={currentRefId === r.id}
                      aria-label={isAux ? `移出辅助参考 ${r.id}` : `加入辅助参考 ${r.id}`}
                      title={currentRefId === r.id ? '已是主参考' : isAux ? '移出辅助参考' : '加入辅助参考'}
                      onClick={(e) => {
                        e.stopPropagation()
                        onUpdateAux(isAux ? currentAuxRefIds.filter((x) => x !== r.id) : [...currentAuxRefIds, r.id])
                      }}>
                      {isAux ? '✓' : '+'}
                    </button>
                  )}
                  <button className="small-btn" disabled={!ok} onClick={(e) => { e.stopPropagation(); onPick({ ref_id: r.id, emotion: null }) }}>
                    {currentRefId === r.id ? '已选' : '选择'}
                  </button>
                </div>
              )
            })}
            {!filtered.length && <div className="muted small empty compact">没有符合条件的参考音频。</div>}
          </div>
        </div>
      )}
    </Modal>
  )
}
