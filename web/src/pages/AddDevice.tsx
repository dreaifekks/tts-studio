// 「添加算力 → 局域网或远程电脑」的四步向导：选网络 → 在那台电脑上跑一行安装命令 →
// 等它自己连回来（或手动填连接地址）→ 确认名称、要不要快速测速。
// 状态机照 Mac 的 DeviceInviteFlow（ComputeStore.swift）：换邀请、关闭时修订号加一，在途的请求回来时对不上就丢掉，不会推进界面。
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createStore, useStore as useFlowState } from 'zustand'
import { ApiError, getJSON, request, sendJSON } from '../api/client'
import type { ComputeDevice, ComputeInvite, InviteCreate, InviteManual, InviteStatus, Job } from '../api/types'
import { rovingKeyDown } from '../a11y'
import { Modal, RadioRow, Segmented } from '../components'
import { displayEngine } from '../display'
import { monitor, patchDevice, recordLocalJob, refreshDevices, setStatus, updateJob } from '../store'

type Network = InviteCreate['network']
type Hardware = NonNullable<ComputeDevice['hw']>

interface FlowState {
  network: Network
  step: 1 | 2 | 3 | 4
  invite: ComputeInvite | null
  /** 当前邀请是在哪个网络下生成的：网络没换、也没过期时「下一步」直接复用 */
  inviteNetwork: Network | null
  device: ComputeDevice | null
  name: string
  quick: boolean
  busy: boolean
  expired: boolean
  /** 配对码已核销、那台电脑还没报第一次心跳（waiting + redeemed）：不会过期，安装中途失败会一直停在这里 */
  installing: boolean
  error: string | null
  manualError: string | null
  manualAddress: string
  /** 配对码到期的时刻（毫秒）；stamp 是算出它的时刻，倒计时从这里往后数，不会比主控给的还多 */
  deadline: number | null
  stamp: number
}

const POLL_MS = 2000
const POLL_ERROR = '连接状态暂时取不到，正在重试…'

