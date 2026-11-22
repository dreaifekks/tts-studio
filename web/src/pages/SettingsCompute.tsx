// 设置 →「算力」「云端 GPU」两页，以及「＋ 添加算力」的弹窗。
import { useEffect, useRef, useState } from 'react'
import { Bar, BarChart, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { ApiError, getJSON, isActive, request, sendJSON } from '../api/client'
import type { AutoSettings, CloudAccount, CloudGpu, CloudLoginStart, CloudLoginStatus, CloudSettings as CloudPrefs, ComputeDevice } from '../api/types'
import { CommonSpeedIcons, Menu, Modal, ModelSpeedTable, Segmented, SpeedIcon, SpeedLegend } from '../components'
import {
  LOW_DISK_THRESHOLD_BYTES, cloudStatusSummary, deviceStatus, deviceType, displayBytes, displayGPU, displayHours,
  displayUnits, statusDot,
} from '../display'
import {
  benchmark, cancelCloudLogin, navigate, patchDevice, perform, preferredEngine, refreshCloud, refreshDevices,
  removeDevice, saveAuto, setStatus, showError, useStore,
} from '../store'
import { LanWizard } from './AddDevice'
import { Item, Section } from './Settings'

// ───────── 算力

export function ComputeSettings() {
  const s = useStore()
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<{ device: ComputeDevice; rePair: boolean } | null>(null)
  const [expanded, setExpanded] = useState<string[]>([])
  const local = s.devices.filter((d) => d.type !== 'cloud')
  const clouds = s.devices.filter((d) => d.type === 'cloud')
  const cloudState = cloudStatusSummary(s.devices)
  const accounts = s.cloud?.accounts.filter((a) => !a.disabled).length ?? 0
  const jobKey = s.jobs.map((j) => j.id + j.status).join()
  useEffect(() => { refreshDevices().catch(showError) }, [jobKey])

  return (
    <>
      <Section title="设备">
        <div className="dev h" aria-hidden><span>名称</span><span>状态</span><span>常用模型的速度 <SpeedLegend /></span><span /></div>
        <div className="legend-m" aria-hidden>常用模型的速度 <SpeedLegend /></div>
        {!s.devices.length && (
          <div className="empty compact"><div className="empty-title">还没有设备</div><div className="muted">添加一台电脑，或连接云端 GPU。</div></div>
        )}
        {local.map((d) => {
          const open = expanded.includes(d.id)
          const benching = s.jobs.some((j) => j.kind === 'bench' && j.target === d.id && isActive(j))
          const measured = d.engines.filter((e) => d.speeds[e]?.source === 'measured' && (d.speeds[e]?.sec_per_item ?? 0) > 0).length
          return (
            <div key={d.id} className={`dev-wrap ${d.status === 'paused' ? 'paused' : ''}`}>
              <div className="dev">
                <span className="nm">
                  <button className="exp" aria-expanded={open} aria-label={`${open ? '收起' : '展开'}${d.name}`}
                    onClick={() => setExpanded(open ? expanded.filter((x) => x !== d.id) : [...expanded, d.id])}>{open ? '▾' : '▸'}</button>
                  <span><b>{d.name}</b><small>{deviceType(d.type)}</small></span>
                </span>
                <span className="st"><span className={`dot ${benching ? 'busy' : statusDot(d.status)}`} aria-hidden /> {benching ? '完整测速中…' : deviceStatus(d)}</span>
                <span className="speed-cell"><CommonSpeedIcons speeds={d.speeds} engines={d.engines} /><span className="tcount">已测 {measured} / {d.engines.length}</span></span>
                <span className="acts">
                  <button className="ghost" disabled={benching} onClick={() => void perform(() => benchmark(d.id))}>完整测速</button>
                  <DeviceMenu device={d} onEdit={(rePair) => setEditing({ device: d, rePair })} />
                </span>
              </div>
              {open && (
                <>
                  <ModelSpeedTable speeds={d.speeds} engines={d.engines} device={d.id} />
                  {(d.disk_free_bytes != null || d.disk_total_bytes != null) && (
                    <div className="dev-disk small muted">
                      <span>
                        本机可用空间{' '}
                        <b className={d.disk_free_bytes != null && d.disk_free_bytes < LOW_DISK_THRESHOLD_BYTES ? 'warn' : ''}>
                          {displayBytes(d.disk_free_bytes)}
                        </b>
                        {d.disk_total_bytes != null && ` / 共 ${displayBytes(d.disk_total_bytes)}`}
                      </span>
                      {d.cache_bytes != null && <span>缓存占用 <b className="mono">{displayBytes(d.cache_bytes)}</b></span>}
                    </div>
                  )}
                </>
              )}
            </div>
          )
        })}
        {!!clouds.length && (
          <div className="dev dev-cloud">
            <span className="nm"><span className="exp-space" /><span><b>云端 GPU</b><small>{accounts} 个账号 · 平时不开</small></span></span>
            <span className="st"><span className={`dot ${cloudState.dot}`} aria-hidden /><span>{cloudState.text}</span></span>
            <span className="speed-cell muted">共 {s.cloud?.gpus.filter((g) => g.available).length ?? 0} 张卡能开</span>
            <span className="acts"><button className="ghost" onClick={() => navigate('settings', 'cloud')}>管理</button></span>
          </div>
        )}
        <Item action={null}>
          <span className="row gap wrap"><button onClick={() => setAdding(true)}>＋ 添加算力</button><span className="small muted">家里或远程的电脑，或者 Colab 云端 GPU</span></span>
        </Item>
      </Section>
      <p className="desc">点名称左边的 ▸ 展开，看每个模型的速度，没测过的可以单独测。「完整测速」测全部模型，约 8 分钟；添加设备时的「快速测速」只测常用模型，约 2 分钟。</p>
      <AutoChoice key={JSON.stringify(s.auto)} />
      {adding && <AddCompute onClose={() => setAdding(false)} />}
      {editing && <DeviceEditor device={editing.device} rePair={editing.rePair} onClose={() => setEditing(null)} />}
    </>
  )
}

function DeviceMenu({ device: d, onEdit }: { device: ComputeDevice; onEdit: (rePair: boolean) => void }) {
  return (
    <Menu label="⋯" ariaLabel={`${d.name}的操作`} items={[
      { label: d.type === 'local' ? '改名…' : '改名 / 编辑…', onSelect: () => onEdit(false) },
      ...(d.type === 'lan' ? [{ label: '重新配对…', onSelect: () => onEdit(true) }] : []),
      { label: d.status === 'paused' ? '恢复使用' : '暂停使用', onSelect: () => void perform(() => patchDevice(d.id, { paused: d.status !== 'paused' })) },
      { label: '完整测速', onSelect: () => void perform(() => benchmark(d.id)) },
      ...(d.type === 'lan' ? [{ label: '移除', danger: true, onSelect: () => void perform(() => removeDevice(d.id)) }] : []),
    ]} />
  )
}

function InfoTip({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="info-wrap">
      <button className="info" aria-label="怎么选的" aria-expanded={open} onClick={() => setOpen(!open)} onBlur={() => setOpen(false)}
        onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false) }}>i</button>
      <span className={`tip ${open ? 'show' : ''}`} role="tooltip">{children}</span>
    </span>
  )
}

