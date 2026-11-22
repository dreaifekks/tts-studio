// 方案 C 的「在哪跑」：平时一行「自动：在 X 上生成 · 约 N 分钟 · 理由」+「更改」；花费超过提醒线时弹确认窗。
import { useEffect, useId, useRef, useState } from 'react'
import type { ComputeDevice, PlanCandidate } from './api/types'
import { rovingKeyDown, trapTab } from './a11y'
import { RadioRow, SpeedIcon } from './components'
import {
  candidateCost, computeReason, deviceStatus, deviceType, displayDuration, displayGPU, displayUnits, exactEstimate, unavailableReason,
} from './display'
import {
  candidateUsable, clearPreview, leaveDraft, patchPlanner, refreshPlan, reminder, resolveConfirmation, selectedCandidate, usePlanners, withdrawPlan, type PlannerId,
} from './planner'
import { navigate, useStore } from './store'

/** 云端开始干活前准备环境要几分钟（core 的 prep_sec，取整、至少 1）；没给时 null */
const prepMinutes = (c: PlanCandidate) => (c.prep_sec != null ? Math.max(1, Math.round(c.prep_sec / 60)) : null)

/** 候选的第二行：不能用的原因、睡眠中、设备忙闲 / 排队、云端要准备环境。 */
function candidateNote(c: PlanCandidate, kind: string, devices: ComputeDevice[]) {
  if (!c.available) return c.reason || unavailableReason(c)
  if (c.wake) return '睡眠中，开始前先唤醒'
  const device = devices.find((d) => d.id === c.device)
  if (c.type === 'cloud') {
    const minutes = prepMinutes(c)
    return `云端 GPU · 要先准备环境${minutes != null ? `约 ${minutes} 分钟` : ' 5–7 分钟'}`
  }
  if (kind === 'train' && c.type === 'local' && device?.hw?.accel !== 'cuda') return `${deviceType(c.type)} · 只能用 CPU 训练`
  const status = device ? deviceStatus(device) : c.idle ? '空闲' : '忙'
  return `${deviceType(c.type)} · ${status}${!c.idle ? '，要排队' : ''}`
}
const dotOf = (c: PlanCandidate | undefined) => (!c || !c.available || c.type === 'cloud' ? 'off' : c.idle ? 'ok' : 'busy')

