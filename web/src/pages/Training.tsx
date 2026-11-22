import { useEffect, useRef, useState } from 'react'
import { getJSON, isActive, jobStatusLabel, request } from '../api/client'
import type {
  BlindSide, DatasetInfo, DatasetList, DatasetPreview, Engine, EngineInfo, Job, LoraRank, ModelVersion, SweepRequest,
  SweepTrain, TrainDefaults, TrainEval, TrainRequest, WeightEntry,
} from '../api/types'
import { Confirm, EngineSelect, JobProgress, Metric, NumberInput, PageHeading, Segmented, Select, SortControl } from '../components'
import { ComputePicker } from '../compute'
import { displayAccuracy, displayEngine, displayEvalSet, displayScore, displaySimilarity, modelVersionLabel, scoreHelp, sortEngines } from '../display'
import { useSortPref } from '../hooks'
import { planReady, submitPlanned, usePlanners } from '../planner'
import {
  cancelJob, characterName, engineInfoOf, fetchVersions, fmt, isDeletedTrainingCharacter,
  isSettingDefault, loadEvaluation, loadTrainingJobs, monitor, perform, preferredEngine, recordLocalJob,
  setDefaultComboByNames, startBlindWithSides, submitTraining, trainableComputeEngines, tryCombination, updateJob,
  useStore, visibleWeights, type SortPref,
} from '../store'

const jobSorts = [
  { value: 'created', label: '创建时间' }, { value: 'started', label: '开始时间' }, { value: 'finished', label: '结束时间' },
  { value: 'kind', label: '任务类型' }, { value: 'status', label: '状态' }, { value: 'progress', label: '进度' },
]

const LORA_ENGINES = new Set(['v3', 'v4', 'v5turbo', 'v5dev'])
const FALLBACK_LORA_RANKS: LoraRank[] = [16, 32, 64, 128]

function defaultsForEngine(info: EngineInfo | undefined, engineId: string): TrainDefaults {
  if (info?.train_defaults) return info.train_defaults
  if (LORA_ENGINES.has(engineId)) {
    return {
      sovits_epochs: 2, sovits_save_every: 1, sovits_batch: 4,
      gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8,
      lora: true, lora_rank: 32, lora_ranks: [...FALLBACK_LORA_RANKS],
    }
  }
  return {
    sovits_epochs: 8, sovits_save_every: 4, sovits_batch: 8,
    gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8,
    lora: false, lora_rank: null, lora_ranks: [],
  }
}

export function TrainingPage() {
  const s = useStore()
  const [mode, setMode] = useState<'train' | 'sweep'>('train')
  const [cancelling, setCancelling] = useState<Job | null>(null)
  const [jobSort, setJobSort] = useSortPref('training', { sort: 'created', order: 'desc' })

  useEffect(() => {
    if (s.connected) void perform(() => loadTrainingJobs(jobSort))
  }, [s.connected]) // eslint-disable-line react-hooks/exhaustive-deps

  const isTraining = (j: Job) => j.kind === 'train' || j.kind === 'sweep'
  const trainJobs = [
    ...s.jobs.filter((j) => isTraining(j) && !s.trainOrder.includes(j.id)),
    ...s.trainOrder.flatMap((id) => s.jobs.filter((j) => j.id === id)),
  ]

  return (
    <div className="page">
      <PageHeading>先看数据和过滤结果，再决定训练。预览不会占用 GPU。</PageHeading>
      <div className="split">
        <div className="col config">
          <Segmented label="任务" value={mode} onChange={setMode} options={[{ value: 'train', label: '训练' }, { value: 'sweep', label: '扫参' }]} />
          {mode === 'train' ? <TrainForm /> : <SweepForm sort={jobSort} />}
        </div>
        <div className="col">
          <div className="row between wrap"><h2>训练任务</h2>
            <div className="row gap wrap">
              <SortControl value={jobSort} options={jobSorts}
                onChange={(v) => { setJobSort(v); void perform(() => loadTrainingJobs(v)) }} />
              <button onClick={() => void perform(() => loadTrainingJobs(jobSort))}>刷新</button>
            </div>
          </div>
          {trainJobs.map((job) => (
            <div key={job.id} className="card stack">
              <div className="row gap wrap">
                <span className="strong">{job.target}</span>
                {job.kind === 'sweep' && <span className="badge">扫参</span>}
                <span className="muted">{jobStatusLabel[job.status]}</span>
                {job.compute && <span className="muted small">· {job.compute.name}</span>}
                <span className="spacer" />
                {isActive(job) && <button className="danger" onClick={() => setCancelling(job)}>取消任务…</button>}
                {job.kind === 'train' && job.status === 'succeeded' && s.trainingCatalogLoaded && !isDeletedTrainingCharacter(s, job) && (
                  <button onClick={() => void perform(() => loadEvaluation(job))}>查看评测</button>
                )}
              </div>
              {isDeletedTrainingCharacter(s, job) && <div className="muted small">角色已删除</div>}
              <JobProgress job={job} />
              {job.error && <div className="error-text small">{job.error}</div>}
              <details className="disclosure">
                <summary>日志尾部</summary>
                <pre className="log selectable">{job.log_tail.join('\n')}</pre>
              </details>
            </div>
          ))}
          {!trainJobs.length && <div className="muted">还没有训练任务。</div>}
          {s.evaluation && <Evaluation key={s.evaluation.job_id} ev={s.evaluation} />}
        </div>
      </div>
      {cancelling && (
        <Confirm message={`取消训练任务？将停止 ${cancelling.target}（${cancelling.id}），未保存的训练进度会丢失。`} confirmLabel="取消训练" cancelLabel="继续训练"
          onClose={() => setCancelling(null)} onConfirm={() => void perform(() => cancelJob(cancelling))} />
      )}
    </div>
  )
}

