import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import type { AudioFormat, AudioItem, EngineInfo, EngineSpeed, EvalSetName, Job } from './api/types'
import { isActive, jobKindLabel, jobStatusLabel } from './api/client'
import { activateKeyDown, rovingKeyDown, trapTab } from './a11y'
import {
  displayAccuracy, displayEngine, displayEvalSet, displayItemSpeed, displayScore, displayScorerSpeed, displaySeconds,
  displaySimilarity, displayTrainSpeed, isZeroShot, scoreHelp, sortEngines, sourceLabel, speedGrade, speedTip,
  versionOption, versionScore,
} from './display'
import {
  benchmark, characterName, clearReview, engineLabel, engineList, exportAudioItem, navigate, perform, playItem, problemTags, review, useStore,
  type SortPref, type VersionList,
} from './store'

// ───────── 基础

export function PageHeading({ children }: { children: ReactNode }) {
  return <p className="page-heading">{children}</p>
}

export function Metric({ title, value, help }: { title: string; value: string; help?: string }) {
  return (
    <div className="metric" title={help}>
      <div className="metric-title">{title}</div>
      <div className="metric-value">{value}</div>
    </div>
  )
}

export function JobProgress({ job }: { job: Job }) {
  return (
    <div className="job-progress">
      <div className="row between gap wrap">
        <span className="row gap wrap">
          <span className="muted small">{jobKindLabel[job.kind] ?? job.kind}</span>
          {job.compute?.name && <span className="where-tag">{job.compute.name}</span>}
          <span>{job.stage || jobStatusLabel[job.status]}</span>
        </span>
        <span className="mono">{Math.round(job.progress * 100)}%</span>
      </div>
      <div className="progress" role="progressbar" aria-valuenow={Math.round(job.progress * 100)} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
    </div>
  )
}

/** 弹窗：Esc 关闭，Tab 在弹窗内循环，打开时焦点进弹窗、关闭后回到原来的位置。 */
export function Modal({ title, onClose, children, wide, actions, footer, closeLabel = '完成' }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean; actions?: ReactNode; footer?: ReactNode; closeLabel?: string | null
}) {
  const ref = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  useEffect(() => { close.current = onClose })
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    ref.current?.focus()
    // 内容换掉（多步弹窗换一步）后焦点会掉到页面上：这时 Esc 照样关闭，Tab 回到弹窗里
    const onKey = (e: KeyboardEvent) => {
      if (document.activeElement && document.activeElement !== document.body) return
      const open = document.querySelectorAll('[aria-modal="true"]')
      if (open[open.length - 1] !== ref.current) return
      if (e.key === 'Escape') { e.preventDefault(); close.current() }
      else if (e.key === 'Tab') { e.preventDefault(); ref.current?.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); before?.focus?.() }
  }, [])
  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className={`modal ${wide ? 'wide' : ''}`} ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-label={title}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } else trapTab(e) }}>
        <div className="modal-head">
          <h2>{title}</h2>
          <div className="row gap">{actions}{closeLabel && <button onClick={onClose}>{closeLabel}</button>}</div>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}

export function Confirm({ message, confirmLabel, onConfirm, onClose, danger = true, cancelLabel = '取消' }: {
  message: string; confirmLabel: string; onConfirm: () => void; onClose: () => void; danger?: boolean; cancelLabel?: string
}) {
  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal confirm" tabIndex={-1} role="alertdialog" aria-modal="true" aria-label={message}
        onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } else trapTab(e) }}>
        <p>{message}</p>
        <div className="row gap end">
          <button onClick={onClose}>{cancelLabel}</button>
          <button className={danger ? 'danger' : 'primary'} autoFocus onClick={() => { onClose(); onConfirm() }}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}

