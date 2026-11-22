import { create } from 'zustand'
import {
  ApiError, follow, getJSON, hooks, isAbort, isActive, jobKindLabel, request, sendJSON,
} from './api/client'
import type {
  AudioFormat, AudioItem, AudioPage, AutoSettings, BlindSide, CandidatesParams, CharacterInfo, CharacterPrefs,
  CharacterPrefsPatch, CharacterSynthParams, CloudInfo, ComboEval, ComputeDevice, ComputeList, Engine, EngineInfo,
  EvalSet, EvalSetList, Health, Job, JobList, LibraryInfo, ModelVersion, ModelsResponse, Ref, RefList, Review,
  ReviewIn, SplitMethod, StorageOverview, SystemInfo, TextLang, TrainEval, TrainRequest, WeightEntry, WeightList,
  Work, WorkLine, WorkLineIn, WorkList, WorkSummary,
} from './api/types'
import { player } from './audio'
import { cloudStatusSummary, deviceStatus, displayEngine, ENGINE_ORDER, sortEngines } from './display'
import { planReady, submitPlanned, usePlanners, type PlannerId } from './planner'
import { checkVersions } from './versions'

export const pages = [
  { key: 'generate', title: '合成', icon: '♫' },
  { key: 'works', title: '作品', icon: '▤' },
  { key: 'characters', title: '角色', icon: '◈' },
  { key: 'training', title: '训练', icon: '⚙' },
  { key: 'settings', title: '设置', icon: '⚙' },
] as const
export type PageKey = (typeof pages)[number]['key']

export const characterTabs = [
  { key: 'versions', title: '模型版本' },
  { key: 'refs', title: '参考音频' },
  { key: 'blind', title: '盲听' },
  { key: 'stats', title: '评价统计' },
] as const
export type CharacterTab = (typeof characterTabs)[number]['key']

export const settingsTabs = [
  { key: 'general', title: '通用', icon: '⚙︎' },
  { key: 'library', title: '资料库', icon: '▤' },
  { key: 'compute', title: '算力', icon: '▦' },
  { key: 'cloud', title: '云端 GPU', icon: '☁︎' },
] as const
export type SettingsTab = (typeof settingsTabs)[number]['key']

export type GenerateMode = 'candidates' | 'compare'
export type GenerateRightTab = 'candidates' | 'history'

const BASE = import.meta.env.BASE_URL
function locationState(): {
  page: PageKey
  characterTab: CharacterTab
  settingsTab: SettingsTab
  generateRightTab: GenerateRightTab
} {
  const [seg, sub] = location.pathname.slice(BASE.length).split('/')
  if (seg === 'models') return { page: 'characters', characterTab: 'versions', settingsTab: 'compute', generateRightTab: 'candidates' }
  if (seg === 'refs' || seg === 'references') return { page: 'characters', characterTab: 'refs', settingsTab: 'compute', generateRightTab: 'candidates' }
  if (seg === 'blind') return { page: 'characters', characterTab: 'blind', settingsTab: 'compute', generateRightTab: 'candidates' }
  if (seg === 'stats') return { page: 'characters', characterTab: 'stats', settingsTab: 'compute', generateRightTab: 'candidates' }
  if (seg === 'history') return { page: 'generate', characterTab: 'versions', settingsTab: 'compute', generateRightTab: 'history' }
  if (seg === 'characters') {
    const characterTab = (characterTabs.find((t) => t.key === sub)?.key ?? 'versions') as CharacterTab
    return { page: 'characters', characterTab, settingsTab: 'compute', generateRightTab: 'candidates' }
  }
  if (seg === 'settings') {
    const settingsTab = (settingsTabs.find((t) => t.key === sub)?.key ?? 'compute') as SettingsTab
    return { page: 'settings', characterTab: 'versions', settingsTab, generateRightTab: 'candidates' }
  }
  const page = (pages.find((p) => p.key === seg)?.key ?? 'generate') as PageKey
  return { page, characterTab: 'versions', settingsTab: 'compute', generateRightTab: 'candidates' }
}

export const problemTags = ['吞字', '多字', '电音', '声调怪', '情绪不对', '节奏怪']
/** /models 还没加载时用的引擎列表；加载后以 /models 的 engines[] 为准。 */
const fallbackEngines: Engine[] = [...ENGINE_ORDER]
export function engineList(s: { engineInfos: EngineInfo[] }, filter?: (e: EngineInfo) => boolean): Engine[] {
  if (!s.engineInfos.length) return fallbackEngines
  return sortEngines(s.engineInfos.filter((e) => !filter || filter(e)).map((e) => e.id as Engine))
}
/** 统一默认引擎解析：单选直接用它，多选优先 v2proplus，否则按规范顺序取第一个，兜底 v2proplus。 */
export function preferredEngine(commonModels: readonly string[], availableEngines?: readonly string[]): Engine {
  const avail = availableEngines?.length ? new Set(availableEngines) : null
  const pool = (avail ? commonModels.filter((e) => avail.has(e)) : commonModels).filter(Boolean)
  if (pool.length === 1) return pool[0] as Engine
  if (pool.length > 1) return (pool.includes('v2proplus') ? 'v2proplus' : sortEngines(pool)[0]) as Engine
  if (availableEngines?.length) return (availableEngines.includes('v2proplus') ? 'v2proplus' : sortEngines(availableEngines)[0]) as Engine
  return 'v2proplus'
}
/** 当前所有算力设备支持的引擎并集（尚未拉取设备时退回 computeEngines）。 */
export function installedComputeEngines(s: Pick<State, 'devices' | 'computeEngines'>): Set<string> {
  if (s.devices.length) return new Set(s.devices.flatMap((d) => d.engines))
  return new Set(s.computeEngines)
}
/** 支持普通训练的算力设备（本机 + 云端 GPU，不含纯 LAN worker）的引擎并集。 */
export function trainableComputeEngines(s: Pick<State, 'devices'>): Set<string> {
  return new Set(s.devices.filter((d) => d.type === 'local' || d.type === 'cloud').flatMap((d) => d.engines))
}
/** 合并 /compute 与 /models 的全部引擎并按规范顺序排列。 */
export function allKnownEngines(s: Pick<State, 'devices' | 'computeEngines' | 'engineInfos'>): string[] {
  const ids = new Set<string>([
    ...s.computeEngines,
    ...s.devices.flatMap((d) => d.engines),
    ...s.engineInfos.map((e) => e.id),
  ])
  ids.delete('train')
  ids.delete('scorer')
  return sortEngines([...ids])
}
export function engineInfoOf(s: { engineInfos: EngineInfo[] }, id: string): EngineInfo | undefined {
  return s.engineInfos.find((e) => e.id === id)
}
export function engineSupportsAuxRefs(s: { engineInfos: EngineInfo[] }, id: string): boolean {
  const info = engineInfoOf(s, id)
  if (info && typeof info.supports_aux_refs === 'boolean') return info.supports_aux_refs
  return ['v1', 'v2', 'v2pro', 'v2proplus'].includes(id)
}
export function engineSupportsSampleSteps(id: string): boolean {
  return ['v3', 'v4', 'v5turbo', 'v5dev'].includes(id)
}
export function isValidPrimaryRefDuration(sec: number): boolean {
  return Number.isFinite(sec) && sec >= 3 && sec <= 10
}
/** 下拉框里的引擎名，带「零样本」「可训练」标记。 */
export function engineLabel(s: { engineInfos: EngineInfo[] }, id: string) {
  const info = s.engineInfos.find((e) => e.id === id)
  return [displayEngine(id), info?.zero_shot && '零样本', info?.trainable && '可训练'].filter(Boolean).join(' · ')
}
export const characterName = (slug: string) => slug
export const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? '—' : v.toFixed(digits))

const byScore = (a: AudioItem, b: AudioItem) => (b.score ?? -Infinity) - (a.score ?? -Infinity)
const errMessage = (e: unknown) => (e instanceof Error ? e.message : String(e))

const load = <T,>(key: string, fallback: T): T => {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    return JSON.parse(raw) as T
  } catch { return fallback }
}
const loadString = (key: string, fallback: string) => localStorage.getItem(key) ?? fallback
const save = (key: string, value: unknown) => localStorage.setItem(key, JSON.stringify(value))

export const DEFAULT_TAG_KEYS = 'q,w,e,r,t'
export const DEFAULT_TAG_NAMES = '开心,生气,害羞,低落,平静'

export const DEFAULT_SYNTH_PARAMS: {
  speed: number
  fragment_interval: number
  top_k: number
  top_p: number
  temperature: number
  repetition_penalty: number
  text_split_method: SplitMethod
  sample_steps: number | null
  text_lang: TextLang
} = {
  speed: 1,
  fragment_interval: 0.3,
  top_k: 15,
  top_p: 1,
  temperature: 1,
  repetition_penalty: 1.35,
  text_split_method: 'cut5',
  sample_steps: null,
  text_lang: 'ja',
}

export function lineInput(l: WorkLine): WorkLineIn {
  const { candidates: _c, picked: _p, id, ...rest } = l
  return { ...rest, id: id.startsWith('draft-') ? null : id }
}
/** 候选是否按这一行当前的台词和声音设置生成。 */
export function lineMatches(line: WorkLine, audio: AudioItem) {
  return audio.text === line.text && audio.character === line.character && audio.engine === line.engine
    && audio.text_lang === line.lang && (!line.ref_id || audio.ref_id === line.ref_id)
}
function synthesisKey(l: WorkLine): string {
  const v = { ...lineInput(l), id: null, pause_after: 0 } as Record<string, unknown>
  return JSON.stringify(Object.keys(v).sort().map((k) => [k, v[k]]))
}

export type RefMode = '默认' | '情绪' | '参考库'
/** 盲听页当前视图；放在 store 里，离开页面再回来仍停在原来那次盲听。 */
export type BlindView = { kind: 'list' } | { kind: 'new' } | { kind: 'test'; id: string }
export interface BlindPrefill { character: string; a: BlindSide; b: BlindSide; title?: string }
export type SortPref = { sort: string; order: 'asc' | 'desc' }
/** 一组模型版本（生成页、模型页的评测、盲听两边各一组）。 */
export interface VersionList { character: string; rows: ModelVersion[]; selectedID: string | null; loading: boolean; message: string | null }
export const emptyVersions = (): VersionList => ({ character: '', rows: [], selectedID: null, loading: false, message: null })
export const VERSION_GONE = '这个模型版本已不可用，请重新选择。'