function TrainForm() {
  const s = useStore()
  const trainPlanner = usePlanners((p) => p.train)
  const submitting = trainPlanner.submitting
  const planOk = planReady(trainPlanner)
  const [datasets, setDatasets] = useState<DatasetInfo[]>([])
  const [targetMode, setTargetMode] = useState<'existing' | 'new'>('existing')
  const [targetModeTouched, setTargetModeTouched] = useState(false)
  const [engineTouched, setEngineTouched] = useState(false)
  const [touchedFields, setTouchedFields] = useState<string[]>([])
  const computeSupported = trainableComputeEngines(s)
  const trainable = sortEngines(s.engineInfos.filter((e) => e.trainable).map((e) => e.id))
  const supportedTrainable = computeSupported.size ? trainable.filter((id) => computeSupported.has(id)) : trainable
  const preferredTrainEngine = preferredEngine(
    s.commonModels,
    supportedTrainable.length ? supportedTrainable : (trainable.length ? trainable : undefined),
  )
  const [req, setReq] = useState<TrainRequest>(() => {
    const initialEngine = preferredTrainEngine
    const td = defaultsForEngine(engineInfoOf(s, initialEngine), initialEngine)
    return {
      slug: s.models.length ? s.params.character : '',
      dataset_id: '',
      min_sec: 1,
      max_sec: 12,
      exclude_interjections: true,
      max_lines: null,
      engine: initialEngine,
      approved: false,
      sovits_epochs: td.sovits_epochs,
      sovits_batch: td.sovits_batch,
      sovits_save_every: td.sovits_save_every,
      gpt_epochs: td.gpt_epochs,
      gpt_batch: td.gpt_batch,
      gpt_save_every: td.gpt_save_every,
      lora_rank: td.lora ? (td.lora_rank ?? 32) : null,
    }
  })
  const [preview, setPreview] = useState<DatasetPreview | null>(null)
  const [previewFor, setPreviewFor] = useState('')
  const [previewErrorFor, setPreviewErrorFor] = useState<{ key: string; message: string } | null>(null)
  const seq = useRef(0)
  const patch = (p: Partial<TrainRequest>) => setReq((r) => ({ ...r, ...p }))
  const markTouched = (k: keyof TrainRequest) => setTouchedFields((cur) => (cur.includes(k) ? cur : [...cur, k]))

  // 冷开加载完引擎目录/算力后，若用户未手动改动引擎，自动对齐到当前「常用模型」优先引擎，但绝不覆盖用户已编辑过的超参字段
  const [syncedPreferred, setSyncedPreferred] = useState(() => (trainable.length ? preferredTrainEngine : ''))
  if (!engineTouched && trainable.length && syncedPreferred !== preferredTrainEngine) {
    setSyncedPreferred(preferredTrainEngine)
    if (req.engine !== preferredTrainEngine) {
      const td = defaultsForEngine(engineInfoOf(s, preferredTrainEngine), preferredTrainEngine)
      const touched = new Set(touchedFields)
      setReq((r) => ({
        ...r,
        engine: preferredTrainEngine,
        sovits_epochs: touched.has('sovits_epochs') ? r.sovits_epochs : td.sovits_epochs,
        sovits_save_every: touched.has('sovits_save_every') ? r.sovits_save_every : td.sovits_save_every,
        sovits_batch: touched.has('sovits_batch') ? r.sovits_batch : td.sovits_batch,
        gpt_epochs: touched.has('gpt_epochs') ? r.gpt_epochs : td.gpt_epochs,
        gpt_save_every: touched.has('gpt_save_every') ? r.gpt_save_every : td.gpt_save_every,
        gpt_batch: touched.has('gpt_batch') ? r.gpt_batch : td.gpt_batch,
        lora_rank: !td.lora ? null : (touched.has('lora_rank') ? r.lora_rank : (td.lora_rank ?? 32)),
      }))
    }
  }

  const engine = (trainable.includes(req.engine) ? req.engine : preferredTrainEngine) as Engine
  const currentTd = defaultsForEngine(engineInfoOf(s, engine), engine)

  const applyEngineDefaults = (eng: Engine) => {
    const td = defaultsForEngine(engineInfoOf(s, eng), eng)
    setTouchedFields([])
    patch({
      engine: eng,
      sovits_epochs: td.sovits_epochs,
      sovits_save_every: td.sovits_save_every,
      sovits_batch: td.sovits_batch,
      gpt_epochs: td.gpt_epochs,
      gpt_save_every: td.gpt_save_every,
      gpt_batch: td.gpt_batch,
      lora_rank: td.lora ? (td.lora_rank ?? 32) : null,
    })
  }

  // 冷开训练页：若用户尚未手动切到「新建角色」，等 /models 返回非空角色列表后自动落到「已有角色」并选中默认角色
  const [syncedModels, setSyncedModels] = useState(s.models.length > 0)
  if (!syncedModels && s.models.length > 0) {
    setSyncedModels(true)
    if (!targetModeTouched) {
      const firstSlug = s.models.some((m) => m.slug === s.params.character) ? s.params.character : s.models[0].slug
      setTargetMode('existing')
      setReq((r) => {
        if (r.slug) return r
        const matchedChar = s.models.find((m) => m.slug === firstSlug)
        const charDs = matchedChar?.dataset_id && datasets.some((d) => d.id === matchedChar.dataset_id) ? matchedChar.dataset_id : r.dataset_id
        return { ...r, slug: firstSlug, dataset_id: charDs }
      })
    }
  }

  useEffect(() => {
    if (!s.connected) return
    void perform(async () => {
      const list = await getJSON<DatasetList>('/datasets')
      setDatasets(list.datasets)
      setReq((r) => {
        if (r.dataset_id) return r
        const matchedChar = s.models.find((m) => m.slug === r.slug)
        const charDs = matchedChar?.dataset_id && list.datasets.some((d) => d.id === matchedChar.dataset_id) ? matchedChar.dataset_id : ''
        return { ...r, dataset_id: charDs || list.datasets[0]?.id || '' }
      })
    })
  }, [s.connected]) // eslint-disable-line react-hooks/exhaustive-deps

  const previewKey = `${req.dataset_id}|${req.min_sec}|${req.max_sec}|${req.exclude_interjections}`
  const previewError = previewErrorFor?.key === previewKey ? previewErrorFor.message : null
  const previewLoading = !!s.connected && !!req.dataset_id && previewFor !== previewKey && !previewError

  useEffect(() => {
    if (!s.connected || !req.dataset_id) return
    const id = ++seq.current
    const key = previewKey
    const t = setTimeout(() => {
      request(`/datasets/${encodeURIComponent(req.dataset_id)}/preview`, {
        method: 'POST', query: { min_sec: req.min_sec, max_sec: req.max_sec, exclude_interjections: req.exclude_interjections },
      })
        .then((r) => r.json() as Promise<DatasetPreview>)
        .then((p) => { if (id === seq.current) { setPreview(p); setPreviewFor(key); setPreviewErrorFor(null) } })
        .catch((e: Error) => { if (id === seq.current) { setPreview(null); setPreviewErrorFor({ key, message: e.message }) } })
    }, 350)
    return () => clearTimeout(t)
  }, [s.connected, previewKey, req.dataset_id, req.min_sec, req.max_sec, req.exclude_interjections])

  const selectExistingCharacter = (slug: string) => {
    const matched = s.models.find((m) => m.slug === slug)
    const ds = matched?.dataset_id && datasets.some((d) => d.id === matched.dataset_id) ? matched.dataset_id : req.dataset_id
    patch({ slug, dataset_id: ds })
  }

  const switchTargetMode = (next: 'existing' | 'new') => {
    setTargetModeTouched(true)
    setTargetMode(next)
    if (next === 'existing') {
      const slug = s.models.some((m) => m.slug === req.slug) ? req.slug : (s.params.character || s.models[0]?.slug || '')
      selectExistingCharacter(slug)
    } else {
      patch({ slug: '' })
    }
  }

  const slugOk = /^[a-z0-9][a-z0-9-]{1,40}$/.test(req.slug)
  const slugExists = targetMode === 'new' && !!req.slug && s.models.some((m) => m.slug === req.slug)
  const sovitsBatchOk = Number.isInteger(req.sovits_batch) && req.sovits_batch >= 1
  const gptBatchOk = Number.isInteger(req.gpt_batch) && req.gpt_batch >= 1
  const epochsOk = req.sovits_epochs >= 1 && req.gpt_epochs >= 1 && req.sovits_save_every >= 1 && req.gpt_save_every >= 1
  const maxLinesOk = req.max_lines === null || req.max_lines === undefined || req.max_lines >= 10
  const previewReady = !!preview && previewFor === previewKey && !previewLoading
  const lines = Math.min(preview?.lines_kept ?? 0, req.max_lines ?? Infinity)

  const blockReason = !req.slug
    ? (targetMode === 'existing' ? '请先选择目标角色' : '请输入新角色的 slug')
    : !slugOk ? '角色 slug 只能包含小写英文、数字和连字符（2–41 位，不能以连字符开头）'
      : slugExists ? '该角色已存在，请改名或切到「已有角色」'
        : !req.dataset_id ? '请先选择训练数据集'
          : req.min_sec > req.max_sec ? '最短时长不能大于最长时长'
            : !sovitsBatchOk ? 'SoVITS 每批大小至少为 1'
              : !gptBatchOk ? 'GPT 每批大小至少为 1'
                : !epochsOk ? '训练轮数与保存间隔均需至少为 1'
                  : !maxLinesOk ? '最多句数至少为 10'
                    : !trainable.includes(engine) ? `当前引擎 ${displayEngine(engine)} 不支持训练`
                      : previewLoading ? '正在加载数据集过滤预览…'
                        : previewError ? '数据集过滤预览失败，请调整条件重试'
                          : !previewReady ? '等待数据集过滤预览完成'
                            : lines <= 0 ? '当前过滤条件下保留 0 句，无法训练'
                              : trainPlanner.loading ? '正在估算算力报价…'
                                : trainPlanner.error ? `算力估算失败：${trainPlanner.error}`
                                  : !planOk ? '当前暂无可用算力设备，请检查算力设置'
                                    : null

  const canSubmit = !blockReason && !submitting
  const plan = {
    kind: 'train', engine, lines, gpt_epochs: req.gpt_epochs, sovits_epochs: req.sovits_epochs,
    gpt_save_every: req.gpt_save_every, sovits_save_every: req.sovits_save_every,
  }
  const int = (key: keyof TrainRequest, label: string) => (
    <NumberInput value={req[key] as number} step="1" min={1} placeholder="" className="num-input" ariaLabel={label}
      onChange={(v) => { markTouched(key); patch({ [key]: v ?? 0 } as Partial<TrainRequest>) }} />
  )
  const rankOptions = (currentTd.lora_ranks.length ? currentTd.lora_ranks : FALLBACK_LORA_RANKS) as LoraRank[]

  return (
    <>
      <section>
        <h3>数据集与过滤</h3>
        <div className="group stack">
          <Select label="数据集" value={req.dataset_id}
            options={[{ value: '', label: '选择数据集' }, ...datasets.map((d) => ({ value: d.id, label: `${d.name} · ${d.lines.toLocaleString()} 句 · 约 ${Math.max(1, Math.round(d.total_minutes))} 分钟` }))]}
            onChange={(v) => patch({ dataset_id: v })} />
          <div className="row gap">
            <span>时长</span>
            <input type="number" inputMode="decimal" aria-label="最短秒" className="grow" value={req.min_sec} onChange={(e) => patch({ min_sec: Number(e.target.value) })} />
            <span>–</span>
            <input type="number" inputMode="decimal" aria-label="最长秒" className="grow" value={req.max_sec} onChange={(e) => patch({ max_sec: Number(e.target.value) })} />
            <span>秒</span>
          </div>
          <label className="check"><input type="checkbox" checked={req.exclude_interjections} onChange={(e) => patch({ exclude_interjections: e.target.checked })} />排除纯语气词</label>
          {previewLoading ? <div className="muted">过滤预览…</div> : preview && (
            <>
              <div className="metrics two">
                <Metric title="保留句数" value={`${preview.lines_kept} / ${preview.lines_total}`} />
                <Metric title="保留时长" value={`约 ${Math.round(preview.minutes_kept)} 分钟`} help={`${fmt(preview.minutes_kept, 1)} 分钟`} />
              </div>
              <div className="muted small">过短 {preview.excluded_short} · 过长 {preview.excluded_long} · 语气词 {preview.excluded_interjection}</div>
              <div className="small clamp3">语气词示例：{preview.interjection_examples.slice(0, 12).join('、')}</div>
            </>
          )}
          {previewError && <div className="error-text small">{previewError}</div>}
        </div>
      </section>
      <section>
        <h3>训练配置</h3>
        <div className="group stack">
          <div className="row between wrap gap">
            <span className="small muted">目标角色</span>
            <Segmented label="目标角色方式" value={targetMode} onChange={switchTargetMode}
              options={[{ value: 'existing', label: '已有角色' }, { value: 'new', label: '新建角色' }]} />
          </div>
          {targetMode === 'existing' ? (
            <Select label="角色" value={req.slug}
              options={[{ value: '', label: '选择已有角色' }, ...s.models.map((m) => ({ value: m.slug, label: `${characterName(m.slug)}（${m.slug}）` }))]}
              onChange={selectExistingCharacter} />
          ) : (
            <>
              <input placeholder="角色 slug（小写英文、数字、连字符）" aria-label="角色 slug" value={req.slug} onChange={(e) => patch({ slug: e.target.value })} />
              {req.slug && !slugOk && <div className="error-text small">只能用小写英文、数字和连字符，2–41 个字符，不能以连字符开头。</div>}
              {slugExists && <div className="error-text small">该角色已存在，请改名或切到「已有角色」。</div>}
            </>
          )}
          <div className="row gap wrap">
            <div className="grow">
              <EngineSelect label="引擎" value={engine} filter={(e) => e.trainable} supportedEngines={computeSupported}
                onChange={(v) => { setEngineTouched(true); applyEngineDefaults(v as Engine) }} />
            </div>
            <button className="ghost small-btn" onClick={() => applyEngineDefaults(engine)}>恢复此引擎默认</button>
          </div>
          {currentTd.lora && (
            <Select label="LoRA Rank" value={req.lora_rank ?? currentTd.lora_rank ?? 32}
              options={rankOptions.map((r) => ({ value: r, label: String(r) }))}
              onChange={(v) => { markTouched('lora_rank'); patch({ lora_rank: Number(v) as LoraRank }) }} />
          )}
          <div className="train-grid">
            <span /><span className="muted small">轮数</span><span className="muted small">每批</span><span className="muted small">保存间隔</span>
            <span>SoVITS</span>{int('sovits_epochs', 'SoVITS 轮数')}{int('sovits_batch', 'SoVITS 每批')}{int('sovits_save_every', 'SoVITS 保存间隔')}
            <span>GPT</span>{int('gpt_epochs', 'GPT 轮数')}{int('gpt_batch', 'GPT 每批')}{int('gpt_save_every', 'GPT 保存间隔')}
          </div>
          <NumberInput placeholder="最多句数（留空使用全部）" ariaLabel="最多句数" value={req.max_lines} step="1" onChange={(v) => patch({ max_lines: v })} />
          {!maxLinesOk && <div className="error-text small">最多句数至少 10。</div>}
          <div className="muted small">切换引擎已按推荐默认填充轮数与每批大小；轮数建议为保存间隔的整数倍。</div>
          <ComputePicker id="train" input={plan} action="训练" scope={`${lines} 句 ${displayEngine(engine)}`} ready={previewReady && lines > 0} notReadyText="选好数据集后估算"
            taskTitle={`训练 ${req.slug || '未指定角色'} · ${displayEngine(engine)}（${lines} 句）`} />
          <button className="primary" disabled={!canSubmit}
            onClick={() => {
              const snapshot: TrainRequest = { ...req, engine, lora_rank: currentTd.lora ? (req.lora_rank ?? currentTd.lora_rank ?? 32) : null }
              void perform(() => submitTraining(snapshot, lines))
            }}>
            {submitting ? '正在提交…' : '⚙ 提交训练'}
          </button>
          {blockReason && !submitting && <div className="warn small block-reason" role="status">暂不可提交：{blockReason}</div>}
        </div>
      </section>
    </>
  )
}

