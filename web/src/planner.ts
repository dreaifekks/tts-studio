// 「在哪跑」：每个表单一个 planner。「更改」选的设备、确认窗里点的批准都只对那一次提交生效（同 Mac 的 TaskPlanner）。
import { create } from 'zustand'
import { ApiError, sendJSON } from './api/client'
import type { Plan, PlanCandidate, SweepGroup, SweepPlan } from './api/types'
import { computeFailure, displayUnits, unavailableReason } from './display'

export type PlannerId = 'gen' | 'compare' | 'eval' | 'blind' | 'train' | 'sweep' | 'work' | 'blindgen'

export interface PlannerState {
  /** 「更改」里临时选的设备；null = 自动 */
  selected: string | null
  plan: Plan | null
  /** 表单现在的参数和设备（ComputePicker 的 key）；和 planKey 一致、报价也回来了才能提交 */
  want: string | null
  /** 当前报价是按哪份参数和设备估的 */
  planKey: string | null
  groups: SweepGroup[]
  allCompleted: boolean
  loading: boolean
  error: string | null
  /** 上一次提交没能开始的原因（指定的设备不能用）；下次选设备或提交时清掉 */
  notice: string | null
  submitting: boolean
  /** 超过提醒线时服务端给的报价；非空时显示确认窗 */
  confirmation: Plan | null
  confirmationDevice: string | null
  /** 确认窗第一句：提交那一刻的任务（之后表单再改也不变） */
  confirmationTask: string
  previewKind: string
  previewEngine: string | null
  /** 摘要里的动作：「生成」「评测」「训练」… */
  action: string
  /** 确认窗第一句：「训练 alice · v4（800 句）」 */
  taskSummary: string
}

const blank = (): PlannerState => ({
  selected: null, plan: null, want: null, planKey: null, groups: [], allCompleted: false, loading: false, error: null, notice: null, submitting: false,
  confirmation: null, confirmationDevice: null, confirmationTask: '', previewKind: 'gen', previewEngine: null, action: '', taskSummary: '',
})
const ids: PlannerId[] = ['gen', 'compare', 'eval', 'blind', 'train', 'sweep', 'work', 'blindgen']
export const usePlanners = create<Record<PlannerId, PlannerState>>(() => Object.fromEntries(ids.map((id) => [id, blank()])) as Record<PlannerId, PlannerState>)

const revision: Record<string, number> = {}
const decisions: Partial<Record<PlannerId, (accepted: boolean) => void>> = {}
/** 正在进行的提交；换主控时中止 */
const inflight: Partial<Record<PlannerId, AbortController>> = {}

export function patchPlanner(id: PlannerId, patch: Partial<PlannerState>) {
  usePlanners.setState((s) => ({ [id]: { ...s[id], ...patch } }))
}
const get = (id: PlannerId) => usePlanners.getState()[id]
export const selectedCandidate = (plan: Plan | null | undefined) => plan?.candidates.find((c) => c.device === plan.chosen)
/** 与 core dispatch.py 一致：可用，或用户显式指定了未测速的非云端设备。 */
export const candidateUsable = (p: Pick<PlannerState, 'selected'>, c: PlanCandidate | undefined) =>
  !!c && (c.available || (p.selected !== null && c.reason_code === 'untested' && c.type !== 'cloud'))
/** 能提交：当前参数和设备有一份成功的报价，并且选中的设备实际可用。估不出来、扫参都完成、设备不可用（如 worker_outdated）都不能提交。 */
export const planReady = (p: PlannerState) =>
  p.want !== null && p.planKey === p.want && !p.loading && !p.allCompleted && p.error === null && candidateUsable(p, selectedCandidate(p.plan))

type PlanInput = { kind?: string; engine?: string | null; engines?: string[] | null; parts?: { engine: string }[] | null; [k: string]: unknown }

/** 参数或设备变了：旧报价立刻撤下（摘要不再显示它，也不能拿它提交），等新报价。 */
export function withdrawPlan(id: PlannerId, key: string) {
  revision[id] = (revision[id] ?? 0) + 1
  patchPlanner(id, { want: key, plan: null, planKey: null, groups: [], allCompleted: false, loading: true, error: null })
}

/** 预览报价：普通任务 POST /plan，扫参 POST /train/sweep/plan。只接受最后一次请求的结果；key 是这份报价对应的参数和设备。 */
export async function refreshPlan(id: PlannerId, input: PlanInput, sweep = false, key: string | null = null) {
  const token = (revision[id] = (revision[id] ?? 0) + 1)
  patchPlanner(id, {
    previewKind: sweep ? 'train' : String(input.kind ?? 'gen'),
    previewEngine: input.engine ?? input.engines?.[0] ?? input.parts?.[0]?.engine ?? null,
    loading: true, error: null,
  })
  const selected = get(id).selected
  try {
    const body = selected ? { ...input, compute: selected } : input
    if (sweep) {
      const value = await sendJSON<SweepPlan>('/train/sweep/plan', body, { retry503: false })
      if (token === revision[id]) patchPlanner(id, { plan: value.plan, planKey: key, groups: value.groups, allCompleted: value.plan === null })
    } else {
      const value = await sendJSON<Plan>('/plan', body, { retry503: false })
      if (token === revision[id]) patchPlanner(id, { plan: value, planKey: key, groups: [], allCompleted: false })
    }
  } catch (e) {
    if (token === revision[id]) patchPlanner(id, { plan: null, planKey: key, allCompleted: false, error: e instanceof Error ? e.message : String(e) })
  } finally {
    if (token === revision[id]) patchPlanner(id, { loading: false })
  }
}

export function clearPreview(id: PlannerId) {
  revision[id] = (revision[id] ?? 0) + 1
  patchPlanner(id, { want: null, plan: null, planKey: null, groups: [], allCompleted: false, loading: false, error: null })
}