interface State {
  page: PageKey
  characterTab: CharacterTab
  settingsTab: SettingsTab
  generateMode: GenerateMode
  generateRightTab: GenerateRightTab
  // 连接
  /** null：还没查过 */
  connected: boolean | null
  trainingJob: string | null
  started: boolean
  status: string
  errorMessage: string | null
  playingId: string | null
  playing: boolean
  // 数据
  models: CharacterInfo[]
  engineInfos: EngineInfo[]
  characterPrefs: Record<string, CharacterPrefs>
  /** 已将 prefs 同步进合成页的角色 slug；同角色切页再回合成页时不重刷覆盖未保存修改 */
  prefsLoadedFor: string | null
  /** 正在执行「设为默认」互斥锁的角色列表 */
  settingDefaultChars: string[]
  jobs: Job[]
  localJobIds: string[]
  // 算力、资料库、云端 GPU（主控上的设置）
  devices: ComputeDevice[]
  computeEngines: string[]
  auto: AutoSettings
  monthUsed: number
  library: LibraryInfo | null
  libraryChecking: boolean
  cloud: CloudInfo | null
  cloudError: string | null
  systemInfo: SystemInfo | null
  storage: StorageOverview | null
  storageLoading: boolean
  computeError: string | null
  computeLoading: boolean
  /** 列表收起时显示速度的模型（本浏览器记住） */
  commonModels: string[]
  // 用户偏好（localStorage）
  defaultCandidates: number
  defaultExportFormat: AudioFormat
  tagKeys: string
  tagNames: string
  // 合成
  params: CandidatesParams
  fixedSeed: boolean
  referenceMode: RefMode
  emotion: string
  genRefs: Ref[]
  composerVersions: VersionList
  /** 在「进阶设置」里自己挑了引擎或 GPT / SoVITS */
  composerManual: boolean
  /** 合成页当前角色 + 引擎下可选的模型（不含隐藏的，含这个引擎的底模） */
  composerWeights: WeightEntry[]
  candidates: AudioItem[]
  selectedCandidate: string | null
  generationBusy: boolean
  generationJobId: string | null
  // 历史
  history: AudioPage
  // 作品
  works: WorkSummary[]
  worksSort: SortPref
  work: Work | null
  workDirty: boolean
  selectedLines: string[]
  staleLines: Record<string, string[]>
  /** 作品 → 行 → 合成参数改动前就已存在的候选（只可试听，不可选用） */
  obsoleteCandidates: Record<string, Record<string, string[]>>
  dirtyRenders: string[]
  workBusy: boolean
  workSaving: boolean
  // 训练
  evaluation: TrainEval | null
  trainingCatalogLoaded: boolean
  trainOrder: string[]
  evalSets: EvalSet[]
  // 盲听
  blindView: BlindView
  blindPrefill: BlindPrefill | null
}

const storedCandidates = Math.min(16, Math.max(1, Math.round(load<number>('pref:defaultCandidates', 6)) || 6))
const storedExportFormatRaw = load<string>('pref:defaultExportFormat', 'wav')
const storedExportFormat: AudioFormat = storedExportFormatRaw === 'flac' || storedExportFormatRaw === 'mp3' ? storedExportFormatRaw : 'wav'
const storedCommonModels = load<string[]>('commonModels', ['v2proplus'])

const initialParams: CandidatesParams = {
  text: 'おはよう。今日はいい天気だね。', character: '', engine: preferredEngine(storedCommonModels), gpt: null, sovits: null, ref_id: null, version: null,
  emotion: null, top_k: 15, top_p: 1, temperature: 1, repetition_penalty: 1.35, seed: -1, speed: 1, fragment_interval: 0.3,
  aux_ref_ids: [], approved: false, text_split_method: 'cut5', sample_steps: null, text_lang: 'ja', format: 'wav', debug: false,
  return_json: true, n: storedCandidates,
}

export const useStore = create<State>(() => ({
  ...locationState(),
  generateMode: 'candidates',
  connected: null, trainingJob: null, started: false, status: '准备就绪', errorMessage: null, playingId: null, playing: false,
  models: [], engineInfos: [], characterPrefs: {}, prefsLoadedFor: null, settingDefaultChars: [], jobs: [], localJobIds: load<string[]>('localJobIds', []),
  devices: [], computeEngines: [], auto: { mode: 'auto', device: null, ask_over: 2, ask_month: 500 }, monthUsed: 0,
  library: null, libraryChecking: false, cloud: null, cloudError: null, systemInfo: null, storage: null, storageLoading: false, computeError: null, computeLoading: false,
  commonModels: storedCommonModels,
  defaultCandidates: storedCandidates,
  defaultExportFormat: storedExportFormat,
  tagKeys: loadString('tagKeys', DEFAULT_TAG_KEYS),
  tagNames: loadString('tagNames', DEFAULT_TAG_NAMES),
  params: initialParams, fixedSeed: false, referenceMode: '默认', emotion: '', genRefs: [], composerVersions: emptyVersions(), composerManual: false,
  composerWeights: [], candidates: [], selectedCandidate: null, generationBusy: false, generationJobId: null,
  history: { total: 0, items: [] },
  works: [], worksSort: load('sort:works', { sort: 'created', order: 'desc' }), work: null, workDirty: false, selectedLines: [],
  staleLines: load('staleWorkLines', {}), obsoleteCandidates: load('obsoleteWorkCandidates', {}), dirtyRenders: load('dirtyWorkRenders', []), workBusy: false, workSaving: false,
  evaluation: null, trainingCatalogLoaded: false, trainOrder: [], evalSets: [],
  blindView: { kind: 'list' }, blindPrefill: null,
}))

const set = useStore.setState
const get = useStore.getState

// ───────── 通用

export function setStatus(status: string) { set({ status }) }
export function showError(e: unknown) {
  if (isAbort(e)) return
  const m = errMessage(e)
  set({ errorMessage: m, status: m })
  if (e instanceof ApiError && e.code === 'library_not_selected') navigate('settings', 'library')
}
/** 页面动作的统一入口：错误进错误条和状态栏。 */
export function perform(action: () => Promise<unknown>) {
  return action().catch(showError)
}

player.subscribe(() => set({ playingId: player.currentId, playing: player.playing }))
player.onError = (m) => showError(new Error(m))
hooks.status = setStatus

export function navigate(page: PageKey, subTab?: SettingsTab | CharacterTab) {
  const s = get()
  const nextSettings = page === 'settings' ? ((subTab as SettingsTab | undefined) ?? s.settingsTab) : s.settingsTab
  const nextCharacter = page === 'characters' ? ((subTab as CharacterTab | undefined) ?? s.characterTab) : s.characterTab
  if (s.page === page && nextSettings === s.settingsTab && nextCharacter === s.characterTab) return
  const path = page === 'generate' ? ''
    : page === 'settings' ? `settings/${nextSettings}`
      : page === 'characters' ? `characters/${nextCharacter}`
        : page
  history.pushState(null, '', BASE + path)
  set({ page, settingsTab: nextSettings, characterTab: nextCharacter })
}
window.addEventListener('popstate', () => set(locationState()))

export function setDefaultCandidates(n: number) {
  const clamped = Math.min(16, Math.max(1, Math.round(n) || 6))
  save('pref:defaultCandidates', clamped)
  set((s) => ({ defaultCandidates: clamped, params: { ...s.params, n: clamped } }))
}
export function setDefaultExportFormat(format: AudioFormat) {
  save('pref:defaultExportFormat', format)
  set({ defaultExportFormat: format })
}
export function setTagShortcuts(tagKeys: string, tagNames: string) {
  localStorage.setItem('tagKeys', tagKeys)
  localStorage.setItem('tagNames', tagNames)
  set({ tagKeys, tagNames })
}

// ───────── 连接

let healthFailures = 0
/** 每 15 秒查一次 /health；连续两次失败算主控不在线。连上后第一次加载数据，之后刷新设备和资料库状态。 */
export async function refreshConnection() {
  let health: Health
  try {
    health = await getJSON<Health>('/health', { retry503: false, signal: AbortSignal.timeout(8000) })
  } catch {
    healthFailures += 1
    if (healthFailures >= 2 || get().connected === null) set({ connected: false })
    return
  }
  healthFailures = 0
  set({ connected: health.status === 'ok', trainingJob: health.training_job ?? null })
  if (health.training_job) setStatus(`正在训练：${health.training_job}`)
  if (!get().connected) return
  if (!get().started) { await loadInitial(); return }
  try {
    await refreshDevices()
    set({ library: await getJSON<LibraryInfo>('/library', { retry503: false }) })
  } catch (e) { set({ computeError: errMessage(e) }) }
}

async function loadInitial() {
  if (get().started) return
  set({ started: true })
  try {
    await reloadModels()
    const list = await getJSON<JobList>('/jobs')
    // 合并而不是替换：训练页可能已经先载入了训练任务
    for (const job of list.jobs) updateJob(job)
    for (const job of list.jobs.filter(isActive)) monitor(job)
  } catch (e) { set({ started: false }); showError(e) }
  void refreshCompute()
  void checkVersions()
}

export async function reloadModels(resetSelection = true) {
  const result = await getJSON<ModelsResponse>('/models')
  set((s) => {
    const nextPrefs = { ...s.characterPrefs }
    for (const c of result.characters) {
      if (c.prefs) nextPrefs[c.slug] = c.prefs
    }
    return { models: result.characters, engineInfos: result.engines ?? [], characterPrefs: nextPrefs }
  })
  if (resetSelection) applyDefaults()
}

// ───────── 角色偏好（prefs）

let prefsSeq = 0
/** 每次用户修改合成参数、切角色、复用或重置时递增；用于丢弃过期的重置响应。 */
let composerParamsEpoch = 0
/** 仅复用历史参数（reuseAudioParams）时阻止下一次自动 prefs 覆盖表单。 */
let suppressPrefsForSlug: string | null = null
/** 自上次切换角色以来用户在表单里明确编辑过的字段；prefs GET 返回时只跳过这些字段，未编辑字段仍应用目标角色 prefs。 */
const editedKeysSinceCharSwitch = new Set<keyof CandidatesParams>()
let editedRefModeSinceCharSwitch = false

const SYNTH_PARAM_KEYS: (keyof CandidatesParams)[] = [
  'speed', 'fragment_interval', 'top_k', 'top_p', 'temperature', 'repetition_penalty',
  'text_split_method', 'sample_steps', 'text_lang',
]
const SYNTH_OR_REF_KEYS = new Set<keyof CandidatesParams>([
  ...SYNTH_PARAM_KEYS, 'ref_id', 'aux_ref_ids',
])