/** 一次添加的全部状态和动作（Mac 的 DeviceInviteFlow）。 */
function inviteFlow() {
  const store = createStore<FlowState>(() => ({
    network: 'lan', step: 1, invite: null, inviteNetwork: null, device: null, name: '', quick: true, busy: false, expired: false,
    installing: false, error: null, manualError: null, manualAddress: '', deadline: null, stamp: 0,
  }))
  const get = store.getState
  const set = store.setState
  let revision = 0
  const path = (id: string) => `/compute/invites/${encodeURIComponent(id)}`
  const cancel = (id: string) => { request(path(id), { method: 'DELETE', retry503: false }).catch(() => {}) }
  const locallyExpired = (s = get(), now = Date.now()) => !s.installing && s.deadline != null && s.deadline <= now
  /** 查询成功后只收起「暂时取不到」，生成失败之类的提示留着 */
  const clearPollError = () => (get().error === POLL_ERROR ? { error: null } : {})

  async function generate() {
    const version = ++revision
    const network = get().network
    set({ busy: true, error: null, manualError: null })
    try {
      const value = await sendJSON<ComputeInvite>('/compute/invites', { network } satisfies InviteCreate, { retry503: false })
      if (revision !== version) { cancel(value.id); return }
      const now = Date.now()
      set({ invite: value, inviteNetwork: network, deadline: now + value.expires_in * 1000, stamp: now, expired: false, installing: false, step: 2 })
    } catch (e) {
      if (revision !== version) return
      set(e instanceof ApiError && e.code === 'tailscale_unavailable'
        ? { step: 1, error: '这台主控还没连上 Tailscale，请连接后重试，或选择家里的局域网。' }
        : { error: '配对码没能生成，请检查连接后重试。' })
    } finally {
      if (revision === version) set({ busy: false })
    }
  }

  function connected(device: ComputeDevice) {
    set({ device, name: device.name, step: 4, expired: false, installing: false, error: null, manualError: null })
    refreshDevices().catch(() => {})
  }

  function apply(value: InviteStatus) {
    if (value.state === 'expired') { set({ ...clearPollError(), expired: true, installing: false }); return }
    if (value.state === 'connected' && value.device) { connected(value.device); return }
    const s = get()
    const now = Date.now()
    if (value.redeemed) {
      // 核销后到那台电脑报第一次心跳前是 waiting + redeemed。刚开始安装时跳到第 3 步；之后用户自己回到前面几步看命令，不再拽回来
      set({ ...clearPollError(), expired: false, installing: true, deadline: null, manualError: null, ...(s.installing || s.step >= 3 ? {} : { step: 3 }) })
    } else {
      set({ ...clearPollError(), expired: false, installing: false, deadline: now + value.expires_in * 1000, stamp: now })
    }
  }

  return {
    store,
    /** 正在生成配对码时不能换网络（不然会拿着原网络的命令往下走） */
    setNetwork: (network: Network) => { if (!get().busy) set({ network }) },
    setName: (name: string) => set({ name }),
    setQuick: (quick: boolean) => set({ quick }),
    setManualAddress: (manualAddress: string) => set({ manualAddress }),
    go: (step: FlowState['step']) => set({ step }),
    generate,

    /** 第 1 步「下一步」：同一个网络、还没过期的邀请直接用，否则生成一个。 */
    async next() {
      const s = get()
      if (s.busy) return
      if (s.device) { set({ step: 4 }); return }
      if (s.invite && s.inviteNetwork === s.network && !s.expired && !locallyExpired(s)) { set({ step: 2 }); return }
      await generate()
    },

    /** 每 2 秒查一次这个邀请：上一个请求回来后才排下一个。返回的函数停掉这一轮，在途的请求作废。 */
    poll(id: string) {
      if (get().busy) return () => {}
      const version = revision
      const stop = new AbortController()
      let timer: ReturnType<typeof setTimeout> | undefined
      const live = () => !stop.signal.aborted && revision === version && get().invite?.id === id && !get().device
      const tick = async () => {
        if (!live() || get().expired) return
        try {
          const value = await getJSON<InviteStatus>(path(id), { retry503: false, signal: stop.signal })
          if (!live()) return
          apply(value)
          if (get().expired || get().device) return
        } catch (e) {
          if (!live()) return
          if ((e instanceof ApiError && (e.status === 404 || e.status === 410)) || locallyExpired()) {
            set({ ...clearPollError(), expired: true })
            return
          }
          set({ error: POLL_ERROR })
        }
        timer = setTimeout(() => void tick(), POLL_MS)
      }
      void tick()
      return () => { stop.abort(); clearTimeout(timer) }
    },

    /** 「没有自动连上？手动填」：主控拿这个邀请的配对码去连那台电脑。 */
    async manual() {
      const s = get()
      if (s.device || s.busy || s.installing) return
      const id = s.invite?.id
      if (!id || s.expired || locallyExpired(s)) { set({ expired: true }); return }
      const version = revision
      const stale = () => revision !== version || get().invite?.id !== id || !!get().device
      set({ busy: true, manualError: null })
      try {
        const value = await sendJSON<InviteStatus>(`${path(id)}/manual`, { address: s.manualAddress.trim() } satisfies InviteManual, { retry503: false })
        if (stale()) return
        apply(value)
      } catch (e) {
        if (stale()) return
        if (e instanceof ApiError && (e.code === 'invite_expired' || e.status === 404 || e.status === 410)) set({ expired: true, installing: false })
        else if (e instanceof ApiError && e.code === 'invite_used') set({ manualError: '配对码已使用，正在查看连接结果…' })
        else set({ manualError: '连接失败，请检查连接地址后重试。' })
      } finally {
        if (revision === version) set({ busy: false })
      }
    },

    /** 第 4 步「完成」：名称改了就保存，选了快速测速就提交（任务跟进和「完整测速」一样）。成功时返回 true。 */
    async finish() {
      const s = get()
      if (!s.device || s.busy) return false
      const device = s.device
      const name = s.name.trim()
      set({ busy: true, error: null })
      try {
        if (name !== device.name) {
          await patchDevice(device.id, { name })
          set({ device: { ...device, name } })
        }
        setStatus(`已添加「${name}」`)
        if (s.quick) {
          const job = await sendJSON<Job>('/bench', { device: device.id, quick: true })
          recordLocalJob(job.id)
          updateJob(job)
          monitor(job)
        }
        return true
      } catch {
        set({ error: '保存或测速没有完成，请重试。' })
        return false
      } finally {
        set({ busy: false })
      }
    },

    /** 关掉向导（取消、完成、回到选类型）：配对码作废；已经连上的设备不受影响。 */
    close() {
      revision += 1
      const old = get().invite
      set({ invite: null, deadline: null, busy: false })
      if (old) cancel(old.id)
    },
  }
}