/** 分段选择（单选组）：方向键切换。 */
export function Segmented<T extends string>({ value, onChange, options, label, disabled }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: string }[]; label: string; disabled?: boolean
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}
      onKeyDown={(e) => rovingKeyDown(e, (el) => onChange(el.dataset.value as T), 'horizontal')}>
      {options.map((o) => (
        <button key={o.value} role="radio" aria-checked={value === o.value} data-roving data-value={o.value} tabIndex={value === o.value ? 0 : -1}
          className={value === o.value ? 'active' : ''} disabled={disabled} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  )
}

/** 评测集切换（难句 / 快速）。 */
export function EvalSetSwitch({ value, onChange, label = '评测集', disabled }: { value: EvalSetName; onChange: (v: EvalSetName) => void; label?: string; disabled?: boolean }) {
  return (
    <div className="row gap">
      <span className="muted small">{label}</span>
      <Segmented label={label} value={value} disabled={disabled} onChange={onChange}
        options={(['hard', 'mini'] as const).map((v) => ({ value: v, label: displayEvalSet(v) }))} />
    </div>
  )
}

export interface MenuItem { label: string; onSelect: () => void; danger?: boolean; disabled?: boolean; checked?: boolean; note?: boolean }
/** 「⋯」之类的菜单：回车 / 空格 / 下箭头打开，方向键移动，Esc 关闭并回到按钮。 */
export function Menu({ label, ariaLabel, items, up, className }: { label: ReactNode; ariaLabel: string; items: MenuItem[]; up?: boolean; className?: string }) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const id = useId()
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLElement>('[data-roving]:not(:disabled)')?.focus()
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  const close = () => { setOpen(false); button.current?.focus() }
  return (
    <div className={`menu ${up ? 'up' : ''} ${className ?? ''}`} ref={root} onClick={(e) => e.stopPropagation()}>
      <button ref={button} className="menu-button" aria-label={ariaLabel} aria-haspopup="menu" aria-expanded={open} aria-controls={id}
        onClick={() => setOpen(!open)} onKeyDown={(e) => { if (e.key === 'ArrowDown' && !open) { e.preventDefault(); setOpen(true) } }}>{label}</button>
      {open && (
        <div className="menu-list" role="menu" id={id} aria-label={ariaLabel}
          onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); close() } else if (e.key === 'Tab') setOpen(false); else rovingKeyDown(e, undefined, 'vertical') }}>
          {items.map((item, i) => item.note
            ? <div key={i} className="menu-note" role="presentation">{item.label}</div>
            : (
              <button key={i} data-roving role={item.checked === undefined ? 'menuitem' : 'menuitemcheckbox'} aria-checked={item.checked} tabIndex={-1}
                className={`${item.danger ? 'danger' : ''} ${item.checked ? 'checked' : ''}`} disabled={item.disabled}
                onClick={() => { if (item.checked === undefined) close(); item.onSelect() }}>
                {item.checked !== undefined && (item.checked ? '● ' : '○ ')}{item.label}
              </button>
            ))}
        </div>
      )}
    </div>
  )
}

// ───────── 候选

export function Stars({ item }: { item: AudioItem }) {
  const rating = item.review?.rating ?? 0
  return (
    <div className="stars" role="group" aria-label="评价">
      <span className="muted small">评价</span>
      {[1, 2, 3, 4, 5].map((r) => (
        <button key={r} className={`star ${r <= rating ? 'on' : ''}`} aria-label={`打 ${r} 分`} aria-pressed={r <= rating}
          onClick={(e) => { e.stopPropagation(); void review(item, { rating: r }) }}>
          {r <= rating ? '★' : '☆'}
        </button>
      ))}
    </div>
  )
}

export function TagMenu({ item }: { item: AudioItem }) {
  const tags = item.review?.tags ?? []
  return (
    <Menu label="⌗ 问题标签 ▾" ariaLabel="问题标签"
      items={problemTags.map((t) => ({ label: t, checked: tags.includes(t), onSelect: () => void review(item, { toggleTag: t }) }))} />
  )
}

export function PlayButton({ id, onClick, label }: { id: string; onClick: () => void; label: string }) {
  const active = useStore((s) => s.playingId === id && s.playing)
  return (
    <button className="play" aria-label={label} aria-pressed={active} onClick={(e) => { e.stopPropagation(); onClick() }}>
      {active ? '❚❚' : '▶'}
    </button>
  )
}