function AutoChoice() {
  const s = useStore()
  const [auto, setAuto] = useState<AutoSettings>(s.auto)
  const fixedOk = auto.mode !== 'fixed' || !!auto.device
  return (
    <Section title="自动选择" info={<InfoTip><b>怎么选的</b><br />本机很快能跑完就用本机。<br />局域网里有明显更快、又空着的设备，就换那台。<br />任务很大、云端 GPU 能省一半以上时间时，用云端 GPU；训练默认用云端 GPU。<br />花费超过下面设的提醒线时先问你。</InfoTip>}>
      <Item k="在哪跑">
        <span className="row gap wrap">
          <select aria-label="在哪跑" value={auto.mode} onChange={(e) => setAuto({ ...auto, mode: e.target.value as AutoSettings['mode'] })}>
            <option value="auto">自动选择</option><option value="fixed">固定一台</option>
          </select>
          {auto.mode === 'fixed' ? (
            <select aria-label="固定的设备" value={auto.device ?? ''} onChange={(e) => setAuto({ ...auto, device: e.target.value || null })}>
              <option value="">选择设备</option>
              {s.devices.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          ) : <span className="small muted">也可以总用固定的一台</span>}
        </span>
      </Item>
      <Item k="先问我" action={<button disabled={!fixedOk} onClick={() => void perform(async () => { await saveAuto(auto); setStatus('自动选择已保存') })}>保存</button>}>
        <span className="row gap wrap">一次花费超过
          <input className="in num" type="number" min={0} step="any" aria-label="一次花费超过多少单元" value={auto.ask_over} onChange={(e) => setAuto({ ...auto, ask_over: Number(e.target.value) })} />
          单元，或本月超过
          <input className="in num" type="number" min={0} step="any" aria-label="本月超过多少单元" value={auto.ask_month} onChange={(e) => setAuto({ ...auto, ask_month: Number(e.target.value) })} />
          单元时
        </span>
      </Item>
    </Section>
  )
}

/** 局域网设备的改名、地址、唤醒、重新配对；本机只能改名。 */
function DeviceEditor({ device, rePair, onClose }: { device: ComputeDevice | null; rePair?: boolean; onClose: () => void }) {
  const [name, setName] = useState(device?.name ?? '')
  const [address, setAddress] = useState(device?.address ?? '')
  const [pairCode, setPairCode] = useState('')
  const [wake, setWake] = useState(device?.wake?.mac ?? '')
  const [broadcast, setBroadcast] = useState(device?.wake?.broadcast ?? '')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const lan = device?.type !== 'local'
  const wakePayload = wake ? { mac: wake, broadcast: broadcast || null } : null
  const title = !device ? '添加局域网设备' : rePair ? '重新配对' : device.type === 'local' ? '设备改名' : '编辑设备'
  const canSave = !saving && (!!name.trim() || device?.type === 'local') && (!(!device || rePair) || (!!address.trim() && !!pairCode.trim()))
  const save = async () => {
    setSaving(true); setError(null)
    try {
      if (device) {
        const fields: Record<string, unknown> = { name }
        if (device.type === 'lan') { fields.address = address; fields.wake = wakePayload; if (pairCode) fields.pair_code = pairCode }
        await patchDevice(device.id, fields)
      } else {
        await sendJSON<ComputeDevice>('/compute/devices', { name, address, pair_code: pairCode, ...(wakePayload ? { wake: wakePayload } : {}) })
        await refreshDevices()
        setStatus(`已添加「${name}」`)
      }
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        setError(pairCode ? (e.message.includes('配对') ? '配对码不对或已过期，请从那台电脑重新取得配对码。' : '连不上这台设备，请检查连接地址。')
          : '连不上这台设备，请检查连接地址。')
      } else setError(e instanceof Error ? e.message : String(e))
    } finally { setSaving(false) }
  }
  return (
    <Modal title={title} onClose={onClose} closeLabel={null}
      footer={<><button onClick={onClose}>取消</button><span className="spacer" /><button className="primary" disabled={!canSave} onClick={() => void save()}>{saving ? '正在连接…' : device ? '保存' : '连接并添加'}</button></>}>
      <DeviceFields {...{ device, rePair, lan, name, setName, address, setAddress, pairCode, setPairCode, wake, setWake, broadcast, setBroadcast }} />
      {error && <div className="error-text selectable" role="alert">{error}</div>}
    </Modal>
  )
}