const refPatchSeq = new Map<string, number>()
const pendingRefPatches = new Map<string, { patch: CharacterPrefsPatch; timer: ReturnType<typeof setTimeout> }>()
const inFlightPrefsPatch = new Map<string, Promise<unknown>>()

/** 按角色 slug 串行化 prefs PATCH 写入，并让同角色的 GET 能在途 PATCH 完成后再读。 */
function enqueuePrefsWrite<T>(slug: string, run: () => Promise<T>): Promise<T> {
  const prev = inFlightPrefsPatch.get(slug) ?? Promise.resolve()
  const next = prev.catch(() => {}).then(run)
  const tracked = next.finally(() => {
    if (inFlightPrefsPatch.get(slug) === tracked) inFlightPrefsPatch.delete(slug)
  })
  inFlightPrefsPatch.set(slug, tracked)
  return next
}

function dispatchRefPrefsPatch(slug: string, patch: CharacterPrefsPatch) {
  const seq = (refPatchSeq.get(slug) ?? 0) + 1
  refPatchSeq.set(slug, seq)
  return enqueuePrefsWrite(slug, () =>
    sendJSON<CharacterPrefs>(`/characters/${encodeURIComponent(slug)}/prefs`, patch, { method: 'PATCH' })
      .then((prefs) => {
        if (refPatchSeq.get(slug) !== seq) return prefs
        set((s) => ({
          characterPrefs: { ...s.characterPrefs, [slug]: prefs },
          models: s.models.map((m) => (m.slug === slug ? { ...m, default_ref: prefs.default_ref ?? null, prefs } : m)),
        }))
        return prefs
      })
      .catch((e) => {
        showError(e)
        return null
      }),
  )
}

/** 切角色或保存/重置前，立即发出待写入的参考 prefs 变更。 */
export function flushRefPrefsPatch(slug?: string) {
  const targets = slug ? (pendingRefPatches.has(slug) ? [slug] : []) : [...pendingRefPatches.keys()]
  const promises: Promise<unknown>[] = []
  for (const key of targets) {
    const entry = pendingRefPatches.get(key)
    if (!entry) continue
    clearTimeout(entry.timer)
    pendingRefPatches.delete(key)
    promises.push(dispatchRefPrefsPatch(key, entry.patch))
  }
  return Promise.all(promises)
}

/** 参考/辅参选择防抖写入（200ms 合并、带序号保护、失败进错误条）。 */
export function queueRefPrefsPatch(slug: string, patch: Pick<CharacterPrefsPatch, 'last_ref_id' | 'aux_ref_ids'>) {
  if (!slug) return
  const existing = pendingRefPatches.get(slug)
  if (existing) clearTimeout(existing.timer)
  const merged: CharacterPrefsPatch = { ...(existing?.patch ?? {}), ...patch }
  const timer = setTimeout(() => {
    const cur = pendingRefPatches.get(slug)
    if (!cur) return
    pendingRefPatches.delete(slug)
    void dispatchRefPrefsPatch(slug, cur.patch)
  }, 200)
  pendingRefPatches.set(slug, { patch: merged, timer })
}

export function synthParamsFromPrefs(prefs?: CharacterPrefs | null) {
  const sp = prefs?.synth_params ?? {}
  return {
    speed: sp.speed ?? DEFAULT_SYNTH_PARAMS.speed,
    fragment_interval: sp.fragment_interval ?? DEFAULT_SYNTH_PARAMS.fragment_interval,
    top_k: sp.top_k ?? DEFAULT_SYNTH_PARAMS.top_k,
    top_p: sp.top_p ?? DEFAULT_SYNTH_PARAMS.top_p,
    temperature: sp.temperature ?? DEFAULT_SYNTH_PARAMS.temperature,
    repetition_penalty: sp.repetition_penalty ?? DEFAULT_SYNTH_PARAMS.repetition_penalty,
    text_split_method: (sp.text_split_method ?? DEFAULT_SYNTH_PARAMS.text_split_method) as SplitMethod,
    sample_steps: sp.sample_steps ?? DEFAULT_SYNTH_PARAMS.sample_steps,
    text_lang: (sp.text_lang ?? DEFAULT_SYNTH_PARAMS.text_lang) as TextLang,
  }
}

export function applyPrefsToParams(
  current: CandidatesParams,
  prefs: CharacterPrefs,
  skipKeys?: ReadonlySet<keyof CandidatesParams>,
): CandidatesParams {
  const sp = synthParamsFromPrefs(prefs)
  const skip = (k: keyof CandidatesParams) => !!skipKeys?.has(k)
  // 无显式 last_ref_id 时保留 ref_id=null（跟随角色默认参考），避免更换默认参考后提交旧默认 ID
  const nextRefId = skip('ref_id') ? current.ref_id : (prefs.last_ref_id ?? null)
  const rawAux = skip('aux_ref_ids') ? (current.aux_ref_ids ?? []) : (prefs.aux_ref_ids ?? [])
  const effectivePrimary = nextRefId ?? prefs.default_ref ?? null
  const nextAux = rawAux.filter((id) => id && id !== effectivePrimary)
  return {
    ...current,
    speed: skip('speed') ? current.speed : sp.speed,
    fragment_interval: skip('fragment_interval') ? current.fragment_interval : sp.fragment_interval,
    top_k: skip('top_k') ? current.top_k : sp.top_k,
    top_p: skip('top_p') ? current.top_p : sp.top_p,
    temperature: skip('temperature') ? current.temperature : sp.temperature,
    repetition_penalty: skip('repetition_penalty') ? current.repetition_penalty : sp.repetition_penalty,
    text_split_method: skip('text_split_method') ? current.text_split_method : sp.text_split_method,
    sample_steps: skip('sample_steps') ? current.sample_steps : sp.sample_steps,
    text_lang: skip('text_lang') ? current.text_lang : sp.text_lang,
    ref_id: nextRefId,
    aux_ref_ids: nextAux,
  }
}

export async function loadCharacterPrefs(slug: string, { applyToComposer = true }: { applyToComposer?: boolean } = {}) {
  if (!slug) return null
  const n = ++prefsSeq
  // 等待同角色任何待写或在途的 PATCH 完成后再 GET，防止快速 A→B→A 读回旧主参考
  await flushRefPrefsPatch(slug)
  while (inFlightPrefsPatch.has(slug)) {
    await inFlightPrefsPatch.get(slug)!.catch(() => {})
  }
  if (applyToComposer && (n !== prefsSeq || get().params.character !== slug)) return null
  const patchSeqBeforeGet = refPatchSeq.get(slug) ?? 0
  const prefs = await getJSON<CharacterPrefs>(`/characters/${encodeURIComponent(slug)}/prefs`)
  const cacheFresh = (refPatchSeq.get(slug) ?? 0) === patchSeqBeforeGet
  set((s) => {
    const models = cacheFresh
      ? s.models.map((m) => (m.slug === slug ? { ...m, default_ref: prefs.default_ref ?? m.default_ref, prefs } : m))
      : s.models
    const nextPrefs = cacheFresh ? { ...s.characterPrefs, [slug]: prefs } : s.characterPrefs
    if (!applyToComposer || n !== prefsSeq || s.params.character !== slug) {
      return { models, characterPrefs: nextPrefs }
    }
    if (suppressPrefsForSlug === slug) {
      suppressPrefsForSlug = null
      return { models, characterPrefs: nextPrefs, prefsLoadedFor: slug }
    }
    const keepRefMode = editedRefModeSinceCharSwitch || editedKeysSinceCharSwitch.has('ref_id')
    return {
      models,
      characterPrefs: nextPrefs,
      prefsLoadedFor: slug,
      ...(keepRefMode ? {} : { referenceMode: prefs.last_ref_id ? '参考库' : '默认', emotion: '' }),
      params: applyPrefsToParams(s.params, prefs, editedKeysSinceCharSwitch),
    }
  })
  return prefs
}

export async function patchCharacterPrefs(slug: string, body: CharacterPrefsPatch) {
  await flushRefPrefsPatch(slug)
  const seq = (refPatchSeq.get(slug) ?? 0) + 1
  refPatchSeq.set(slug, seq)
  const prefs = await enqueuePrefsWrite(slug, () =>
    sendJSON<CharacterPrefs>(`/characters/${encodeURIComponent(slug)}/prefs`, body, { method: 'PATCH' }),
  )
  if (refPatchSeq.get(slug) === seq) {
    set((s) => {
      const syncDefaultRef = 'default_ref' in body
        && s.params.character === slug
        && !prefs.last_ref_id
        && s.referenceMode === '默认'
      return {
        characterPrefs: { ...s.characterPrefs, [slug]: prefs },
        models: s.models.map((m) => (m.slug === slug ? { ...m, default_ref: prefs.default_ref ?? null, prefs } : m)),
        ...(syncDefaultRef
          ? {
            params: {
              ...s.params,
              ref_id: null,
              aux_ref_ids: (s.params.aux_ref_ids ?? []).filter((id) => id && id !== prefs.default_ref),
            },
          }
          : {}),
      }
    })
  }
  return prefs
}

export async function saveComposerParamsAsDefault() {
  const { params } = get()
  const targetSlug = params.character
  const synth_params: CharacterSynthParams = {
    speed: params.speed,
    fragment_interval: params.fragment_interval,
    top_k: params.top_k,
    top_p: params.top_p,
    temperature: params.temperature,
    repetition_penalty: params.repetition_penalty,
    text_split_method: params.text_split_method as SplitMethod,
    sample_steps: params.sample_steps ?? null,
    text_lang: params.text_lang as TextLang,
  }
  await patchCharacterPrefs(targetSlug, { synth_params })
  setStatus(`已将当前参数存为 ${characterName(targetSlug)} 的默认设置`)
}

export async function resetComposerParamsToDefault() {
  const targetSlug = get().params.character
  const startEpoch = ++composerParamsEpoch
  for (const k of SYNTH_PARAM_KEYS) editedKeysSinceCharSwitch.add(k)
  await patchCharacterPrefs(targetSlug, { reset_synth_params: true })
  // 响应返回前核对当前活跃角色未改变且未发生新的本地修改
  if (get().params.character === targetSlug && composerParamsEpoch === startEpoch) {
    set((s) => ({
      params: {
        ...s.params,
        ...DEFAULT_SYNTH_PARAMS,
      },
    }))
  }
  setStatus(`已恢复 ${characterName(targetSlug)} 的默认合成参数`)
}

// ───────── 算力、资料库、云端 GPU