const networks: { value: Network; title: string; note: string }[] = [
  { value: 'lan', title: '家里的局域网', note: '插在同一个路由器上的电脑选这个。' },
  { value: 'tailscale', title: 'Tailscale', note: '不在家里、但装了 Tailscale 并登录同一个账号的电脑选这个。' },
]

function osLabel(hw: Hardware | null | undefined) {
  if (!hw?.os) return '暂未识别'
  return ({ darwin: 'macOS', macos: 'macOS', linux: 'Linux', windows: 'Windows' } as Record<string, string>)[hw.os.toLowerCase()] ?? hw.os
}
function accelerationLabel(hw: Hardware | null | undefined) {
  if (!hw) return '暂未识别'
  if (hw.accel === 'cuda') return hw.gpu ? `NVIDIA · ${hw.gpu}` : 'NVIDIA 显卡'
  if (hw.accel === 'mps') return 'Apple 芯片'
  return hw.cores ? `CPU · ${hw.cores} 核` : 'CPU'
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 纯 HTTP 打开（非安全上下文）时没有 clipboard API：退回 execCommand，之后把焦点还给原来的按钮
    const active = document.activeElement as HTMLElement | null
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    let ok = false
    try { ok = document.execCommand('copy') } catch { ok = false }
    area.remove()
    active?.focus()
    return ok
  }
}

/** 倒计时用：active 时每秒更新一次。 */
function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

const CONTROLS = 'input:not(:disabled), button:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [role="radio"]'
/** 这一步第一个能用 Tab 到的控件（单选组是选中的那一项）。 */
function firstControl(root: HTMLElement | null) {
  return Array.from(root?.querySelectorAll<HTMLElement>(CONTROLS) ?? []).find((el) => el.tabIndex >= 0 && el.offsetParent !== null) ?? null
}