function DeviceFields(p: {
  device: ComputeDevice | null; rePair?: boolean; lan: boolean; name: string; setName: (v: string) => void; address: string; setAddress: (v: string) => void
  pairCode: string; setPairCode: (v: string) => void; wake: string; setWake: (v: string) => void; broadcast: string; setBroadcast: (v: string) => void
}) {
  return (
    <div className="stack">
      {(!p.device || p.rePair) && <div className="muted small">在那台电脑上启动执行程序后，终端会显示连接地址和配对码。</div>}
      <label className="field"><span>设备名</span><input value={p.name} disabled={p.rePair} onChange={(e) => p.setName(e.target.value)} placeholder="例如 gpu-worker" /></label>
      {p.lan && (
        <>
          <label className="field"><span>连接地址</span><input value={p.address} onChange={(e) => p.setAddress(e.target.value)} placeholder="那台电脑终端里显示的连接地址" /></label>
          <label className="field"><span>配对码</span><input value={p.pairCode} onChange={(e) => p.setPairCode(e.target.value.toUpperCase())} placeholder={!p.device || p.rePair ? 'XXX-XXX' : '重新配对时填写'} /></label>
          <details className="disclosure" open={!!p.wake}>
            <summary>唤醒设置</summary>
            <div className="stack">
              <div className="muted small">设备睡眠时，派任务前先唤醒。</div>
              <label className="field"><span>唤醒 MAC</span><input value={p.wake} disabled={p.rePair} onChange={(e) => p.setWake(e.target.value)} placeholder="可选" /></label>
              <label className="field"><span>广播地址</span><input value={p.broadcast} disabled={p.rePair} onChange={(e) => p.setBroadcast(e.target.value)} placeholder="可选" /></label>
            </div>
          </details>
        </>
      )}
    </div>
  )
}

// ───────── 添加算力（多步弹窗）

type Step = 'type' | 'lan' | 'cb1' | 'cb2' | 'cb3'
const stepLabel: Record<Exclude<Step, 'lan'>, string> = {
  type: '', cb1: '云端 GPU · 第 1 步，共 3 步', cb2: '云端 GPU · 第 2 步，共 3 步', cb3: '云端 GPU · 第 3 步，共 3 步',
}