function sweepTask(req: SweepRequest, skipped: number) {
  const groups = req.characters.length * req.engines.length * req.trains.length
  return `扫参 ${req.characters.map(characterName).join('、')} · ${req.engines.map(displayEngine).join(' / ')}（${groups} 组${skipped ? `，其中 ${skipped} 组已完成会跳过` : ''}）`
}

const newTrain = (td?: TrainDefaults): SweepTrain => ({
  sovits_epochs: td?.sovits_epochs ?? 8,
  gpt_epochs: td?.gpt_epochs ?? 15,
  sovits_save_every: td?.sovits_save_every ?? 4,
  gpt_save_every: td?.gpt_save_every ?? 5,
  sovits_batch: td?.sovits_batch ?? 8,
  gpt_batch: td?.gpt_batch ?? 8,
  lora_rank: td?.lora ? (td.lora_rank ?? 32) : null,
})

function SweepForm({ sort }: { sort: SortPref }) {
  const s = useStore()
  const planner = usePlanners((p) => p.sweep)
  const characters = s.models.filter((m) => m.dataset_id)
  const cloudDevices = s.devices.filter((d) => d.type === 'cloud')
  const cloudEngines = new Set(cloudDevices.flatMap((d) => d.engines))
  const allTrainable = sortEngines(s.engineInfos.filter((e) => e.trainable).map((e) => e.id))
  const supportedEngines = allTrainable.filter((id) => cloudEngines.has(id))
  const unsupportedEngines = cloudDevices.length ? allTrainable.filter((id) => !cloudEngines.has(id)) : []
  const preferredSweepEngine = preferredEngine(s.commonModels, supportedEngines.length ? supportedEngines : (allTrainable.length ? allTrainable : undefined))
  const [req, setReq] = useState<SweepRequest>(() => ({
    characters: [], engines: [], trains: [newTrain(defaultsForEngine(engineInfoOf(s, preferredSweepEngine), preferredSweepEngine))],
    min_sec: 1, max_sec: 12, exclude_interjections: true, max_lines: null, keep: 3, samples: 2, approved: false, ref_ids: {}, into_builtin: true,
  }))
  const [paramsTouched, setParamsTouched] = useState(false)
  const patch = (p: Partial<SweepRequest>) => setReq((r) => ({ ...r, ...p }))
  const toggle = (list: string[], item: string, on: boolean) => (on ? [...list.filter((x) => x !== item), item] : list.filter((x) => x !== item))
  const setTrain = (i: number, p: Partial<SweepTrain>) => {
    setParamsTouched(true)
    patch({ trains: req.trains.map((t, j) => (j === i ? { ...t, ...p } : t)) })
  }
  const toggleEngine = (id: string, checked: boolean) => {
    const nextEngines = toggle(req.engines, id, checked) as Engine[]
    if (!paramsTouched && req.trains.length === 1 && nextEngines.length === 1) {
      const eng = nextEngines[0]
      patch({ engines: nextEngines, trains: [newTrain(defaultsForEngine(engineInfoOf(s, eng), eng))] })
    } else {
      patch({ engines: nextEngines })
    }
  }
  const hasLoraEngine = req.engines.some((e) => defaultsForEngine(engineInfoOf(s, e), e).lora)
  const sweepBatchesOk = req.trains.every((t) =>
    Number.isInteger(t.sovits_batch) && (t.sovits_batch ?? 0) >= 1
    && Number.isInteger(t.gpt_batch) && (t.gpt_batch ?? 0) >= 1
    && (t.sovits_epochs ?? 0) >= 1 && (t.gpt_epochs ?? 0) >= 1
    && (t.sovits_save_every ?? 0) >= 1 && (t.gpt_save_every ?? 0) >= 1)
  const sweepMaxLinesOk = req.max_lines === null || req.max_lines === undefined || req.max_lines >= 10
  const ready = !!req.characters.length && !!req.engines.length && !!req.trains.length && sweepBatchesOk && sweepMaxLinesOk

  const sweepBlockReason = !req.characters.length ? '请先勾选至少一个带训练数据的角色'
    : !req.engines.length ? '请先勾选至少一个模型'
      : !req.trains.length ? '请添加至少一组训练参数'
        : !sweepBatchesOk ? '每组训练参数的每批大小、轮数与保存间隔均需至少为 1'
          : !sweepMaxLinesOk ? '每组最多句数至少为 10'
            : planner.allCompleted ? '所选组合都已完成，无需重复扫参'
              : planner.loading ? '正在估算扫参算力…'
                : planner.error ? `算力估算失败：${planner.error}`
                  : !planReady(planner) ? '当前暂无可用云端算力'
                    : null

  const submit = () => perform(async () => {
    const skipped = planner.groups.filter((g) => g.status === 'skipped').length
    const body: SweepRequest = {
      ...req,
      trains: req.trains.map((t) => ({ ...t, lora_rank: hasLoraEngine ? (t.lora_rank ?? null) : null })),
    }
    const job = await submitPlanned<Job>('sweep', '/train/sweep', body, { task: sweepTask(body, skipped) })
    recordLocalJob(job.id)
    updateJob(job)
    monitor(job)
    await loadTrainingJobs(sort)
  })
  const num = (value: number | null | undefined, onChange: (v: number) => void, label: string) => (
    <NumberInput value={value} step="1" className="num-input" ariaLabel={label} onChange={(v) => onChange(v ?? 0)} />
  )
  return (
    <>
      <section>
        <h3>角色与模型</h3>
        <div className="group stack">
          <div className="chips" role="group" aria-label="角色">
            {characters.map((c) => (
              <label key={c.slug} className={`cpick ${req.characters.includes(c.slug) ? 'on' : ''}`}>
                <input type="checkbox" checked={req.characters.includes(c.slug)} onChange={(e) => patch({ characters: toggle(req.characters, c.slug, e.target.checked) })} />
                {characterName(c.slug)}
              </label>
            ))}
            {!characters.length && <span className="muted small">没有带训练数据的角色。</span>}
          </div>
          <hr />
          <div className="chips" role="group" aria-label="模型">
            {supportedEngines.map((id) => (
              <label key={id} className={`cpick ${req.engines.includes(id as Engine) ? 'on' : ''}`}>
                <input type="checkbox" checked={req.engines.includes(id as Engine)}
                  onChange={(ev) => toggleEngine(id, ev.target.checked)} />
                {displayEngine(id)}
              </label>
            ))}
            {unsupportedEngines.map((id) => (
              <label key={id} className="cpick dim" title="当前云端算力未安装">
                <input type="checkbox" checked={false} disabled />
                {displayEngine(id)} <span className="badge">当前算力未安装</span>
              </label>
            ))}
            {!supportedEngines.length && !unsupportedEngines.length && <span className="muted small">扫参在云端 GPU 上跑，先在设置里连接云端 GPU。</span>}
          </div>
        </div>
      </section>
      <section>
        <h3>训练组合</h3>
        <div className="group stack">
          {req.trains.map((t, i) => (
            <div key={i} className="stack tight">
              <div className="row between"><b>组合 {i + 1}</b>{req.trains.length > 1 && <button className="ghost" onClick={() => patch({ trains: req.trains.filter((_, j) => j !== i) })}>移除</button>}</div>
              <div className="train-grid">
                <span /><span className="muted small">轮数</span><span className="muted small">保存间隔</span><span className="muted small">每批</span>
                <span>SoVITS</span>
                {num(t.sovits_epochs, (v) => setTrain(i, { sovits_epochs: v }), `组合 ${i + 1} SoVITS 轮数`)}
                {num(t.sovits_save_every, (v) => setTrain(i, { sovits_save_every: v }), `组合 ${i + 1} SoVITS 保存间隔`)}
                {num(t.sovits_batch, (v) => setTrain(i, { sovits_batch: v }), `组合 ${i + 1} SoVITS 每批`)}
                <span>GPT</span>
                {num(t.gpt_epochs, (v) => setTrain(i, { gpt_epochs: v }), `组合 ${i + 1} GPT 轮数`)}
                {num(t.gpt_save_every, (v) => setTrain(i, { gpt_save_every: v }), `组合 ${i + 1} GPT 保存间隔`)}
                {num(t.gpt_batch, (v) => setTrain(i, { gpt_batch: v }), `组合 ${i + 1} GPT 每批`)}
              </div>
              {hasLoraEngine && (
                <Select label="LoRA Rank" value={t.lora_rank ?? 0}
                  options={[{ value: 0, label: '引擎默认（32）' }, ...FALLBACK_LORA_RANKS.map((r) => ({ value: r, label: String(r) }))]}
                  onChange={(v) => setTrain(i, { lora_rank: v ? (Number(v) as LoraRank) : null })} />
              )}
            </div>
          ))}
          <button onClick={() => {
            setParamsTouched(true)
            const firstEng = req.engines[0] ?? preferredSweepEngine
            patch({ trains: [...req.trains, newTrain(defaultsForEngine(engineInfoOf(s, firstEng), firstEng))] })
          }}>＋ 添加组合</button>
          <NumberInput placeholder="每组最多句数（留空使用全部）" ariaLabel="每组最多句数" value={req.max_lines} step="1" onChange={(v) => patch({ max_lines: v })} />
          <div className="row gap wrap sweep-inline-field">
            <span className="nowrap">保留前</span>
            <NumberInput value={req.keep} step="1" min={1} className="w80" ariaLabel="保留前几个模型组合" onChange={(v) => patch({ keep: v ?? 0 })} />
            <span className="nowrap">个模型组合</span>
          </div>
          <div className="row gap wrap sweep-inline-field">
            <span className="nowrap">生成样本的组合数</span>
            <NumberInput value={req.samples} step="1" min={0} className="w80" ariaLabel="生成样本的组合数" onChange={(v) => patch({ samples: v ?? 0 })} />
          </div>
        </div>
      </section>
      <ComputePicker id="sweep" input={req} action="扫参" sweep ready={ready} notReadyText="选好角色和模型后估算"
        scope={`${req.characters.length * req.engines.length * req.trains.length} 组`} taskTitle={sweepTask(req, 0)} />
      {!!planner.groups.length && (
        <div className="muted small">共 {planner.groups.length} 组 · {planner.groups.filter((g) => g.status === 'skipped').length} 组已完成</div>
      )}
      <button className="primary" disabled={!ready || planner.submitting || planner.allCompleted || !planReady(planner)} onClick={() => void submit()}>
        {planner.allCompleted ? '都已完成' : planner.submitting ? '正在提交…' : '提交扫参'}
      </button>
      {sweepBlockReason && !planner.submitting && <div className="warn small block-reason" role="status">暂不可提交：{sweepBlockReason}</div>}
    </>
  )
}