let devicesSeq = 0
export async function refreshDevices() {
  const seq = ++devicesSeq
  const value = await getJSON<ComputeList>('/compute', { retry503: false })
  if (seq !== devicesSeq) return
  set({ devices: value.devices, computeEngines: value.engines, auto: value.auto, monthUsed: value.month_used })
}
let devicesRefreshQueued = false
function scheduleDevicesRefresh() {
  if (devicesRefreshQueued) return
  devicesRefreshQueued = true
  queueMicrotask(() => {
    devicesRefreshQueued = false
    if (!get().connected) return
    void refreshDevices().catch(() => {})
  })
}
let storageSeq = 0
/** 读取资料库与主控本机存储占用；老主控返回 404 时静默降级不报错。 */
export async function refreshStorage(refresh = false) {
  const n = ++storageSeq
  set({ storageLoading: true })
  try {
    const storage = await getJSON<StorageOverview>('/system/storage', {
      query: { refresh: refresh || undefined },
      retry503: false,
    })
    if (n !== storageSeq) return storage
    set({ storage })
    return storage
  } catch (e) {
    if (n !== storageSeq) return null
    if (e instanceof ApiError && e.status === 404) {
      set({ storage: null })
      return null
    }
    if (refresh) showError(e)
    return null
  } finally {
    if (n === storageSeq) set({ storageLoading: false })
  }
}
/** 云端 GPU 信息：按发出顺序写入，晚回来的旧请求不覆盖已经显示的新结果。signal 中止时什么都不写。 */
let cloudSent = 0
let cloudShown = 0
export async function refreshCloud(refresh = false, signal?: AbortSignal) {
  const n = ++cloudSent
  const timeout = AbortSignal.timeout(200_000)
  try {
    const cloud = await getJSON<CloudInfo>('/cloud', { query: { refresh: refresh || undefined }, retry503: false, signal: signal ? AbortSignal.any([signal, timeout]) : timeout })
    if (signal?.aborted || n < cloudShown) return
    cloudShown = n
    set({ cloud, cloudError: null })
  } catch {
    if (signal?.aborted || n < cloudShown) return
    set({ cloudError: '云端 GPU 信息暂时取不到' })
  }
}
/** Cancel the active add-cloud login: terminates it before commit, or removes a newly added account after probe release. */
export async function cancelCloudLogin(loginId: string) {
  await request(`/cloud/accounts/login/${encodeURIComponent(loginId)}`, { method: 'DELETE', retry503: false }).catch(showError)
  await refreshCloud()
}
/** 打开设置时：设备、资料库、云端 GPU、本机信息都刷新一遍。 */
export async function refreshCompute(refreshAccounts = false) {
  set({ computeLoading: true, computeError: null })
  try {
    await Promise.all([
      refreshDevices().catch((e) => set({ computeError: errMessage(e) })),
      getJSON<LibraryInfo>('/library', { retry503: false }).then((library) => set({ library })).catch((e) => set({ computeError: errMessage(e) })),
      refreshCloud(refreshAccounts),
      getJSON<SystemInfo>('/system/info', { retry503: false }).then((systemInfo) => set({ systemInfo })).catch(() => {}),
    ])
  } finally { set({ computeLoading: false }) }
}
export async function rescanLibrary() {
  set({ libraryChecking: true })
  try { set({ library: await sendJSON<LibraryInfo>('/library/rescan', undefined) }) } finally { set({ libraryChecking: false }) }
}
export async function selectLibrary(path: string) {
  set({ library: await sendJSON<LibraryInfo>('/library', { path }), storage: null })
  void refreshStorage(true)
  await reloadModels()
}
export async function claimLibrary() { set({ library: await sendJSON<LibraryInfo>('/library/claim', undefined) }) }
export async function saveAuto(value: AutoSettings) {
  set({ auto: await sendJSON<AutoSettings>('/compute/auto', value, { method: 'PUT' }) })
}
export async function patchDevice(id: string, fields: Record<string, unknown>) {
  await sendJSON<ComputeDevice>(`/compute/devices/${encodeURIComponent(id)}`, fields, { method: 'PATCH' })
  await refreshDevices()
}
export async function removeDevice(id: string) {
  await request(`/compute/devices/${encodeURIComponent(id)}`, { method: 'DELETE', retry503: false })
  await refreshDevices()
}
export async function benchmark(device: string, engine?: string) {
  const job = await sendJSON<Job>('/bench', { device, quick: false, ...(engine ? { engines: [engine] } : {}) })
  recordLocalJob(job.id)
  updateJob(job)
  monitor(job)
}
export function setCommonModels(list: string[]) {
  save('commonModels', list)
  set({ commonModels: list })
}
/** 状态栏：「资料库已同步 · mini 空闲 · 云端 GPU 未开」（活跃时显示具体云端卡状态，热机时显示保留中）。 */
export function computeSummary(s: State) {
  if (!s.connected) return '主控不在线'
  const items: string[] = []
  if (s.library?.selected) items.push(s.libraryChecking ? '资料库正在同步' : '资料库已同步')
  for (const d of s.devices.filter((x) => x.type !== 'cloud')) items.push(`${d.name} ${deviceStatus(d)}`)
  items.push(cloudStatusSummary(s.devices).barText)
  return items.join(' · ')
}

// ───────── 模型版本

export async function fetchVersions(character: string) {
  return getJSON<ModelVersion[]>(`/characters/${encodeURIComponent(character)}/versions`)
}
/** 读一个角色的模型版本；当前选的版本还在就保留，否则选推荐的（chooseDefault=false 时留空）。 */
export async function loadVersionList(current: VersionList, character: string, chooseDefault = true): Promise<VersionList> {
  const rows = await fetchVersions(character)
  const keep = current.character === character && rows.some((v) => v.id === current.selectedID)
  const selectedID = keep ? current.selectedID : chooseDefault ? (rows.find((v) => v.recommended) ?? rows[0])?.id ?? null : null
  return { character, rows, selectedID, loading: false, message: current.character === character ? current.message : null }
}

export const isSettingDefault = (s: Pick<State, 'settingDefaultChars'>, character: string) =>
  s.settingDefaultChars.includes(character)

/** 同一角色的「设为默认」串行互斥执行，防止连续点击触发双 PATCH 交错写出混搭半边。 */
export async function runWithDefaultLock<T>(character: string, fn: () => Promise<T>): Promise<T | null> {
  if (!character || get().settingDefaultChars.includes(character)) return null
  set((s) => ({ settingDefaultChars: [...s.settingDefaultChars, character] }))
  try {
    return await fn()
  } finally {
    set((s) => ({ settingDefaultChars: s.settingDefaultChars.filter((c) => c !== character) }))
  }
}

/** 按文件名把一对 GPT + SoVITS 设为角色的默认组合（模型版本、盲听胜出者、训练评测共用）。 */
export async function setDefaultComboByNames(character: string, engine: string, gptName: string, sovitsName: string) {
  const res = await runWithDefaultLock(character, async () => {
    const weights = await visibleWeights(character, engine)
    const gpt = weights.find((w) => w.type === 'gpt' && w.name === gptName)
    const sovits = weights.find((w) => w.type === 'sovits' && w.name === sovitsName)
    if (!gpt || !sovits) throw new ApiError(404, '找不到这个组合对应的模型文件，可能已被隐藏或删除。')
    const patch = (w: WeightEntry) => sendJSON<WeightEntry>(`/weights/${encodeURIComponent(w.id)}`, { is_default: true, character }, { method: 'PATCH' })
    await patch(gpt)
    try { await patch(sovits) } catch (e) {
      await reloadModels(false)
      throw new Error(`GPT 默认已更新，SoVITS 设置未完成：${e instanceof Error ? e.message : String(e)}`)
    }
    await reloadModels(false)
    if (get().params.character === character) await loadComposerVersions({ preferRecommended: true })
    const msg = `已设为 ${characterName(character)} / ${displayEngine(engine)} 的默认组合`
    setStatus(msg)
    return msg
  })
  return res ?? ''
}

// ───────── 合成 & 活跃角色

export function currentCharacter(s: State) { return s.models.find((m) => m.slug === s.params.character) }
export const composerReady = (s: State) => s.composerManual || !!s.composerVersions.rows.find((v) => v.id === s.composerVersions.selectedID)

/** 合成页是否手动锁定了一个非推荐版本；为 false 时，外部「设为默认」后自动跟随新推荐版本。 */
let composerVersionPinned = false

export function setActiveCharacter(slug: string) {
  const prev = get().params.character
  if (!slug || prev === slug) return
  void flushRefPrefsPatch(prev)
  composerParamsEpoch += 1
  suppressPrefsForSlug = null
  editedKeysSinceCharSwitch.clear()
  editedRefModeSinceCharSwitch = false
  composerVersionPinned = false
  set((s) => {
    const cachedPrefs = s.characterPrefs[slug] ?? s.models.find((m) => m.slug === slug)?.prefs ?? null
    return {
      params: {
        ...s.params,
        ...synthParamsFromPrefs(cachedPrefs),
        character: slug,
        engine: preferredEngine(s.commonModels, engineList(s)),
        gpt: null,
        sovits: null,
        ref_id: cachedPrefs?.last_ref_id ?? null,
        aux_ref_ids: cachedPrefs?.aux_ref_ids ?? [],
        version: null,
      },
      genRefs: [],
      prefsLoadedFor: null,
      composerManual: false,
      composerVersions: emptyVersions(),
      referenceMode: cachedPrefs?.last_ref_id ? '参考库' : '默认',
      emotion: '',
      blindView: s.blindView.kind === 'test' ? { kind: 'list' } : s.blindView,
    }
  })
}

export function applyDefaults() {
  const s = get()
  let { character, engine } = s.params
  if (s.models.length && !s.models.some((m) => m.slug === character)) character = s.models[0].slug
  if (s.engineInfos.length && !s.engineInfos.some((e) => e.id === engine)) engine = preferredEngine(s.commonModels, s.engineInfos.map((e) => e.id))
  const changedChar = character !== s.params.character
  if (changedChar) {
    void flushRefPrefsPatch(s.params.character)
    composerParamsEpoch += 1
    editedKeysSinceCharSwitch.clear()
    editedRefModeSinceCharSwitch = false
    composerVersionPinned = false
  }
  const cachedPrefs = changedChar ? (s.characterPrefs[character] ?? s.models.find((m) => m.slug === character)?.prefs ?? null) : null
  set({
    params: {
      ...s.params,
      ...(changedChar ? synthParamsFromPrefs(cachedPrefs) : {}),
      character,
      engine,
      gpt: null,
      sovits: null,
      ref_id: changedChar ? (cachedPrefs?.last_ref_id ?? null) : s.params.ref_id,
      aux_ref_ids: changedChar ? (cachedPrefs?.aux_ref_ids ?? []) : s.params.aux_ref_ids,
      version: null,
    },
    ...(changedChar ? {
      genRefs: [],
      prefsLoadedFor: null,
      referenceMode: cachedPrefs?.last_ref_id ? '参考库' : '默认',
      emotion: '',
    } : {}),
    composerManual: false,
  })
  if (s.composerVersions.character === character) applyComposerVersion(s.composerVersions.selectedID, composerVersionPinned)
  else set({ composerVersions: emptyVersions() })
}