export function AddCompute({ onClose, start = 'type' }: { onClose: () => void; start?: Step }) {
  const [step, setStep] = useState<Step>(start)
  const [account, setAccount] = useState<CloudAccount | null>(null)
  // Active login_id to cancel via DELETE /cloud/accounts/login/{id} on 换一个, leaving cb1 before done, or closing/unmounting before step 3.
  // Cleared when core reports failed or done with added !== true.
  const pendingLogin = useRef<string | null>(null)
  // Incremented whenever an in-flight login attempt is abandoned so late POST responses cancel instead of opening auth.
  const loginEpoch = useRef(0)
  const stepRef = useRef(step)
  useEffect(() => { stepRef.current = step }, [step])
  const discardPending = () => {
    loginEpoch.current += 1
    const id = pendingLogin.current
    if (!id) return
    pendingLogin.current = null
    void cancelCloudLogin(id)
  }
  useEffect(() => () => {
    loginEpoch.current += 1
    if (stepRef.current !== 'cb3') discardPending()
  }, [])
  const beginLoginAttempt = () => {
    discardPending()
    return loginEpoch.current
  }
  const onLoginStart = (epoch: number, id: string) => {
    if (epoch !== loginEpoch.current) { void cancelCloudLogin(id); return false }
    pendingLogin.current = id
    return true
  }
  const onLoginDone = (a: CloudAccount, added: boolean) => {
    if (!added) pendingLogin.current = null
    setAccount(a)
  }
  const onLoginFailed = () => { pendingLogin.current = null }
  const onSwitchAccount = () => { discardPending(); setAccount(null) }
  const close = () => {
    loginEpoch.current += 1
    if (step !== 'cb3') discardPending()
    onClose()
  }
  const finish = () => { loginEpoch.current += 1; onClose(); navigate('settings', 'cloud') }
  const body = useRef<HTMLDivElement>(null)
  // 换一步时原来的按钮没了：焦点放到这一步的第一个控件（没有就放到「下一步」）。
  // 从局域网向导回来时弹窗是重新挂上的，等它自己把焦点放进弹窗之后再放
  useEffect(() => {
    const timer = setTimeout(() => {
      const dialog = body.current?.closest<HTMLElement>('[role="dialog"]')
      const target = body.current?.querySelector<HTMLElement>('input, button:not(:disabled)')
        ?? dialog?.querySelector<HTMLElement>('.modal-foot button.primary:not(:disabled)') ?? dialog
      target?.focus()
    })
    return () => clearTimeout(timer)
  }, [step])
  // 局域网 / 远程是自己的四步向导（AddDevice.tsx），第 1 步的「上一步」回到这里选类型
  if (step === 'lan') return <LanWizard onBack={() => setStep('type')} onClose={close} />
  const footer = {
    type: <><span className="spacer" /><button onClick={close}>取消</button></>,
    cb1: <><button onClick={() => { onSwitchAccount(); setStep('type') }}>上一步</button><span className="spacer" /><button onClick={close}>取消</button>
      <button className="primary" disabled={!account} onClick={() => setStep('cb2')}>下一步</button></>,
    cb2: <><button onClick={() => setStep('cb1')}>上一步</button><span className="spacer" /><button onClick={close}>取消</button>
      <button className="primary" disabled={!account} onClick={() => setStep('cb3')}>下一步</button></>,
    cb3: <><button onClick={() => setStep('cb2')}>上一步</button><span className="spacer" /><button className="primary" autoFocus onClick={finish}>完成</button></>,
  }[step]
  return (
    <Modal title="添加算力" onClose={close} closeLabel={null} actions={stepLabel[step] && <span className="wstep">{stepLabel[step]}</span>} footer={footer}>
      <div className="stack" ref={body}>
      {step === 'type' && (
        <>
          <p className="wlead">要添加哪一种？</p>
          <div className="kinds">
            <button className="kind" onClick={() => setStep('lan')}>
              <span className="ki" aria-hidden>🖥</span><b>局域网或远程电脑</b>
              <span>家里的另一台电脑，或者通过 Tailscale 连着的电脑。免费，用它自己的 CPU / 显卡。</span>
            </button>
            <button className="kind" onClick={() => setStep('cb1')}>
              <span className="ki" aria-hidden>☁︎</span><b>云端 GPU（Colab）</b>
              <span>把一个 Google 账号加进账号池。开机时只选卡，账号自动挑。</span>
            </button>
          </div>
        </>
      )}
      {step === 'cb1' && (
        <CloudLogin
          account={account}
          beginLoginAttempt={beginLoginAttempt}
          onLoginStart={onLoginStart}
          onLoginDone={onLoginDone}
          onLoginFailed={onLoginFailed}
          onSwitchAccount={onSwitchAccount}
        />
      )}
      {step === 'cb2' && account && <CloudScan account={account} />}
      {step === 'cb3' && account && <CloudDone account={account} />}
      </div>
    </Modal>
  )
}