/** 训练评测：每个保存点组合的评分、读音准确、音色相似，支持试用、设为默认、前两名盲听。 */
function Evaluation({ ev }: { ev: TrainEval }) {
  const defaultBusy = useStore((s) => isSettingDefault(s, ev.character))
  const [versions, setVersions] = useState<ModelVersion[]>([])
  const [weights, setWeights] = useState<WeightEntry[]>([])
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    Promise.all([fetchVersions(ev.character), visibleWeights(ev.character, ev.engine, ev.eval_set)])
      .then(([v, w]) => { if (!stale) { setVersions(v); setWeights(w) } })
      .catch(() => {})
    return () => { stale = true }
  }, [ev.job_id, ev.character, ev.engine, ev.eval_set])

  const sorted = ev.combos.slice().sort((a, b) => (b.score_median ?? -Infinity) - (a.score_median ?? -Infinity))
  const best = (g: string, sv: string) => ev.best.gpt === g && ev.best.sovits === sv

  const launchTop2Blind = () => {
    if (sorted.length < 2) return
    const toSide = (c: (typeof sorted)[number]): BlindSide => {
      const matched = versions.find((v) => v.engine === ev.engine && v.gpt === c.gpt && v.sovits === c.sovits)
      return { engine: ev.engine as BlindSide['engine'], gpt: c.gpt, sovits: c.sovits, version: matched?.id ?? null }
    }
    startBlindWithSides(ev.character, toSide(sorted[0]), toSide(sorted[1]), `${characterName(ev.character)} · 训练评测前两名`)
  }

  return (
    <div className="stack">
      <div className="row between wrap gap">
        <h3>模型评测 · {characterName(ev.character)} · {displayEvalSet(ev.eval_set)}</h3>
        {sorted.length >= 2 && (
          <button className="primary small-btn" onClick={launchTop2Blind}>前两名盲听</button>
        )}
      </div>
      {notice && <div className="notice small" role="status">{notice}</div>}
      <div className="table-wrap">
        <table className="eval-table">
          <thead><tr><th>模型版本</th><th className="num">评分</th><th className="num">读音准确</th><th className="num">音色相似</th><th>操作</th></tr></thead>
          <tbody>
            {sorted.map((c) => {
              const key = c.gpt + '|' + c.sovits
              return (
                <tr key={key} className={best(c.gpt, c.sovits) ? 'best-row' : ''}>
                  <td>{modelVersionLabel(c.gpt, c.sovits, versions, weights)}</td>
                  <td className="num" title={scoreHelp(c.score_median)}>{displayScore(c.score_median)}</td>
                  <td className="num">{displayAccuracy(c.cer_median)}</td>
                  <td className="num">{displaySimilarity(c.spk_sim_median)}</td>
                  <td>
                    <div className="row gap">
                      <button className="small-btn" onClick={() => void perform(() => tryCombination(c, ev))}>{best(c.gpt, c.sovits) ? '★ 试用' : '试用'}</button>
                      <button className="small-btn" disabled={defaultBusy || busyKey === key}
                        onClick={() => {
                          if (defaultBusy) return
                          setBusyKey(key)
                          void perform(async () => {
                            const msg = await setDefaultComboByNames(ev.character, ev.engine, c.gpt, c.sovits)
                            if (msg) setNotice(msg)
                          }).finally(() => setBusyKey(null))
                        }}>
                        {busyKey === key ? '设置中…' : '设为默认'}
                      </button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
