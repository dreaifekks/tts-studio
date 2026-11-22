// 设置：通用 / 资料库 / 算力 / 云端 GPU 四个标签页（design.md 第 2 节）。Web 永远是连到主控的网页，没有「作为主控」。
import { useEffect, useState, type ReactNode } from 'react'
import type { AudioFormat, StorageCategory, StorageOverview } from '../api/types'
import { rovingKeyDown } from '../a11y'
import { Segmented } from '../components'
import { LOW_DISK_THRESHOLD_BYTES, displayBytes, displayEngine } from '../display'
import {
  DEFAULT_TAG_KEYS, DEFAULT_TAG_NAMES, allKnownEngines, claimLibrary, installedComputeEngines, navigate, perform,
  refreshCompute, refreshStorage, rescanLibrary, selectLibrary, setCommonModels, setDefaultCandidates,
  setDefaultExportFormat, setTagShortcuts, settingsTabs, useStore, type SettingsTab,
} from '../store'
import { checkVersions, hasNewerVersion, latestVersion, useVersions, webVersion } from '../versions'
import { CloudSettings, ComputeSettings } from './SettingsCompute'

export function SettingsPage() {
  const tab = useStore((s) => s.settingsTab)
  const error = useStore((s) => s.computeError)
  useEffect(() => { void refreshCompute(true) }, [])
  const title = settingsTabs.find((t) => t.key === tab)!.title
  return (
    <div className="page settings">
      <div className="tabs" role="tablist" aria-label="设置" onKeyDown={(e) => rovingKeyDown(e, (el) => navigate('settings', el.dataset.tab as SettingsTab), 'horizontal')}>
        {settingsTabs.map((t) => (
          <button key={t.key} role="tab" id={`tab-${t.key}`} aria-selected={tab === t.key} aria-controls={`pane-${t.key}`} data-roving data-tab={t.key}
            tabIndex={tab === t.key ? 0 : -1} className={`tab ${tab === t.key ? 'on' : ''}`} onClick={() => navigate('settings', t.key)}>
            <span className="ti" aria-hidden>{t.icon}</span>{t.title}
          </button>
        ))}
      </div>
      <div className="pane" role="tabpanel" id={`pane-${tab}`} aria-labelledby={`tab-${tab}`} aria-label={title}>
        {tab === 'general' && <GeneralSettings />}
        {tab === 'library' && <LibrarySettings />}
        {tab === 'compute' && <ComputeSettings />}
        {tab === 'cloud' && <CloudSettings />}
        {tab !== 'general' && error && <div className="warn small selectable">{error}</div>}
      </div>
    </div>
  )
}

export function Section({ title, children, info }: { title: string; children: ReactNode; info?: ReactNode }) {
  return (
    <section className="sec">
      <h5>{title}{info}</h5>
      <div className="group">{children}</div>
    </section>
  )
}
export function Item({ k, children, action, className }: { k?: string; children: ReactNode; action?: ReactNode; className?: string }) {
  return <div className={`gi ${className ?? ''}`}>{k !== undefined && <span className="k">{k}</span>}<span className="v">{children}</span>{action}</div>
}