/** 第 1 步：在浏览器里授权，把页面上的授权码填回来；主控核对后把账号加进账号池。 */
function CloudLogin({
  account, beginLoginAttempt, onLoginStart, onLoginDone, onLoginFailed, onSwitchAccount,
}: {
  account: CloudAccount | null
  beginLoginAttempt: () => number
  onLoginStart: (epoch: number, id: string) => boolean
  onLoginDone: (a: CloudAccount, added: boolean) => void
  onLoginFailed: () => void
  onSwitchAccount: () => void
}) {
  const cloud = useStore((s) => s.cloud)
  const [login, setLogin] = useState<CloudLoginStart | null>(null)
  const [state, setState] = useState<CloudLoginStatus | null>(null)
  const [code, setCode] = useState('')
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [blocked, setBlocked] = useState(false)
  const pendingTab = useRef<Window | null>(null)
  useEffect(() => () => { pendingTab.current?.close(); pendingTab.current = null }, [])

  useEffect(() => {
    if (!login) return
    let stop = false
    const poll = async () => {
      while (!stop) {
        try {
          const value = await getJSON<CloudLoginStatus>(`/cloud/accounts/login/${encodeURIComponent(login.login_id)}`, { retry503: false })
          if (stop) return
          setState(value)
          setError(value.state === 'failed' ? value.error ?? '登录没有完成。' : null)
          if (value.state === 'done' && value.account) {
            onLoginDone(value.account, value.added === true)
            await refreshCloud()
            return
          }
          if (value.state === 'failed') { onLoginFailed(); return }
        } catch { if (!stop) setError('登录状态暂时取不到，正在重试…') }
        await new Promise((r) => setTimeout(r, 2000))
      }
    }
    void poll()
    return () => { stop = true }
  }, [login?.login_id]) // eslint-disable-line react-hooks/exhaustive-deps

  const start = () => {
    // 在点击里先开一个空白页，拿到授权地址后再跳过去，浏览器不会拦
    const epoch = beginLoginAttempt()
    const tab = window.open('', '_blank')
    pendingTab.current = tab
    setWorking(true); setError(null)
    void sendJSON<CloudLoginStart>('/cloud/accounts/login', {}, { retry503: false })
      .then((value) => {
        if (pendingTab.current === tab) pendingTab.current = null
        if (!onLoginStart(epoch, value.login_id)) { tab?.close(); return }
        setLogin(value)
        if (tab) tab.location.href = value.auth_url
        else setBlocked(true)
      })
      .catch((e) => {
        if (pendingTab.current === tab) pendingTab.current = null
        tab?.close()
        setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => setWorking(false))
  }
  const submit = () => {
    if (!login) return
    setWorking(true)
    void sendJSON<CloudLoginStatus>(`/cloud/accounts/login/${encodeURIComponent(login.login_id)}`, { code }, { retry503: false })
      .then(() => { setCode(''); setError(null) })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setWorking(false))
  }
  const reset = () => { setLogin(null); setState(null); setCode(''); setError(null); onSwitchAccount() }

  if (account) {
    return (
      <>
        <p className="wlead">登录要加进账号池的 Google 账号。</p>
        <div className="ok-line"><span className="okc" aria-hidden>✓</span><span>已登录：<b>{account.email ?? account.label}</b></span>
          <button className="ghost" onClick={reset}>换一个</button></div>
      </>
    )
  }
  return (
    <>
      <p className="wlead">登录要加进账号池的 Google 账号。</p>
      {!login ? (
        <div className="login">
          <button className="gbtn" disabled={working} onClick={start}>G　用 Google 账号登录</button>
          <span className="small muted">{working ? (cloud?.installed === false ? '正在准备云端 GPU 组件…' : '正在打开登录…') : '会打开浏览器让你同意，回来就好。'}</span>
        </div>
      ) : (
        <div className="stack">
          <div className="small">{blocked ? '浏览器拦住了新窗口，' : '已在新标签页打开登录，'}<a href={login.auth_url} target="_blank" rel="noreferrer">在浏览器中继续登录</a>。同意后页面上会显示授权码。</div>
          {login.needs_code && (
            <div className="row gap wrap">
              <input className="grow" placeholder="粘贴授权码" aria-label="授权码" value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" autoFocus />
              <button className="primary" disabled={!code.trim() || working} onClick={submit}>继续</button>
            </div>
          )}
          {(!state || state.state === 'waiting') && <div className="waiting-line"><span className="spin" aria-hidden /> 等待浏览器授权…</div>}
          {state?.state === 'failed' && <button onClick={reset}>重新登录</button>}
        </div>
      )}
      {error && <div className="warn small selectable" role="alert">{error}</div>}
    </>
  )
}

