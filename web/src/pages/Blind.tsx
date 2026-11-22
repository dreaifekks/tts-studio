import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, getJSON, isAbort, request, sendJSON, url } from '../api/client'
import type {
  BlindCombo, BlindCreate, BlindList, BlindQuestion, BlindResult, BlindRevealQuestion, BlindSide, BlindSummary, BlindTest, CharacterInfo,
  EvalSetName, WeightEntry, WeightList,
} from '../api/types'
import { player } from '../audio'
import { blindTitle } from '../blind'
import { Confirm, Metric, ModelVersionPicker, PageHeading, Segmented, Select } from '../components'
import { ComputePicker } from '../compute'
import { displayEngine, displayEvalSet, modelLabel, scoreHelp } from '../display'
import { planReady, submitPlanned, usePlanners } from '../planner'
import {
  characterName, clearBlindPrefill, emptyVersions, engineLabel, engineList, fmt, isSettingDefault, loadEvalSets,
  loadVersionList, modelCompletionKey, navigate, openBlind, perform, playAudio, preferredEngine, recordLocalJob,
  setActiveCharacter, setDefaultComboByNames, setStatus, showError, trackJob, useStore, VERSION_GONE,
  type VersionList,
} from '../store'

type Choice = 'left' | 'right' | 'tie'
const choices: Choice[] = ['left', 'tie', 'right']
const choiceLabel: Record<Choice, string> = { left: '左更好', tie: '差不多', right: '右更好' }
const choiceKey: Record<Choice, string> = { left: 'A', tie: 'S', right: 'D' }
const winnerLabel = { A: 'A', B: 'B', tie: '平' }
const date = (t: string) => t.slice(0, 16).replace('T', ' ')
const percent = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`)

function statusOf(t: BlindSummary) {
  if (t.revealed) return { label: '已揭晓', cls: 'accent' }
  if (t.status === 'generating') return { label: `生成中 ${t.n_ready}/${t.n_questions}`, cls: 'warn' }
  if (t.status === 'failed') return { label: '生成失败', cls: 'bad' }
  return { label: '可以做', cls: 'ok' }
}

function weightName(models: CharacterInfo[], character: string, engine: string, type: 'gpt' | 'sovits', name: string) {
  const w = models.find((m) => m.slug === character)?.engines[engine]?.[type].find((x) => x.name === name)
  return w?.display_name || name
}
const comboText = (models: CharacterInfo[], character: string, c: BlindCombo) =>
  `${displayEngine(c.engine)} · ${weightName(models, character, c.engine, 'gpt', c.gpt)} + ${weightName(models, character, c.engine, 'sovits', c.sovits)}`

async function deleteBlind(id: string) {
  await request(`/blind/${encodeURIComponent(id)}`, { method: 'DELETE' })
  setStatus('盲听已删除')
}

function stopBlindAudio() {
  if (player.currentId?.startsWith('blind-')) player.pause()
}

function PlaySideButton({ id, label, disabled, onClick, className = 'big' }: {
  id: string; label: string; disabled?: boolean; onClick: () => void; className?: string
}) {
  const active = useStore((s) => s.playingId === id && s.playing)
  return <button className={className} disabled={disabled} onClick={onClick}>{active ? `❚❚ 暂停${label}` : `▶ 播放${label}`}</button>
}

export function BlindPage({ character: propChar, embedded = false }: { character?: string; embedded?: boolean }) {
  const view = useStore((s) => s.blindView)
  const storeChar = useStore((s) => s.params.character)
  const activeChar = propChar ?? storeChar
  return (
    <div className={embedded ? 'stack' : 'page'}>
      {!embedded && <PageHeading>两个组合念同一句话，只凭耳朵挑更好的一边；投完再揭晓哪边是哪个模型。</PageHeading>}
      {view.kind === 'list' && <ListView activeCharacter={activeChar} />}
      {view.kind === 'new' && <NewView activeCharacter={activeChar} />}
      {view.kind === 'test' && <TestView key={view.id} id={view.id} activeCharacter={activeChar} />}
    </div>
  )
}

// ───────── 列表

function ListView({ activeCharacter }: { activeCharacter: string }) {
  const models = useStore((s) => s.models)
  const [scope, setScope] = useState<'current' | 'all'>('current')
  const [tests, setTests] = useState<BlindSummary[] | null>(null)
  const [results, setResults] = useState<Record<string, BlindResult>>({})
  const [version, setVersion] = useState(0)
  const [deleting, setDeleting] = useState<BlindSummary | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    void (async () => {
      const list = await getJSON<BlindList>('/blind', { query: { limit: 100 }, signal: ctrl.signal })
      setTests(list.tests)
      const got = await Promise.all(list.tests.filter((t) => t.revealed)
        .map((t) => getJSON<BlindResult>(`/blind/${encodeURIComponent(t.id)}/result`, { signal: ctrl.signal })))
      setResults(Object.fromEntries(got.map((r) => [r.id, r])))
    })().catch(showError)
    return () => ctrl.abort()
  }, [version])

  const generating = !!tests?.some((t) => t.status === 'generating')
  useEffect(() => {
    if (!generating) return
    const t = setTimeout(() => setVersion((v) => v + 1), 5000)
    return () => clearTimeout(t)
  }, [generating, tests])

  const visibleTests = tests
    ? (scope === 'current' && activeCharacter ? tests.filter((t) => t.character === activeCharacter) : tests)
    : null

  return (
    <div className="stack blind-wrap">
      <div className="row between wrap gap">
        <div className="row gap wrap">
          <h2>盲听</h2>
          {activeCharacter && (
            <Segmented label="盲听筛选范围" value={scope} onChange={setScope}
              options={[{ value: 'current', label: characterName(activeCharacter) }, { value: 'all', label: '全部角色' }]} />
          )}
        </div>
        <div className="row gap">
          <button onClick={() => setVersion((v) => v + 1)}>刷新</button>
          <button className="primary" onClick={() => { clearBlindPrefill(); openBlind({ kind: 'new' }) }}>＋ 新建盲听</button>
        </div>
      </div>
      {visibleTests && !visibleTests.length && (
        <div className="empty"><div className="empty-title">还没有盲听</div><div className="muted">选两个组合，用评测集的句子比一比。</div></div>
      )}
      {visibleTests?.map((t) => {
        const st = statusOf(t)
        const r = results[t.id]
        const openItem = () => {
          if (t.character && t.character !== activeCharacter) setActiveCharacter(t.character)
          openBlind({ kind: 'test', id: t.id })
        }
        return (
          <div key={t.id} className="card blind-item" role="button" tabIndex={0}
            onClick={openItem}
            onKeyDown={(e) => { if (e.key === 'Enter' && e.target === e.currentTarget) openItem() }}>
            <div className="row between gap">
              <span className="strong break">{blindTitle(t, characterName(t.character))}</span>
              <span className={`badge ${st.cls}`}>{st.label}</span>
            </div>
            <div className="muted small">{characterName(t.character)} · {displayEvalSet(t.eval_set)} · {date(t.created)} · 已投 {t.n_voted}/{t.n_questions}</div>
            {t.revealed && r && (
              <div className="small stack tight">
                <span>A 胜 {r.a.wins} 负 {r.a.losses} 平 {r.a.ties}</span>
                <span className="muted break">A：{comboText(models, t.character, r.a.combo)}</span>
                <span className="muted break">B：{comboText(models, t.character, r.b.combo)}</span>
              </div>
            )}
            <div className="row end">
              <button className="link" onClick={(e) => { e.stopPropagation(); setDeleting(t) }}>删除</button>
            </div>
          </div>
        )
      })}
      {deleting && (
        <Confirm message={`删除盲听「${blindTitle(deleting, characterName(deleting.character))}」？${deleting.status === 'generating' ? '正在生成的任务会被取消。' : ''}投票记录会一起删除。`}
          confirmLabel="删除" onClose={() => setDeleting(null)}
          onConfirm={() => { const id = deleting.id; void perform(async () => { await deleteBlind(id); setVersion((v) => v + 1) }) }} />
      )}
    </div>
  )
}

// ───────── 新建

function SideEditor({ label, versions, side, weights, onChange }: {
  label: string; versions: VersionList; side: BlindSide; weights: WeightEntry[]; onChange: (s: BlindSide) => void
}) {
  const s = useStore()
  const options = (type: 'gpt' | 'sovits') => [
    { value: '', label: `选择 ${type === 'gpt' ? 'GPT' : 'SoVITS'}` },
    ...weights.filter((w) => w.engine === side.engine && w.type === type).map((w) => ({ value: w.name, label: modelLabel(w) })),
  ]
  const manual = !side.version && !!(side.gpt || side.sovits)
  return (
    <div className="group stack blind-side">
      <h3>组合 {label}</h3>
      <ModelVersionPicker versions={{ ...versions, selectedID: side.version ?? null, message: null }} manual={manual} onSelect={(id) => onChange(applyVersion(versions, id, side))} />
      <details className="disclosure" open={manual}>
        <summary>更多设置</summary>
        <div className="form-grid">
          <Select label="引擎" value={side.engine} options={engineList(s).map((e) => ({ value: e, label: engineLabel(s, e) }))}
            onChange={(engine) => onChange({ engine: engine as BlindSide['engine'], version: null, gpt: null, sovits: null })} />
          <Select label="GPT" value={side.gpt ?? ''} options={options('gpt')} onChange={(gpt) => onChange({ ...side, version: null, gpt: gpt || null })} />
          <Select label="SoVITS" value={side.sovits ?? ''} options={options('sovits')} onChange={(sovits) => onChange({ ...side, version: null, sovits: sovits || null })} />
        </div>
      </details>
    </div>
  )
}

function applyVersion(versions: VersionList, id: string | null, side: BlindSide): BlindSide {
  const v = versions.rows.find((x) => x.id === id)
  return v ? { engine: v.engine as BlindSide['engine'], gpt: v.gpt, sovits: v.sovits, version: v.id } : { ...side, version: null }
}

function NewView({ activeCharacter }: { activeCharacter: string }) {
  const s = useStore()
  const prefill = s.blindPrefill
  const completion = useStore(modelCompletionKey)
  const submitting = usePlanners((p) => p.blind.submitting)
  const planOk = usePlanners((p) => planReady(p.blind))
  const [character, setCharacter] = useState(() => {
    if (prefill?.character) return prefill.character
    if (activeCharacter && s.models.some((m) => m.slug === activeCharacter)) return activeCharacter
    return s.models.some((m) => m.slug === s.params.character) ? s.params.character : s.models[0]?.slug ?? ''
  })
  const [versions, setVersions] = useState<VersionList>(emptyVersions)
  const [weights, setWeights] = useState<WeightEntry[]>([])
  const defaultEngine = preferredEngine(s.commonModels, engineList(s))
  const [a, setA] = useState<BlindSide>(() => prefill?.a ?? { engine: defaultEngine })
  const [b, setB] = useState<BlindSide>(() => prefill?.b ?? { engine: defaultEngine })
  const [evalSet, setEvalSet] = useState<EvalSetName | null>(null)
  const [repeats, setRepeats] = useState(1)
  const [title, setTitle] = useState(() => prefill?.title ?? '')
  const [error, setError] = useState<string | null>(null)
  const hasPrefillRef = useRef(!!prefill)
  const aTouched = useRef(false)
  const bTouched = useRef(false)
  const characterRef = useRef(character)
  useEffect(() => { characterRef.current = character }, [character])

  useEffect(() => {
    void perform(async () => {
      await loadEvalSets()
      setWeights((await getJSON<WeightList>('/weights', { query: { hidden: false, eval_set: 'hard' } })).weights)
    })
  }, [])

  // 换角色时读模型版本；有预填时保留预填（若按 gpt/sovits 能匹配到版本则自动补上 version）；异步返回只补未编辑侧
  useEffect(() => {
    if (!character) return
    let stale = false
    loadVersionList(versions, character).then((next) => {
      if (stale || characterRef.current !== character) return
      const changed = versions.character !== character
      setVersions(next)
      if (hasPrefillRef.current) {
        hasPrefillRef.current = false
        clearBlindPrefill()
        const matchSide = (side: BlindSide): BlindSide => {
          if (side.version && next.rows.some((v) => v.id === side.version)) return applyVersion(next, side.version, side)
          const matched = next.rows.find((v) => v.engine === side.engine && v.gpt === side.gpt && v.sovits === side.sovits)
          return matched ? applyVersion(next, matched.id, side) : side
        }
        if (!aTouched.current) setA((cur) => matchSide(cur))
        if (!bTouched.current) setB((cur) => matchSide(cur))
        return
      }
      if (changed) {
        const st = useStore.getState()
        const def = preferredEngine(st.commonModels, engineList(st))
        if (next.selectedID) {
          if (!aTouched.current) setA(applyVersion(next, next.selectedID, { engine: def }))
          if (!bTouched.current) setB(applyVersion(next, (next.rows.find((v) => v.id !== next.selectedID) ?? next.rows[0]).id, { engine: def }))
        } else {
          if (!aTouched.current) setA({ engine: def })
          if (!bTouched.current) setB({ engine: def })
        }
      }
    }).catch((e) => { if (!stale) setError(e instanceof Error ? e.message : String(e)) })
    return () => { stale = true }
  }, [character, completion]) // eslint-disable-line react-hooks/exhaustive-deps

  const editA = (next: BlindSide) => { aTouched.current = true; setA(next) }
  const editB = (next: BlindSide) => { bTouched.current = true; setB(next) }

  const set = s.evalSets.find((x) => x.name === (evalSet ?? s.evalSets.find((d) => d.default)?.name ?? 'hard'))
  const items = (set?.sentences.length ?? 0) * repeats
  const shown = versions.character === character ? versions : { ...emptyVersions(), character, loading: true }
  const sideWeights = weights.filter((w) => !w.character || w.character === character)
  const pickable = (x: BlindSide, type: 'gpt' | 'sovits', name: string | null | undefined) =>
    !!name && sideWeights.some((w) => w.engine === x.engine && w.type === type && w.name === name)
  const sideReady = (x: BlindSide) => (x.version
    ? versions.character === character && versions.rows.some((v) => v.id === x.version)
    : pickable(x, 'gpt', x.gpt) && pickable(x, 'sovits', x.sovits))
  const chooseCharacter = (value: string) => {
    if (value === character) return
    hasPrefillRef.current = false
    aTouched.current = false
    bTouched.current = false
    clearBlindPrefill()
    const def = preferredEngine(s.commonModels, engineList(s))
    setCharacter(value)
    setA({ engine: def })
    setB({ engine: def })
    setError(null)
  }
  const ready = !!character && sideReady(a) && sideReady(b) && items > 0
  const draft = useRef<AbortController | null>(null)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const cancelDraft = () => { draft.current?.abort(); clearBlindPrefill(); openBlind({ kind: 'list' }) }
  const submit = () => {
    if (!set) return
    setError(null)
    const body: BlindCreate = { character, a, b, eval_set: set.name, repeats, title: title.trim() || null, approved: false }
    const stop = new AbortController()
    draft.current = stop
    void perform(async () => {
      try {
        const test = await submitPlanned<BlindTest>('blind', '/blind', body, { signal: stop.signal })
        recordLocalJob(test.job_id)
        setStatus('盲听已创建，正在生成')
        if (alive.current && !stop.signal.aborted) openBlind({ kind: 'test', id: test.id })
        await trackJob(test.job_id)
      } catch (e) {
        if (isAbort(e)) return
        if (e instanceof ApiError && e.code === 'version_unavailable') {
          const next = await loadVersionList(versions, character, false)
          setVersions({ ...next, selectedID: null })
          setA((x) => ({ engine: x.engine })); setB((x) => ({ engine: x.engine }))
          setError(VERSION_GONE)
          return
        }
        setError(e instanceof Error ? e.message : String(e))
      }
    })
  }

  if (!s.models.length) {
    return (
      <div className="stack blind-wrap">
        <h2>新建盲听</h2>
        <div className="empty"><div className="empty-title">资料库还没有角色</div><div className="muted">先在资料库中准备角色，再创建盲听。</div>
          <button className="link" onClick={() => navigate('settings', 'library')}>查看资料库</button></div>
      </div>
    )
  }
  return (
    <div className="stack blind-wrap">
      <div className="row between wrap">
        <h2>新建盲听</h2>
        <button onClick={cancelDraft}>返回列表</button>
      </div>
      <div className="muted">两组使用同一句台词，左右顺序随机。创建后只显示左右，揭晓时再看模型。</div>
      <div className="group stack">
        <Select label="角色" value={character} disabled={submitting}
          options={[{ value: '', label: '选择角色' }, ...s.models.map((m) => ({ value: m.slug, label: characterName(m.slug) }))]} onChange={chooseCharacter} />
        <div className="muted small">两边都用这个角色的默认参考音频。</div>
      </div>
      <div className="blind-sides">
        <SideEditor label="A" versions={shown} side={a} weights={sideWeights} onChange={editA} />
        <SideEditor label="B" versions={shown} side={b} weights={sideWeights} onChange={editB} />
      </div>
      <div className="group stack">
        <h3>题目</h3>
        <Select label="评测集" value={set?.name ?? 'hard'} options={s.evalSets.map((x) => ({ value: x.name, label: `${displayEvalSet(x.name)} · ${x.sentences.length} 句` }))}
          onChange={(v) => setEvalSet(v as EvalSetName)} />
        <div className="row gap wrap">
          <span>每句重复</span>
          <div className="row gap stepper" role="group" aria-label="每句重复次数">
            <button aria-label="减少重复" disabled={repeats <= 1} onClick={() => setRepeats(repeats - 1)}>−</button>
            <span>{repeats} 次 · 共 {items} 题</span>
            <button aria-label="增加重复" disabled={repeats >= 5} onClick={() => setRepeats(repeats + 1)}>+</button>
          </div>
        </div>
        <div className="muted small">每次重复单独出题，A、B 使用相同条件。边生成边能做题。</div>
        <label className="field"><span>标题</span><input placeholder="可选标题" maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      </div>
      <ComputePicker id="blind" input={{ kind: 'gen', engines: [a.engine, b.engine], items, cloud_capable: false }} action="出题" ready={ready} notReadyText="两边都选好模型版本后估算"
        scope={`${items} 题 · ${displayEngine(a.engine)} / ${displayEngine(b.engine)}`} taskTitle={`盲听 ${characterName(character)} · ${displayEngine(a.engine)} / ${displayEngine(b.engine)}`} />
      {error && <div className="error-text selectable" role="alert">{error}</div>}
      <div className="row gap">
        <button onClick={cancelDraft}>取消</button>
        <span className="spacer" />
        <button className="primary big" disabled={!ready || submitting || !planOk} onClick={submit}>{submitting ? '正在创建…' : '创建并开始生成'}</button>
      </div>
    </div>
  )
}

// ───────── 做题 / 结果

function TestView({ id, activeCharacter }: { id: string; activeCharacter: string }) {
  const [test, setTest] = useState<BlindTest | null>(null)
  const seq = useRef(0)
  const initialChar = useRef(activeCharacter)
  const load = async () => {
    const n = ++seq.current
    const t = await getJSON<BlindTest>(`/blind/${encodeURIComponent(id)}`)
    if (n === seq.current) setTest(t)
    return t
  }
  useEffect(() => { void perform(load) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // 挂载及 activeCharacter 变化时，只要当前盲听详情不属于活跃角色，自动退回列表
  useEffect(() => {
    if (activeCharacter && test && test.character !== activeCharacter) {
      stopBlindAudio()
      openBlind({ kind: 'list' })
    } else if (activeCharacter !== initialChar.current && !test) {
      stopBlindAudio()
      openBlind({ kind: 'list' })
    }
  }, [activeCharacter, test])
  const generating = test?.status === 'generating'
  useEffect(() => {
    if (!generating) return
    const t = setInterval(() => void perform(load), 3000)
    return () => clearInterval(t)
  }, [generating]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!test) return <div className="muted">正在读取…</div>
  return test.revealed ? <ResultView test={test} reload={load} /> : <QuizView test={test} reload={load} />
}

function nextUnvoted(qs: BlindQuestion[], after: number): number | null {
  for (let k = 1; k <= qs.length; k++) {
    const i = (after + k) % qs.length
    if (!qs[i].vote) return i
  }
  return null
}

function Header({ test, onDelete }: { test: BlindTest; onDelete: () => void }) {
  const st = statusOf(test)
  return (
    <div className="row between wrap gap blind-head">
      <div className="stack tight grow">
        <div className="row gap wrap"><h2 className="break">{blindTitle(test, characterName(test.character))}</h2><span className={`badge ${st.cls}`}>{st.label}</span></div>
        <span className="muted small">{characterName(test.character)} · {displayEvalSet(test.eval_set)} · 每句 {test.repeats} 次 · {date(test.created)}</span>
      </div>
      <div className="row gap">
        <button onClick={() => openBlind({ kind: 'list' })}>返回列表</button>
        <button className="danger" onClick={onDelete}>删除…</button>
      </div>
    </div>
  )
}

type Reload = () => Promise<BlindTest>

function GenerationBar({ test, reload, children }: { test: BlindTest; reload: Reload; children?: ReactNode }) {
  const [needsVersion, setNeedsVersion] = useState(false)
  const busy = usePlanners((p) => p.blindgen.submitting)
  const regenerate = () => void perform(async () => {
    let t: BlindTest
    try {
      t = await submitPlanned<BlindTest>('blindgen', `/blind/${encodeURIComponent(test.id)}/generate`, {},
        { task: `补生成盲听「${blindTitle(test, characterName(test.character))}」（还差 ${test.n_questions - test.n_ready} 题）`, retry503: true })
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_unavailable') {
        setNeedsVersion(true)
        throw new ApiError(409, '这个模型版本已不可用，请重新选择版本后新建盲听。', { code: e.code })
      }
      throw e
    }
    recordLocalJob(t.job_id)
    setStatus('已开始补生成')
    await reload()
    await trackJob(t.job_id)
  })
  return (
    <>
      <div className="row between wrap gap">
        <span>已生成 {test.n_ready}/{test.n_questions} · 已投 {test.n_voted}/{test.n_questions}</span>
        <span className="row gap">
          {test.status === 'failed' && (needsVersion
            ? <button className="primary" onClick={() => openBlind({ kind: 'new' })}>重选版本…</button>
            : <button className="primary" disabled={busy} onClick={regenerate}>{busy ? '正在提交…' : '补生成'}</button>)}
          {children}
        </span>
      </div>
      {test.status === 'generating' && (
        <div className="progress"><div style={{ width: `${Math.round((test.n_ready / test.n_questions) * 100)}%` }} /></div>
      )}
      {test.status === 'failed' && <div className="error-text small">生成没有完成（失败、被取消或服务重启）。已生成的题照样能听能投，点「补生成」补齐剩下的。</div>}
    </>
  )
}

function QuizView({ test, reload }: { test: BlindTest; reload: Reload }) {
  const qs = test.questions
  const [index, setIndex] = useState(() => nextUnvoted(qs, -1) ?? 0)
  const [note, setNote] = useState(() => qs[index].note ?? '')
  const [voting, setVoting] = useState(false)
  const [confirm, setConfirm] = useState<'reveal' | 'delete' | null>(null)
  const q = qs[index]
  const nav = useRef(0)

  const moveTo = (i: number, list: BlindQuestion[]) => {
    nav.current++
    stopBlindAudio()
    setIndex(i)
    setNote(list[i].note ?? '')
  }
  const go = (i: number) => { if (i >= 0 && i < qs.length && i !== index) moveTo(i, qs) }
  const play = (side: 'left' | 'right') => {
    const path = side === 'left' ? q.left_url : q.right_url
    if (!q.ready || !path) return
    playAudio(`blind-${test.id}-${q.id}-${side}`, url(path, { format: 'mp3' }), side === 'left' ? '正在播放左' : '正在播放右')
  }
  const vote = (choice: Choice, advance = true) => {
    if (!q.ready || voting) return
    const qid = q.id
    const at = nav.current
    setVoting(true)
    void perform(async () => {
      await sendJSON<BlindQuestion>(`/blind/${encodeURIComponent(test.id)}/questions/${encodeURIComponent(qid)}/vote`, { choice, note }, { method: 'PUT' })
      setStatus(advance ? `已投：${choiceLabel[choice]}` : '备注已保存')
      const t = await reload()
      if (!advance || t.revealed || nav.current !== at) return
      const next = nextUnvoted(t.questions, t.questions.findIndex((x) => x.id === qid))
      if (next !== null) moveTo(next, t.questions)
    }).finally(() => setVoting(false))
  }
  const reveal = () => void perform(async () => {
    await sendJSON<BlindResult>(`/blind/${encodeURIComponent(test.id)}/reveal`, undefined)
    await reload()
  })

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.repeat || confirm) return
      if ((e.target as HTMLElement).closest('input, textarea, select')) return
      const k = e.key.toLowerCase()
      if (k === 'arrowleft' || k === '1') play('left')
      else if (k === 'arrowright' || k === '2') play('right')
      else if (k === 'a') vote('left')
      else if (k === 's') vote('tie')
      else if (k === 'd') vote('right')
      else if (k === '[') go(index - 1)
      else if (k === ']') go(index + 1)
      else return
      e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return (
    <div className="stack blind-wrap">
      <Header test={test} onDelete={() => setConfirm('delete')} />
      <div className="group stack">
        <GenerationBar test={test} reload={reload}>
          <button onClick={() => setConfirm('reveal')}>提前揭晓…</button>
        </GenerationBar>
        <div className="q-strip" aria-label="题目">
          {qs.map((x, i) => (
            <button key={x.id} className={`q-dot ${i === index ? 'current' : ''} ${x.vote ? 'voted' : ''} ${x.ready ? '' : 'pending'}`}
              aria-label={`第 ${i + 1} 题${x.vote ? '（已投）' : ''}${x.ready ? '' : '（生成中）'}`} onClick={() => go(i)}>{i + 1}</button>
          ))}
        </div>
      </div>

      <div className="card stack blind-card">
        <div className="row between">
          <span className="muted">第 {index + 1} / {qs.length} 题</span>
          <span className={q.vote ? 'accent-text strong' : 'muted'}>{q.vote ? `已投：${choiceLabel[q.vote]}` : '未投'}</span>
        </div>
        <div className="blind-text">{q.text}</div>
        {!!q.tests.length && <div className="row gap wrap">{q.tests.map((t) => <span key={t} className="badge">{t}</span>)}</div>}
        {!q.ready && <div className="warn">生成中…这题生成好之后才能听和投票。</div>}
        <div className="blind-play">
          <PlaySideButton id={`blind-${test.id}-${q.id}-left`} label="左" disabled={!q.ready} onClick={() => play('left')} />
          <PlaySideButton id={`blind-${test.id}-${q.id}-right`} label="右" disabled={!q.ready} onClick={() => play('right')} />
        </div>
        <div className="blind-vote">
          {choices.map((c) => (
            <button key={c} className={q.vote === c ? 'chosen' : ''} aria-pressed={q.vote === c} disabled={!q.ready || voting} onClick={() => vote(c)}>
              {choiceLabel[c]}<span className="kbd-hint desktop-only">{choiceKey[c]}</span>
            </button>
          ))}
        </div>
        <div className="row gap">
          <input className="grow" placeholder="备注（可选，200 字内）" aria-label="备注" maxLength={200} value={note} onChange={(e) => setNote(e.target.value)} />
          {q.vote && note !== (q.note ?? '') && <button disabled={voting} onClick={() => vote(q.vote!, false)}>保存备注</button>}
        </div>
        <div className="row between">
          <button disabled={index === 0} onClick={() => go(index - 1)}>上一题</button>
          <button disabled={index === qs.length - 1} onClick={() => go(index + 1)}>下一题</button>
        </div>
        <div className="muted small desktop-only">快捷键：← 或 1 播左 · → 或 2 播右 · A 左更好 · S 差不多 · D 右更好 · [ ] 上一题 / 下一题。备注框里打字时快捷键不生效。</div>
      </div>

      {confirm === 'reveal' && (
        <Confirm message={`还有 ${test.n_questions - test.n_voted} 题没投，未投的题不计入结果。揭晓后仍可改票。现在揭晓？`} confirmLabel="揭晓"
          onClose={() => setConfirm(null)} onConfirm={reveal} />
      )}
      {confirm === 'delete' && (
        <Confirm message={`删除这次盲听？${test.status === 'generating' ? '正在生成的任务会被取消。' : ''}投票记录会一起删除。`} confirmLabel="删除"
          onClose={() => setConfirm(null)}
          onConfirm={() => void perform(async () => { await deleteBlind(test.id); openBlind({ kind: 'list' }) })} />
      )}
    </div>
  )
}

function ResultView({ test, reload }: { test: BlindTest; reload: Reload }) {
  const models = useStore((s) => s.models)
  const defaultBusy = useStore((s) => isSettingDefault(s, test.character))
  const [result, setResult] = useState<BlindResult | null>(null)
  const [version, setVersion] = useState(0)
  const [voting, setVoting] = useState<string | null>(null)
  const [settingDefault, setSettingDefault] = useState<string | null>(null)
  const [defaultNotice, setDefaultNotice] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => {
    const ctrl = new AbortController()
    getJSON<BlindResult>(`/blind/${encodeURIComponent(test.id)}/result`, { signal: ctrl.signal }).then(setResult).catch(showError)
    return () => ctrl.abort()
  }, [test.id, test.n_ready, test.n_voted, version])

  const revote = (q: BlindRevealQuestion, choice: Choice) => {
    setVoting(q.id)
    void perform(async () => {
      await sendJSON<BlindQuestion>(`/blind/${encodeURIComponent(test.id)}/questions/${encodeURIComponent(q.id)}/vote`, { choice, note: q.note }, { method: 'PUT' })
      setStatus(`已改为：${choiceLabel[choice]}`)
      await reload()
      setVersion((v) => v + 1)
    }).finally(() => setVoting(null))
  }
  const playSide = (q: BlindRevealQuestion, side: 'A' | 'B') => {
    const path = side === 'A' ? q.a_audio_url : q.b_audio_url
    if (path) playAudio(`blind-${test.id}-${q.id}-${side}`, url(path, { format: 'mp3' }), `正在播放 ${side}`)
  }

  const rate = (v: number | null | undefined) => v ?? -1
  const best = !result || rate(result.a.win_rate) === rate(result.b.win_rate) ? null : rate(result.a.win_rate) > rate(result.b.win_rate) ? 'A' : 'B'
  const winner = best === 'A' ? result?.a : best === 'B' ? result?.b : null

  const applyComboDefault = (label: string, combo: BlindCombo) => {
    if (defaultBusy) return
    setSettingDefault(label)
    setDefaultNotice(null)
    void perform(async () => {
      const msg = await setDefaultComboByNames(test.character, combo.engine, combo.gpt, combo.sovits)
      if (msg) setDefaultNotice(msg)
    }).finally(() => setSettingDefault(null))
  }

  return (
    <div className="stack blind-wrap">
      <Header test={test} onDelete={() => setConfirmDelete(true)} />
      {result && (
        <>
          <div className="row between wrap gap">
            <div className="muted small">结果 · {displayEvalSet(result.eval_set)} · 揭晓于 {date(result.revealed_at)} · 已投 {result.n_voted}/{result.n_questions}</div>
            {winner && (
              <button className="primary" disabled={defaultBusy || settingDefault !== null}
                onClick={() => applyComboDefault(winner.label, winner.combo)}>
                {settingDefault === winner.label ? '设置中…' : `将胜出者（${winner.label}）设为默认`}
              </button>
            )}
          </div>
          {defaultNotice && <div className="notice small" role="status">{defaultNotice}</div>}
          {test.status !== 'ready' && <div className="group stack"><GenerationBar test={test} reload={reload} /></div>}
          <div className="blind-cards">
            {[result.a, result.b].map((c) => (
              <div key={c.label} className={`card stack blind-combo ${best === c.label ? 'winner' : ''}`}>
                <div className="row between wrap gap">
                  <span className="blind-label">{c.label}</span>
                  <div className="row gap">
                    {best === c.label && <span className="badge accent">胜率更高</span>}
                    <button className="small-btn" disabled={defaultBusy || settingDefault !== null}
                      onClick={() => applyComboDefault(c.label, c.combo)}>
                      {settingDefault === c.label ? '设置中…' : '设为默认'}
                    </button>
                  </div>
                </div>
                <div className="strong">{displayEngine(c.combo.engine)}</div>
                <div className="small break">GPT · {weightName(models, test.character, c.combo.engine, 'gpt', c.combo.gpt)}</div>
                <div className="small break">SoVITS · {weightName(models, test.character, c.combo.engine, 'sovits', c.combo.sovits)}</div>
                <div className="metrics five">
                  <Metric title="胜" value={String(c.wins)} />
                  <Metric title="负" value={String(c.losses)} />
                  <Metric title="平" value={String(c.ties)} />
                  <Metric title="胜率" value={percent(c.win_rate)} />
                  <Metric title="自动分均值" value={fmt(c.auto_score_mean, 1)} help={scoreHelp(c.auto_score_mean)} />
                </div>
              </div>
            ))}
          </div>
          <h3>逐题</h3>
          {result.questions.map((q) => (
            <div key={q.id} className="card stack blind-rq">
              <div className="row between wrap gap small">
                <span className="muted">第 {q.index + 1} 题</span>
                <span>你的选择 <b>{q.vote ? choiceLabel[q.vote] : '未投'}</b> · 赢家 <b>{q.winner ? winnerLabel[q.winner] : '—'}</b> · 左边是 <b>{q.left ?? '—'}</b></span>
              </div>
              <div>{q.text}</div>
              {!!q.tests.length && <div className="row gap wrap">{q.tests.map((t) => <span key={t} className="badge">{t}</span>)}</div>}
              {q.note && <div className="muted small">备注：{q.note}</div>}
              <div className="row gap wrap">
                <PlaySideButton id={`blind-${test.id}-${q.id}-A`} label=" A" className="" disabled={!q.a_audio_url} onClick={() => playSide(q, 'A')} />
                <span className="muted small">自动分 {fmt(q.a_score, 1)}</span>
                <PlaySideButton id={`blind-${test.id}-${q.id}-B`} label=" B" className="" disabled={!q.b_audio_url} onClick={() => playSide(q, 'B')} />
                <span className="muted small">自动分 {fmt(q.b_score, 1)}</span>
                <span className="spacer" />
                <div className="segmented" role="group" aria-label="改票">
                  {choices.map((c) => (
                    <button key={c} className={q.vote === c ? 'active' : ''} aria-pressed={q.vote === c} disabled={!q.left || voting !== null} onClick={() => revote(q, c)}>{choiceLabel[c]}</button>
                  ))}
                </div>
              </div>
            </div>
          ))}
        </>
      )}
      {confirmDelete && (
        <Confirm message="删除这次盲听和它的投票记录？" confirmLabel="删除" onClose={() => setConfirmDelete(false)}
          onConfirm={() => void perform(async () => { await deleteBlind(test.id); openBlind({ kind: 'list' }) })} />
      )}
    </div>
  )
}
