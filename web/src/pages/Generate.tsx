import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import type { AudioItem, Engine, SplitMethod } from '../api/types'
import { isActive } from '../api/client'
import {
  CandidateCard, JobProgress, ModelVersionPicker, NumberInput, PlayButton, Segmented, Select, SliderField,
} from '../components'
import { ComputePicker } from '../compute'
import { planReady, usePlanners } from '../planner'
import { displayEngine, displaySeconds, displaySplit, modelLabel } from '../display'
import {
  DEFAULT_SYNTH_PARAMS, characterName, composerReady, currentCharacter, engineLabel, engineList, engineSupportsAuxRefs,
  engineSupportsSampleSteps, isValidPrimaryRefDuration, loadCharacterPrefs, loadComposerVersions, loadComposerWeights,
  loadGenRefs, navigate, perform, playItem, playReference, queueRefPrefsPatch, rateSelected, resetComposerParamsToDefault,
  reuseAudioParams, saveComposerParamsAsDefault, selectCandidate, selectComposerVersion, setActiveCharacter,
  setReferenceMode, showError, startComparison, startGeneration, stopGeneration, switchToManualComposer, updateParams,
  useStore, type GenerateMode, type GenerateRightTab,
} from '../store'
import { AddToWork } from './AddToWork'
import { HistoryPanel } from './History'

const DebugPanel = lazy(() => import('./Debug').then((m) => ({ default: m.DebugPanel })))

const LANG_OPTIONS = [
  { value: 'ja', label: '日语' },
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英语' },
  { value: 'ko', label: '韩语' },
  { value: 'yue', label: '粤语' },
  { value: 'auto', label: '自动' },
]

const floatEq = (a: number | null | undefined, b: number | null | undefined) => {
  if (a == null && b == null) return true
  if (a == null || b == null) return false
  return Math.abs(a - b) < 1e-4
}