/** 第 2 步：这个账号的余额、本月已用、能开哪些卡（加进账号池后主控在后台试开，结果陆续出来）。 */
function CloudScan({ account }: { account: CloudAccount }) {
  const cloud = useStore((s) => s.cloud)
  const live = cloud?.accounts.find((a) => a.id === account.id) ?? account
  const common = useStore((s) => s.commonModels)
  // 一次只发一个请求：上一次回来后再等 5 秒；离开这一步时中止在途的请求，回来的结果也不写
  useEffect(() => {
    if (live.gpus.length) return
    const stop = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      await refreshCloud(false, stop.signal)
      if (!stop.signal.aborted) timer = setTimeout(() => void poll(), 5000)
    }
    // 第一次也走定时器：立刻卸载（开发模式的双重挂载）时请求根本不发出去
    timer = setTimeout(() => void poll(), 0)
    return () => { stop.abort(); clearTimeout(timer) }
  }, [live.gpus.length])
  const gpus = cloud?.gpus ?? []
  const engine = preferredEngine(common)
  return (
    <>
      <div className="ok-line"><span className="okc" aria-hidden>✓</span><b>{live.email ?? live.label}</b></div>
      <div className="fl2">
        <span className="muted">剩余</span><span><b>{live.balance != null ? `${displayUnits(live.balance)} 单元` : '—'}</b>　<span className="hint2">本月已用 {displayUnits(live.month_used_estimate)}</span></span>
        <span className="muted">能开</span>
        <span className="chips">
          {live.gpus.length ? live.gpus.map((g) => {
            const info = gpus.find((x) => x.gpu === g)
            return <span key={g} className="gchip" title={info?.units_per_hour != null ? `每小时约 ${displayUnits(info.units_per_hour)} 单元` : undefined}><SpeedIcon engine={engine} speed={info?.speeds[engine]} /> {displayGPU(g)}</span>
          }) : <span className="waiting-line small"><span className="spin" aria-hidden /> 正在查看能开哪些卡…</span>}
        </span>
        {!!live.gpus.length && <><span className="muted">开不了</span><span className="hint2">{gpus.filter((g) => !live.gpus.includes(g.gpu)).map((g) => g.label).join('、') || '—'}</span></>}
      </div>
      {live.error && <div className="warn small">{live.error}</div>}
    </>
  )
}

function CloudDone({ account }: { account: CloudAccount }) {
  const cloud = useStore((s) => s.cloud)
  const enabled = cloud?.accounts.filter((a) => !a.disabled) ?? [account]
  return (
    <>
      <div className="ok-line"><span className="okc" aria-hidden>✓</span><b>已加入账号池</b></div>
      <p className="small">加入后账号池共 <b>{enabled.length} 个账号</b>，合计剩余 <b>{displayUnits(cloud?.total_balance ?? account.balance ?? 0)} 单元</b>。</p>
      <ul className="what">
        <li>开机时只选卡，账号自动挑：能开这张卡、剩余最多的那个。</li>
        <li>每次真正跑任务前要先准备环境，约 5–7 分钟；同一批任务接着跑不用再等。</li>
        <li>花费超过你设的提醒线时先问你（设置 → 算力 → 自动选择）。</li>
      </ul>
    </>
  )
}

// ───────── 云端 GPU

export function CloudSettings() {
  const s = useStore()
  const cloud = s.cloud
  const [view, setView] = useState<'gpu' | 'acct'>('gpu')
  const [adding, setAdding] = useState(false)
  if (!cloud) {
    return s.computeLoading ? <div className="muted">正在读取云端 GPU…</div> : (
      <div className="empty compact"><div className="empty-title">云端 GPU 信息暂时取不到</div><button onClick={() => void refreshCloud()}>重试</button></div>
    )
  }
  const enabled = cloud.accounts.filter((a) => !a.disabled)
  return (
    <>
      <div className="row between wrap gap">
        <Segmented label="查看方式" value={view} onChange={setView} options={[{ value: 'gpu', label: '按 GPU' }, { value: 'acct', label: '按账号' }]} />
        <span className="small" title={`本月用量来自主控的记录，是估计值。精确合计：${cloud.month_used_estimate} 单元`}>
          <b>{enabled.length} 个账号 · 合计剩余 {displayUnits(cloud.total_balance)} 单元 · 本月合计已用 {displayUnits(cloud.month_used_estimate)}</b>
        </span>
      </div>
      {view === 'gpu' ? (
        <>
          <Section title="账号池能开的卡">
            <div className="gpur h" aria-hidden><span>GPU</span><span>常用模型 <SpeedLegend /></span><span>能开的账号</span><span className="n">账号池够用</span></div>
            <div className="legend-m" aria-hidden>常用模型 <SpeedLegend /></div>
            {[...cloud.gpus.filter((g) => g.available), ...cloud.gpus.filter((g) => !g.available)].map((g) => <GpuRow key={g.gpu} gpu={g} />)}
            {!enabled.length && (
              <Item><span className="row gap wrap"><span className="small muted">还没有登录账号，添加一个 Google 账号来使用云端 GPU。</span><button onClick={() => setAdding(true)}>＋ 添加 Google 账号</button></span></Item>
            )}
          </Section>
          <p className="desc">开机时只选卡，账号自动挑：能开这张卡、剩余最多的那个；它余额不够或开不出卡时换下一个。</p>
          <CloudPreferences key={JSON.stringify(cloud.settings)} prefs={cloud.settings} gpus={cloud.gpus} />
        </>
      ) : (
        <>
          <Section title="账号">
            <div className="acr h" aria-hidden><span>账号</span><span className="n">剩余</span><span className="n">本月已用</span><span>能开的卡</span><span /></div>
            {cloud.accounts.map((a, i) => <AccountRow key={a.id} account={a} color={palette[i % palette.length]} />)}
            {!cloud.accounts.length && <Item><span className="muted">还没有登录账号</span></Item>}
            <Item><span className="row gap wrap"><button onClick={() => setAdding(true)}>＋ 添加 Google 账号</button><span className="small muted">加进来的账号自动参与开机</span></span></Item>
          </Section>
          <Section title="每月用量（单元）"><UsageChart /></Section>
        </>
      )}
      {s.cloudError && <div className="row gap"><span className="warn small">{s.cloudError}</span><button onClick={() => void refreshCloud()}>重试</button></div>}
      {adding && <AddCompute start="cb1" onClose={() => { setAdding(false); setView('acct') }} />}
    </>
  )
}

