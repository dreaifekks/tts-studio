// 界面上的叫法和取整（design.md 第 12 节）：和 Mac 的 ComputeModels.swift / StudioModels.swift 同一套。
import type { ComputeDevice, EngineSpeed, ModelVersion, PlanCandidate, WeightEntry } from './api/types'

export const ENGINE_ORDER = ['v2proplus', 'v2pro', 'v4', 'v5turbo', 'v5dev', 'v3', 'v2', 'v1'] as const
const ENGINE_RANK = new Map<string, number>(ENGINE_ORDER.map((e, i) => [e, i]))
const ENGINE_LABELS: Record<string, string> = {
  v2pro: 'v2Pro',
  v2proplus: 'v2ProPlus',
  v4: 'v4',
  v5turbo: 'v5 Turbo',
  v5dev: 'v5 Dev',
  v3: 'v3',
  v2: 'v2',
  v1: 'v1',
}

export const displayEngine = (engine: string) => ENGINE_LABELS[engine] ?? engine
export function sortEngines<T extends string>(engines: readonly T[]): T[] {
  return [...engines].sort((a, b) => {
    const ra = ENGINE_RANK.get(a) ?? Number.MAX_SAFE_INTEGER
    const rb = ENGINE_RANK.get(b) ?? Number.MAX_SAFE_INTEGER
    return ra !== rb ? ra - rb : a.localeCompare(b)
  })
}
export const displayGPU = (gpu: string) => (gpu === 'G4' ? 'RTX PRO 6000' : gpu)
export const displayEvalSet = (value: string) => ({ hard: '难句评测', mini: '快速评测' } as Record<string, string>)[value] ?? value

/** 「约 0.3 单元」：10 以下保留一位小数，整数不带小数。 */
export function displayUnits(units: number) {
  const scale = units < 10 ? 10 : 1
  const value = Math.round(units * scale) / scale
  return value.toLocaleString('en-US', { maximumFractionDigits: value === Math.round(value) ? 0 : 1 })
}
export function displayDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return '时间待测'
  if (seconds >= 7200) return `约 ${Math.round(seconds / 3600)} 小时`
  return `约 ${Math.max(1, Math.round(seconds / 60))} 分钟`
}
export function displayHours(value: number) {
  const step = value >= 100 ? 10 : 1
  return (Math.round(value / step) * step).toLocaleString('en-US', { maximumFractionDigits: 0 })
}
export const displaySeconds = (value: number | null | undefined) =>
  value === null || value === undefined ? '—' : value.toLocaleString('en-US', { maximumFractionDigits: 1 })
export const number = (value: number | null | undefined, digits = 2) => (value === null || value === undefined ? '—' : value.toFixed(digits))
export const displayScore = (value: number | null | undefined) => number(value, 0)
export const scoreHelp = (value: number | null | undefined) => `评分：${number(value, 1)}`
export const displayAccuracy = (error: number | null | undefined) =>
  error === null || error === undefined ? '—' : `${Math.round(Math.max(0, 1 - error) * 100)}%`