export function ComputePicker({ id, input, action, scope, taskTitle = '', ready = true, notReadyText = '选择模型版本后估算', sweep = false }: {
  id: PlannerId; input: object; action: string; scope: string; taskTitle?: string; ready?: boolean; notReadyText?: string; sweep?: boolean
}) {
  const planner = usePlanners((s) => s[id])
  const connected = useStore((s) => s.connected)
  const devices = useStore((s) => s.devices)
  const [open, setOpen] = useState(false)
  const [up, setUp] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const listId = useId()
  const key = JSON.stringify(input) + (planner.selected ?? 'auto') + ready + connected
  // 只认按现在的参数和设备估的报价
  const fresh = planner.planKey === key
  const plan = fresh ? planner.plan : null
  const chosen = selectedCandidate(plan)
  const shown = open && ready && !!plan

  // 参数或设备一变，旧报价立刻撤下；停 250ms 再要新报价
  useEffect(() => {
    if (!ready || !connected) { clearPreview(id); return }
    withdrawPlan(id, key)
    const t = setTimeout(() => void refreshPlan(id, input as Record<string, unknown>, sweep, key), 250)
    return () => clearTimeout(t)
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps -- key 已包含 input、选择和就绪状态
  useEffect(() => { patchPlanner(id, { action, taskSummary: taskTitle }) }, [id, action, taskTitle])
  // 离开这份表单（取消草稿、换页）时，这次临时选的设备不带到下一次
  useEffect(() => () => leaveDraft(id), [id])

  useEffect(() => {
    if (!shown) return
    root.current?.querySelector<HTMLElement>('[role="option"][aria-selected="true"]')?.focus()
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [shown])

  const pick = (device: string | null) => { patchPlanner(id, { selected: device, notice: null }); setOpen(false) }
  // 报价失败后手动再要一次（没有成功报价不能提交）
  const retry = () => { withdrawPlan(id, key); void refreshPlan(id, input as Record<string, unknown>, sweep, key) }
  // 下面放不下就向上展开（桌面上状态栏会挡住）
  const openList = () => {
    const r = root.current?.getBoundingClientRect()
    const bottom = document.querySelector('.statusbar')?.getBoundingClientRect().top ?? innerHeight
    setUp(!!r && bottom - r.bottom < 340 && r.top > bottom - r.bottom)
    setOpen(true)
  }
  const busy = planner.submitting || !ready
  const chosenUnavailable = !!plan && !!chosen && !candidateUsable(planner, chosen)
  const chosenWarning = chosenUnavailable ? `${chosen.name}：${chosen.reason || unavailableReason(chosen)}` : null
  let summary: React.ReactNode
  if (!ready) summary = notReadyText
  else if (fresh && planner.allCompleted) summary = '都已完成'
  else if (plan && chosen) {
    const suffix = chosenUnavailable ? (chosen.reason || unavailableReason(chosen)) : planner.selected === null ? computeReason(plan.reason_code) : candidateCost(chosen)
    const prep = !chosenUnavailable && chosen.type === 'cloud' ? prepMinutes(chosen) : null
    const dur = `${displayDuration(chosen.seconds)}${prep != null ? `（含准备约 ${prep} 分钟）` : ''}`
    summary = <>{planner.selected === null ? '自动：' : '这次：'}在 <b>{chosen.name}</b> 上{action} · {dur}{suffix ? ` · ${suffix}` : ''}</>
  } else if (!fresh || planner.loading) summary = '正在估算…'
  else summary = planner.error ? '暂时估算不了' : '暂无可用设备'

  return (
    <div className="where" ref={root}>
      <div className="auto-c">
        <span className={`dot ${ready && plan ? dotOf(chosen) : 'off'}`} aria-hidden />
        <span className="auto-sum" title={chosen ? exactEstimate(chosen) : undefined} aria-live="polite">{summary}</span>
        {ready && fresh && !planner.loading && planner.error !== null && <button className="chg" disabled={planner.submitting} onClick={retry}>重新估算</button>}
        {planner.selected !== null && <button className="chg" disabled={busy} onClick={() => pick(null)}>恢复自动</button>}
        <button className="chg" disabled={busy || !plan} aria-haspopup="listbox" aria-expanded={shown} aria-controls={listId}
          onClick={() => (shown ? setOpen(false) : openList())} onKeyDown={(e) => { if (e.key === 'ArrowDown' && plan && !shown) { e.preventDefault(); openList() } }}>
          {shown ? '收起' : '更改'}
        </button>
      </div>
      {ready && (planner.notice || chosenWarning || (fresh && planner.error)) && (
        <div className="warn small" role="status">{planner.notice ?? chosenWarning ?? planner.error}</div>
      )}
      {shown && (
        <div className={`chg-pop ${up ? 'up' : ''}`} id={listId}>
          <div className="ph">这一次在哪{action}（{scope}）</div>
          <div role="listbox" aria-label={`这一次在哪${action}`}
            onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false) } else rovingKeyDown(e, undefined, 'vertical') }}>
            <div className={`opt ${planner.selected === null ? 'on' : ''}`} role="option" aria-selected={planner.selected === null} data-roving
              tabIndex={planner.selected === null ? 0 : -1} onClick={() => pick(null)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(null) } }}>
              <span className={`radio ${planner.selected === null ? 'on' : ''}`} aria-hidden />
              <span><b>自动选择</b><small>{chosen && planner.selected === null ? `现在是 ${chosen.name}` : '按推荐选'}</small></span>
              <span />
            </div>
            {plan.candidates.map((c) => {
              const on = planner.selected === c.device
              return (
                <div key={c.device} className={`opt ${on ? 'on' : ''} ${c.available ? '' : 'dis'}`} role="option" aria-selected={on} aria-disabled={!c.available}
                  data-roving tabIndex={on ? 0 : -1} title={exactEstimate(c)}
                  onClick={() => { if (c.available) pick(c.device) }}
                  onKeyDown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && c.available) { e.preventDefault(); pick(c.device) } }}>
                  <span className={`radio ${on ? 'on' : ''}`} aria-hidden />
                  <span><b>{c.name}</b>{c.device === plan.recommended && <> <span className="badge acc">推荐</span></>}<small>{candidateNote(c, planner.previewKind, devices)}</small></span>
                  <span className="t">{displayDuration(c.seconds)}<small>{candidateCost(c)}</small></span>
                </div>
              )
            })}
          </div>
          <div className="pf">只对这一次生效</div>
        </div>
      )}
    </div>
  )
}