/** 离开表单（取消草稿、换页）：这次临时选的设备和报价不带进下一份草稿。正在提交的那次照常结束。 */
export function leaveDraft(id: PlannerId) {
  if (get(id).submitting) return
  clearPreview(id)
  patchPlanner(id, { selected: null, notice: null })
}

export function resolveConfirmation(id: PlannerId, accepted: boolean) {
  const decide = decisions[id]
  delete decisions[id]
  decide?.(accepted)
}

/** 换主控等场合：放弃等待中的确认，清掉选择和报价。 */
export function resetPlanner(id: PlannerId) {
  revision[id] = (revision[id] ?? 0) + 1
  inflight[id]?.abort()
  resolveConfirmation(id, false)
  usePlanners.setState({ [id]: blank() })
}

const cancelled = () => new DOMException('已取消', 'AbortError')

/** 等 promise，但中止时立刻放弃等待（请求本身照常完成，结果另行处理）。 */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(cancelled())
  return new Promise<T>((resolve, reject) => {
    const stop = () => reject(cancelled())
    signal.addEventListener('abort', stop, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', stop))
  })
}

/** 停止等待之后才回来的结果：报价丢掉；已经建好的任务拿到 id 就取消。 */
function cancelLate(value: unknown) {
  const v = value as { id?: string; kind?: string; status?: string; job_id?: string } | null
  const job = v?.kind && v.status ? v.id : v?.job_id
  if (job) void sendJSON(`/jobs/${encodeURIComponent(job)}/cancel`, undefined).catch(() => {})
}

/**
 * 带上「更改」里选的设备提交；超过提醒线（409 confirm_required）时弹确认窗，
 * 用户点「用 X 开始」后带 approved、approved_device、approved_units 再提交（服务端重算后可能再要确认）。
 * 请求体和确认窗里的任务都按提交那一刻冻结（task 不给时用表单当时的摘要）。
 * signal 中止（「停止等待」）后：不再等这次请求，迟到的报价丢掉，迟到建好的任务取消，等待中的确认窗关掉。
 * retry503：训练中（503）按 Retry-After 自动重试（作品生成、盲听补生成沿用原来的行为）；停止等待时不再重试。
 */
export async function submitPlanned<T>(id: PlannerId, path: string, input: object,
  { signal, task, retry503 = false }: { signal?: AbortSignal; task?: string; retry503?: boolean } = {}): Promise<T> {
  const start = get(id)
  if (start.submitting) throw cancelled()
  const frozenTask = task ?? start.taskSummary
  const stop = new AbortController()
  const forward = () => stop.abort()
  if (signal?.aborted) stop.abort()
  signal?.addEventListener('abort', forward, { once: true })
  inflight[id] = stop
  patchPlanner(id, { submitting: true, notice: null })
  let device = start.selected
  let approval: PlanCandidate | null = null
  try {
    for (;;) {
      const body: Record<string, unknown> = { ...input }
      if (device) body.compute = device
      if (approval) Object.assign(body, { approved: true, approved_device: approval.device, approved_units: approval.units ?? null })
      const sent = sendJSON<T>(path, body, { retry503, retrySignal: stop.signal })
      try {
        return await untilAborted(sent, stop.signal)
      } catch (e) {
        if (stop.signal.aborted) {
          sent.then(cancelLate, () => {})
          throw cancelled()
        }
        if (!(e instanceof ApiError)) throw e
        if (e.code === 'confirm_required' && e.plan) {
          const quote = e.plan
          patchPlanner(id, { confirmation: quote, confirmationDevice: quote.chosen ?? null, confirmationTask: frozenTask })
          const accepted = await new Promise<boolean>((resolve) => {
            decisions[id] = resolve
            stop.signal.addEventListener('abort', () => resolve(false), { once: true })
          })
          const chosen = quote.candidates.find((c) => c.device === get(id).confirmationDevice)
          if (!accepted || !chosen?.available) throw cancelled()
          device = chosen.device
          approval = chosen
          patchPlanner(id, { confirmation: null })
          continue
        }
        if (e.code === 'compute_unavailable') {
          const chosen = selectedCandidate(e.plan)
          const message = chosen ? (chosen.reason || unavailableReason(chosen)) : computeFailure(e.message)
          patchPlanner(id, { notice: chosen ? `${chosen.name}：${message}` : message })
          throw new ApiError(e.status, message, { code: e.code, plan: e.plan })
        }
        throw e
      }
    }
  } finally {
    signal?.removeEventListener('abort', forward)
    if (inflight[id] === stop) delete inflight[id]
    delete decisions[id]
    patchPlanner(id, { submitting: false, selected: null, confirmation: null, confirmationDevice: null })
  }
}

/** 确认窗第二句：超过了哪条提醒线、是不是高费率卡。 */
export function reminder(plan: Plan, candidate: PlanCandidate | undefined) {
  if (!candidate || candidate.type !== 'cloud' || candidate.units == null) return ''
  const units = candidate.units
  const lines: string[] = []
  if (plan.ask_over === 0 || units > plan.ask_over) lines.push(`一次 ${displayUnits(plan.ask_over)} 单元`)
  if (plan.ask_month === 0 || plan.month_used + units > plan.ask_month) lines.push(`本月 ${displayUnits(plan.ask_month)} 单元`)
  const limits = `一次 ${displayUnits(plan.ask_over)} 单元、本月 ${displayUnits(plan.ask_month)} 单元`
  const head = lines.length ? `超过了你设的「${lines.join('、')}」提醒线。` : candidate.premium ? `你设的提醒线是「${limits}」。` : ''
  return head + (candidate.premium ? '这张卡的单元花费较高。' : '')
}