function GpuRow({ gpu: g }: { gpu: CloudGpu }) {
  const [open, setOpen] = useState(false)
  const dev = useStore((s) => s.devices.find((d) => d.type === 'cloud' && (d.gpu === g.gpu || d.id === `colab-${g.gpu}`)))
  const active = !!dev && ['busy', 'training', 'waking'].includes(dev.status)
  const warm = !active && !!dev && (dev.warm ?? 0) > 0
  return (
    <div className={`gpu-wrap ${g.available ? '' : 'dis'}`}>
      <div className="gpur">
        <span className="gpur-name"><button className="exp" aria-expanded={open} aria-label={`${open ? '收起' : '展开'}${g.label}`} onClick={() => setOpen(!open)}>{open ? '▾' : '▸'}</button>
          <b title={g.units_per_hour != null ? `每小时约 ${displayUnits(g.units_per_hour)} 单元` : '暂无报价'}>{g.label}</b>
          {active && dev && <span className="badge warn">{deviceStatus(dev)}</span>}
          {warm && <span className="badge ok">保留中</span>}
        </span>
        <CommonSpeedIcons speeds={g.speeds} />
        <span>{g.available ? `${g.account_count} 个账号能开` : '没有账号能开'}</span>
        <span className="n">{g.available && g.hours_left != null ? <><span className="lbl">账号池够用</span>约 {displayHours(g.hours_left)} 小时</> : '—'}</span>
      </div>
      {!g.available && g.reason && <div className="gpu-reason small muted">{g.reason}</div>}
      {open && <ModelSpeedTable speeds={g.speeds} note="云端 GPU 第一次跑这个模型后，会自动记下速度。" />}
    </div>
  )
}

function CloudPreferences({ prefs, gpus }: { prefs: CloudPrefs; gpus: CloudGpu[] }) {
  const [value, setValue] = useState<CloudPrefs>(prefs)
  const save = () => perform(async () => {
    await request('/cloud/settings', { method: 'PUT', json: value, retry503: false })
    await refreshCloud()
    setStatus('云端 GPU 偏好已保存')
  })
  return (
    <Section title="使用偏好">
      <Item k="开哪张卡">
        <select aria-label="开哪张卡" value={value.gpu_pref} onChange={(e) => setValue({ ...value, gpu_pref: e.target.value as CloudPrefs['gpu_pref'] })}>
          <option value="auto">按任务推荐</option>
          {gpus.map((g) => <option key={g.gpu} value={g.gpu}>总用 {g.label}</option>)}
        </select>
      </Item>
      <Item k="最长用多久">
        <span className="row gap wrap">
          <select aria-label="最长用多久" value={value.max_minutes == null ? 'auto' : 'custom'} onChange={(e) => setValue({ ...value, max_minutes: e.target.value === 'auto' ? null : 120 })}>
            <option value="auto">按任务安排</option><option value="custom">自定时间</option>
          </select>
          {value.max_minutes != null && <><input className="in num" type="number" min={0.5} step="0.5" aria-label="小时" value={value.max_minutes / 60}
            onChange={(e) => setValue({ ...value, max_minutes: Math.round(Number(e.target.value) * 60) })} /> 小时</>}
          <span className="small muted">{value.keep_minutes === 0 ? '到时间自动关掉；任务做完也马上关' : '到时间自动关掉'}</span>
        </span>
      </Item>
      <Item k="做完先别关" action={<button onClick={() => void save()}>保存</button>}>
        <span className="row gap wrap">
          <label className="check"><input type="checkbox" checked={value.keep_minutes > 0} onChange={(e) => setValue({ ...value, keep_minutes: e.target.checked ? 10 : 0 })} />再留</label>
          {value.keep_minutes > 0 && <><input className="in num" type="number" min={1} aria-label="分钟" value={value.keep_minutes} onChange={(e) => setValue({ ...value, keep_minutes: Number(e.target.value) })} /> 分钟</>}
          <span className="small muted">接着跑下一批就不用再准备环境。</span>
        </span>
      </Item>
    </Section>
  )
}

