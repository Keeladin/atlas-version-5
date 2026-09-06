import { useEffect, useState } from 'react'
import './App.css'
import { getHealth, type Health } from './api'

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={`status-dot ${ok ? 'ok' : 'bad'}`} aria-hidden="true" />
}

function AtlasPage({ health }: { health: Health | null }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <div className="eyebrow">ATLAS V5</div>
          <h1>Atlas</h1>
        </div>
        <div className="topbar-actions">
          <span className="runtime-pill">
            <StatusDot ok={Boolean(health?.database.ok)} />
            {health ? `runtime ${health.status}` : 'runtime checking'}
          </span>
          <a className="control-link" href="/control">Control</a>
        </div>
      </header>

      <main className="atlas-grid">
        <aside className="workspace-panel panel">
          <div className="panel-title">Workspace</div>
          <p className="muted">No active workspace yet.</p>
          <div className="empty-card">Files, mail, research and artifacts will surface here when relevant.</div>
        </aside>

        <section className="chat-panel panel">
          <div className="chat-scroll">
            <div className="system-card">
              <span className="phase-label">PHASE 0</span>
              <h2>The seat is being built.</h2>
              <p>
                Transcript, artifacts, registry and runtime truth are present. The OpenAI inference adapter arrives in Phase 1.
              </p>
            </div>
          </div>
          <form className="composer" onSubmit={(event) => event.preventDefault()}>
            <button className="attach-button" type="button" aria-label="Attach artifact">+</button>
            <textarea placeholder="Talk to Atlas…" rows={2} disabled />
            <button className="send-button" type="submit" disabled>Send</button>
          </form>
        </section>

        <aside className="attention-panel panel">
          <div className="panel-title">Needs You</div>
          <p className="muted">Nothing needs your attention.</p>
          <div className="runtime-card">
            <span><StatusDot ok={Boolean(health)} /> API</span>
            <strong>{health ? health.version : 'checking'}</strong>
          </div>
          <div className="runtime-card">
            <span><StatusDot ok={Boolean(health?.database.ok)} /> PostgreSQL</span>
            <strong>{health?.database.ok ? 'healthy' : 'not ready'}</strong>
          </div>
        </aside>
      </main>
    </div>
  )
}

function ControlPage({ health }: { health: Health | null }) {
  return (
    <div className="control-shell">
      <header className="topbar">
        <div>
          <div className="eyebrow">ATLAS V5</div>
          <h1>Control</h1>
        </div>
        <a className="control-link" href="/">Back to Atlas</a>
      </header>
      <main className="control-grid">
        <section className="panel control-card">
          <div className="panel-title">Runtime</div>
          <dl>
            <div><dt>Version</dt><dd>{health?.version ?? 'checking'}</dd></div>
            <div><dt>Environment</dt><dd>{health?.environment ?? 'checking'}</dd></div>
            <div><dt>Status</dt><dd>{health?.status ?? 'checking'}</dd></div>
          </dl>
        </section>
        <section className="panel control-card">
          <div className="panel-title">PostgreSQL</div>
          <p className={health?.database.ok ? 'healthy-text' : 'warning-text'}>
            {health?.database.ok ? 'Connected and healthy.' : 'Not connected yet.'}
          </p>
        </section>
        <section className="panel control-card">
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