export function LanWizard({ onBack, onClose }: { onBack: () => void; onClose: () => void }) {
  const [flow] = useState(inviteFlow)
  const s = useFlowState(flow.store)
  const [os, setOs] = useState<'unix' | 'windows'>('unix')
  const [copied, setCopied] = useState<{ key: string; ok: boolean } | null>(null)
  const [connecting, setConnecting] = useState(false)
  const body = useRef<HTMLDivElement>(null)
  const address = useRef<HTMLInputElement>(null)
  const nameId = useId()
  const now = useNow(s.deadline != null && !s.expired && !s.device)
  const inviteId = s.invite?.id

  // 有邀请就每 2 秒查一次（Mac 的 pollingKey）：换了邀请、开始或结束一个操作时这一轮作废，重新开始
  useEffect(() => (inviteId && !s.busy ? flow.poll(inviteId) : undefined), [flow, inviteId, s.busy])
  // 关掉向导、回到选类型时作废邀请（完成后也作废；不删设备）
  useEffect(() => () => flow.close(), [flow])
  // 换一步（或换了配对码）时原来的按钮没了：焦点放到这一步的第一个控件，没有就放到主按钮
  useEffect(() => {
    const dialog = body.current?.closest<HTMLElement>('[role="dialog"]')
    const target = firstControl(body.current) ?? dialog?.querySelector<HTMLElement>('.modal-foot button.primary:not(:disabled)') ?? dialog
    target?.focus()
  }, [s.step, inviteId])

  /** 按钮在处理时被禁用、焦点掉到页面上时，放回这个控件。 */
  const refocus = (el: HTMLElement | null | undefined) => {
    const dialog = body.current?.closest<HTMLElement>('[role="dialog"]')
    if (el?.isConnected && dialog && !dialog.contains(document.activeElement)) el.focus()
  }
  const next = async () => {
    await flow.next()
    if (flow.store.getState().step === 1) refocus(body.current?.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]'))
  }
  const connect = async () => {
    setConnecting(true)
    try { await flow.manual() } finally { setConnecting(false) }
    refocus(address.current)
  }
  const done = async () => { if (await flow.finish()) onClose() }

  const t = Math.max(now, s.stamp)
  const expiredNow = !s.device && (s.expired || (!s.installing && s.deadline != null && s.deadline <= t))
  const windows = s.invite?.commands.windows ?? null
  const unsupported = os === 'windows' && !windows
  const command = os === 'windows' ? windows ?? '' : s.invite?.commands.unix ?? ''
  const copyKey = `${inviteId}-${os}`
  const copyState = copied?.key === copyKey ? (copied.ok ? 'ok' : 'bad') : null
  const expiration = !s.device && (expiredNow
    ? <div className="warn small" role="alert">配对码已过期，请重新生成。</div>
    : s.deadline != null && <div className="small muted">配对码还剩约 {Math.max(1, Math.ceil((s.deadline - t) / 60_000))} 分钟</div>)

  const back = (onClick: () => void) => <button disabled={s.busy} onClick={onClick}>上一步</button>
  const regenerate = <button disabled={s.busy} onClick={() => void flow.generate()}>重新生成配对码</button>
  const end = (primary?: ReactNode) => (
    <span className="lan-foot-end">{s.busy && <span className="spin" aria-hidden />}<button onClick={onClose}>取消</button>{primary}</span>
  )
  const footer = {
    1: <>{back(onBack)}{end(<button className="primary" disabled={s.busy} onClick={() => void next()}>下一步</button>)}</>,
    2: <>{back(() => flow.go(1))}{expiredNow && regenerate}
      {end(<button className="primary" disabled={s.busy || expiredNow || unsupported} onClick={() => flow.go(s.device ? 4 : 3)}>{s.device ? '查看设备' : '我已经运行了'}</button>)}</>,
    3: <>{back(() => flow.go(2))}{!s.device && regenerate}{end(s.device && <button className="primary" onClick={() => flow.go(4)}>查看设备</button>)}</>,
    4: <>{back(() => flow.go(3))}{end(<button className="primary" disabled={s.busy || !s.name.trim()} onClick={() => void done()}>完成</button>)}</>,
  }[s.step]

  return (
    <Modal title="添加算力" onClose={onClose} closeLabel={null} actions={<span className="wstep">局域网 / 远程 · 第 {s.step} 步，共 4 步</span>} footer={footer}>
      <div className="stack lan-wizard" ref={body}>
        {s.step === 1 && (
          <>
            <p className="wlead">那台电脑和这台 Studio 在哪个网络里？</p>
            <div className="lan-radios" role="radiogroup" aria-label="网络"
              onKeyDown={(e) => rovingKeyDown(e, (el) => { const v = el.closest<HTMLElement>('[data-network]')?.dataset.network; if (v) flow.setNetwork(v as Network) })}>
              {networks.map((n) => (
                <div key={n.value} data-network={n.value}>
                  <RadioRow className="ro" checked={s.network === n.value} disabled={s.busy} onSelect={() => flow.setNetwork(n.value)}>
                    <span><b>{n.title}</b><small>{n.note}</small></span>
                  </RadioRow>
                </div>
              ))}
            </div>
          </>
        )}
        {s.step === 2 && s.invite && (
          <>
            {!unsupported && <p className="wlead">在要添加的那台电脑上打开终端，粘贴下面这一行运行。</p>}
            <Segmented label="系统" value={os} onChange={(v) => { setOs(v); setCopied(null) }}
              options={[{ value: 'unix', label: 'macOS / Linux' }, { value: 'windows', label: windows ? 'Windows' : 'Windows · 暂不支持' }]} />
            {unsupported ? <div className="muted">Windows 暂不支持</div> : (
              <>
                <div className="cmd">
                  <code className="selectable">{command}</code>
                  <button className={`copy ${copyState === 'ok' ? 'okk' : ''}`} aria-live="polite"
                    onClick={() => void copyText(command).then((ok) => setCopied({ key: copyKey, ok }))}>{copyState === 'ok' ? '已复制' : '复制'}</button>
                </div>
                <ul className="what">
                  <li>装好需要的程序（约 7GB，5–10 分钟）</li>
                  <li>装完自动连回这台 Studio，这里会自己跳到下一步</li>
                  <li>配对码 <b>{s.invite.code}</b> 只能用一次，10 分钟内有效</li>
                </ul>
              </>
            )}
            {expiration}
            {copyState === 'bad' && <div className="warn small" role="alert">复制失败，请选中命令后手动复制。</div>}
          </>
        )}
        {s.step === 3 && (
          <>
            {s.device ? <div className="muted">设备已连上，可以返回查看识别信息。</div> : !expiredNow && (
              <div className="waiting" role="status">
                <span className="spin" aria-hidden />
                <div>
                  <b>{s.installing ? '已连上主控，正在安装…' : '正在等待设备连接…'}</b>
                  <small>{s.installing ? '安装完成后会自动显示设备信息。安装没有继续时，可以重新生成配对码。' : '那台电脑装完会自动连上。'}</small>
                </div>
              </div>
            )}
            {expiration}
            <details className="disclosure lan-manual">
              <summary>没有自动连上？手动填</summary>
              <form className="stack" onSubmit={(e) => { e.preventDefault(); void connect() }}>
                <div className="muted small">安装结束时，那台电脑会显示连接地址，填到这里。</div>
                <div className="row gap">
                  <input ref={address} className="grow" aria-label="连接地址" placeholder="那台电脑终端里显示的连接地址" value={s.manualAddress}
                    autoComplete="off" autoCapitalize="off" spellCheck={false} onChange={(e) => flow.setManualAddress(e.target.value)} />
                  <button type="submit" disabled={s.busy || !s.manualAddress.trim() || expiredNow || !!s.device || s.installing}>{connecting ? '正在连接…' : '连接'}</button>
                </div>
                {s.manualError && <div className="warn small selectable" role="alert">{s.manualError}</div>}
              </form>
            </details>
          </>
        )}
        {s.step === 4 && s.device && (
          <>
            <div className="ok-line"><span className="okc" aria-hidden>✓</span><b>已连上</b><span className="muted small">自动识别出这些信息，名称可以改</span></div>
            <div className="fl2">
              <label className="muted" htmlFor={nameId}>名称</label>
              <input id={nameId} value={s.name} autoComplete="off" onChange={(e) => flow.setName(e.target.value)} />
              <span className="muted">系统</span><span>{osLabel(s.device.hw)}</span>
              <span className="muted">可用加速</span><span>{accelerationLabel(s.device.hw)}</span>
              <span className="muted">能跑的引擎</span><span>{s.device.engines.map(displayEngine).join(' · ') || '—'}</span>
            </div>
            <div className="lan-radios" role="radiogroup" aria-label="测速"
              onKeyDown={(e) => rovingKeyDown(e, (el) => flow.setQuick(el.closest<HTMLElement>('[data-quick]')?.dataset.quick === 'now'))}>
              <div data-quick="now">
                <RadioRow className="ro" checked={s.quick} onSelect={() => flow.setQuick(true)}>
                  <span><b>现在做快速测速</b>（约 2 分钟）<small>测完自动选最快的设置，估时更准。以后可以在设备列表里做约 8 分钟的完整测速。</small></span>
                </RadioRow>
              </div>
              <div data-quick="later">
                <RadioRow className="ro" checked={!s.quick} onSelect={() => flow.setQuick(false)}>
                  <span><b>以后再测</b>，先用参考值</span>
                </RadioRow>
              </div>
            </div>
          </>
        )}
        {s.error && <div className="warn small selectable" role="alert">{s.error}</div>}
      </div>
    </Modal>
  )
}