const palette = ['#2a78d6', '#eb6834', '#1baf7a', '#9b6bd6', '#d4a017', '#d6457a']

function AccountRow({ account: a, color }: { account: CloudAccount; color: string }) {
  const [error, setError] = useState<string | null>(null)
  const act = (path: string, method = 'POST') => perform(async () => {
    setError(null)
    try { await request(`/cloud/accounts/${encodeURIComponent(a.id)}${path}`, { method, retry503: false }) }
    catch (e) {
      // 移除时授权没删掉：主控已先停用，可以再试
      if (method === 'DELETE' && e instanceof ApiError && e.status === 502) { setError('这个账号的授权没删掉，已先停用，可以再试'); await refreshCloud(); return }
      throw e
    }
    await refreshCloud()
  })
  return (
    <div className="acr-wrap">
      <div className="acr">
        <span><i className="sw" style={{ background: color }} aria-hidden />{a.email ?? a.label}{a.disabled && <span className="badge"> 已停用</span>}</span>
        <span className="n"><span className="lbl">剩余</span><b>{a.balance != null ? displayUnits(a.balance) : '—'}</b><span className="lbl after">单元</span></span>
        <span className="n" title={`主控记录的估计值：${a.month_used_estimate} 单元`}><span className="lbl">本月已用</span>{displayUnits(a.month_used_estimate)}</span>
        <span className="chips">{a.gpus.map((g) => <span key={g} className="gchip">{displayGPU(g)}</span>)}</span>
        <span className="acts">
          {a.id === 'default'
            ? <button className="ghost" onClick={() => void act(a.disabled ? '/enable' : '/disable')}>{a.disabled ? '启用' : '停用'}</button>
            : <button className="ghost" onClick={() => void act('', 'DELETE')}>移除</button>}
          <Menu label="⋯" ariaLabel={`${a.email ?? a.label}的操作`} items={[
            { label: '刷新', disabled: a.disabled, onSelect: () => void act('/refresh') },
            ...(a.id !== 'default' ? [{ label: a.disabled ? '启用' : '停用', onSelect: () => void act(a.disabled ? '/enable' : '/disable') }] : []),
          ]} />
        </span>
      </div>
      {(error || a.error) && <div className="small warn acr-err">{error ?? a.error}</div>}
    </div>
  )
}

function UsageChart() {
  const cloud = useStore((s) => s.cloud)!
  const ref = useRef<HTMLDivElement>(null)
  if (!cloud.usage_monthly.length) return <div className="muted chart-empty">还没有用量记录</div>
  const ids = [...new Set(cloud.usage_monthly.flatMap((m) => Object.keys(m.by_account)))]
  const label = (id: string) => cloud.account_labels[id] ?? id
  const order = cloud.accounts.map((a) => a.id)
  const color = (id: string) => palette[(order.indexOf(id) >= 0 ? order.indexOf(id) : order.length + ids.indexOf(id)) % palette.length]
  // 合计写在这个月最上面那一段的顶上（上面的账号这个月可能是 0）：每段只在自己是最上面那段时带上合计
  const data = cloud.usage_monthly.map((m) => {
    const total = Object.values(m.by_account).reduce((a, b) => a + b, 0)
    const top = [...ids].reverse().find((id) => (m.by_account[id] ?? 0) > 0)
    return { month: `${Number(m.month.slice(5))} 月`, ...m.by_account, ...Object.fromEntries(ids.map((id) => [`top:${id}`, id === top ? total : null])) }
  })
  return (
    <div className="chart-wrap" ref={ref} title="用量来自主控的记录，是估计值。">
      <div className="legend">{ids.map((id) => <span key={id}><i className="sw" style={{ background: color(id) }} aria-hidden />{label(id)}</span>)}</div>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 18, right: 8, left: 0, bottom: 0 }}>
          <XAxis dataKey="month" tickLine={false} axisLine={false} fontSize={12} />
          <YAxis hide />
          <Tooltip cursor={{ fillOpacity: 0.08 }} formatter={(v, name) => [displayUnits(Number(v)), label(String(name))]} />
          {ids.map((id) => (
            <Bar key={id} dataKey={id} stackId="u" fill={color(id)} maxBarSize={28} isAnimationActive={false}>
              <LabelList dataKey={`top:${id}`} position="top" fontSize={11} formatter={(v) => (v == null ? '' : displayUnits(Number(v)))} />
            </Bar>
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