let versionsSeq = 0
export async function loadComposerVersions(
  opts?: string | null | { preferredVersionId?: string | null; preferRecommended?: boolean },
) {
  const preferredVersionId = typeof opts === 'string' || opts === null ? opts : opts?.preferredVersionId
  const preferRecommended = typeof opts === 'object' && opts !== null ? !!opts.preferRecommended : false
  const character = get().params.character
  const n = ++versionsSeq
  const before = get().composerVersions
  const prevRecommendedId = before.character === character
    ? (before.rows.find((v) => v.recommended)?.id ?? before.rows[0]?.id ?? null)
    : null
  const wasFollowingDefault = !get().composerManual
    && (!composerVersionPinned || !before.selectedID || before.selectedID === prevRecommendedId)
  set({ composerVersions: { ...before, loading: true } })
  try {
    const chooseDefault = !get().composerManual && (before.character !== character || before.message === null)
    const next = await loadVersionList(before, character, chooseDefault)
    if (n !== versionsSeq || get().params.character !== character) return
    const now = get().composerVersions
    const manual = get().composerManual
    const same = now.character === character
    const wantId = preferredVersionId && next.rows.some((v) => v.id === preferredVersionId) ? preferredVersionId : null
    const recommendedId = (next.rows.find((v) => v.recommended) ?? next.rows[0])?.id ?? null
    const followNewDefault = preferRecommended && wasFollowingDefault && (!same || now.selectedID === before.selectedID)
    const keep = !followNewDefault && same && next.rows.some((v) => v.id === now.selectedID)
    const selectedID = manual
      ? null
      : wantId ?? (followNewDefault ? recommendedId : (keep ? now.selectedID : same && now.selectedID !== before.selectedID ? null : next.selectedID))
    set({ composerVersions: { ...next, selectedID, message: same ? now.message : next.message } })
    if (!manual) {
      const nextPinned = wantId
        ? !(next.rows.find((v) => v.id === wantId)?.recommended)
        : (followNewDefault ? false : composerVersionPinned)
      applyComposerVersion(selectedID, nextPinned)
    }
  } finally {
    if (n === versionsSeq) set((s) => ({ composerVersions: { ...s.composerVersions, loading: false } }))
  }
}
function applyComposerVersion(id: string | null, pinned: boolean) {
  composerVersionPinned = pinned
  const versions = get().composerVersions
  const version = versions.rows.find((v) => v.id === id)
  if (!version) { set((s) => ({ composerVersions: { ...versions, selectedID: id }, params: { ...s.params, version: null } })); return }
  set((s) => ({
    composerVersions: { ...versions, selectedID: id, message: null }, composerManual: false,
    params: { ...s.params, engine: version.engine as Engine, gpt: version.gpt, sovits: version.sovits, version: version.id },
  }))
}
export function selectComposerVersion(id: string | null) {
  const version = get().composerVersions.rows.find((v) => v.id === id)
  applyComposerVersion(id, !!version && !version.recommended)
}
export function switchToManualComposer() {
  composerVersionPinned = true
  set((s) => ({ composerVersions: { ...s.composerVersions, selectedID: null, message: null }, composerManual: true, params: { ...s.params, version: null } }))
}
/** 409 version_unavailable 之后：重新读版本、不自动选，提示重新选择。 */
async function invalidateComposerVersions(character: string) {
  composerVersionPinned = false
  set((s) => ({ composerManual: false, params: { ...s.params, version: null }, composerVersions: { ...s.composerVersions, selectedID: null } }))
  const next = await loadVersionList(get().composerVersions, character, false)
  if (get().params.character === character) set({ composerVersions: { ...next, selectedID: null, message: VERSION_GONE } })
}

/** 跨页跳转至合成页（可带角色、版本、参考或手动组合）。 */
export async function goToGenerate(opts: {
  character?: string
  versionId?: string | null
  refId?: string | null
  manual?: { engine: Engine; gpt: string | null; sovits: string | null }
} = {}) {
  const s = get()
  const prevChar = s.params.character
  const character = opts.character ?? prevChar
  const changingChar = character !== prevChar
  if (changingChar) {
    void flushRefPrefsPatch(prevChar)
    editedKeysSinceCharSwitch.clear()
    editedRefModeSinceCharSwitch = false
    suppressPrefsForSlug = null
    composerVersionPinned = false
    composerParamsEpoch += 1
  }
  if (opts.refId !== undefined) {
    editedKeysSinceCharSwitch.add('ref_id')
    editedRefModeSinceCharSwitch = true
    composerParamsEpoch += 1
  }
  if (opts.manual && (changingChar || opts.refId !== undefined)) {
    editedKeysSinceCharSwitch.add('aux_ref_ids')
    composerParamsEpoch += 1
  }
  const cachedPrefs = changingChar ? (s.characterPrefs[character] ?? s.models.find((m) => m.slug === character)?.prefs ?? null) : null
  if (opts.manual) {
    // 试用组合（如训练评测）跨角色切入时立即重置 synth_params，并清理上一角色的 ref_id 和 aux_ref_ids，不跳过目标角色的 prefs 加载
    const nextRefId = opts.refId !== undefined ? opts.refId : null
    const nextAux = changingChar ? [] : (s.params.aux_ref_ids ?? []).filter((id) => id !== nextRefId)
    composerVersionPinned = true
    set((st) => ({
      composerManual: true,
      composerVersions: changingChar ? emptyVersions() : { ...st.composerVersions, selectedID: null, message: null },
      genRefs: changingChar ? [] : st.genRefs,
      prefsLoadedFor: changingChar ? null : st.prefsLoadedFor,
      referenceMode: nextRefId ? '参考库' : '默认',
      emotion: '',
      params: {
        ...st.params,
        ...(changingChar ? synthParamsFromPrefs(cachedPrefs) : {}),
        character,
        engine: opts.manual!.engine,
        gpt: opts.manual!.gpt,
        sovits: opts.manual!.sovits,
        version: null,
        ref_id: nextRefId,
        aux_ref_ids: nextAux,
      },
    }))
  } else {
    const nextRefId = opts.refId !== undefined ? opts.refId : (changingChar ? (cachedPrefs?.last_ref_id ?? null) : s.params.ref_id)
    const nextAux = changingChar ? (cachedPrefs?.aux_ref_ids ?? []).filter((id) => id !== nextRefId) : (s.params.aux_ref_ids ?? []).filter((id) => id !== nextRefId)
    set((st) => ({
      composerManual: false,
      composerVersions: changingChar ? emptyVersions() : st.composerVersions,
      genRefs: changingChar ? [] : st.genRefs,
      prefsLoadedFor: changingChar ? null : st.prefsLoadedFor,
      referenceMode: opts.refId !== undefined ? (opts.refId ? '参考库' : '默认') : (changingChar ? (cachedPrefs?.last_ref_id ? '参考库' : '默认') : st.referenceMode),
      emotion: opts.refId !== undefined || changingChar ? '' : st.emotion,
      params: {
        ...st.params,
        ...(changingChar ? synthParamsFromPrefs(cachedPrefs) : {}),
        character,
        ref_id: nextRefId,
        aux_ref_ids: nextAux,
        ...(changingChar ? { engine: preferredEngine(st.commonModels, engineList(st)), gpt: null, sovits: null, version: null } : {}),
      },
    }))
    if (opts.versionId) {
      await loadComposerVersions({ preferredVersionId: opts.versionId })
    }
  }
  if (opts.refId) {
    queueRefPrefsPatch(character, { last_ref_id: opts.refId })
  }
  navigate('generate')
}

/** 复用某条音频的参数与种子回填合成页。 */
export function reuseAudioParams(item: AudioItem) {
  const s = get()
  const prevChar = s.params.character
  const changingChar = item.character !== prevChar
  if (changingChar) {
    void flushRefPrefsPatch(prevChar)
    editedKeysSinceCharSwitch.clear()
    editedRefModeSinceCharSwitch = false
  }
  composerParamsEpoch += 1
  suppressPrefsForSlug = item.character
  const matchedVersion = s.composerVersions.character === item.character
    ? s.composerVersions.rows.find((v) => v.engine === item.engine && v.gpt === item.gpt && v.sovits === item.sovits)
    : undefined
  composerVersionPinned = !matchedVersion || !matchedVersion.recommended
  set((st) => ({
    generateMode: 'candidates',
    fixedSeed: item.seed >= 0,
    referenceMode: item.ref_id ? '参考库' : '默认',
    emotion: '',
    genRefs: changingChar ? [] : st.genRefs,
    prefsLoadedFor: item.character,
    composerManual: !matchedVersion,
    composerVersions: changingChar
      ? emptyVersions()
      : matchedVersion
        ? { ...st.composerVersions, selectedID: matchedVersion.id, message: null }
        : { ...st.composerVersions, selectedID: null, message: null },
    params: {
      ...st.params,
      character: item.character,
      text: item.text,
      text_lang: item.text_lang || st.params.text_lang,
      engine: item.engine as Engine,
      gpt: item.gpt ?? null,
      sovits: item.sovits ?? null,
      version: matchedVersion?.id ?? null,
      ref_id: item.ref_id ?? null,
      aux_ref_ids: item.aux_ref_ids ?? [],
      speed: item.speed ?? st.params.speed,
      fragment_interval: item.fragment_interval ?? DEFAULT_SYNTH_PARAMS.fragment_interval,
      temperature: item.temperature ?? st.params.temperature,
      repetition_penalty: item.repetition_penalty ?? st.params.repetition_penalty,
      top_k: item.top_k ?? st.params.top_k,
      top_p: item.top_p ?? st.params.top_p,
      text_split_method: (item.text_split_method ?? st.params.text_split_method) as SplitMethod,
      sample_steps: item.sample_steps ?? null,
      seed: item.seed,
    },
  }))
  navigate('generate')
  setStatus(`已复用参数与种子（${item.seed}）`)
}

