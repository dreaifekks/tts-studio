import { Suspense, lazy } from 'react'
import { rovingKeyDown } from '../a11y'
import { characterName, characterTabs, navigate, setActiveCharacter, useStore, type CharacterTab } from '../store'
import { ModelsPage } from './Models'
import { ReferencesPage } from './References'

const BlindPage = lazy(() => import('./Blind').then((m) => ({ default: m.BlindPage })))
const StatsPage = lazy(() => import('./Stats').then((m) => ({ default: m.StatsPage })))

export function CharactersPage() {
  const s = useStore()
  const active = s.params.character
  const tab = s.characterTab

  return (
    <div className="page chars-page">
      <div className="split chars-split">
        <aside className="col char-list-col" aria-label="角色列表">
          <div className="row between">
            <h3>角色</h3>
            <span className="muted small">{s.models.length} 个</span>
          </div>
          <div className="char-list" role="listbox" aria-label="切换角色">
            {s.models.map((m) => {
              const on = m.slug === active
              return (
                <button key={m.slug} role="option" aria-selected={on}
                  className={`list-item char-item ${on ? 'selected' : ''}`}
                  onClick={() => setActiveCharacter(m.slug)}>
                  <div className="row between gap">
                    <span className="strong">{characterName(m.slug)}</span>
                    <span className="muted tiny">{m.source === 'builtin' ? '内置' : '自训'}</span>
                  </div>
                  <div className="muted tiny mono">{m.slug}</div>
                </button>
              )
            })}
            {!s.models.length && <div className="muted small">资料库暂无角色</div>}
          </div>
        </aside>

        <div className="col chars-main-col">
          <div className="subtabs" role="tablist" aria-label="角色子页"
            onKeyDown={(e) => rovingKeyDown(e, (el) => navigate('characters', el.dataset.tab as CharacterTab), 'horizontal')}>
            {characterTabs.map((t) => (
              <button key={t.key} role="tab" id={`ctab-${t.key}`} aria-selected={tab === t.key} aria-controls={`cpane-${t.key}`}
                data-roving data-tab={t.key} tabIndex={tab === t.key ? 0 : -1}
                className={`subtab ${tab === t.key ? 'on' : ''}`}
                onClick={() => navigate('characters', t.key)}>
                {t.title}
              </button>
            ))}
          </div>

          <div role="tabpanel" id={`cpane-${tab}`} aria-labelledby={`ctab-${tab}`} className="stack">
            {tab === 'versions' && <ModelsPage character={active} embedded />}
            {tab === 'refs' && <ReferencesPage character={active} embedded />}
            {tab === 'blind' && <Suspense fallback={null}><BlindPage character={active} embedded /></Suspense>}
            {tab === 'stats' && <Suspense fallback={null}><StatsPage character={active} embedded /></Suspense>}
          </div>
        </div>
      </div>
    </div>
  )
}
