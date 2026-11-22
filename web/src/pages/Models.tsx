import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ApiError, getJSON, isActive, request, sendJSON } from '../api/client'
import type { BlindSide, ComboScore, DeletedWeight, EvalSetName, Job, ModelVersion, WeightEntry, WeightList, WeightPatch } from '../api/types'
import { rovingKeyDown } from '../a11y'
import { Confirm, EngineSelect, JobProgress, Modal, ModelVersionPicker, Segmented, Select, SortControl } from '../components'
import { ComputePicker } from '../compute'
import {
  displayAccuracy, displayDateTime, displayDuration, displayEngine, displayEvalSet, displayScore, displaySimilarity, displaySource,
  isZeroShot, modelLabel, modelVersionLabel, scoreHelp, weightType,
} from '../display'
import { useSortPref } from '../hooks'
import { planReady, selectedCandidate, submitPlanned, usePlanners } from '../planner'
import {
  cancelJob, characterName, emptyVersions, engineLabel, engineList, goToGenerate, isSettingDefault,
  loadComposerVersions, loadEvalSets, loadVersionList, modelCompletionKey, monitor, perform, recordLocalJob,
  reloadModels, runWithDefaultLock, setDefaultComboByNames, showError, startBlindWithSides, updateJob, useStore,
  visibleWeights, VERSION_GONE, type SortPref, type VersionList,
} from '../store'

const sorts = [
  { value: 'created_at', label: '创建时间' }, { value: 'score', label: '评测分' }, { value: 'epoch', label: '训练轮数' },
  { value: 'engine', label: '引擎' }, { value: 'character', label: '角色' }, { value: 'size', label: '大小' },
  { value: 'name', label: '显示名' }, { value: 'type', label: '类型' }, { value: 'source', label: '来源' },
]
const size = (b: number) => (b >= 1 << 30 ? `${(b / (1 << 30)).toFixed(2)} GB` : `${(b / (1 << 20)).toFixed(0)} MB`)
const evalSetOptions = (['hard', 'mini'] as const).map((v) => ({ value: v, label: displayEvalSet(v) }))

