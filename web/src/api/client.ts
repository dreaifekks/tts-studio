import type { Health, Job, Plan } from './types'

export class ApiError extends Error {
  status: number
  /** 服务端的错误码：confirm_required、compute_unavailable、version_unavailable、library_not_selected、audio_elsewhere… */
  code?: string
  /** confirm_required / compute_unavailable 附带的报价 */
  plan?: Plan | null
  /** audio_elsewhere：音频所在的机器 */
  machine?: string
  constructor(status: number, message: string, extra: { code?: string; plan?: Plan | null; machine?: string } = {}) {
    super(message)
    this.status = status
    Object.assign(this, extra)
  }
}

/** store 注入的回调：状态栏文案。 */
export const hooks = {
  status: (_message: string) => {},
}

type Query = Record<string, string | number | boolean | undefined | null>

export function url(path: string, query?: Query): string {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined && v !== null && v !== '') params.set(k, String(v))
  const qs = params.toString()
  return qs ? `${path}${path.includes('?') ? '&' : '?'}${qs}` : path
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal?.aborted) { reject(signal.reason); return }
    const t = setTimeout(resolve, ms)
    signal?.addEventListener('abort', () => { clearTimeout(t); reject(signal.reason) }, { once: true })
  })

export function retryDelay(header: string | null, now = Date.now()): number {
  if (header) {
    const seconds = Number(header)
    if (Number.isFinite(seconds)) return Math.max(1, seconds)
    const date = Date.parse(header)
    if (!Number.isNaN(date)) return Math.max(1, (date - now) / 1000)
  }
  return 5
}

export async function parseError(res: Response): Promise<ApiError> {
  let body: unknown
  try { body = await res.json() } catch { body = undefined }
  const obj = body as { detail?: unknown; code?: string; plan?: Plan | null; machine?: string } | undefined
  let message = `请求失败（HTTP ${res.status}）`
  if (typeof obj?.detail === 'string') message = obj.detail
  else if (Array.isArray(obj?.detail)) {
    message = obj.detail
      .map((item: { loc?: unknown[]; msg?: string }) => `${(item.loc ?? []).join('.')}：${item.msg ?? '参数无效'}`)
      .join('；')
  }
  if (obj?.code === 'library_not_selected') message = '请先在设置里选择资料库文件夹。'
  return new ApiError(res.status, message, { code: obj?.code, plan: obj?.plan, machine: obj?.machine })
}

export interface RequestOptions {
  method?: string
  query?: Query
  json?: unknown
  form?: FormData
  signal?: AbortSignal
  retry503?: boolean
  /** 只停 503 之后的等待重试、不中止已发出的请求（不给时同 signal） */
  retrySignal?: AbortSignal
}

/** 503（训练中推理暂停）按 Retry-After 等待后重试；其余错误直接抛出。 */
export async function request(path: string, opts: RequestOptions = {}): Promise<Response> {
  const { method = 'GET', query, json, form, signal, retry503 = true, retrySignal = signal } = opts
  const headers: Record<string, string> = {}
  if (json !== undefined) headers['Content-Type'] = 'application/json'
  const body = json !== undefined ? JSON.stringify(json) : form
  for (;;) {
    const res = await fetch(url(path, query), { method, headers, body, signal })
    if (res.ok) return res
    const err = await parseError(res)
    if (res.status !== 503 || !retry503) throw err
    const reason = await describeUnavailable(err.message, retrySignal)
    retrySignal?.throwIfAborted()
    const delay = retryDelay(res.headers.get('Retry-After'))
    hooks.status(`${reason} · ${Math.round(delay)} 秒后重试`)
    await sleep(delay * 1000, retrySignal)
  }
}

async function describeUnavailable(detail: string, signal?: AbortSignal): Promise<string> {
  try {
    const health = await getJSON<Health>('/health', { retry503: false, signal })
    if (health.training_job) return `正在训练：${health.training_job}`
  } catch { /* 无法归类 */ }
  return detail
}

export async function getJSON<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  return (await request(path, opts)).json() as Promise<T>
}

export async function sendJSON<T>(path: string, json: unknown, opts: RequestOptions = {}): Promise<T> {
  return (await request(path, { method: 'POST', ...opts, json })).json() as Promise<T>
}

export const isActive = (job: Job) => job.status === 'queued' || job.status === 'running'

export const jobKindLabel: Record<string, string> = {
  blind: '盲听生成', eval: '组合评测', train: '训练', sweep: '扫参', bench: '测速', candidates: '生成候选', compare: '声音对比',
  work_generate: '作品生成', work_render: '作品渲染',
}

export const jobStatusLabel: Record<string, string> = {
  queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
}

/** SSE 解析：保留空行边界，支持多行 data，忽略注释。 */
export class SSEParser {
  private buffer = ''
  private data: string[] = []
  push(chunk: string): string[] {
    this.buffer += chunk
    const out: string[] = []
    let idx: number
    while ((idx = this.buffer.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buffer.slice(0, idx)
      const sepLen = this.buffer.startsWith('\r\n', idx) ? 2 : 1
      // 末尾单独的 \r 可能是 \r\n 的前半，等下一块再判断
      if (sepLen === 1 && this.buffer[idx] === '\r' && idx === this.buffer.length - 1) break
      this.buffer = this.buffer.slice(idx + sepLen)
      if (line === '') {
        if (this.data.length) out.push(this.data.join('\n'))
        this.data = []
      } else if (line.startsWith('data:')) {
        this.data.push(line.slice(5).replace(/^ /, ''))
      }
    }
    return out
  }
}

/** 按 Job ID 跟踪到终态；断线后 GET 快照再重连，从不重新 POST 创建任务。 */
export async function follow(initial: Job, update: (job: Job) => Promise<void> | void, signal?: AbortSignal): Promise<Job> {
  let job = initial
  await update(job)
  while (isActive(job)) {
    try {
      const res = await fetch(`/jobs/${encodeURIComponent(job.id)}/events`, { headers: { Accept: 'text/event-stream' }, signal })
      if (!res.ok || !res.body) throw new ApiError(res.status, '任务事件连接失败')
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
      const parser = new SSEParser()
      outer: for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        for (const data of parser.push(value)) {
          job = JSON.parse(data) as Job
          await update(job)
          if (!isActive(job)) { reader.cancel().catch(() => {}); break outer }
        }
      }
    } catch (e) {
      if (signal?.aborted) throw e
      hooks.status('任务事件已断开，正在重新连接…')
    }
    if (!isActive(job)) break
    await sleep(2000, signal)
    job = await getJSON<Job>(`/jobs/${encodeURIComponent(job.id)}`, { signal })
    await update(job)
  }
  if (job.status !== 'succeeded') throw new ApiError(0, job.error ?? jobStatusLabel[job.status] ?? job.status)
  return job
}

export const isAbort = (e: unknown) => e instanceof DOMException && e.name === 'AbortError'