export function CandidateCard({ item, best, selected, onSelect, onDebug, onReuse, showDownload = false, extra }: {
  item: AudioItem; best?: boolean; selected?: boolean; onSelect?: () => void; onDebug?: () => void; onReuse?: () => void; showDownload?: boolean; extra?: ReactNode
}) {
  const where = useStore((s) => (item.compute ? s.devices.find((d) => d.id === item.compute)?.name ?? null : null))
  const defaultFmt = useStore((s) => s.defaultExportFormat)
  const formats: AudioFormat[] = [defaultFmt, ...(['wav', 'flac', 'mp3'] as AudioFormat[]).filter((f) => f !== defaultFmt)]
  const menuItems: MenuItem[] = [
    { label: `生成编号：${item.seed}`, note: true, onSelect: () => {} },
    ...(onDebug ? [{ label: '生成详情', onSelect: onDebug }] : []),
    ...(onReuse ? [{ label: '复用参数与种子', onSelect: onReuse }] : []),
    ...(showDownload
      ? formats.map((f) => ({
        label: `下载 ${f.toUpperCase()}${f === defaultFmt ? '（默认）' : ''}`,
        onSelect: () => void perform(() => exportAudioItem(item, f)),
      }))
      : []),
  ]
  return (
    <div className={`card candidate ${selected ? 'selected' : ''} ${best ? 'best' : ''}`} onClick={onSelect} data-id={item.id}>
      <div className="row gap wrap">
        <PlayButton id={item.id} onClick={() => playItem(item)} label={`播放候选 ${item.id}`} />
        {best && <span className="best-label">✦ 最佳</span>}
        <span>{characterName(item.character)} · {displayEngine(item.engine)}</span>
        {where && <span className="where-tag" title="在这台设备上生成">{where}</span>}
        <span className="spacer" />
        <span className="muted small mono">{displaySeconds(item.duration)} 秒</span>
        {onReuse && (
          <button className="small-btn" onClick={(e) => { e.stopPropagation(); onReuse() }}>复用参数与种子</button>
        )}
        {extra}
        <Menu label="⋯" ariaLabel={`候选 ${item.id} 的更多操作`} items={menuItems} />
      </div>
      <div className="candidate-text">{item.asr_text ?? item.text}</div>
      <div className="metrics three">
        <Metric title="评分" value={displayScore(item.score)} help={scoreHelp(item.score)} />
        <Metric title="读音准确" value={displayAccuracy(item.cer_kana ?? item.cer)} />
        <Metric title="音色相似" value={displaySimilarity(item.spk_sim)} />
      </div>
      <div className="row between wrap">
        <Stars item={item} />
        <div className="row gap">
          {item.review && <button className="link-btn" onClick={(e) => { e.stopPropagation(); void clearReview(item) }}>清除评价</button>}
          <TagMenu item={item} />
        </div>
      </div>
      {!!item.review?.tags?.length && <div className="tags-line">{item.review.tags.join(' · ')}</div>}
    </div>
  )
}

export function SliderField({ label, value, min, max, step, display, onChange, disabled }: {
  label: string; value: number; min: number; max: number; step: number; display?: string; onChange: (v: number) => void; disabled?: boolean
}) {
  const id = useId()
  return (
    <div className="slider-field">
      <div className="row between">
        <label htmlFor={id} className="small">{label}</label>
        <span className="mono small">{display ?? value}</span>
      </div>
      <input id={id} type="range" min={min} max={max} step={step} value={value} disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))} />
    </div>
  )
}

// ───────── 表单

export function Select<T extends string | number>({ value, onChange, options, label, disabled, className, groups }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: string; title?: string; disabled?: boolean }[]; label?: string; disabled?: boolean; className?: string
  groups?: { label: string; options: { value: T; label: string; title?: string }[] }[]
}) {
  const all = [...options, ...(groups ?? []).flatMap((g) => g.options)]
  const sel = (
    <select value={String(value)} disabled={disabled} aria-label={label} className={className}
      onChange={(e) => {
        const o = all.find((x) => String(x.value) === e.target.value)
        if (o) onChange(o.value)
      }}>
      {options.map((o) => <option key={String(o.value)} value={String(o.value)} title={o.title} disabled={o.disabled}>{o.label}</option>)}
      {groups?.map((g) => (
        <optgroup key={g.label} label={g.label}>
          {g.options.map((o) => <option key={String(o.value)} value={String(o.value)} title={o.title}>{o.label}</option>)}
        </optgroup>
      ))}
    </select>
  )
  return label ? <label className="field"><span>{label}</span>{sel}</label> : sel
}

export function NumberInput({ value, onChange, placeholder, label, ariaLabel, step, className, min }: {
  value: number | null | undefined; onChange: (v: number | null) => void; placeholder?: string; label?: string; ariaLabel?: string; step?: string; className?: string; min?: number
}) {
  const input = (
    <input type="number" inputMode="decimal" step={step ?? 'any'} min={min} className={className} placeholder={placeholder}
      aria-label={ariaLabel ?? label ?? placeholder} value={value ?? ''}
      onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} />
  )
  return label ? <label className="field"><span>{label}</span>{input}</label> : input
}