function CopyButton({ text, label = '复制链接' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'ok' | 'bad'>('idle')
  const copy = async () => {
    let ok = false
    try { await navigator.clipboard.writeText(text); ok = true } catch {
      // 非安全上下文（纯 HTTP 打开）里没有 clipboard API，退回 execCommand
      const ta = document.createElement('textarea')
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0'
      document.body.appendChild(ta); ta.select()
      try { ok = document.execCommand('copy') } catch { ok = false }
      ta.remove()
    }
    setState(ok ? 'ok' : 'bad')
    setTimeout(() => setState('idle'), 1500)
  }
  return <button className={`copy ${state === 'ok' ? 'okk' : state === 'bad' ? 'bad' : ''}`} onClick={() => void copy()} aria-live="polite">
    {state === 'ok' ? '已复制' : state === 'bad' ? '复制失败' : label}
  </button>
}

function GeneralSettings() {
  const s = useStore()
  const v = useVersions()
  const latest = latestVersion(v)
  const newer = !!latest && hasNewerVersion(latest, webVersion)
  const info = s.systemInfo
  return (
    <>
      <Section title="这台 Studio">
        <Item k="主控"><span className="dot ok" aria-hidden /> {info?.machine ?? '主控'} · {s.connected ? '已连接' : '未连接'}</Item>
        <Item><span className="small muted">这个网页由主控提供，任务、资料库和设备都和主控上的 Studio 一样，所有任务都由主控统一安排。</span></Item>
        {info?.web_available && (
          <Item k="在浏览器里用" action={<CopyButton text={info.web_url} />}><span className="small muted">手机、平板或其他电脑都可以用浏览器打开</span></Item>
        )}
      </Section>
      <Section title="合成与打标偏好">
        <Item k="默认候选数">
          <div className="row gap stepper" role="group" aria-label="默认候选数">
            <button aria-label="减少默认候选数" disabled={s.defaultCandidates <= 1} onClick={() => setDefaultCandidates(s.defaultCandidates - 1)}>−</button>
            <span>{s.defaultCandidates} 个</span>
            <button aria-label="增加默认候选数" disabled={s.defaultCandidates >= 16} onClick={() => setDefaultCandidates(s.defaultCandidates + 1)}>+</button>
          </div>
        </Item>
        <Item k="默认导出格式">
          <Segmented label="默认导出格式" value={s.defaultExportFormat}
            options={(['wav', 'flac', 'mp3'] as AudioFormat[]).map((f) => ({ value: f, label: f.toUpperCase() }))}
            onChange={setDefaultExportFormat} />
        </Item>
        <Item k="情绪速标键位" className="gi-stack" action={
          <button className="ghost small-btn" disabled={s.tagKeys === DEFAULT_TAG_KEYS && s.tagNames === DEFAULT_TAG_NAMES}
            onClick={() => setTagShortcuts(DEFAULT_TAG_KEYS, DEFAULT_TAG_NAMES)}>恢复默认</button>
        }>
          <div className="stack tight">
            <div className="tag-shortcut-fields">
              <label className="field tag-shortcut-field"><span>按键</span>
                <input aria-label="速标按键（逗号分隔）" value={s.tagKeys} onChange={(e) => setTagShortcuts(e.target.value, s.tagNames)} />
              </label>
              <label className="field tag-shortcut-field"><span>情绪标签</span>
                <input aria-label="速标对应标签（逗号分隔）" value={s.tagNames} onChange={(e) => setTagShortcuts(s.tagKeys, e.target.value)} />
              </label>
            </div>
            <span className="small muted">逗号分隔，与「角色 → 参考音频」键盘速标流实时同步。</span>
          </div>
        </Item>
      </Section>
      <Section title="常用模型">
        {(() => {
          const allEngines = allKnownEngines(s)
          const installed = installedComputeEngines(s)
          const hasDeviceInfo = s.devices.length > 0 || s.computeEngines.length > 0
          const installedList = hasDeviceInfo ? allEngines.filter((e) => installed.has(e)) : allEngines
          const missingList = hasDeviceInfo ? allEngines.filter((e) => !installed.has(e)) : []
          const toggleEngine = (e: string, on: boolean) =>
            setCommonModels(on ? s.commonModels.filter((x) => x !== e) : [...s.commonModels, e])
          return (
            <>
              <Item k="列表里显示" className="gi-stack">
                <div className="stack tight">
                  <span className="chips" role="group" aria-label="常用模型">
                    {installedList.map((e) => {
                      const on = s.commonModels.includes(e)
                      return (
                        <label key={e} className={`cpick ${on ? 'on' : ''}`}>
                          <input type="checkbox" checked={on} onChange={() => toggleEngine(e, on)} />
                          {displayEngine(e)}
                        </label>
                      )
                    })}
                  </span>
                  {!!missingList.length && (
                    <div className="row gap wrap muted tiny" role="group" aria-label="当前算力未安装的模型">
                      <span>未安装：</span>
                      <span className="chips">
                        {missingList.map((e) => {
                          const on = s.commonModels.includes(e)
                          return (
                            <label key={e} className={`cpick dim ${on ? 'on' : ''}`}>
                              <input type="checkbox" checked={on} onChange={() => toggleEngine(e, on)} />
                              {displayEngine(e)} <span className="badge">未安装</span>
                            </label>
                          )
                        })}
                      </span>
                    </div>
                  )}
                </div>
              </Item>
              <Item><span className="small muted">决定「算力」和「云端 GPU」收起时显示的速度，以及新建训练、盲听和无默认版本时的优先引擎（多选时优先 v2ProPlus）。</span></Item>
            </>
          )
        })()}
      </Section>
      <Section title="版本">
        <Item k="TTS Studio" action={<button disabled={v.checking} onClick={() => void checkVersions(true)}>{v.checking ? '正在检查…' : '检查更新'}</button>}>
          {webVersion} {latest && (newer ? <span className="badge acc">新版本 {latest}</span> : <span className="badge ok">已是最新</span>)}
          {newer && v.info?.studio.notes_url && <> <a href={v.info.studio.notes_url} target="_blank" rel="noreferrer">更新说明</a></>}
        </Item>
        {newer && <Item><span className="small muted">网页跟着主控更新：在主控的 Studio 里更新后，刷新这个网页。</span></Item>}
        {v.error && <Item><span className="small warn">{v.error}</span></Item>}
      </Section>
      <Section title="音频保留">
        <Item><span className="small">没有加入作品、也没有评价的音频，7 天后自动删除。</span></Item>
      </Section>
    </>
  )
}

function displayPath(path: string | null | undefined) {
  if (!path) return '未选择'
  const drive = path.indexOf('/My Drive/')
  if (drive >= 0) return '我的云端硬盘 › ' + path.slice(drive + '/My Drive/'.length).split('/').join(' › ')
  return path.replace(/^\/(Users|home)\/[^/]+/, '~')
}
function syncTime(value: string) {
  const diff = (Date.now() - new Date(value).getTime()) / 1000
  if (!Number.isFinite(diff)) return value
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`
  return `${Math.round(diff / 86400)} 天前`
}

const STORAGE_COLORS = ['#2a78d6', '#1baf7a', '#eb6834', '#9b6bd6', '#d4a017', '#8e8e93']

function StorageBreakdown({ categories, totalBytes, showLocal }: { categories: StorageCategory[]; totalBytes: number; showLocal?: boolean }) {
  const denom = Math.max(totalBytes, categories.reduce((sum, c) => sum + Math.max(0, c.bytes), 0))
  const colorOf = (c: StorageCategory, i: number) => (c.key === 'other' ? '#8e8e93' : STORAGE_COLORS[i % STORAGE_COLORS.length])
  return (
    <>
      {denom > 0 && categories.length > 0 && (
        <div className="storage-bar" role="img" aria-label="空间分类占比">
          {categories.map((c, i) => (c.bytes > 0 ? (
            <span key={c.key} className="storage-seg"
              style={{ width: `${(c.bytes / denom) * 100}%`, background: colorOf(c, i) }}
              title={`${c.label}：${displayBytes(c.bytes)}`} />
          ) : null))}
        </div>
      )}
      {categories.length > 0 && (
        <ul className="storage-list">
          {categories.map((c, i) => {
            const hasLocalDiff = showLocal && c.local_bytes != null
            return (
              <li key={c.key} className="storage-row">
                <span className="storage-name">
                  <i className="sw" style={{ background: colorOf(c, i) }} aria-hidden />
                  <span>{c.label}</span>
                </span>
                <span className="storage-val">
                  <span className="mono">{displayBytes(c.bytes)}</span>
                  {hasLocalDiff && <span className="muted tiny">（本机 {displayBytes(c.local_bytes)}）</span>}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

function StorageSection({ storage, loading }: { storage: StorageOverview | null; loading: boolean }) {
  if (!storage && !loading) return null
  if (!storage) {
    return (
      <Section title="空间占用">
        <Item><span className="small muted">正在统计空间占用…</span></Item>
      </Section>
    )
  }
  const { disk, library: lib, local } = storage
  const isDrive = lib.kind === 'drive'
  const showLocalDiff = isDrive && lib.total_bytes > 0 && (lib.total_bytes - lib.local_bytes) / lib.total_bytes > 0.05
  const diskLow = disk.free_bytes < LOW_DISK_THRESHOLD_BYTES
  const usedPct = disk.total_bytes > 0 ? Math.min(100, Math.max(0, (disk.used_bytes / disk.total_bytes) * 100)) : 0

  return (
    <Section title="空间占用">
      <Item k="统计时间" action={<button disabled={loading} onClick={() => void refreshStorage(true)}>{loading ? '正在统计…' : '重新统计'}</button>}>
        <span className="muted" title={storage.measured_at}>{loading ? '正在重新统计…' : syncTime(storage.measured_at)}</span>
      </Item>
      {lib.selected && (
        <div className="gi storage-block" data-testid="storage-library">
          <div className="storage-head">
            <span className="strong">资料库占用</span>
            <span className="storage-meta">
              {isDrive ? (
                <>
                  <span>Google Drive</span>
                  <span className="muted">·</span>
                  <span>文件总大小 <b className="mono">{displayBytes(lib.total_bytes)}</b></span>
                  <span className="muted">·</span>
                  <span>本机占用 <b className="mono">{displayBytes(lib.local_bytes)}</b></span>
                </>
              ) : (
                <b className="mono">{displayBytes(lib.total_bytes)}</b>
              )}
            </span>
          </div>
          <StorageBreakdown categories={lib.categories} totalBytes={lib.total_bytes} showLocal={showLocalDiff} />
        </div>
      )}
      <div className="gi storage-block" data-testid="storage-local">
        <div className="storage-head">
          <span className="strong">主控本机存储{local.machine ? `（${local.machine}）` : ''}</span>
          <span className={`storage-meta ${diskLow ? 'warn' : ''}`}>
            <span className="mono">剩余 {displayBytes(disk.free_bytes)} / 共 {displayBytes(disk.total_bytes)}</span>
            {diskLow && <span className="badge warn">空间不足</span>}
          </span>
        </div>
        <div className="storage-bar disk-bar" role="progressbar" aria-label="主控本机磁盘使用率"
          aria-valuenow={Math.round(usedPct)} aria-valuemin={0} aria-valuemax={100}>
          {usedPct > 0 && (
            <span className={`storage-seg ${diskLow ? 'disk-low' : 'disk-used'}`}
              style={{ width: `${usedPct}%` }}
              title={`已用 ${displayBytes(disk.used_bytes)} / 共 ${displayBytes(disk.total_bytes)}`} />
          )}
        </div>
        <div className="storage-subhead">
          <span className="small muted">TTS Studio 本机占用</span>
          <b className="mono small">{displayBytes(local.total_bytes)}</b>
        </div>
        <StorageBreakdown categories={local.categories} totalBytes={local.total_bytes} />
      </div>
    </Section>
  )
}

function LibrarySettings() {
  const s = useStore()
  const library = s.library
  const [path, setPath] = useState('')
  const [changing, setChanging] = useState(false)
  useEffect(() => { void refreshStorage() }, [])
  const choose = (
    <div className="row gap wrap">
      <input className="grow" placeholder="主控上的文件夹，例如 ~/tts-library" aria-label="资料库文件夹" value={path} onChange={(e) => setPath(e.target.value)} />
      <button className="primary" disabled={!path.trim()} onClick={() => void perform(async () => { await selectLibrary(path.trim()); setChanging(false) })}>使用这个文件夹</button>
      {changing && <button onClick={() => setChanging(false)}>取消</button>}
    </div>
  )
  if (!library) return <div className="muted">{s.computeLoading ? '正在读取资料库…' : '资料库信息暂时取不到'}</div>
  return (
    <>
      <Section title="位置">
        {library.selected ? (
          <>
            <Item k="存放在">{library.kind === 'drive' ? 'Google Drive' : '本机文件夹'}</Item>
            <Item k="文件夹" action={!changing && <button onClick={() => setChanging(true)}>更改…</button>}>
              <span className="selectable">{displayPath(library.path)}</span>
            </Item>
            {changing && <Item>{choose}</Item>}
            <Item><span className="small muted">{library.kind === 'drive' ? '资料库放角色、参考音频、模型、训练数据、评测和盲听记录，通过 Google Drive 电脑版同步。' : '资料库放角色、参考音频、模型、训练数据、评测和盲听记录。'}</span></Item>
            {!library.owned && (
              <Item action={<button onClick={() => void perform(claimLibrary)}>改为由 {library.this_machine.name} 管理</button>}>
                <span className="muted">由 {library.owner?.name ?? '别的主控'} 管理</span>
              </Item>
            )}
          </>
        ) : (
          <>
            <Item><b>选一个文件夹，存放资料库</b></Item>
            <Item><span className="small muted">角色、模型和训练数据都放在这里。填主控上的文件夹；Google Drive 电脑版同步的文件夹也可以。</span></Item>
            <Item>{choose}</Item>
            {library.error && <Item><span className="warn small">{library.error}</span></Item>}
          </>
        )}
      </Section>
      {library.selected && (
        <Section title="内容">
          <Item k="状态" action={<button disabled={s.libraryChecking} onClick={() => void perform(rescanLibrary)}>重新检查</button>}>
            <span className="dot ok" aria-hidden /> {s.libraryChecking ? '正在同步' : '已同步'}{library.scanned_at && <span className="muted" title={library.scanned_at}> · {syncTime(library.scanned_at)}</span>}
          </Item>
          {library.counts && (
            <Item k="包含">{library.counts.characters} 个角色 · {library.counts.weights} 个模型版本 · {library.counts.refs} 条参考音频 · {library.counts.datasets} 个训练数据集 · {library.counts.evals} 次评测 · {library.counts.blind} 场盲听</Item>
          )}
          {!!library.conflicts && (
            <Item k="需要处理"><span className="badge warn">{library.conflicts} 个文件有同步冲突</span>
              <details className="files"><summary>查看</summary><ul>{library.conflict_files.map((f) => <li key={f} className="selectable">{f}</li>)}</ul></details>
            </Item>
          )}
          {!!library.broken_files.length && <Item k="需要处理"><span className="warn">{library.broken_files.length} 个文件暂时无法读取</span></Item>}
        </Section>
      )}
      <StorageSection storage={s.storage} loading={s.storageLoading} />
    </>
  )
}
