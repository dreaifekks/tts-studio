import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import { isActive, jobKindLabel, jobStatusLabel } from './api/client'
import { player } from './audio'
import { ComputeConfirmation } from './compute'
import { LOW_DISK_THRESHOLD_BYTES, displayBytes } from './display'
import { computeSummary, navigate, pages, refreshConnection, startGeneration, useStore } from './store'
import { GeneratePage } from './pages/Generate'
import { WorksPage } from './pages/Works'
import { CharactersPage } from './pages/Characters'
import { TrainingPage } from './pages/Training'
import { updateAvailable, useVersions } from './versions'

const SettingsPage = lazy(() => import('./pages/Settings').then((m) => ({ default: m.SettingsPage })))

const views = {
  generate: GeneratePage,
  works: WorksPage,
  characters: CharactersPage,
  training: TrainingPage,
  settings: SettingsPage,
}

export default function App() {
  const page = useStore((s) => s.page)
  const connected = useStore((s) => s.connected)
  const hasUpdate = useVersions(updateAvailable)
  const errorMessage = useStore((s) => s.errorMessage)
  const View = views[page]
  const navPages = pages.filter((p) => p.key !== 'settings')

  useEffect(() => {
    void refreshConnection()
    const t = setInterval(() => void refreshConnection(), 15_000)
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (mod && !e.altKey && e.key === 'Enter') {
        e.preventDefault()
        if (useStore.getState().page !== 'generate') navigate('generate')
        startGeneration()
      } else if (mod && e.altKey && /^Digit[1-4]$/.test(e.code)) {
        const target = navPages[Number(e.code.slice(5)) - 1]
        if (target) {
          e.preventDefault()
          navigate(target.key)
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => { clearInterval(t); window.removeEventListener('keydown', onKey) }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { document.title = `${pages.find((p) => p.key === page)?.title} · TTS Studio` }, [page])
  const navRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const nav = navRef.current
    const active = nav?.querySelector<HTMLElement>('a.active')
    if (!nav || !active || nav.scrollWidth <= nav.clientWidth) return
    const n = nav.getBoundingClientRect(), a = active.getBoundingClientRect()
    nav.scrollBy({ left: a.left - n.left - (n.width - a.width) / 2 })
  }, [page])

  const link = (p: (typeof pages)[number], i?: number) => (
    <a key={p.key} href={import.meta.env.BASE_URL + (p.key === 'generate' ? '' : p.key)} className={page === p.key ? 'active' : ''}
      aria-current={page === p.key ? 'page' : undefined} title={i !== undefined ? `${p.title}（⌘⌥${i + 1}）` : p.title}
      onClick={(e) => { e.preventDefault(); navigate(p.key) }}>
      <span className="nav-icon" aria-hidden>{p.icon}</span>{p.title}
      {p.key === 'settings' && hasUpdate && <span className="update-dot" role="img" aria-label="有新版本" />}
    </a>
  )

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand"><img src={`${import.meta.env.BASE_URL}favicon.svg`} alt="" /> TTS Studio</div>
        <nav aria-label="页面" ref={navRef}>
          {navPages.map((p, i) => link(p, i))}
          <span className="nav-foot">{link(pages.find((p) => p.key === 'settings')!)}</span>
        </nav>
      </aside>
      <main className="main">
        {errorMessage && (
          <div className="error-bar" role="alert">
            <span aria-hidden>⚠</span><span className="grow selectable">{errorMessage}</span>
            <button className="icon" aria-label="收起错误" onClick={() => useStore.setState({ errorMessage: null })}>✕</button>
          </div>
        )}
        <div className="content">
          {connected === false ? <Offline /> : <Suspense fallback={null}><View /></Suspense>}
        </div>
        <StatusBar />
      </main>
      <ComputeConfirmation />
    </div>
  )
}

function Offline() {
  return (
    <div className="empty offline">
      <div className="empty-title">主控不在线</div>
      <div className="muted">连接主控后继续，任务和资料库都在主控上。</div>
      <button className="primary" onClick={() => void refreshConnection()}>重新连接</button>
    </div>
  )
}

function StatusBar() {
  const s = useStore()
  const running = s.jobs.filter(isActive)
  const groups = [...new Set(running.map((j) => j.compute?.name ?? '等待分配'))].sort()
  const summary = computeSummary(s)
  const localDev = s.devices.find((d) => d.type === 'local')
  const diskFree = localDev?.disk_free_bytes ?? s.storage?.disk.free_bytes ?? null
  const lowDisk = s.connected === true && diskFree !== null && diskFree < LOW_DISK_THRESHOLD_BYTES
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  return (
    <footer className="statusbar">
      <span className={`dot ${s.connected ? 'ok' : s.connected === false ? 'bad' : 'off'}`} aria-hidden />
      <span>{s.connected ? '已连接' : s.connected === false ? '未连接' : '正在连接'}</span>
      <span className="gw-label" title={summary}>{summary}</span>
      {lowDisk && (
        <button className="disk-warn" title={`主控本机磁盘剩余 ${displayBytes(diskFree)}，点击查看空间占用`}
          onClick={() => navigate('settings', 'library')}>
          磁盘剩余 {displayBytes(diskFree)}
        </button>
      )}
      {s.trainingJob && <span className="warn" title={s.trainingJob}>训练中</span>}
      <span className="sep" />
      <span className="status-text" title={s.status} aria-live="polite">{s.status}</span>
      <span className="spacer" />
      {s.playing && <button className="small-btn" onClick={() => player.pause()}>❚❚ 暂停</button>}
      <div className="menu up jobs-menu" ref={root}>
        <button className="small-btn ghost" aria-expanded={open} aria-haspopup="true" onClick={() => setOpen(!open)}
          onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false) }}>{running.length} 个任务运行中 ▴</button>
        {open && (
          <div className="menu-list" role="region" aria-label="运行中的任务" onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false) }}>
            {!running.length && <div className="job-line muted">无运行中任务</div>}
            {groups.map((name) => (
              <section key={name}>
                <div className="job-line strong">{name}</div>
                {running.filter((j) => (j.compute?.name ?? '等待分配') === name).map((j) => (
                  <div key={j.id} className="job-line">
                    <span>{jobKindLabel[j.kind] ?? j.kind} · {j.stage || jobStatusLabel[j.status]} · {Math.round(j.progress * 100)}%</span>
                  </div>
                ))}
              </section>
            ))}
          </div>
        )}
      </div>
      <button className="small-btn icon" aria-label="刷新连接" title="刷新连接" onClick={() => void refreshConnection()}>↻</button>
    </footer>
  )
}