export function SortControl({ value, onChange, options }: {
  value: SortPref; onChange: (v: SortPref) => void; options: { value: string; label: string }[]
}) {
  return (
    <div className="row gap sort-control">
      <Select label="排序" value={value.sort} options={options} onChange={(sort) => onChange({ ...value, sort })} />
      <button className="order-btn" aria-label={value.order === 'desc' ? '降序' : '升序'} title="切换升序 / 降序"
        onClick={() => onChange({ ...value, order: value.order === 'desc' ? 'asc' : 'desc' })}>
        {value.order === 'desc' ? '↓ 降序' : '↑ 升序'}
      </button>
    </div>
  )
}

/** 引擎下拉框：选项来自 /models 的 engines[]，选项文字带「零样本」「可训练」标记；传入 supportedEngines 时将当前算力可用引擎排在前，其余标注「当前算力未安装」。 */
export function EngineSelect({ value, onChange, label, filter, allowAll, className, disabled, supportedEngines }: {
  value: string; onChange: (v: string) => void; label?: string; filter?: (e: EngineInfo) => boolean
  allowAll?: boolean; className?: string; disabled?: boolean; supportedEngines?: ReadonlySet<string>
}) {
  const s = useStore()
  const list = engineList(s, filter)
  const hasFilter = !!supportedEngines && supportedEngines.size > 0
  const ordered = hasFilter
    ? [...list.filter((id) => supportedEngines.has(id)), ...list.filter((id) => !supportedEngines.has(id))]
    : list
  return (
    <Select label={label} value={value} className={className} disabled={disabled}
      options={[
        ...(allowAll ? [{ value: '', label: '全部' }] : []),
        ...ordered.map((id) => ({
          value: id,
          label: `${engineLabel(s, id)}${hasFilter && !supportedEngines.has(id) ? ' · 当前算力未安装' : ''}`,
        })),
      ]}
      onChange={onChange} />
  )
}

/** 单一「模型版本」选择器：按评测集分组，推荐的在选项里标出；空列表时给出「查看算力」。 */
export function ModelVersionPicker({ versions, onSelect, manual, disabled }: {
  versions: VersionList; onSelect: (id: string | null) => void; manual?: boolean; disabled?: boolean
}) {
  const selected = versions.rows.find((v) => v.id === versions.selectedID)
  const group = (set: string) => versions.rows.filter((v) => (v.score_set ?? 'none') === set)
  const score = selected && versionScore(selected)
  return (
    <div className="stack tight version-picker">
      <Select label="模型版本" value={versions.selectedID ?? ''} disabled={disabled}
        options={[{ value: '', label: manual ? '更多设置中的自定组合' : '选择模型版本' }]}
        groups={['hard', 'mini', 'none'].filter((set) => group(set).length).map((set) => ({
          label: set === 'none' ? '尚未评测' : displayEvalSet(set),
          options: group(set).map((v) => ({ value: v.id, label: versionOption(v), title: scoreHelp(v.score) })),
        }))}
        onChange={(v) => onSelect(v || null)} />
      {score && <div className="muted small field-note" title={scoreHelp(selected.score)}>{score}</div>}
      {versions.loading ? <div className="muted small field-note">正在读取模型版本…</div>
        : versions.character && !versions.rows.length ? (
          <div className="muted small field-note">这个角色还没有能用的模型版本。零样本不需要训练，但也需要底模。
            <button className="link" onClick={() => navigate('settings', 'compute')}>查看算力</button></div>
        ) : selected && isZeroShot(selected) ? <div className="muted small field-note">底模（零样本）：使用参考音频的声音生成。</div> : null}
      {versions.message && <div className="warn small field-note" role="alert">{versions.message}</div>}
    </div>
  )
}

// ───────── 速度

export function SpeedIcon({ engine, speed }: { engine: string; speed: EngineSpeed | undefined | null }) {
  const grade = speedGrade(speed)
  const tip = speedTip(engine, speed)
  return <i className={`sic ${grade ?? 'none'}`} title={tip} role="img" aria-label={tip} />
}

export function SpeedLegend() {
  return (
    <span className="sleg" title="悬停查看每句大约多久和数据来源">
      <i className="sic fast" aria-hidden />快<i className="sic mid" aria-hidden />中<i className="sic slow" aria-hidden />慢<i className="sic none" aria-hidden />未测
    </span>
  )
}