export const displaySimilarity = (value: number | null | undefined) => (value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`)
export const displaySource = (source: string) => ({ user: '自己训练', july: '随附模型', pretrained: '底模' } as Record<string, string>)[source] ?? '其他模型'
export const displaySplit = (split: string) =>
  ({ cut0: '整段生成', cut1: '每四句一段', cut2: '约五十字一段', cut3: '按中文句号', cut4: '按英文句号', cut5: '按标点分句' } as Record<string, string>)[split] ?? '按标点分句'

/** 「2026/11/20 10:00」；compact：「11/20 10:00:00」。只有日期的记录只显示日期。 */
export function displayDateTime(value: string, compact = false) {
  const date = new Date(value)
  if (!/T\d/.test(value) || Number.isNaN(date.getTime())) return value.slice(0, 10)
  const p = (n: number) => String(n).padStart(2, '0')
  const day = `${p(date.getMonth() + 1)}/${p(date.getDate())}`
  return compact ? `${day} ${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`
    : `${date.getFullYear()}/${day} ${p(date.getHours())}:${p(date.getMinutes())}`
}

export const weightLabel = (w: Pick<WeightEntry, 'display_name' | 'name'>) => w.display_name || w.name
export const weightType = (type: string) => (type === 'gpt' ? 'GPT' : 'SoVITS')
/** 下拉框里的模型名：显示名，没有时「v2Pro · 11/20 10:00:00 · GPT 第 8 轮」。 */
export function modelLabel(w: WeightEntry) {
  return w.display_name || `${displayEngine(w.engine)} · ${displayDateTime(w.created_at, true)} · ${weightType(w.type)}${w.epoch != null ? ` 第 ${w.epoch} 轮` : ''}`
}
export const versionOption = (v: ModelVersion) => `${v.label} · ${displayEngine(v.engine)}${v.recommended ? ' · 推荐' : ''}`
export const versionScore = (v: ModelVersion) => (v.score != null && v.score_set ? `${displayEvalSet(v.score_set)} · 评分 ${displayScore(v.score)}` : null)
export const isZeroShot = (v: ModelVersion) => v.label === '底模（零样本）'

/** 评测组合的名字：先按模型版本认，认不出就拼 GPT / SoVITS 两半。 */
export function modelVersionLabel(gpt: string, sovits: string, versions: ModelVersion[], weights: WeightEntry[] = []) {
  const version = versions.find((v) => v.gpt === gpt && v.sovits === sovits)
  if (version) return `${version.label} · ${displayEngine(version.engine)}`
  const part = (name: string, type: string, title: string) => {
    const entry = weights.find((w) => w.name === name && w.type === type)
    if (entry?.display_name) return `${title} · ${entry.display_name}`
    const epoch = entry?.epoch ?? (/(?:^|[-_])e(?:poch)?[_-]?(\d+)/i.exec(name)?.[1] ? Number(/(?:^|[-_])e(?:poch)?[_-]?(\d+)/i.exec(name)![1]) : null)
    if (epoch != null) return `${title} ${entry ? `${entry.created_at.slice(0, 10)} · ` : ''}第 ${epoch} 轮`
    if (entry) return `${title} · ${displaySource(entry.source)} · ${displayDateTime(entry.created_at)}`
    return `${title} 自定模型`
  }
  return `${part(gpt, 'gpt', 'GPT')} · ${part(sovits, 'sovits', 'SoVITS')}`
}

// ───────── 算力

export const speedGrade = (s: EngineSpeed | undefined | null) => {
  if (!s || s.source === 'untested' || s.sec_per_item == null || s.sec_per_item <= 0) return null
  return s.sec_per_item <= 4 ? 'fast' : s.sec_per_item <= 12 ? 'mid' : 'slow'
}
export const gradeWord = { fast: '快', mid: '中', slow: '慢' } as const
export const sourceLabel = (source: string | undefined) => ({ measured: '已实测', reference: '参考值', untested: '未测' } as Record<string, string>)[source ?? ''] ?? '未测'
/** 具体耗时数字：<10 保留 1 位小数，>=10 取整。 */
export function displaySpeedSec(sec: number) {
  return sec < 10 ? sec.toFixed(1) : String(Math.round(sec))
}
export function displayItemSpeed(s: EngineSpeed | undefined | null) {
  if (!s || s.source === 'untested' || s.sec_per_item == null || s.sec_per_item <= 0) return null
  return `约 ${displaySpeedSec(s.sec_per_item)} 秒/句`
}
export function displayTrainSpeed(s: EngineSpeed | undefined | null) {
  if (!s || s.source === 'untested' || s.sec_per_item == null || s.sec_per_item <= 0) return null
  return `约 ${displaySpeedSec(s.sec_per_item)} 秒/行（千句${displayDuration(s.sec_per_item * 1000)}）`
}
export function displayScorerSpeed(s: EngineSpeed | undefined | null) {
  if (!s || s.source === 'untested' || s.sec_per_item == null || s.sec_per_item <= 0) return null
  return `约 ${displaySpeedSec(s.sec_per_item)} 秒/条`
}
export function speedTip(engine: string, s: EngineSpeed | undefined | null) {
  const grade = speedGrade(s)
  if (!grade || !s || s.sec_per_item == null) return `${displayEngine(engine)} · 未测`
  return `${displayEngine(engine)} · ${gradeWord[grade]} · 约 ${displaySpeedSec(s.sec_per_item)} 秒/句 · ${sourceLabel(s.source)}`
}

export const LOW_DISK_THRESHOLD_BYTES = 20_000_000_000

/** 字节大小格式化：GB / MB / KB / B。 */
export function displayBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '—'
  const KB = 1024
  const MB = 1024 ** 2
  const GB = 1024 ** 3
  const fmtUnit = (v: number, unit: string, smallDigits: number) => {
    const digits = v >= 100 ? 0 : v >= 10 ? 1 : smallDigits
    const fixed = Number(v.toFixed(digits))
    return `${fixed.toLocaleString('en-US', { maximumFractionDigits: digits })} ${unit}`
  }
  if (bytes >= GB) return fmtUnit(bytes / GB, 'GB', 2)
  if (bytes >= MB) return fmtUnit(bytes / MB, 'MB', 1)
  if (bytes >= KB) return fmtUnit(bytes / KB, 'KB', 1)
  return `${Math.round(bytes)} B`
}

export const deviceType = (type: string) => ({ local: '本机', lan: '局域网设备', cloud: '云端 GPU' } as Record<string, string>)[type] ?? type
export function deviceStatus(d: { status: string; activity?: string | null; wakeable?: boolean; type?: string; warm?: number }) {
  if (d.status === 'offline' && d.wakeable) return '睡眠中'
  if ((d.status === 'busy' || d.status === 'training') && d.activity) return d.activity
  if (d.type === 'cloud' && d.status === 'idle' && (d.warm ?? 0) > 0) return '保留中'
  return ({ idle: '空闲', busy: '忙', training: '正在训练', offline: '离线', paused: '已暂停', unavailable: '未开', waking: '唤醒中' } as Record<string, string>)[d.status] ?? d.status
}
export const statusDot = (status: string) => (status === 'idle' ? 'ok' : ['busy', 'training', 'waking'].includes(status) ? 'busy' : 'off')

/** 云端 GPU 汇总状态：优先展示活跃卡，其次保留中热机，全空闲无热机才显示未开。 */
export function cloudStatusSummary(devices: readonly ComputeDevice[]): { dot: 'busy' | 'ok' | 'off'; text: string; barText: string } {
  const clouds = devices.filter((d) => d.type === 'cloud')
  const active = clouds.filter((d) => ['busy', 'training', 'waking'].includes(d.status))
  if (active.length) {
    const labels = active.map((d) => `${d.name} ${deviceStatus(d)}`).join(' · ')
    return { dot: 'busy', text: labels, barText: labels }
  }
  const warm = clouds.filter((d) => (d.warm ?? 0) > 0)
  if (warm.length) {
    const labels = warm.map((d) => `${d.name} 保留中`).join(' · ')
    return { dot: 'ok', text: labels, barText: labels }
  }
  return { dot: 'off', text: '未开', barText: '云端 GPU 未开' }
}

export function computeReason(code: string) {
  return ({
    local_fast_enough: '本机就很快', lan_faster: '这台空闲、能更快完成', cloud_halves: '云端能省一半以上时间', train_needs_gpu: '训练用云端更快',
    local_untested: '时间待测', fastest: '这台更合适', only_cloud: '这项任务用云端 GPU', pinned: '已固定这台',
  } as Record<string, string>)[code] ?? ''
}
export function unavailableReason(c: Pick<PlanCandidate, 'reason_code'>) {
  return ({
    offline: '设备离线', paused: '已暂停使用', training: '正在训练', unavailable: '暂时不能使用', kind_unsupported: '不支持这项任务',
    cloud_only: '这项任务需要云端 GPU', no_rate: '暂时没有报价', insufficient_balance: '剩余单元不够', cloud_inputs_missing: '训练资料还没同步完成',
    engine_missing: '没有这个模型', not_enough_memory: '可用内存不足', untested: '还没有速度数据', over_max_minutes: '预计时间超过了最长使用时间',
    worker_outdated: '设备版本较旧，请更新后再试',
  } as Record<string, string>)[c.reason_code ?? ''] ?? '暂时不能使用'
}
export const candidateCost = (c: PlanCandidate) => (c.type === 'cloud' ? (c.units != null ? `约 ${displayUnits(c.units)} 单元` : '暂无报价') : '免费')
export const exactEstimate = (c: PlanCandidate) =>
  [c.seconds != null && `预计 ${c.seconds} 秒`, c.units != null && `预计 ${c.units} 单元`].filter(Boolean).join(' · ')
/** 服务端原话里的实现词换成界面叫法。 */
export const computeFailure = (detail: string) =>
  detail.replaceAll('compute_unavailable', '设备暂时不可用').replaceAll('Not Found', '暂时找不到这项内容').replaceAll('timed out', '连接超时').replaceAll('worker', '设备')