function useNarrow() {
  const query = '(max-width: 1100px)'
  const [narrow, setNarrow] = useState(() => matchMedia(query).matches)
  useEffect(() => {
    const m = matchMedia(query)
    const on = () => setNarrow(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return narrow
}

let submittedEval: { id: string; set: EvalSetName } | null = null

interface Pair { character: string; engine: string; gptID: string; sovitsID: string; manual: boolean }

export function ModelsPage({ character: propChar, embedded = false }: { character?: string; embedded?: boolean }) {
  const s = useStore()
  const activeChar = propChar ?? s.params.character
  const narrow = useNarrow()
  const completion = useStore(modelCompletionKey)
  const [evalSet, setEvalSet] = useState<EvalSetName>('hard')
  const [versions, setVersions] = useState<VersionList>(emptyVersions)
  const [charWeights, setCharWeights] = useState<WeightEntry[]>([])
  const [pickedForBlind, setPickedForBlind] = useState<string[]>([])
  const [evalTarget, setEvalTarget] = useState<ModelVersion | null>(null)
  const [busyVersion, setBusyVersion] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const reload = () => setTick((t) => t + 1)

  // 底部折叠区：单文件权重表 & 手动混搭
  const [tableChar, setTableChar] = useState(activeChar)
  const [engine, setEngine] = useState('')
  const [source, setSource] = useState('')
  const [type, setType] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [sort, setSort] = useSortPref('models', { sort: 'created_at', order: 'desc' })
  const [list, setList] = useState<WeightList>({ total: 0, weights: [] })
  const [shownSet, setShownSet] = useState<EvalSetName>('hard')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pair, setPair] = useState<Pair>(() => ({ character: activeChar, engine: s.params.engine, gptID: '', sovitsID: '', manual: false }))
  const [pairVersions, setPairVersions] = useState<VersionList>(emptyVersions)

  // 角色切换时同步当前角色版本与清理勾选
  const [prevChar, setPrevChar] = useState(activeChar)
  if (prevChar !== activeChar) {
    setPrevChar(activeChar)
    setPickedForBlind([])
    setTableChar(activeChar)
    setPair((p) => ({ ...p, character: activeChar, gptID: '', sovitsID: '', manual: false }))
    setPairVersions(emptyVersions())
  }

  useEffect(() => { void perform(loadEvalSets) }, [])

  // 读取当前角色的模型版本和权重（用于按 evalSet 显示难句/快速评分）
  useEffect(() => {
    if (!s.connected || !activeChar) return
    let stale = false
    loadVersionList(versions, activeChar, true).then((next) => {
      if (!stale) setVersions(next)
    }).catch(showError)
    getJSON<WeightList>('/weights', { query: { character: activeChar, hidden: false, eval_set: evalSet } })
      .then((w) => { if (!stale) setCharWeights(w.weights) })
      .catch(() => {})
    return () => { stale = true }
  }, [s.connected, activeChar, evalSet, tick, completion]) // eslint-disable-line react-hooks/exhaustive-deps

  // 折叠区单文件权重表
  useEffect(() => {
    if (!s.connected) return
    const ctrl = new AbortController()
    getJSON<WeightList>('/weights', {
      signal: ctrl.signal,
      query: { character: tableChar, engine, source, type, hidden: showHidden ? undefined : false, sort: sort.sort, order: sort.order, eval_set: evalSet },
    }).then((value) => {
      if (ctrl.signal.aborted) return
      setList(value)
      setShownSet(evalSet)
      setSelectedId((id) => (value.weights.some((w) => w.id === id) ? id : null))
    }).catch((e) => { if (!ctrl.signal.aborted) showError(e) })
    return () => ctrl.abort()
  }, [s.connected, tableChar, engine, source, type, showHidden, sort, evalSet, tick, completion])

  const defaultBusy = isSettingDefault(s, activeChar)
  // 以角色当前推荐/默认版本为准，不被其他引擎的自动默认误判为「已为默认」
  const isDefaultVersion = (v: ModelVersion) => !!v.recommended
  const defaultVersion = versions.rows.find((v) => v.recommended) ?? versions.rows[0] ?? null

  // 查找每个版本在当前 evalSet 下的评分
  const comboScoreFor = (v: ModelVersion): ComboScore | null => {
    const matches: ComboScore[] = []
    for (const w of charWeights) {
      for (const e of w.evals) {
        if (e.character === activeChar && e.eval_set === evalSet && e.gpt === v.gpt && e.sovits === v.sovits) {
          matches.push(e)
        }
      }
    }
    matches.sort((a, b) => b.evaluated_at.localeCompare(a.evaluated_at))
    return matches[0] ?? null
  }

  const toggleBlindPick = (id: string) => {
    setPickedForBlind((cur) => {
      if (cur.includes(id)) return cur.filter((x) => x !== id)
      if (cur.length >= 2) return cur
      return [...cur, id]
    })
  }
  const launchBlindPK = () => {
    if (pickedForBlind.length !== 2) return
    const [v1, v2] = pickedForBlind.map((id) => versions.rows.find((r) => r.id === id)).filter((x): x is ModelVersion => !!x)
    if (!v1 || !v2) return
    const toSide = (v: ModelVersion): BlindSide => ({ engine: v.engine as BlindSide['engine'], gpt: v.gpt, sovits: v.sovits, version: v.id })
    startBlindWithSides(activeChar, toSide(v1), toSide(v2), `${characterName(activeChar)} · ${v1.label} vs ${v2.label}`)
  }

  const setVersionDefault = (v: ModelVersion) => {
    if (defaultBusy) return
    setBusyVersion(v.id)
    void perform(async () => {
      const msg = await setDefaultComboByNames(activeChar, v.engine, v.gpt, v.sovits)
      if (msg) setNotice(msg)
      reload()
    }).finally(() => setBusyVersion(null))
  }

  const choosePairCharacter = (slug: string) => {
    if (slug === pair.character) return
    setPair((p) => ({ ...p, character: slug, gptID: '', sovitsID: '', manual: false }))
    setPairVersions(emptyVersions())
  }
  const switchToManualPair = () => {
    setPairVersions((v) => ({ ...v, selectedID: null, message: null }))
    setPair((p) => ({ ...p, manual: true }))
  }
  const filterCharacter = (slug: string) => { setTableChar(slug); if (slug) choosePairCharacter(slug) }
  const filterEngine = (value: string) => { setEngine(value); if (value) { switchToManualPair(); setPair((p) => withEngine(p, value)) } }
  const selected = list.weights.find((w) => w.id === selectedId) ?? null
  const select = (w: WeightEntry) => {
    setSelectedId(w.id)
    if (w.hidden) return
    setPairVersions((v) => ({ ...v, selectedID: null, message: null }))
    setPair((p) => {
      const next = withEngine({ ...p, manual: true, character: w.character ?? p.character }, w.engine)
      return w.type === 'gpt' ? { ...next, gptID: w.id } : { ...next, sovitsID: w.id }
    })
  }
  const clickHeader = (key: string) => setSort(sort.sort === key ? { ...sort, order: sort.order === 'desc' ? 'asc' : 'desc' } : { sort: key, order: 'desc' })
  const editor = selected && (
    <WeightEditor key={selected.id} weight={selected} onSaved={(message) => { setNotice(message); reload() }} />
  )

  return (
    <div className={embedded ? 'stack' : 'page'}>
      {/* 顶部当前默认声音卡片 */}
      <div className="card default-voice-card">
        <div className="row between wrap gap">
          <div className="stack tight">
            <div className="row gap wrap">
              <span className="muted small">当前默认声音</span>
              {defaultVersion && <span className="badge acc">{displayEngine(defaultVersion.engine)}</span>}
              {defaultVersion?.recommended && <span className="badge ok">推荐</span>}
            </div>
            <div className="strong">
              {defaultVersion ? `${characterName(activeChar)} · ${defaultVersion.label}` : `${characterName(activeChar)} · 暂无可用版本`}
            </div>
            {defaultVersion && (
              <div className="muted small">
                {defaultVersion.score != null && defaultVersion.score_set
                  ? `${displayEvalSet(defaultVersion.score_set)} · 评分 ${displayScore(defaultVersion.score)}`
                  : isZeroShot(defaultVersion) ? '底模（零样本）' : '尚未评测'}
              </div>
            )}
          </div>
          <button className="primary" disabled={!defaultVersion}
            onClick={() => void perform(() => goToGenerate({ character: activeChar, versionId: defaultVersion?.id }))}>
            去合成 →
          </button>
        </div>
      </div>

      {/* 工具栏：评测集切换 + 盲听双选浮动条 */}
      <div className="toolbar wrap">
        <Segmented label="评分评测集" value={evalSet} onChange={setEvalSet} options={evalSetOptions} />
        <span className="muted small">{versions.rows.length} 个模型版本</span>
        <span className="spacer" />
        <button onClick={() => void perform(async () => { await reloadModels(false); reload() })}>刷新</button>
      </div>

      {pickedForBlind.length > 0 && (
        <div className="pk-bar row between wrap gap" role="region" aria-label="盲听双选操作栏">
          <span>已勾选 <b>{pickedForBlind.length}</b> / 2 个版本{pickedForBlind.length < 2 ? '（再选 1 个即可发起盲听 PK）' : ''}</span>
          <div className="row gap">
            <button className="ghost" onClick={() => setPickedForBlind([])}>清除选择</button>
            <button className="primary" disabled={pickedForBlind.length !== 2} onClick={launchBlindPK}>发起盲听 PK</button>
          </div>
        </div>
      )}

      {notice && <div className="notice small selectable" role="status">{notice}</div>}

      {/* 主列表：已配对的模型版本 */}
      <div className="stack versions-list">
        {versions.rows.map((v) => {
          const checked = pickedForBlind.includes(v.id)
          const disabledCheck = !checked && pickedForBlind.length >= 2
          const combo = comboScoreFor(v)
          const scoreVal = combo?.score_median ?? (v.score_set === evalSet ? v.score : null)
          const isDef = isDefaultVersion(v)
          return (
            <div key={v.id} className={`card version-row ${checked ? 'selected' : ''}`} data-version-id={v.id}>
              <div className="row gap wrap version-row-inner">
                <input type="checkbox" checked={checked} disabled={disabledCheck}
                  aria-label={`勾选 ${v.label} 参与盲听 PK`}
                  onChange={() => toggleBlindPick(v.id)} />
                <div className="stack tight version-info">
                  <div className="row gap wrap">
                    <span className="strong">{v.label}</span>
                    <span className="badge">{displayEngine(v.engine)}</span>
                    {v.recommended && <span className="badge acc">推荐</span>}
                    {isDef && <span className="badge ok">默认</span>}
                  </div>
                  <div className="muted small row gap wrap">
                    <span title={scoreHelp(scoreVal)}>
                      {displayEvalSet(evalSet)} 评分 <b>{displayScore(scoreVal)}</b>
                    </span>
                    {combo && (
                      <>
                        <span>读音准确 {displayAccuracy(combo.cer_median)}</span>
                        <span>音色相似 {displaySimilarity(combo.spk_sim_median)}</span>
                      </>
                    )}
                    {v.created && <span>{displayDateTime(v.created)}</span>}
                  </div>
                </div>
                <div className="row gap wrap version-actions">
                  <button disabled={defaultBusy || isDef} onClick={() => setVersionDefault(v)}>
                    {isDef ? '已为默认' : busyVersion === v.id ? '设置中…' : '设为默认'}
                  </button>
                  <button onClick={() => setEvalTarget(v)}>评测</button>
                  <button className="primary" onClick={() => void perform(() => goToGenerate({ character: activeChar, versionId: v.id }))}>去合成</button>
                </div>
              </div>
            </div>
          )
        })}
        {!versions.loading && !versions.rows.length && (
          <div className="empty compact">
            <div className="empty-title">暂无可用模型版本</div>
            <div className="muted small">训练完成后模型版本会出现在这里，或确认设备已安装底模。</div>
          </div>
        )}
      </div>

      {/* 底部折叠区：原单文件权重表与手动混搭 */}
      <details className="disclosure group raw-weights-section">
        <summary>单文件权重与手动混搭（{list.total}）</summary>
        <div className="stack">
          <div className="toolbar wrap">
            <Select label="角色" value={tableChar} options={[{ value: '', label: '全部' }, ...s.models.map((m) => ({ value: m.slug, label: characterName(m.slug) }))]} onChange={filterCharacter} />
            <EngineSelect label="引擎" value={engine} allowAll onChange={filterEngine} />
            <Select label="来源" value={source} options={[{ value: '', label: '全部' }, ...['july', 'user', 'pretrained'].map((v) => ({ value: v, label: displaySource(v) }))]} onChange={setSource} />
            <Select label="类型" value={type} options={[{ value: '', label: '全部' }, { value: 'gpt', label: 'GPT' }, { value: 'sovits', label: 'SoVITS' }]} onChange={setType} />
            <label className="check"><input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} />显示已隐藏</label>
            <SortControl value={sort} onChange={setSort} options={sorts} />
          </div>

          {narrow ? (
            <div className="stack">
              {list.weights.map((w) => (
                <button key={w.id} className={`card weight-card ${w.id === selectedId ? 'selected' : ''}`} onClick={() => select(w)}>
                  <div className="row between gap">
                    <span className="strong break">{modelLabel(w)}</span>
                    <span className="row gap">{w.is_default && <span className="badge acc">默认</span>}{w.hidden && <span className="badge">隐藏</span>}</span>
                  </div>
                  <div className="muted small">{w.character ? characterName(w.character) : '—'} · {displayEngine(w.engine)} · {weightType(w.type)} · {displaySource(w.source)}</div>
                  <div className="row between small wrap">
                    <span>评分 · {displayEvalSet(shownSet)} {displayScore(w.best_eval?.score_median)} · 读音准确 {displayAccuracy(w.best_eval?.cer_median)} · 音色相似 {displaySimilarity(w.best_eval?.spk_sim_median)}</span>
                    <span className="muted">{displayDateTime(w.created_at)} · {size(w.size_bytes)}</span>
                  </div>
                </button>
              ))}
            </div>
          ) : (
            <div className="table-wrap">
              <table className="weights-table">
                <thead>
                  <tr>
                    <SortTh k="name" sort={sort} onClick={clickHeader}>显示名</SortTh>
                    <SortTh k="character" sort={sort} onClick={clickHeader}>角色</SortTh>
                    <SortTh k="engine" sort={sort} onClick={clickHeader}>引擎</SortTh>
                    <SortTh k="type" sort={sort} onClick={clickHeader}>类型</SortTh>
                    <SortTh k="source" sort={sort} onClick={clickHeader}>来源</SortTh>
                    <SortTh k="score" sort={sort} onClick={clickHeader} num>评分 · {displayEvalSet(shownSet)}</SortTh>
                    <th className="num">读音准确 / 音色相似</th>
                    <SortTh k="created_at" sort={sort} onClick={clickHeader}>创建时间</SortTh>
                    <SortTh k="size" sort={sort} onClick={clickHeader} num>大小</SortTh>
                    <th>默认</th><th>隐藏</th>
                  </tr>
                </thead>
                <tbody onKeyDown={(e) => rovingKeyDown(e, (el) => { const w = list.weights.find((x) => x.id === el.dataset.id); if (w) select(w) }, 'vertical')}>
                  {list.weights.map((w, i) => (
                    <tr key={w.id} data-roving data-id={w.id} aria-selected={w.id === selectedId} tabIndex={(selectedId ? w.id === selectedId : i === 0) ? 0 : -1}
                      className={`clickable ${w.id === selectedId ? 'selected' : ''} ${w.hidden ? 'dim' : ''}`} onClick={() => select(w)}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(w) } }}>
                      <td className="break" title={w.name}>{modelLabel(w)}</td>
                      <td>{w.character ? characterName(w.character) : '—'}</td>
                      <td>{displayEngine(w.engine)}</td>
                      <td>{weightType(w.type)}</td>
                      <td>{displaySource(w.source)}</td>
                      <td className="num" title={scoreHelp(w.best_eval?.score_median)}>{displayScore(w.best_eval?.score_median)}</td>
                      <td className="num nowrap">{displayAccuracy(w.best_eval?.cer_median)} / {displaySimilarity(w.best_eval?.spk_sim_median)}</td>
                      <td className="nowrap">{displayDateTime(w.created_at)}</td>
                      <td className="num nowrap">{size(w.size_bytes)}</td>
                      <td>{w.is_default ? '是' : '—'}</td>
                      <td>{w.hidden ? '是' : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!list.weights.length && <div className="muted small">没有匹配模型，选「全部」角色可查看底模。</div>}
          {!narrow && editor}
          {narrow && selected && <Modal title={modelLabel(selected)} onClose={() => setSelectedId(null)}>{editor}</Modal>}
          <hr />
          <PairPanel pair={pair} setPair={setPair} versions={pairVersions} setVersions={setPairVersions} evalSet={evalSet} tick={tick}
            choosePairCharacter={choosePairCharacter} switchToManualPair={switchToManualPair} onChanged={(message) => { setNotice(message); reload() }} />
        </div>
      </details>

      {evalTarget && (
        <EvalVersionModal character={activeChar} version={evalTarget} initialSet={evalSet}
          onClose={() => setEvalTarget(null)}
          onStarted={(msg) => { setNotice(msg); reload(); setEvalTarget(null) }} />
      )}
    </div>
  )
}

function EvalVersionModal({ character, version, initialSet, onClose, onStarted }: {
  character: string; version: ModelVersion; initialSet: EvalSetName; onClose: () => void; onStarted: (msg: string) => void
}) {
  const s = useStore()
  const evalPlan = usePlanners((p) => p.eval)
  const [evaluationSet, setEvaluationSet] = useState<EvalSetName>(initialSet)
  const set = s.evalSets.find((x) => x.name === evaluationSet)
  const items = set ? set.sentences.length * set.candidates : 36
  const chosen = selectedCandidate(evalPlan.plan)

  const run = () => void perform(async () => {
    const value = await submitPlanned<Job>('eval', '/weights/eval', {
      version: version.id, engine: version.engine, gpt: version.gpt, sovits: version.sovits, character, eval_set: evaluationSet,
    })
    recordLocalJob(value.id)
    updateJob(value)
    monitor(value)
    onStarted(`已提交 ${version.label} 的${displayEvalSet(evaluationSet)}`)
  })

  return (
    <Modal title={`评测模型版本 · ${version.label}`} onClose={onClose} closeLabel={null}
      footer={(
        <>
          <span className="spacer" />
          <button onClick={onClose}>取消</button>
          <button className="primary" disabled={evalPlan.submitting || !planReady(evalPlan)} onClick={run}>
            {evalPlan.submitting ? '正在提交…' : '开始评测'}
          </button>
        </>
      )}>
      <div className="stack">
        <div className="muted small">{characterName(character)} · {displayEngine(version.engine)}</div>
        <div className="row gap wrap">
          <Select label="评测集" value={evaluationSet} options={evalSetOptions} onChange={setEvaluationSet} />
          {chosen && <span className="muted small">{displayDuration(chosen.seconds)}</span>}
        </div>
        <ComputePicker id="eval" input={{ kind: 'eval', engine: version.engine, items }} action="评测" ready
          scope={`${displayEvalSet(evaluationSet)} · ${displayEngine(version.engine)}`}
          taskTitle={`评测 ${characterName(character)} · ${version.label}（${displayEvalSet(evaluationSet)}）`} />
      </div>
    </Modal>
  )
}

const withEngine = (p: Pair, engine: string): Pair => (p.engine === engine ? p : { ...p, engine, gptID: '', sovitsID: '' })

function SortTh({ k, sort, onClick, children, num }: { k: string; sort: SortPref; onClick: (k: string) => void; children: ReactNode; num?: boolean }) {
  const active = sort.sort === k
  return (
    <th className={`sortable ${num ? 'num' : ''} ${active ? 'active' : ''}`} aria-sort={active ? (sort.order === 'desc' ? 'descending' : 'ascending') : 'none'}>
      <button className="th-sort" onClick={() => onClick(k)}>{children}{active ? (sort.order === 'desc' ? ' ↓' : ' ↑') : ''}</button>
    </th>
  )
}

function WeightEditor({ weight: w, onSaved }: { weight: WeightEntry; onSaved: (message: string) => void }) {
  const [displayName, setDisplayName] = useState(w.display_name ?? '')
  const [note, setNote] = useState(w.note ?? '')
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const run = (action: () => Promise<void>) => {
    setBusy(true)
    void perform(action).finally(() => setBusy(false))
  }
  const patch = (body: WeightPatch) => sendJSON<WeightEntry>(`/weights/${encodeURIComponent(w.id)}`, body, { method: 'PATCH' })
  return (
    <div className="group stack weight-editor">
      <details className="disclosure small muted">
        <summary>模型详情</summary>
        <div className="selectable">文件：{w.name}</div>
        {w.epoch != null && <div>训练轮数：{w.epoch}</div>}
      </details>
      <div className="row gap wrap">
        <input className="grow" aria-label="显示名" placeholder="显示名" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        <input className="grow" aria-label="备注" placeholder="备注" value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <div className="row gap wrap">
        <button disabled={busy} onClick={() => run(async () => { await patch({ display_name: displayName.trim(), note }); onSaved('模型信息已保存') })}>保存名称 / 备注</button>
        <button disabled={busy} onClick={() => run(async () => { await patch({ hidden: !w.hidden }); onSaved(w.hidden ? '已取消隐藏' : '已隐藏') })}>{w.hidden ? '取消隐藏' : '隐藏'}</button>
        <button className="danger" disabled={busy || !w.deletable || w.source !== 'user'} onClick={() => setDeleting(true)}>删除…</button>
      </div>
      {deleting && (
        <Confirm message="删除这个模型？默认声音会使用可用的其他模型。" confirmLabel={`删除 ${modelLabel(w)}`} onClose={() => setDeleting(false)}
          onConfirm={() => run(async () => {
            const info = (await (await request(`/weights/${encodeURIComponent(w.id)}`, { method: 'DELETE' })).json()) as DeletedWeight
            await reloadModels(false)
            onSaved(info.was_default_for.length ? `已删除原默认模型，${info.was_default_for.map(characterName).join('、')}的默认声音已更新` : `已删除 ${modelLabel(w)}`)
          })} />
      )}
    </div>
  )
}

function PairPanel({ pair, setPair, versions, setVersions, evalSet, tick, choosePairCharacter, switchToManualPair, onChanged }: {
  pair: Pair; setPair: React.Dispatch<React.SetStateAction<Pair>>; versions: VersionList; setVersions: React.Dispatch<React.SetStateAction<VersionList>>
  evalSet: EvalSetName; tick: number; choosePairCharacter: (slug: string) => void; switchToManualPair: () => void; onChanged: (message: string) => void
}) {
  const s = useStore()
  const completion = useStore(modelCompletionKey)
  const evalPlan = usePlanners((p) => p.eval)
  const [rows, setRows] = useState<WeightEntry[]>([])
  const [evaluationSet, setEvaluationSet] = useState<EvalSetName>('hard')
  const [evalJob, setEvalJobState] = useState(submittedEval)
  const setEvalJob = (value: typeof submittedEval) => { submittedEval = value; setEvalJobState(value) }
  const [busy, setBusy] = useState(false)
  const handledJobId = useRef<string | null>(null)
  const version = versions.rows.find((v) => v.id === versions.selectedID)

  const manual = useRef(pair.manual)
  const latest = useRef(versions)
  useEffect(() => { manual.current = pair.manual; latest.current = versions })
  useEffect(() => {
    if (!s.connected || !pair.character) return
    let stale = false
    setVersions((v) => ({ ...v, loading: true }))
    loadVersionList(versions, pair.character, !pair.manual).then((next) => {
      if (stale) return
      const now = latest.current
      const picked = now.character === next.character && now.selectedID !== versions.selectedID && next.rows.some((v) => v.id === now.selectedID)
      const value = manual.current ? { ...next, selectedID: null } : picked ? { ...next, selectedID: now.selectedID, message: null } : next
      setVersions(value)
      const chosen = manual.current ? undefined : value.rows.find((v) => v.id === value.selectedID)
      if (chosen) setPair((p) => withEngine(p, chosen.engine))
    }).catch(showError).finally(() => { if (!stale) setVersions((v) => ({ ...v, loading: false })) })
    return () => { stale = true }
  }, [s.connected, pair.character, completion]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!s.connected || !pair.character || !pair.engine) return
    let stale = false
    visibleWeights(pair.character, pair.engine, evalSet).then((result) => {
      if (stale) return
      setRows(result)
      setPair((p) => {
        if (p.character !== pair.character || p.engine !== pair.engine) return p
        const fits = (id: string, kind: string) => result.some((w) => w.id === id && w.type === kind && w.engine === p.engine && (!w.character || w.character === p.character))
        if (version && !p.manual) {
          return {
            ...p,
            gptID: result.find((w) => w.type === 'gpt' && w.name === version.gpt)?.id ?? `version-gpt:${version.id}`,
            sovitsID: result.find((w) => w.type === 'sovits' && w.name === version.sovits)?.id ?? `version-sovits:${version.id}`,
          }
        }
        const defaults = s.models.find((m) => m.slug === p.character)?.engines[p.engine]
        const pick = (kind: 'gpt' | 'sovits', current: string, name: string | null | undefined) =>
          fits(current, kind) ? current : result.find((w) => w.type === kind && w.name === name)?.id ?? result.find((w) => w.type === kind)?.id ?? ''
        return { ...p, gptID: pick('gpt', p.gptID, defaults?.default_gpt), sovitsID: pick('sovits', p.sovitsID, defaults?.default_sovits) }
      })
    }).catch(showError)
    return () => { stale = true }
  }, [s.connected, pair.character, pair.engine, evalSet, versions.selectedID, pair.manual, tick, completion]) // eslint-disable-line react-hooks/exhaustive-deps

  const find = (id: string, kind: string) => rows.find((w) => w.id === id && w.type === kind && w.engine === pair.engine && (!w.character || w.character === pair.character))
  const pairGPT = find(pair.gptID, 'gpt')
  const pairSoVITS = find(pair.sovitsID, 'sovits')
  const pairReady = !!pair.character && !!pairGPT && !!pairSoVITS
  const own = (kind: 'gpt' | 'sovits', name: string) =>
    rows.find((w) => w.type === kind && w.name === name && w.engine === version?.engine && (!w.character || w.character === pair.character))
  const defaultGPT = version ? own('gpt', version.gpt) : pairGPT
  const defaultSoVITS = version ? own('sovits', version.sovits) : pairSoVITS
  const job = s.jobs.find((j) => j.id === evalJob?.id)
  const evaluating = evalPlan.submitting || (!!job && isActive(job))
  const set = s.evalSets.find((x) => x.name === evaluationSet)
  const items = set ? set.sentences.length * set.candidates : 36
  const comparisons = useMemo(() => {
    const latestMap = new Map<string, ComboScore>()
    for (const e of rows.filter((w) => w.engine === pair.engine).flatMap((w) => w.evals)) {
      if (e.character !== pair.character || e.eval_set !== evalSet) continue
      const key = `${e.gpt}|${e.sovits}`
      const prev = latestMap.get(key)
      if (!prev || prev.evaluated_at < e.evaluated_at) latestMap.set(key, e)
    }
    return [...latestMap.values()].sort((a, b) => b.score_median - a.score_median)
  }, [rows, pair.engine, pair.character, evalSet])

  useEffect(() => {
    if (!evalJob || job?.id !== evalJob.id || job.status !== 'succeeded' || handledJobId.current === evalJob.id) return
    handledJobId.current = evalJob.id
    submittedEval = null
    onChanged(evalJob.set === evalSet ? `${displayEvalSet(evalJob.set)} 组合评测完成，分数已刷新`
      : `${displayEvalSet(evalJob.set)} 组合评测完成；当前显示 ${displayEvalSet(evalSet)}，切换分数评测集查看本次结果`)
  }, [job?.status, job?.id, evalJob, evalSet, onChanged])

  const selectVersion = (id: string | null) => {
    const chosen = versions.rows.find((v) => v.id === id)
    setVersions((v) => ({ ...v, selectedID: id, message: null }))
    setPair((p) => (chosen ? withEngine({ ...p, manual: false }, chosen.engine) : { ...p, manual: false }))
  }
  const setDefault = () => {
    const gpt = defaultGPT, sovits = defaultSoVITS
    if (!pair.character || !gpt || !sovits || isSettingDefault(s, pair.character)) return
    const target = pair.character, targetEngine = gpt.engine
    setBusy(true)
    void perform(() => runWithDefaultLock(target, async () => {
      const patch = (w: WeightEntry) => sendJSON<WeightEntry>(`/weights/${encodeURIComponent(w.id)}`, { is_default: true, character: target }, { method: 'PATCH' })
      await patch(gpt)
      try { await patch(sovits) } catch (e) {
        await reloadModels(false)
        throw new Error(`GPT 默认已更新，SoVITS 设置未完成：${e instanceof Error ? e.message : String(e)}`)
      }
      await reloadModels(false)
      if (useStore.getState().params.character === target) await loadComposerVersions({ preferRecommended: true })
      onChanged(`已设为 ${characterName(target)} / ${displayEngine(targetEngine)} 的默认组合`)
    })).finally(() => setBusy(false))
  }
  const evaluate = () => perform(async () => {
    const gpt = version?.gpt ?? pairGPT?.name
    const sovits = version?.sovits ?? pairSoVITS?.name
    if (!gpt || !sovits) return
    const submittedSet = evaluationSet
    try {
      const value = await submitPlanned<Job>('eval', '/weights/eval', {
        version: version?.id ?? null, engine: version?.engine ?? pair.engine, gpt, sovits, character: pair.character, eval_set: submittedSet,
      })
      recordLocalJob(value.id)
      updateJob(value)
      monitor(value)
      handledJobId.current = null
      setEvalJob({ id: value.id, set: submittedSet })
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_unavailable') {
        const next = await loadVersionList(versions, pair.character, false)
        setVersions({ ...next, selectedID: null, message: VERSION_GONE })
        throw new ApiError(409, VERSION_GONE, { code: e.code })
      }
      throw e
    }
  })
  const halves = (kind: 'gpt' | 'sovits') => {
    const current = kind === 'gpt' ? pairGPT : pairSoVITS
    const id = kind === 'gpt' ? pair.gptID : pair.sovitsID
    return [
      { value: '', label: `请选择 ${weightType(kind)}` },
      ...(version && !current && id ? [{ value: id, label: `${version.label} · ${weightType(kind)}` }] : []),
      ...rows.filter((w) => w.type === kind).map((w) => ({ value: w.id, label: modelLabel(w) })),
    ]
  }
  const chosen = selectedCandidate(evalPlan.plan)
  const locked = busy || evaluating || isSettingDefault(s, pair.character)

  return (
    <section className="stack pair-panel">
      <div className="row gap wrap">
        <h3>手动混搭与评测</h3>
        <Select label="角色" value={pair.character} disabled={locked} options={s.models.map((m) => ({ value: m.slug, label: characterName(m.slug) }))} onChange={choosePairCharacter} />
        <span className="field-label">引擎</span><span className="field-value">{displayEngine(pair.engine)}</span>
        <span className="spacer" />
        <button disabled={!pair.character || !defaultGPT || !defaultSoVITS || locked} onClick={setDefault}>设为默认组合</button>
        <button className="primary" disabled={(!version && !pair.manual) || (!pairReady && !version) || locked || !planReady(evalPlan)} onClick={() => void evaluate()}>评测</button>
        {evaluating && job && <button onClick={() => void perform(() => cancelJob(job))}>取消评测 / 停止等待</button>}
      </div>
      <ModelVersionPicker versions={versions} manual={pair.manual} disabled={evaluating} onSelect={selectVersion} />
      <ComputePicker id="eval" input={{ kind: 'eval', engine: pair.engine, items }} action="评测" ready={!!version || pair.manual}
        scope={`${displayEvalSet(evaluationSet)} · ${displayEngine(pair.engine)}`} taskTitle={`评测 ${characterName(pair.character)} · ${displayEvalSet(evaluationSet)}`} />
      <div className="row gap wrap">
        <Select label="评测使用" value={evaluationSet} disabled={evaluating} options={evalSetOptions} onChange={setEvaluationSet} />
        {chosen && <span className="muted small">{displayDuration(chosen.seconds)}</span>}
      </div>
      <details className="disclosure" open={pair.manual}>
        <summary>手动选择 GPT / SoVITS</summary>
        <div className="form-grid">
          <Select label="引擎" value={pair.engine} disabled={locked} options={engineList(s).map((e) => ({ value: e, label: engineLabel(s, e) }))}
            onChange={(v) => { switchToManualPair(); setPair((p) => withEngine(p, v)) }} />
          <Select label="GPT" value={pair.gptID} disabled={locked} options={halves('gpt')} onChange={(v) => { switchToManualPair(); setPair((p) => ({ ...p, gptID: v })) }} />
          <Select label="SoVITS" value={pair.sovitsID} disabled={locked} options={halves('sovits')} onChange={(v) => { switchToManualPair(); setPair((p) => ({ ...p, sovitsID: v })) }} />
        </div>
      </details>
      {job && <JobProgress job={job} />}
      {job?.error && <div className="error-text small">{job.error}</div>}
      {comparisons.length ? (
        <div className="table-wrap">
          <table className="eval-table">
            <thead><tr><th>模型版本</th><th className="num">评分 · {displayEvalSet(evalSet)}</th><th className="num">读音准确</th><th className="num">音色相似</th></tr></thead>
            <tbody>
              {comparisons.map((c) => (
                <tr key={`${c.gpt}|${c.sovits}`}>
                  <td className="break">{modelVersionLabel(c.gpt, c.sovits, versions.rows, rows)}</td>
                  <td className="num" title={scoreHelp(c.score_median)}>{displayScore(c.score_median)}</td>
                  <td className="num">{displayAccuracy(c.cer_median)}</td>
                  <td className="num">{displaySimilarity(c.spk_sim_median)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="muted small">当前角色 / 引擎还没有版本评测。</div>}
    </section>
  )
}