/** 这个角色在这个引擎下自己的模型和这个引擎的底模（不含隐藏的）。 */
export async function visibleWeights(character: string, engine: string, evalSet = 'hard') {
  const [own, base] = await Promise.all([
    getJSON<WeightList>('/weights', { query: { character, engine, hidden: false, eval_set: evalSet } }),
    getJSON<WeightList>('/weights', { query: { source: 'pretrained', engine, hidden: false, eval_set: evalSet } }),
  ])
  return [...own.weights, ...base.weights.filter((b) => !own.weights.some((o) => o.id === b.id))]
}
export async function loadComposerWeights() {
  const { character, engine } = get().params
  const list = await visibleWeights(character, engine)
  const p = get().params
  if (p.character !== character || p.engine !== engine) return
  set({ composerWeights: list })
  if (!p.version) {
    const has = (type: string, name: string | null | undefined) => !name || list.some((w) => w.type === type && w.name === name)
    if (!has('gpt', p.gpt)) updateParams({ gpt: null })
    if (!has('sovits', p.sovits)) updateParams({ sovits: null })
  }
}

export function setReferenceMode(referenceMode: RefMode, emotion = '') {
  editedRefModeSinceCharSwitch = true
  editedKeysSinceCharSwitch.add('ref_id')
  composerParamsEpoch += 1
  set({ referenceMode, emotion })
}

export function updateParams(patch: Partial<CandidatesParams>) {
  let touched = false
  for (const k of Object.keys(patch) as (keyof CandidatesParams)[]) {
    if (SYNTH_OR_REF_KEYS.has(k)) {
      editedKeysSinceCharSwitch.add(k)
      touched = true
    }
  }
  if (touched) composerParamsEpoch += 1
  set((s) => ({ params: { ...s.params, ...patch } }))
}

let genRefsSeq = 0
export async function loadGenRefs(character: string) {
  const n = ++genRefsSeq
  const list = await getJSON<RefList>('/refs', { query: { character } })
  if (n === genRefsSeq && get().params.character === character) {
    set({ genRefs: list.refs.filter((r) => r.character === character) })
  }
}

let genAbort: AbortController | null = null

export function startGeneration() {
  const s = get()
  if (s.generationBusy || !s.params.text.trim() || !composerReady(s) || !planReady(usePlanners.getState().gen)) return
  void perform(generate)
}

function requestBody(targetEngines?: string[]) {
  const s = get()
  const engines = targetEngines ?? [s.params.engine]
  const canAux = engines.every((e) => engineSupportsAuxRefs(s, e))
  const ch = currentCharacter(s)
  const defaultRefId = s.characterPrefs[s.params.character]?.default_ref ?? ch?.prefs?.default_ref ?? ch?.default_ref ?? null
  const refId = s.referenceMode === '参考库' ? s.params.ref_id : null
  const effectivePrimary = refId ?? (s.referenceMode === '默认' ? defaultRefId : null)
  const auxIds = canAux ? (s.params.aux_ref_ids ?? []).filter((id) => id && id !== effectivePrimary) : []
  const body: CandidatesParams = {
    ...s.params,
    return_json: true,
    format: 'wav',
    seed: s.fixedSeed ? s.params.seed : -1,
    ref_id: refId,
    emotion: s.referenceMode === '情绪' ? (s.emotion || null) : null,
    aux_ref_ids: auxIds,
    fragment_interval: s.params.fragment_interval ?? 0.3,
  }
  return body
}
/** 提交生成或对比；版本失效（409 version_unavailable）时重新读版本、提示重选。 */
async function submitComposer(planner: PlannerId, path: string, body: object, character: string, signal: AbortSignal) {
  try {
    return await submitPlanned<Job>(planner, path, body, { signal })
  } catch (e) {
    if (e instanceof ApiError && e.code === 'version_unavailable') {
      await invalidateComposerVersions(character)
      throw new ApiError(409, VERSION_GONE, { code: e.code })
    }
    throw e
  }
}

async function generate() {
  const s = get()
  if (s.generationBusy) return
  if (s.composerVersions.message) throw new ApiError(409, s.composerVersions.message, { code: 'version_unavailable' })
  const body = requestBody([s.params.engine])
  genAbort = new AbortController()
  const signal = genAbort.signal
  set({ generationBusy: true, generateRightTab: 'candidates', candidates: [], selectedCandidate: null, generationJobId: null })
  try {
    const job = await submitComposer('gen', '/tts/candidates/jobs', body, body.character, signal)
    recordLocalJob(job.id)
    updateJob(job)
    set({ generationJobId: job.id })
    const result = await follow(job, (j) => consumeProgress(j, signal), signal)
    const prev = get().candidates
    const merged = (result.candidates_result?.candidates ?? prev)
      .map((item) => ({ ...item, review: prev.find((p) => p.id === item.id)?.review ?? item.review }))
      .sort(byScore)
    set((st) => ({ candidates: merged, selectedCandidate: st.selectedCandidate ?? merged[0]?.id ?? null }))
    setStatus(`已生成 ${merged.length} 个候选`)
  } finally {
    set({ generationBusy: false })
    genAbort = null
  }
}

/** 对比引擎：同一句台词、同样的参数，用几个引擎各出 n 个候选。 */
export function startComparison(engines: string[]) {
  const s = get()
  if (s.generationBusy || !s.params.text.trim() || !composerReady(s) || engines.length < 2 || !planReady(usePlanners.getState().compare)) return
  void perform(async () => {
    if (s.composerVersions.message) throw new ApiError(409, s.composerVersions.message, { code: 'version_unavailable' })
    const body = { ...requestBody(engines), engines }
    genAbort = new AbortController()
    const signal = genAbort.signal
    set({ generationBusy: true, generateRightTab: 'candidates', candidates: [], selectedCandidate: null, generationJobId: null })
    try {
      const job = await submitComposer('compare', '/compare/jobs', body, body.character, signal)
      recordLocalJob(job.id)
      updateJob(job)
      set({ generationJobId: job.id })
      const result = await follow(job, (j) => consumeProgress(j, signal), signal)
      const list = (result.compare_result?.results.flatMap((r) => r.candidates) ?? get().candidates).slice().sort(byScore)
      set({ candidates: list, selectedCandidate: list[0]?.id ?? null })
      setStatus('对比完成')
    } finally {
      set({ generationBusy: false })
      genAbort = null
    }
  })
}

export function stopGeneration() {
  genAbort?.abort()
  const job = get().jobs.find((j) => j.id === get().generationJobId && isActive(j))
  if (job) void perform(() => cancelJob(job))
  setStatus('已停止等待')
}

async function consumeProgress(job: Job, signal?: AbortSignal) {
  updateJob(job)
  const s = get()
  if (job.id === s.generationJobId) {
    for (const item of job.completed) {
      if (get().candidates.some((c) => c.id === item.audio_id)) continue
      const audio = await getJSON<AudioItem>(`/audio/${encodeURIComponent(item.audio_id)}/meta`, { signal })
      if (get().generationJobId !== job.id || get().candidates.some((c) => c.id === audio.id)) continue
      const list = [...get().candidates, audio].sort(byScore)
      set((st) => ({ candidates: list, selectedCandidate: st.selectedCandidate ?? list[0].id }))
    }
  } else if (s.work?.id === job.target) {
    for (const item of job.completed) {
      const lineId = item.line_id
      const line = get().work?.lines.find((l) => l.id === lineId)
      if (!lineId || !line || line.candidates.some((c) => c.id === item.audio_id)) continue
      const audio = await getJSON<AudioItem>(`/audio/${encodeURIComponent(item.audio_id)}/meta`, { signal })
      const w = get().work
      if (w?.id !== job.target) continue
      set({
        work: { ...w, lines: w.lines.map((l) => (l.id === lineId && !l.candidates.some((c) => c.id === audio.id)
          ? { ...l, candidates: [...l.candidates, audio].sort(byScore) } : l)) },
      })
    }
  }
}

export function recordLocalJob(id: string) {
  const ids = [...new Set([...get().localJobIds, id])]
  save('localJobIds', ids)
  set({ localJobIds: ids })
}

export function updateJob(job: Job) {
  let shouldRefreshDevices = false
  set((s) => {
    const prev = s.jobs.find((j) => j.id === job.id)
    const active = isActive(job)
    const wasActive = prev ? isActive(prev) : false
    if ((!wasActive && active) || (wasActive && !active) || (wasActive && active && prev?.status !== job.status)) {
      shouldRefreshDevices = true
    }
    const jobs = prev ? s.jobs.map((j) => (j.id === job.id ? job : j)) : [job, ...s.jobs]
    return active && s.localJobIds.includes(job.id) ? { jobs, status: job.stage || s.status } : { jobs }
  })
  if (shouldRefreshDevices) scheduleDevicesRefresh()
}

const monitors = new Set<string>()
export function monitor(job: Job) {
  if (monitors.has(job.id)) return
  monitors.add(job.id)
  follow(job, (j) => consumeProgress(j))
    .then(async (result) => {
      if (get().localJobIds.includes(job.id)) setStatus(`${jobKindLabel[result.kind] ?? result.kind}完成`)
      if (result.kind === 'train' || result.kind === 'sweep') await refreshTrainingCatalog()
      if (['train', 'sweep', 'eval'].includes(result.kind) && get().models.some((m) => m.slug === get().params.character)) await loadComposerVersions()
      if (result.kind === 'bench') await refreshDevices()
    })
    .catch((error) => { if (get().localJobIds.includes(job.id)) showError(error) })
    .finally(() => monitors.delete(job.id))
}
/** 训练、扫参、评测结束的次数：页面据此刷新模型版本。 */
export const modelCompletionKey = (s: State) =>
  s.jobs.filter((j) => !isActive(j) && ['train', 'sweep', 'eval'].includes(j.kind)).map((j) => j.id + j.status).join('|')

export async function cancelJob(job: Job) {
  const cancelled = await sendJSON<Job>(`/jobs/${encodeURIComponent(job.id)}/cancel`, undefined)
  updateJob(cancelled)
}

export function selectCandidate(offset: number) {
  const { candidates, selectedCandidate } = get()
  if (!candidates.length) return
  const cur = Math.max(0, candidates.findIndex((c) => c.id === selectedCandidate))
  set({ selectedCandidate: candidates[Math.min(Math.max(0, cur + offset), candidates.length - 1)].id })
}

// ───────── 播放 & 下载

export function playAudio(id: string, path: string, label = '正在播放') {
  if (player.currentId === id) { player.toggle(); return }
  player.play(id, path)
  setStatus(label)
}
export const playItem = (item: AudioItem) => playAudio(item.id, item.audio_url)