function ownModelEngines(speeds: Record<string, EngineSpeed>, engines?: string[], fallback: string[] = []) {
  const fromSpeeds = Object.keys(speeds).filter((k) => k !== 'train' && k !== 'scorer')
  const own = engines?.length
    ? [...new Set([...engines, ...fromSpeeds.filter((k) => (speeds[k]?.sec_per_item ?? 0) > 0 && speeds[k]?.source !== 'untested')])].filter((k) => k !== 'train' && k !== 'scorer')
    : fromSpeeds
  return sortEngines(own.length ? own : fallback.filter((k) => k !== 'train' && k !== 'scorer'))
}

/** 常用模型的速度图标（列表收起时）。 */
export function CommonSpeedIcons({ speeds, engines }: { speeds: Record<string, EngineSpeed>; engines?: string[] }) {
  const all = useStore((s) => s.computeEngines)
  const common = useStore((s) => s.commonModels)
  const shown = ownModelEngines(speeds, engines, all).filter((e) => common.includes(e))
  return (
    <span className="speed">
      {shown.map((e) => <span key={e} className="spd">{displayEngine(e)} <SpeedIcon engine={e} speed={speeds[e]} /></span>)}
    </span>
  )
}

function speedSourceBadge(source: string | undefined) {
  return <span className={`badge ${source === 'measured' ? 'ok' : source === 'reference' ? 'ref' : ''}`}>{sourceLabel(source)}</span>
}

/** 「设备 × 模型」小表：每个模型一行显式展示耗时 + 来源，下方附训练与评测打分速度；没实测的可以「测这一个」。 */
export function ModelSpeedTable({ speeds, engines, device, note }: { speeds: Record<string, EngineSpeed>; engines?: string[]; device?: string; note?: string }) {
  const s = useStore()
  const benching = s.jobs.some((j) => j.kind === 'bench' && j.target === device && isActive(j))
  const modelEngines = ownModelEngines(speeds, engines, s.computeEngines)
  const trainSpeed = speeds.train
  const trainText = displayTrainSpeed(trainSpeed)
  const scorerSpeed = speeds.scorer
  const scorerText = displayScorerSpeed(scorerSpeed)
  return (
    <div className="mtab">
      <table className="speed-table">
        <thead><tr><th>模型</th><th>速度</th><th>来源</th><th /></tr></thead>
        <tbody>
          {modelEngines.map((e) => {
            const speed = speeds[e]
            const itemText = displayItemSpeed(speed)
            return (
              <tr key={e}>
                <td className="nowrap">{displayEngine(e)} {s.commonModels.includes(e) && <span className="badge accent">常用</span>}</td>
                <td className="speed-val">
                  <SpeedIcon engine={e} speed={speed} />{' '}
                  {itemText ? <span className="mono">{itemText}</span> : <span className="muted">未测</span>}
                </td>
                <td>{speedSourceBadge(itemText ? speed?.source : 'untested')}</td>
                <td className="num">{device && (!itemText || speed?.source !== 'measured') && (
                  <button className="ghost" disabled={benching} onClick={() => void perform(() => benchmark(device, e))}>{benching ? '测速中…' : '测这一个'}</button>
                )}</td>
              </tr>
            )
          })}
          <tr className="speed-extra-row speed-extra-sep">
            <td className="nowrap muted">训练</td>
            <td className="speed-val">
              {trainText ? <span>{trainText}</span> : <span className="muted">未测</span>}
            </td>
            <td>{speedSourceBadge(trainText ? trainSpeed?.source : 'untested')}</td>
            <td />
          </tr>
          {scorerText && (
            <tr className="speed-extra-row">
              <td className="nowrap muted">评测打分</td>
              <td className="speed-val"><span>{scorerText}</span></td>
              <td>{speedSourceBadge(scorerSpeed?.source)}</td>
              <td />
            </tr>
          )}
        </tbody>
      </table>
      {note && <div className="muted small">{note}</div>}
    </div>
  )
}

/** 可键盘操作的单选行（确认窗的设备表、添加算力的选项）。 */
export function RadioRow({ checked, disabled, onSelect, children, className }: { checked: boolean; disabled?: boolean; onSelect: () => void; children: ReactNode; className?: string }) {
  return (
    <div role="radio" aria-checked={checked} aria-disabled={disabled} data-roving tabIndex={checked ? 0 : -1}
      className={`${className ?? ''} ${checked ? 'on' : ''} ${disabled ? 'dis' : ''}`}
      onClick={() => { if (!disabled) onSelect() }} onKeyDown={(e) => { if (!disabled) activateKeyDown(e, onSelect) }}>
      <span className={`radio ${checked ? 'on' : ''}`} aria-hidden />
      {children}
    </div>
  )
}