/** 超过提醒线时的确认窗（画面 1b）：可以换一台，按钮跟着所选那一行变。 */
export function ComputeConfirmation() {
  const planners = usePlanners()
  const cloud = useStore((s) => s.cloud)
  const devices = useStore((s) => s.devices)
  const entry = (Object.entries(planners) as [PlannerId, (typeof planners)[PlannerId]][]).find(([, p]) => p.confirmation)
  const startRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { if (entry) startRef.current?.focus() }, [entry?.[0]]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!entry) return null
  const [id, p] = entry
  const plan = p.confirmation!
  const chosen = plan.candidates.find((c) => c.device === p.confirmationDevice)
  const cancel = () => resolveConfirmation(id, false)
  const task = p.confirmationTask || p.action
  const minutes = chosen?.type === 'cloud' ? prepMinutes(chosen) : null
  const prep = minutes != null ? `（其中准备环境约 ${minutes} 分钟）`
    : chosen?.type === 'cloud' && (chosen.seconds ?? 0) >= 360 ? '（其中准备环境约 6 分钟）' : ''
  const title = chosen?.type === 'cloud' ? `这次约花 ${displayUnits(chosen.units ?? 0)} 单元，开始吗？` : '这次免费，开始吗？'
  return (
    <div className="modal-backdrop sheet-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) cancel() }}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label={title}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); cancel() } else trapTab(e) }}>
        <h3>{title}</h3>
        <p>{task}。用 {chosen?.name ?? '所选设备'} {displayDuration(chosen?.seconds)}{prep}。{reminder(plan, chosen)}</p>
        <div className="tbl">
          <div className="crow h"><span /><span>在哪跑</span><span>速度</span><span className="n">大约多久</span><span className="n">花费</span></div>
          <div role="radiogroup" aria-label="在哪跑" onKeyDown={(e) => rovingKeyDown(e, (el) => patchPlanner(id, { confirmationDevice: el.closest<HTMLElement>('[data-device]')?.dataset.device ?? null }), 'vertical')}>
            {plan.candidates.map((c) => (
              <div key={c.device} data-device={c.device} className="crow-wrap">
                <RadioRow className="crow" checked={c.device === p.confirmationDevice} disabled={!c.available}
                  onSelect={() => patchPlanner(id, { confirmationDevice: c.device })}>
                  <span><b>{c.name}</b>{c.device === plan.recommended && <> <span className="badge acc">推荐</span></>}<small>{candidateNote(c, p.previewKind, devices)}</small></span>
                  <span>{p.previewEngine && <SpeedIcon engine={p.previewEngine} speed={devices.find((d) => d.id === c.device)?.speeds[p.previewEngine]} />}</span>
                  <span className="n" title={exactEstimate(c)}>{displayDuration(c.seconds)}</span>
                  <span className="n">{candidateCost(c)}</span>
                </RadioRow>
              </div>
            ))}
          </div>
        </div>
        <div className="pf">
          <span>本月已用 {displayUnits(plan.month_used)} 单元{cloud ? ` · 账号池还剩 ${displayUnits(cloud.total_balance)}` : ''}</span>
          <span className="spacer" />
          <button className="ghost" onClick={() => { cancel(); navigate('settings', 'compute') }}>调整提醒线</button>
        </div>
        <div className="btns">
          <button onClick={cancel}>取消</button>
          <button ref={startRef} className="primary" disabled={!chosen?.available} onClick={() => resolveConfirmation(id, true)}>
            用 {chosen ? (chosen.gpu ? displayGPU(chosen.gpu) : chosen.name) : '所选设备'} 开始
          </button>
        </div>
      </div>
    </div>
  )
}