export function playReference(ref: Ref, restart = true) {
  const id = 'ref-' + ref.id
  if (!restart && player.currentId === id) { player.toggle(); return }
  player.play(id, `${ref.audio_url}?format=mp3`, true)
  setStatus(`正在试听参考：${ref.id}`)
}

export async function exportAudioItem(item: AudioItem, format?: AudioFormat) {
  const fmtExt = format ?? get().defaultExportFormat
  const res = await request(`${item.audio_url}`, { query: { format: fmtExt } })
  const blob = await res.blob()
  const name = `${characterName(item.character)}-${item.id.slice(0, 8)}.${fmtExt}`
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(href), 10_000)
  setStatus(`已导出：${name}`)
}

// ───────── review

export type ReviewPatch = { rating: number } | { toggleTag: string }
const reviewQueues = new Map<string, Promise<unknown>>()
const savedReviews = new Map<string, Review>()
const applyReview = (id: string, r: Review | null) => {
  const apply = (a: AudioItem) => (a.id === id ? { ...a, review: r } : a)
  set((s) => ({
    candidates: s.candidates.map(apply),
    history: { ...s.history, items: s.history.items.map(apply) },
    work: s.work && { ...s.work, lines: s.work.lines.map((l) => ({ ...l, candidates: l.candidates.map(apply) })) },
  }))
}
export function clearReview(item: AudioItem) {
  const next = (reviewQueues.get(item.id) ?? Promise.resolve()).then(() => perform(async () => {
    const newest = [savedReviews.get(item.id), ...listedReviews(item.id), item.review]
      .filter((r): r is Review => !!r).map((r) => r.updated).sort().at(-1) ?? ''
    await request(`/audio/${encodeURIComponent(item.id)}/review`, { method: 'DELETE' })
    savedReviews.set(item.id, { audio_id: item.id, updated: newest + '~', rating: null, verdict: null, tags: [], note: '' })
    applyReview(item.id, null)
    setStatus('评价已清除')
  }))
  reviewQueues.set(item.id, next)
  return next
}
function listedReviews(id: string): Review[] {
  const s = get()
  const all = [...s.candidates, ...s.history.items, ...(s.work?.lines.flatMap((l) => l.candidates) ?? [])]
  return all.flatMap((a) => (a.id === id && a.review ? [a.review] : []))
}
export function review(item: AudioItem, patch: ReviewPatch) {
  const next = (reviewQueues.get(item.id) ?? Promise.resolve()).then(() => perform(async () => {
    const candidatesForBase = [savedReviews.get(item.id), ...listedReviews(item.id), item.review].filter((r): r is Review => !!r)
    const cur = candidatesForBase.sort((a, b) => b.updated.localeCompare(a.updated))[0]
    const tags = cur?.tags ?? []
    const body: ReviewIn = {
      rating: 'rating' in patch ? patch.rating : cur?.rating ?? null,
      verdict: cur?.verdict ?? null,
      tags: 'toggleTag' in patch
        ? (tags.includes(patch.toggleTag) ? tags.filter((t) => t !== patch.toggleTag) : [...tags, patch.toggleTag])
        : tags,
      note: cur?.note ?? '',
    }
    const r = await sendJSON<Review>(`/audio/${encodeURIComponent(item.id)}/review`, body)
    savedReviews.set(item.id, r)
    applyReview(item.id, r)
    setStatus('评价已保存')
  }))
  reviewQueues.set(item.id, next)
  return next
}

export function rateSelected(rating: number) {
  const { candidates, selectedCandidate } = get()
  const item = candidates.find((c) => c.id === selectedCandidate)
  if (item) void review(item, { rating })
}

// ───────── 参考库

let tagQueue: Promise<unknown> = Promise.resolve()
const writtenTags = new Map<string, string[]>()
export function queueTag(refs: () => Ref[], setRef: (r: Ref) => void, ref: Ref, tag: string, remove = false) {
  tagQueue = tagQueue.then(() => perform(async () => {
    const base = refs().find((r) => r.id === ref.id)?.tags ?? writtenTags.get(ref.id) ?? ref.tags
    const tags = remove ? base.filter((t) => t !== tag) : [...new Set([...base, tag])].sort()
    const updated = await sendJSON<Ref>(`/refs/${encodeURIComponent(ref.id)}`, { tags }, { method: 'PUT' })
    writtenTags.set(ref.id, updated.tags)
    setRef(updated)
    setStatus(`标签已保存：${tags.join('、')}`)
  }))
  return tagQueue
}

// ───────── 作品

function persistWorkState() {
  save('obsoleteWorkCandidates', get().obsoleteCandidates)
  save('staleWorkLines', get().staleLines)
  save('dirtyWorkRenders', get().dirtyRenders)
}
export function invalidateRender(workId?: string) {
  const id = workId ?? get().work?.id
  if (!id) return
  set((s) => ({
    dirtyRenders: s.dirtyRenders.includes(id) ? s.dirtyRenders : [...s.dirtyRenders, id],
    work: s.work?.id === id ? { ...s.work, render: null } : s.work,
  }))
  persistWorkState()
}
function clearStale(workId: string, lineIds: string[]) {
  set((s) => ({ staleLines: { ...s.staleLines, [workId]: (s.staleLines[workId] ?? []).filter((id) => !lineIds.includes(id)) } }))
  persistWorkState()
}
export function needsRegeneration(s: State, line: WorkLine) {
  return (s.staleLines[s.work?.id ?? ''] ?? []).includes(line.id) || !line.picked
}
export function canPick(s: State, audio: AudioItem, line: WorkLine) {
  return lineMatches(line, audio) && !(s.obsoleteCandidates[s.work?.id ?? '']?.[line.id] ?? []).includes(audio.id)
}

