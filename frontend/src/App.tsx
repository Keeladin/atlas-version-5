import { useEffect, useState, type ReactNode } from 'react'
import './App.css'
import { getHealth, type Health } from './api'

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={`status-dot ${ok ? 'ok' : 'bad'}`} aria-hidden="true" />
}

function RailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rail-section">
      <div className="rail-heading">{title}</div>
      <div className="rail-items">{children}</div>
    </section>
  )
}

function RailItem({ label, detail, active = false, nested = false }: {
  label: string
  detail?: string
  active?: boolean
  nested?: boolean
}) {
  return (
    <div className={`rail-item${active ? ' active' : ''}${nested ? ' nested' : ''}`}>
      <span className="rail-glyph" aria-hidden="true">{nested ? '↳' : '›'}</span>
      <span className="rail-label">{label}</span>
      {detail ? <span className="rail-detail">{detail}</span> : null}
    </div>
  )
}
function AtlasPage({ health }: { health: Health | null }) {
  const runtimeOk = Boolean(health)
  const databaseOk = Boolean(health?.database.ok)

  return (
    <div className="atlas-shell">
      <header className="persistent-bar">
        <div className="brand-cluster">
          <img className="brand-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" />
          <strong>Atlas</strong>
          <span className="version-tag">V5</span>
        </div>
        <div className="persistent-context">
          <span className="context-item active">Home</span>
          <span className="context-item">Phase 0</span>
        </div>
        <div className="persistent-status">
          <span className="status-label"><StatusDot ok={runtimeOk} />Runtime</span>
          <span className="status-label"><StatusDot ok={databaseOk} />PostgreSQL</span>
          <a className="topbar-link" href="/control">Control</a>
        </div>
      </header>

      <div className="atlas-body">
        <aside className="left-rail">
          <div className="rail-scroll">
            <RailSection title="Find">
              <RailItem label="Projects" active />
              <RailItem label="Local storage" />
              <RailItem label="Drive storage" />
              <RailItem label="Project folders" />
              <RailItem label="Repositories" />
              <RailItem label="Artifacts" />
            </RailSection>
            <RailSection title="Utilities">
              <RailItem label="Normalization" />
              <RailItem label="Input folder" detail="not set" nested />
              <RailItem label="Output folder" detail="not set" nested />
            </RailSection>
          </div>
          <div className="rail-footer">
            <span className="rail-footer-label">Environment</span>
            <span><StatusDot ok={runtimeOk} />{health ? health.environment : 'checking'}</span>
          </div>
        </aside>

        <main className="main-stage">
          <section className="chat-canvas" aria-label="Atlas chat canvas">
            <div className="canvas-head">
              <div>
                <div className="canvas-kicker">Home</div>
                <h1>Atlas</h1>
              </div>
              <span className="canvas-state">{health ? `v${health.version}` : 'checking'}</span>
            </div>

            <div className="chat-space">
              <div className="phase-message">
                <span className="phase-label">PHASE 0</span>
                <h2>The seat is ready for the agent.</h2>
                <p>Runtime, transcript, artifacts and registry foundations are live. Conversation arrives with Phase 1.</p>
              </div>
            </div>

            <form className="composer" onSubmit={(event) => event.preventDefault()}>
              <button className="attach-button" type="button" aria-label="Attach artifact" disabled>+</button>
              <textarea placeholder="Talk to Atlas…" rows={1} disabled />
              <button className="send-button" type="submit" disabled>Send</button>
            </form>
          </section>
          <aside className="activity-rail" aria-label="Atlas activity">
            <section className="activity-section attention-section">
              <div className="activity-heading-row">
                <span className="activity-heading">Needs You</span>
                <span className="activity-count">0</span>
              </div>
              <p className="activity-empty">Nothing needs your attention.</p>
            </section>

            <div className="activity-divider" />

            <section className="activity-section">
              <div className="activity-heading-row">
                <span className="activity-heading">Latest</span>
                <span className="activity-caption">recent activity</span>
              </div>
              <div className="latest-empty">
                <span className="latest-time">—</span>
                <div>
                  <strong>No recent activity yet</strong>
                  <p>Mail, calendar, repository events and completed work can surface here when connected.</p>
                </div>
              </div>
            </section>
          </aside>
        </main>
      </div>
    </div>
  )
}

function ControlPage({ health }: { health: Health | null }) {
  return (
    <div className="control-shell">
      <header className="control-topbar">
        <div className="control-title-cluster">
          <img className="control-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" />
          <div>
            <div className="eyebrow">ATLAS V5</div>
            <h1>Control</h1>
          </div>
        </div>
        <a className="control-link" href="/">Back to Atlas</a>
      </header>
      <main className="control-grid">
        <section className="control-card">
          <div className="panel-title">Runtime</div>
          <dl>
            <div><dt>Version</dt><dd>{health?.version ?? 'checking'}</dd></div>
            <div><dt>Environment</dt><dd>{health?.environment ?? 'checking'}</dd></div>
            <div><dt>Status</dt><dd>{health?.status ?? 'checking'}</dd></div>
          </dl>
        </section>
        <section className="control-card">
          <div className="panel-title">PostgreSQL</div>
          <p className={health?.database.ok ? 'healthy-text' : 'warning-text'}>
            {health?.database.ok ? 'Connected and healthy.' : 'Not connected yet.'}
          </p>
        </section>
        <section className="control-card">
          <div className="panel-title">Environment Registry</div>
          <p>{health ? `${health.registry_entries} Phase 0 entry` : 'Checking…'}</p>
        </section>
      </main>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null))
  }, [])

  if (window.location.pathname.startsWith('/control')) {
    return <ControlPage health={health} />
  }

  return <AtlasPage health={health} />
}