export function GeneratePage() {
  const s = useStore()
  const genPlanOk = usePlanners((p) => planReady(p.gen))
  const comparePlanOk = usePlanners((p) => planReady(p.compare))
  const { params } = s
  const character = currentCharacter(s)
  const ready = composerReady(s)
  const [debugItem, setDebugItem] = useState<AudioItem | null>(null)
  const [adding, setAdding] = useState<AudioItem | null>(null)
  const [refSearch, setRefSearch] = useState('')
  const [emotionFilter, setEmotionFilter] = useState(() => (s.referenceMode === '情绪' ? s.emotion : ''))
  const [compareExtra, setCompareExtra] = useState<string[]>([])
  const [savingPrefs, setSavingPrefs] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)

  // 换角色（或重新连上）时读模型版本、参考和角色偏好；同角色切页返回时不重刷覆盖未保存修改
  useEffect(() => {
    if (!s.connected || !s.models.length) return
    loadComposerVersions().catch(showError)
    void perform(() => loadGenRefs(params.character))
    const st = useStore.getState()
    if (st.prefsLoadedFor !== params.character) {
      loadCharacterPrefs(params.character, { applyToComposer: true }).catch(showError)
    } else if (!st.characterPrefs[params.character]) {
      loadCharacterPrefs(params.character, { applyToComposer: false }).catch(showError)
    }
  }, [s.connected, s.models.length, params.character])
  useEffect(() => {
    if (s.connected) loadComposerWeights().catch(showError)
  }, [s.connected, params.character, params.engine])

  useEffect(() => {
    if (!s.selectedCandidate) return
    listRef.current?.querySelector(`[data-id="${CSS.escape(s.selectedCandidate)}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [s.selectedCandidate])

  const compareEngines = useMemo(
    () => [...new Set([params.engine, ...compareExtra])].sort(),
    [params.engine, compareExtra],
  )
  const activeEngines = s.generateMode === 'compare' ? compareEngines : [params.engine]
  const unsupportedAuxEngines = activeEngines.filter((e) => !engineSupportsAuxRefs(s, e))
  const supportsAux = unsupportedAuxEngines.length === 0

  const savedPrefs = s.characterPrefs[params.character] ?? character?.prefs ?? null
  const defaultRefId = savedPrefs?.default_ref ?? character?.default_ref ?? null
  const activePrimaryRefId = s.referenceMode === '情绪' ? null : (params.ref_id ?? defaultRefId)

  // 角色参数状态：默认 / 已保存 / 未保存修改
  const paramStatus = useMemo(() => {
    const sp = savedPrefs?.synth_params
    const hasSaved = !!sp && Object.values(sp).some((v) => v !== null && v !== undefined)
    const base = {
      speed: sp?.speed ?? DEFAULT_SYNTH_PARAMS.speed,
      fragment_interval: sp?.fragment_interval ?? DEFAULT_SYNTH_PARAMS.fragment_interval,
      temperature: sp?.temperature ?? DEFAULT_SYNTH_PARAMS.temperature,
      repetition_penalty: sp?.repetition_penalty ?? DEFAULT_SYNTH_PARAMS.repetition_penalty,
      top_k: sp?.top_k ?? DEFAULT_SYNTH_PARAMS.top_k,
      top_p: sp?.top_p ?? DEFAULT_SYNTH_PARAMS.top_p,
      text_split_method: sp?.text_split_method ?? DEFAULT_SYNTH_PARAMS.text_split_method,
      sample_steps: sp?.sample_steps ?? DEFAULT_SYNTH_PARAMS.sample_steps,
      text_lang: sp?.text_lang ?? DEFAULT_SYNTH_PARAMS.text_lang,
    }
    const dirty = !floatEq(params.speed, base.speed)
      || !floatEq(params.fragment_interval, base.fragment_interval)
      || !floatEq(params.temperature, base.temperature)
      || !floatEq(params.repetition_penalty, base.repetition_penalty)
      || params.top_k !== base.top_k
      || !floatEq(params.top_p, base.top_p)
      || params.text_split_method !== base.text_split_method
      || (params.sample_steps ?? null) !== (base.sample_steps ?? null)
      || params.text_lang !== base.text_lang
    if (dirty) return { label: '未保存修改', cls: 'warn', dirty: true, hasSaved }
    if (hasSaved) return { label: '已保存', cls: 'ok', dirty: false, hasSaved }
    return { label: '默认', cls: '', dirty: false, hasSaved: false }
  }, [savedPrefs, params])

  // 仅展示当前活跃角色的参考，避免切换间隙点中上一角色参考
  const charRefs = useMemo(
    () => s.genRefs.filter((r) => r.character === params.character),
    [s.genRefs, params.character],
  )

  // 情绪随机与快捷筛选标签：仅提取存在至少一条合格 3–10s 主参考的标签
  const availableEmotions = useMemo(() => {
    const set = new Set<string>()
    for (const r of charRefs) {
      if (!isValidPrimaryRefDuration(r.duration)) continue
      for (const t of r.tags) set.add(t)
    }
    return [...set].sort()
  }, [charRefs])

  const filteredRefs = useMemo(() => {
    const q = refSearch.trim().toLowerCase()
    return charRefs.filter((r) => {
      if (emotionFilter && !r.tags.includes(emotionFilter)) return false
      if (!q) return true
      return r.text.toLowerCase().includes(q)
        || r.id.toLowerCase().includes(q)
        || r.tags.some((t) => t.toLowerCase().includes(q))
    })
  }, [charRefs, refSearch, emotionFilter])

  const randomByEmotion = s.referenceMode === '情绪'
  const selectPrimaryRef = (refId: string) => {
    const nextAux = (params.aux_ref_ids ?? []).filter((id) => id !== refId)
    setReferenceMode('参考库', '')
    updateParams({ ref_id: refId, aux_ref_ids: nextAux })
    queueRefPrefsPatch(params.character, {
      last_ref_id: refId,
      ...(nextAux.length !== (params.aux_ref_ids ?? []).length ? { aux_ref_ids: nextAux } : {}),
    })
  }
  const toggleAuxRef = (refId: string) => {
    if (!supportsAux || refId === activePrimaryRefId) return
    const cur = params.aux_ref_ids ?? []
    const next = cur.includes(refId) ? cur.filter((x) => x !== refId) : [...cur, refId]
    updateParams({ aux_ref_ids: next })
    queueRefPrefsPatch(params.character, { aux_ref_ids: next })
  }
  const toggleRandomByEmotion = (checked: boolean) => {
    if (checked) {
      const targetEmotion = (emotionFilter && availableEmotions.includes(emotionFilter) ? emotionFilter : '')
        || (s.emotion && availableEmotions.includes(s.emotion) ? s.emotion : '')
        || availableEmotions[0] || ''
      if (!targetEmotion) return
      if (emotionFilter !== targetEmotion) setEmotionFilter(targetEmotion)
      setReferenceMode('情绪', targetEmotion)
    } else {
      setReferenceMode(params.ref_id ? '参考库' : '默认', '')
    }
  }
  const chooseEmotionChip = (tag: string) => {
    setEmotionFilter(tag)
    if (randomByEmotion) {
      if (tag) setReferenceMode('情绪', tag)
      else setReferenceMode(params.ref_id ? '参考库' : '默认', '')
    }
  }

  const effectiveAuxIds = supportsAux
    ? (params.aux_ref_ids ?? []).filter((id) => id && id !== activePrimaryRefId)
    : []
  const planExtra = {
    ...(params.character ? { character: params.character } : {}),
    ...(params.version ? { version: params.version } : {}),
    ...(params.gpt ? { gpt: params.gpt } : {}),
    ...(params.sovits ? { sovits: params.sovits } : {}),
    ...(activePrimaryRefId ? { ref_id: activePrimaryRefId } : {}),
    ...(effectiveAuxIds.length ? { aux_ref_ids: effectiveAuxIds } : {}),
    ...(params.fragment_interval != null && Math.abs(params.fragment_interval - 0.3) > 1e-6
      ? { fragment_interval: params.fragment_interval } : {}),
  }

  const current = s.candidates.find((c) => c.id === s.selectedCandidate)
  const onListKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest('input, textarea, select, button, [role="menu"]')) return
    if (e.key === ' ') { if (current) playItem(current); e.preventDefault() }
    else if (e.key === 'ArrowUp') { selectCandidate(-1); e.preventDefault() }
    else if (e.key === 'ArrowDown') { selectCandidate(1); e.preventDefault() }
    else if (/^[1-5]$/.test(e.key) && !e.metaKey && !e.ctrlKey && !e.altKey) { rateSelected(Number(e.key)); e.preventDefault() }
  }

  const runPrimary = () => {
    if (s.generateMode === 'compare') {
      startComparison(compareEngines)
    } else {
      startGeneration()
    }
    listRef.current?.focus({ preventScroll: true })
  }

  const jobs = s.jobs.filter((j) => (j.kind === 'candidates' || j.kind === 'compare') && isActive(j))

  const weightOptions = (type: 'gpt' | 'sovits') => {
    const list = s.composerWeights.filter((w) => w.type === type)
    const cur = params[type]
    const version = s.composerVersions.rows.find((v) => v.id === s.composerVersions.selectedID)
    return [
      { value: '', label: '默认组合' },
      ...(cur && !list.some((w) => w.name === cur) ? [{ value: cur, label: `${version?.label ?? '当前模型'} · ${type === 'gpt' ? 'GPT' : 'SoVITS'}` }] : []),
      ...list.map((w) => ({ value: w.name, label: modelLabel(w) })),
    ]
  }

  return (
    <div className="page gen-page">
      {/* 顶部角色 + 模型版本条 + 模式切换 */}
      <div className="gen-topbar group">
        <div className="row gap wrap gen-topbar-row">
          <Select label="角色" value={params.character}
            options={s.models.map((m) => ({ value: m.slug, label: characterName(m.slug) }))}
            onChange={(v) => { setEmotionFilter(''); setRefSearch(''); setActiveCharacter(v) }} />
          <div className="gen-version-slot">
            <ModelVersionPicker versions={s.composerVersions} manual={s.composerManual} onSelect={selectComposerVersion} />
          </div>
          <button className="ghost" onClick={() => navigate('characters', 'versions')}>管理此角色 →</button>
          <span className="spacer" />
          <Segmented label="合成模式" value={s.generateMode}
            options={[{ value: 'candidates', label: '出候选' }, { value: 'compare', label: '对比引擎' }]}
            onChange={(m: GenerateMode) => useStore.setState({ generateMode: m })} />
        </div>
      </div>

      {/* 三栏响应式布局 */}
      <div className="gen-grid">
        {/* 左栏：参考音频 */}
        <section className="col gen-refs-col" aria-label="参考音频">
          <div className="row between wrap">
            <h3>参考音频</h3>
            <span className="muted small">{filteredRefs.length} / {charRefs.length} 条</span>
          </div>
          <div className="group stack tight gen-ref-panel">
            <input placeholder="搜索参考台词或标签" aria-label="搜索参考音频" value={refSearch} onChange={(e) => setRefSearch(e.target.value)} />
            {!!availableEmotions.length && (
              <div className="chips" role="group" aria-label="情绪筛选">
                <button className={`chip-btn ${!emotionFilter ? 'on' : ''}`} aria-pressed={!emotionFilter} onClick={() => chooseEmotionChip('')}>全部</button>
                {availableEmotions.map((tag) => (
                  <button key={tag} className={`chip-btn ${emotionFilter === tag ? 'on' : ''}`} aria-pressed={emotionFilter === tag}
                    onClick={() => chooseEmotionChip(emotionFilter === tag ? '' : tag)}>{tag}</button>
                ))}
              </div>
            )}
            <label className="check small">
              <input type="checkbox" checked={randomByEmotion} disabled={!availableEmotions.length}
                onChange={(e) => toggleRandomByEmotion(e.target.checked)} />
              按所选情绪随机{randomByEmotion && s.emotion ? `（${s.emotion}）` : ''}
            </label>
            <div className="gen-ref-list" role="list" aria-label="参考音频列表">
              {filteredRefs.map((r) => {
                const validPrimary = isValidPrimaryRefDuration(r.duration)
                const isPrimary = !randomByEmotion && activePrimaryRefId === r.id
                const isDefault = defaultRefId === r.id
                const isAux = (params.aux_ref_ids ?? []).includes(r.id)
                return (
                  <div key={r.id} role="listitem"
                    className={`gen-ref-item ${isPrimary ? 'selected' : ''} ${!validPrimary ? 'invalid-primary' : ''}`}
                    onClick={() => { if (validPrimary) selectPrimaryRef(r.id) }}>
                    <PlayButton id={'ref-' + r.id} label={`试听参考 ${r.id}`} onClick={() => playReference(r, false)} />
                    <div className="gen-ref-body">
                      <div className="clamp2 small">{r.text}</div>
                      <div className="row gap wrap muted tiny">
                        <span className="mono">{displaySeconds(r.duration)} 秒</span>
                        <span>{r.lang}</span>
                        {isDefault && <span className="badge acc">默认</span>}
                        {r.tags.map((t) => <span key={t} className="badge">{t}</span>)}
                      </div>
                      {!validPrimary && <div className="warn tiny">超出 3–10 秒，不可作主参考</div>}
                    </div>
                    <button className={`aux-btn ${isAux ? 'on' : ''}`}
                      disabled={!supportsAux || isPrimary}
                      aria-pressed={isAux}
                      aria-label={isAux ? `移出辅助参考 ${r.id}` : `加入辅助参考 ${r.id}`}
                      title={!supportsAux ? `当前引擎不支持辅助参考融合` : isPrimary ? '已是主参考' : isAux ? '移出辅助参考' : '加入辅助参考融合'}
                      onClick={(e) => { e.stopPropagation(); toggleAuxRef(r.id) }}>
                      {isAux ? '✓' : '+'}
                    </button>
                  </div>
                )
              })}
              {!filteredRefs.length && <div className="muted small empty compact">没有匹配的参考音频。</div>}
            </div>
            {!supportsAux && (
              <div className="muted small aux-note" role="note">
                当前引擎（{unsupportedAuxEngines.map(displayEngine).join(' / ')}）不支持辅助参考融合，已自动禁用。
              </div>
            )}
            {!!(params.aux_ref_ids?.length) && (
              <div className="aux-section">
                <div className="muted tiny">辅助参考融合（{params.aux_ref_ids.length}）{!supportsAux && ' · 当前引擎暂不生效'}</div>
                <div className="chips aux-pills">
                  {params.aux_ref_ids.map((id) => {
                    const ref = charRefs.find((r) => r.id === id)
                    return (
                      <span key={id} className={`aux-pill ${!supportsAux ? 'dim' : ''}`}>
                        <span className="aux-pill-text">{ref ? ref.text.slice(0, 14) : id}</span>
                        <button className="icon aux-remove" aria-label={`移除辅助参考 ${id}`}
                          onClick={() => {
                            const next = (params.aux_ref_ids ?? []).filter((x) => x !== id)
                            updateParams({ aux_ref_ids: next })
                            queueRefPrefsPatch(params.character, { aux_ref_ids: next })
                          }}>✕</button>
                      </span>
                    )
                  })}
                </div>
              </div>
            )}
            <div className="row between">
              <span className="muted tiny">{randomByEmotion ? `随机情绪：${s.emotion || '未选'}` : s.referenceMode === '参考库' && params.ref_id ? '已选主参考' : '使用角色默认参考'}</span>
              <button className="ghost small-btn" onClick={() => navigate('characters', 'refs')}>打标 / 管理</button>
            </div>
          </div>
        </section>

        {/* 中栏：台词与参数 */}
        <section className="col gen-main-col" aria-label="台词与参数">
          <div className="group stack">
            <div className="row between wrap gap">
              <h3>台词</h3>
              <div className="row gap wrap">
                <Select label="语言" value={params.text_lang} options={LANG_OPTIONS}
                  onChange={(v) => updateParams({ text_lang: v })} />
                <Select label="切句" value={params.text_split_method}
                  options={(['cut0', 'cut1', 'cut2', 'cut3', 'cut4', 'cut5'] as SplitMethod[]).map((v) => ({ value: v, label: displaySplit(v) }))}
                  onChange={(v) => updateParams({ text_split_method: v })} />
              </div>
            </div>
            <textarea className="script" aria-label="台词" value={params.text} onChange={(e) => updateParams({ text: e.target.value })} rows={4} />
          </div>

          {s.generateMode === 'compare' && (
            <div className="group stack">
              <div className="row between wrap">
                <h3>对比引擎</h3>
                <span className="muted small">已选 {compareEngines.length} 个（至少 2 个）</span>
              </div>
              <div className="chips" role="group" aria-label="对比引擎">
                {engineList(s).map((e) => (
                  <label key={e} className={`cpick ${compareEngines.includes(e) ? 'on' : ''}`}>
                    <input type="checkbox" checked={compareEngines.includes(e)} disabled={e === params.engine}
                      onChange={(ev) => setCompareExtra(ev.target.checked ? [...compareExtra, e] : compareExtra.filter((x) => x !== e))} />
                    {displayEngine(e)}
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="group stack param-card">
            <div className="row between wrap gap">
              <div className="row gap">
                <h3>角色参数</h3>
                <span className={`badge ${paramStatus.cls}`}>{paramStatus.label}</span>
              </div>
              <div className="row gap">
                <button className="small-btn" disabled={savingPrefs || (!paramStatus.dirty && paramStatus.hasSaved)}
                  onClick={() => { setSavingPrefs(true); void perform(saveComposerParamsAsDefault).finally(() => setSavingPrefs(false)) }}>
                  存为角色默认
                </button>
                <button className="small-btn ghost" disabled={savingPrefs || (!paramStatus.dirty && !paramStatus.hasSaved)}
                  onClick={() => { setSavingPrefs(true); void perform(resetComposerParamsToDefault).finally(() => setSavingPrefs(false)) }}>
                  恢复默认
                </button>
              </div>
            </div>
            <div className="slider-grid">
              <SliderField label="语速" value={params.speed} min={0.5} max={2.0} step={0.05}
                display={`${params.speed.toFixed(2)}×`} onChange={(v) => updateParams({ speed: v })} />
              <SliderField label="分段间隔" value={params.fragment_interval ?? 0.3} min={0.01} max={2.0} step={0.01}
                display={`${(params.fragment_interval ?? 0.3).toFixed(2)} 秒`} onChange={(v) => updateParams({ fragment_interval: v })} />
              <SliderField label="温度" value={params.temperature} min={0.1} max={2.0} step={0.05}
                display={params.temperature.toFixed(2)} onChange={(v) => updateParams({ temperature: v })} />
              <SliderField label="重复抑制" value={params.repetition_penalty} min={1.0} max={2.5} step={0.05}
                display={params.repetition_penalty.toFixed(2)} onChange={(v) => updateParams({ repetition_penalty: v })} />
              <SliderField label="Top K" value={params.top_k} min={1} max={100} step={1}
                display={String(params.top_k)} onChange={(v) => updateParams({ top_k: v })} />
              <SliderField label="Top P" value={params.top_p} min={0.1} max={1.0} step={0.05}
                display={params.top_p.toFixed(2)} onChange={(v) => updateParams({ top_p: v })} />
            </div>
          </div>

          <details className="disclosure group">
            <summary>进阶设置</summary>
            <div className="stack">
              {engineSupportsSampleSteps(params.engine) && (
                <NumberInput label="采样步数" step="1" min={1} value={params.sample_steps}
                  placeholder={params.engine === 'v5turbo' ? '默认 4' : '默认 32'}
                  onChange={(v) => updateParams({ sample_steps: v })} />
              )}
              <div className="row gap wrap">
                <label className="check">
                  <input type="checkbox" checked={s.fixedSeed}
                    onChange={(e) => {
                      const on = e.target.checked
                      useStore.setState({ fixedSeed: on })
                      if (on && params.seed < 0) updateParams({ seed: 42 })
                    }} />
                  固定种子
                </label>
                {s.fixedSeed && (
                  <NumberInput ariaLabel="固定种子值" step="1" min={0} value={params.seed >= 0 ? params.seed : 42}
                    onChange={(v) => updateParams({ seed: v ?? 0 })} />
                )}
              </div>
              <hr />
              <div className="muted small">手动指定权重（覆盖上方模型版本）</div>
              <div className="form-grid">
                <Select label="引擎" value={params.engine} options={engineList(s).map((e) => ({ value: e, label: engineLabel(s, e) }))}
                  onChange={(v) => { switchToManualComposer(); updateParams({ engine: v as Engine, gpt: null, sovits: null }) }} />
                <Select label="GPT" value={params.gpt ?? ''} options={weightOptions('gpt')}
                  onChange={(v) => { switchToManualComposer(); updateParams({ gpt: v || null }) }} />
                <Select label="SoVITS" value={params.sovits ?? ''} options={weightOptions('sovits')}
                  onChange={(v) => { switchToManualComposer(); updateParams({ sovits: v || null }) }} />
              </div>
            </div>
          </details>

          <div className="stack">
            <div className="row gap wrap">
              <div className="row gap stepper" role="group" aria-label="候选数">
                <button aria-label="减少候选" onClick={() => updateParams({ n: Math.max(1, params.n - 1) })}>−</button>
                <span>{params.n} 个候选</span>
                <button aria-label="增加候选" onClick={() => updateParams({ n: Math.min(16, params.n + 1) })}>+</button>
              </div>
              <button className="primary big grow"
                disabled={s.generationBusy || !ready || !params.text.trim()
                  || (s.generateMode === 'compare' ? (compareEngines.length < 2 || !comparePlanOk) : !genPlanOk)}
                onClick={runPrimary}>
                {s.generationBusy ? '正在生成…' : s.generateMode === 'compare' ? '♫ 开始对比' : '♫ 出候选'}
              </button>
            </div>
            {s.generateMode === 'compare' ? (
              <ComputePicker id="compare" input={{ kind: 'gen', engines: compareEngines, items: params.n, ...planExtra }}
                action="对比" ready={ready && compareEngines.length >= 2}
                notReadyText={compareEngines.length < 2 ? '选择至少 2 个引擎后估算' : '选择模型版本后估算'}
                scope={`${compareEngines.map(displayEngine).join(' / ')} 各 ${params.n} 句`}
                taskTitle={`对比 ${characterName(params.character)} · ${compareEngines.map(displayEngine).join(' / ')}`} />
            ) : (
              <ComputePicker id="gen" input={{ kind: 'gen', engine: params.engine, items: params.n, ...planExtra }}
                action="生成" ready={ready}
                scope={`${params.n} 句 ${displayEngine(params.engine)}`}
                taskTitle={`生成 ${characterName(params.character)} · ${displayEngine(params.engine)}（${params.n} 句）`} />
            )}
            <div className="muted small desktop-only">⌘/Ctrl ↩ 出候选 · 候选区：空格试听，↑↓ 切换，1–5 评分</div>
            {s.generationBusy && <button className="full" onClick={stopGeneration}>取消生成 / 停止等待</button>}
            {jobs.map((j) => <JobProgress key={j.id} job={j} />)}
          </div>
        </section>

        {/* 右栏：本次候选 / 历史记录 */}
        <section className="col gen-right-col" aria-label="候选与历史">
          <div className="row between wrap gap">
            <Segmented label="结果视图" value={s.generateRightTab}
              options={[
                { value: 'candidates', label: `本次候选 (${s.candidates.length})` },
                { value: 'history', label: '历史记录' },
              ]}
              onChange={(v: GenerateRightTab) => useStore.setState({ generateRightTab: v })} />
          </div>
          {s.generateRightTab === 'candidates' ? (
            <div className="candidate-list" ref={listRef} tabIndex={0} onKeyDown={onListKey} aria-label="候选列表（空格试听，↑↓ 切换，1–5 评分）">
              {!s.candidates.length && (
                <div className="empty"><div className="empty-title">等一句台词</div><div className="muted">生成后，候选会按评分排序出现在这里。</div></div>
              )}
              {s.candidates.map((item, i) => (
                <CandidateCard key={item.id} item={item} best={i === 0} selected={item.id === s.selectedCandidate}
                  showDownload
                  onSelect={() => { useStore.setState({ selectedCandidate: item.id }); listRef.current?.focus({ preventScroll: true }) }}
                  onDebug={() => setDebugItem(item)}
                  onReuse={() => reuseAudioParams(item)}
                  extra={<button className="small-btn" disabled={s.workBusy || s.workSaving} onClick={(e) => { e.stopPropagation(); setAdding(item) }}>加入作品</button>} />
              ))}
            </div>
          ) : (
            <HistoryPanel embedded onAddToWork={setAdding} />
          )}
        </section>
      </div>

      {debugItem && <Suspense fallback={null}><DebugPanel item={debugItem} onClose={() => setDebugItem(null)} /></Suspense>}
      {adding && <AddToWork item={adding} onClose={() => setAdding(null)} />}
    </div>
  )
}