export function changeLine(value: WorkLine) {
  const w = get().workSaving ? null : get().work
  if (!w) return
  const old = w.lines.find((l) => l.id === value.id)
  if (!old) return
  if (synthesisKey(old) !== synthesisKey(value)) {
    set((s) => {
      const rows = s.obsoleteCandidates[w.id] ?? {}
      const oldIds = old.candidates.map((c) => c.id)
      return {
        staleLines: { ...s.staleLines, [w.id]: [...new Set([...(s.staleLines[w.id] ?? []), value.id])] },
        obsoleteCandidates: oldIds.length
          ? { ...s.obsoleteCandidates, [w.id]: { ...rows, [value.id]: [...new Set([...(rows[value.id] ?? []), ...oldIds])] } }
          : s.obsoleteCandidates,
      }
    })
  }
  set({ work: { ...w, lines: w.lines.map((l) => (l.id === value.id ? value : l)) }, workDirty: true })
  invalidateRender()
}
export function setWorkTitle(title: string) {
  const w = get().workSaving ? null : get().work
  if (w) set({ work: { ...w, title }, workDirty: true })
}
export function setLines(lines: WorkLine[]) {
  const w = get().workSaving ? null : get().work
  if (!w) return
  set({ work: { ...w, lines }, workDirty: true })
  invalidateRender()
}
export function removeLine(id: string) {
  const w = get().workSaving ? null : get().work
  if (!w) return
  set((s) => ({ selectedLines: s.selectedLines.filter((x) => x !== id) }))
  setLines(w.lines.filter((l) => l.id !== id))
}
export function addLine(defaultVersion?: ModelVersion | null) {
  const w = get().workSaving ? null : get().work
  if (!w) return
  const s = get()
  const character = s.params.character
  const v = defaultVersion !== undefined ? defaultVersion : (s.composerVersions.character === character
    ? s.composerVersions.rows.find((r) => r.recommended) ?? s.composerVersions.rows[0]
    : null)
  const fallbackEngine = preferredEngine(s.commonModels, engineList(s))
  const line: WorkLine = {
    id: `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    character,
    text: '',
    engine: (v?.engine ?? fallbackEngine) as Engine,
    ref_id: null,
    emotion: null,
    gpt: v?.gpt ?? null,
    sovits: v?.sovits ?? null,
    version: v?.id ?? null,
    top_k: 15,
    top_p: 1,
    temperature: 1,
    repetition_penalty: 1.35,
    seed: -1,
    speed: 1,
    fragment_interval: 0.3,
    aux_ref_ids: [],
    text_split_method: 'cut5',
    sample_steps: null,
    lang: 'ja',
    pause_after: 0.5,
    candidates: [],
    picked: null,
  }
  setLines([...w.lines, line])
}
export function toggleLineSelected(id: string, on: boolean) {
  set((s) => ({ selectedLines: on ? [...new Set([...s.selectedLines, id])] : s.selectedLines.filter((x) => x !== id) }))
}

export function setWorksSort(worksSort: SortPref) {
  localStorage.setItem('sort:works', JSON.stringify(worksSort))
  set({ worksSort })
  return loadWorks()
}
let worksSeq = 0
export async function loadWorks(signal?: AbortSignal) {
  const n = ++worksSeq
  const { sort, order } = get().worksSort
  const list = await getJSON<WorkList>('/works', { signal, query: { sort, order } })
  if (n === worksSeq) set({ works: list.works })
}
let openSeq = 0
export async function openWork(id: string, signal?: AbortSignal) {
  const n = ++openSeq
  const work = await getJSON<Work>(`/works/${encodeURIComponent(id)}`, { signal })
  if (n !== openSeq) return
  set({ work: get().dirtyRenders.includes(id) ? { ...work, render: null } : work, workDirty: false })
}
export async function switchWork(id: string) {
  const n = ++openSeq
  if (get().workDirty) await saveWork()
  if (n !== openSeq) return
  await openWork(id)
  set({ selectedLines: [] })
}
export async function createWork(title: string) {
  if (get().workDirty) await saveWork()
  const work = await sendJSON<Work>('/works', { title, lines: [] })
  set({ work, workDirty: false, selectedLines: [] })
  await loadWorks()
  return work
}
let saveInFlight: Promise<void> | null = null
let saveAbort: AbortController | null = null
export function saveWork(signal?: AbortSignal): Promise<void> {
  saveInFlight ??= doSave(signal).finally(() => { saveInFlight = null })
  return saveInFlight
}
async function doSave(outer?: AbortSignal) {
  const w = get().work
  if (!w) return
  const ctrl = new AbortController()
  if (outer?.aborted) ctrl.abort(outer.reason)
  outer?.addEventListener('abort', () => ctrl.abort(outer.reason), { once: true })
  const signal = ctrl.signal
  saveAbort = ctrl
  set({ workSaving: true })
  try {
    const saved = await sendJSON<Work>(`/works/${encodeURIComponent(w.id)}`, { title: w.title, lines: w.lines.map(lineInput) }, { method: 'PATCH', signal })
    const ids = new Map(w.lines.map((l, i) => [l.id, saved.lines[i]?.id]))
    set((s) => ({
      selectedLines: s.selectedLines.map((id) => ids.get(id)).filter((x): x is string => !!x),
      staleLines: { ...s.staleLines, [w.id]: (s.staleLines[w.id] ?? []).map((id) => ids.get(id)).filter((x): x is string => !!x) },
      obsoleteCandidates: {
        ...s.obsoleteCandidates,
        [w.id]: Object.fromEntries(Object.entries(s.obsoleteCandidates[w.id] ?? {}).flatMap(([old, audio]) => {
          const id = ids.get(old)
          return id ? [[id, audio]] : []
        })),
      },
      work: s.dirtyRenders.includes(saved.id) ? { ...saved, render: null } : saved,
      workDirty: false,
    }))
    persistWorkState()
    await loadWorks(signal)
  } finally { set({ workSaving: false }); saveAbort = null }
}
export async function deleteWork() {
  const w = get().work
  if (!w) return
  await request(`/works/${encodeURIComponent(w.id)}`, { method: 'DELETE' })
  set((s) => {
    const { [w.id]: _, ...staleLines } = s.staleLines
    const { [w.id]: __, ...obsoleteCandidates } = s.obsoleteCandidates
    return { work: null, workDirty: false, staleLines, obsoleteCandidates, dirtyRenders: s.dirtyRenders.filter((x) => x !== w.id) }
  })
  persistWorkState()
  await loadWorks()
  setStatus('作品已删除')
}

export async function addCandidateToWork(item: AudioItem, workId: string | null, lineId: string | null, newTitle: string) {
  const s = get()
  const draftPosition = s.work?.id === workId && lineId ? s.work.lines.findIndex((l) => l.id === lineId) : -1
  if (s.workDirty) await saveWork()
  const targetId = workId ?? (await createWork(newTitle || '新作品')).id
  let updated: Work
  if (lineId) {
    const resolved = draftPosition >= 0 ? get().work!.lines[draftPosition].id : lineId
    updated = await sendJSON<Work>(`/works/${encodeURIComponent(targetId)}/lines/${encodeURIComponent(resolved)}/candidates`, { audio_id: item.id, pick: true })
    clearStale(targetId, [resolved])
    set((s) => {
      const rows = s.obsoleteCandidates[targetId]
      if (!rows?.[resolved]) return {}
      return { obsoleteCandidates: { ...s.obsoleteCandidates, [targetId]: { ...rows, [resolved]: rows[resolved].filter((id) => id !== item.id) } } }
    })
    persistWorkState()
  } else {
    updated = await sendJSON<Work>(`/works/${encodeURIComponent(targetId)}/lines`, { audio_id: item.id })
  }
  set({ work: updated, workDirty: false, selectedLines: [] })
  invalidateRender(targetId)
  await loadWorks()
  navigate('works')
  setStatus(lineId ? '已挂到该行并选用此音频' : '已加入新行并选用此音频')
}

let workAbort: AbortController | null = null
async function runWork(action: (signal: AbortSignal) => Promise<void>) {
  workAbort = new AbortController()
  set({ workBusy: true })
  try { await action(workAbort.signal) } finally { set({ workBusy: false }); workAbort = null }
}
export function stopWork() {
  workAbort?.abort()
  saveAbort?.abort()
  const id = get().work?.id
  for (const job of get().jobs.filter((j) => isActive(j) && j.target === id)) void perform(() => cancelJob(job))
  setStatus('已停止等待')
}

export function generateWork(lineIds: string[] | null) {
  return perform(() => runWork(async (signal) => {
    const before = get().work
    const positions = lineIds && before ? before.lines.flatMap((l, i) => (lineIds.includes(l.id) ? [i] : [])) : null
    await saveWork(signal)
    const w = get().work
    if (!w) return
    const resolved = positions ? positions.map((i) => w.lines[i].id) : w.lines.map((l) => l.id)
    invalidateRender(w.id)
    const job = await submitPlanned<Job>('work', `/works/${encodeURIComponent(w.id)}/generate`, { line_ids: resolved, n: 3 },
      { signal, task: `生成作品「${w.title}」的候选（${resolved.length} 行）`, retry503: true })
    recordLocalJob(job.id)
    await follow(job, (j) => consumeProgress(j, signal), signal)
    clearStale(w.id, resolved)
    if (get().work?.id === w.id) await openWork(w.id, signal)
    await loadWorks(signal)
    setStatus('作品候选已生成')
  }))
}
export function regenerateLine(line: WorkLine) {
  return perform(() => runWork(async (signal) => {
    await saveWork(signal)
    const w = get().work
    if (!w) return
    invalidateRender(w.id)
    const row = w.lines.findIndex((l) => l.id === line.id) + 1
    const job = await submitPlanned<Job>('work', `/works/${encodeURIComponent(w.id)}/lines/${encodeURIComponent(line.id)}/regenerate`, { n: 3 },
      { signal, task: `重新生成作品「${w.title}」第 ${row} 行`, retry503: true })
    recordLocalJob(job.id)
    await follow(job, (j) => consumeProgress(j, signal), signal)
    clearStale(w.id, [line.id])
    if (get().work?.id === w.id) await openWork(w.id, signal)
    setStatus('该行已重新生成')
  }))
}
export async function pick(audio: AudioItem, line: WorkLine) {
  const w = get().work
  const current = w?.lines.find((l) => l.id === line.id)
  if (!w || !current) return
  if (!canPick(get(), audio, current)) throw new ApiError(0, '这条候选来自旧台词或声音设置，只可试听；请选用当前设置的候选。')
  const updated = await sendJSON<Work>(`/works/${encodeURIComponent(w.id)}/lines/${encodeURIComponent(line.id)}/pick`, { audio_id: audio.id }, { method: 'PUT' })
  set({ work: updated })
  invalidateRender(w.id)
  setStatus('已选用')
}
export function renderWork() {
  return perform(() => runWork(async (signal) => {
    await saveWork(signal)
    const w = get().work
    if (!w) return
    if (w.lines.some((l) => needsRegeneration(get(), l))) throw new ApiError(0, '请先为修改过的台词重新生成候选。')
    const job = await request(`/works/${encodeURIComponent(w.id)}/render`, { method: 'POST', signal }).then((r) => r.json() as Promise<Job>)
    recordLocalJob(job.id)
    await follow(job, (j) => consumeProgress(j, signal), signal)
    set((s) => ({ dirtyRenders: s.dirtyRenders.filter((x) => x !== w.id) }))
    persistWorkState()
    if (get().work?.id === w.id) await openWork(w.id, signal)
    await loadWorks(signal)
    setStatus('渲染完成')
  }))
}

export async function exportFile(path: string, ext: string) {
  const res = await request(path)
  const blob = await res.blob()
  const name = `${get().work?.title ?? '作品'}.${ext}`
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(href), 10_000)
  setStatus(`已导出：${name}`)
}

// ───────── 训练

async function refreshTrainingCatalog() {
  set({ trainingCatalogLoaded: false })
  await reloadModels(false)
  set({ trainingCatalogLoaded: true })
}
export function isDeletedTrainingCharacter(s: State, job: Job) {
  return job.kind === 'train' && job.status === 'succeeded' && s.trainingCatalogLoaded && !s.models.some((m) => m.slug === job.target)
}
let trainSeq = 0
export async function loadTrainingJobs(sort: SortPref) {
  const n = ++trainSeq
  await refreshTrainingCatalog()
  const query = { limit: 500, sort: sort.sort === 'kind' ? 'created' : sort.sort, order: sort.order }
  const [train, sweep] = await Promise.all([
    getJSON<JobList>('/jobs', { query: { ...query, kind: 'train' } }),
    getJSON<JobList>('/jobs', { query: { ...query, kind: 'sweep' } }),
  ])
  if (n !== trainSeq) return
  const all = [...train.jobs, ...sweep.jobs.filter((j) => !train.jobs.some((t) => t.id === j.id))]
  const value = (j: Job) => (sort.sort === 'progress' ? j.progress : String({ started: j.started, finished: j.finished, kind: j.kind, status: j.status }[sort.sort] ?? j.created ?? ''))
  all.sort((a, b) => {
    const x = value(a), y = value(b)
    const c = x < y ? -1 : x > y ? 1 : 0
    return sort.order === 'asc' ? c : -c
  })
  set({ trainOrder: all.map((j) => j.id) })
  for (const job of all) { updateJob(job); if (isActive(job)) monitor(job) }
}
export async function submitTraining(body: TrainRequest, lines: number) {
  const job = await submitPlanned<Job>('train', '/train', body, { task: `训练 ${body.slug} · ${displayEngine(body.engine)}（${lines} 句）` })
  recordLocalJob(job.id)
  updateJob(job)
  monitor(job)
  setStatus(`训练已提交：${body.slug}（${lines} 句）`)
}
export async function loadEvaluation(job: Job) {
  await refreshTrainingCatalog()
  if (isDeletedTrainingCharacter(get(), job)) { setStatus('角色已删除'); return }
  set({ evaluation: await getJSON<TrainEval>(`/train/${encodeURIComponent(job.id)}/eval`) })
}
/** 训练评测里「试用」：在合成页用这个组合（手动组合，不是模型版本）。 */
export async function tryCombination(combo: ComboEval, ev: TrainEval) {
  await reloadModels(false)
  if (!get().models.some((m) => m.slug === ev.character)) { setStatus('角色已删除'); return }
  await goToGenerate({
    character: ev.character,
    refId: ev.ref_id,
    manual: { engine: ev.engine as Engine, gpt: combo.gpt, sovits: combo.sovits },
  })
}

export async function loadEvalSets() {
  if (get().evalSets.length) return
  set({ evalSets: (await getJSON<EvalSetList>('/eval-sets')).sets })
}

// ───────── 盲听

export function openBlind(view: BlindView) { set({ blindView: view }) }
export function clearBlindPrefill() { set({ blindPrefill: null }) }
export function startBlindWithSides(character: string, a: BlindSide, b: BlindSide, title?: string) {
  setActiveCharacter(character)
  set({ blindPrefill: { character, a, b, title }, blindView: { kind: 'new' } })
  navigate('characters', 'blind')
}

export async function trackJob(id: string) {
  const job = await getJSON<Job>(`/jobs/${encodeURIComponent(id)}`)
  updateJob(job)
  if (isActive(job)) monitor(job)
}
