import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import './App.css'
import { decideAction, getControlConfiguration, getConversation, getConversationContext, getDriveStorage, getHealth, getLocalStorage, getPendingActions, getRecentActions, streamMessage, uploadLocalFile, type ControlConfiguration, type ConversationContext, type DriveStorageListing, type Health, type LocalStorageListing, type PendingAction, type RecentAction, type Turn } from './api'

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={`status-dot ${ok ? 'ok' : 'bad'}`} aria-hidden="true" />
}

function ContextDot({ state }: { state: ConversationContext['state'] | null }) {
  return <span className={`status-dot ${state === 'amber' ? 'warn' : state === 'red' ? 'bad' : 'ok'}`} aria-hidden="true" />
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 === 0 ? 0 : 1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

function RailSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="rail-section"><div className="rail-heading">{title}</div><div className="rail-items">{children}</div></section>
}

function RailItem({ label, detail, active = false, nested = false, onClick }: { label: string; detail?: string; active?: boolean; nested?: boolean; onClick?: () => void }) {
  return (
    <button className={`rail-item${active ? ' active' : ''}${nested ? ' nested' : ''}`} type="button" onClick={onClick}>
      <span className="rail-glyph" aria-hidden="true">{nested ? '↳' : '›'}</span>
      <span className="rail-label">{label}</span>
      {detail ? <span className="rail-detail">{detail}</span> : null}
    </button>
  )
}

function turnText(turn: Turn): string {
  return turn.blocks
    .filter((block): block is { type: 'text'; text: string } => block.type === 'text' && typeof (block as { text?: unknown }).text === 'string')
    .map((block) => block.text)
    .join('\n')
}

function formatBytes(value: number | null): string {
  if (value === null) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}

function storageTitle(path: string): string {
  if (!path) return 'Workspace'
  return path.split('/').filter(Boolean).at(-1) ?? 'Workspace'
}

function AtlasPage({ health }: { health: Health | null }) {
  const runtimeOk = Boolean(health)
  const databaseOk = Boolean(health?.database.ok)
  const providerOk = Boolean(health?.provider.configured)
  const [turns, setTurns] = useState<Turn[]>([])
  const [conversationContext, setConversationContext] = useState<ConversationContext | null>(null)
  const [draft, setDraft] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<'home' | 'local' | 'drive'>('home')
  const [storage, setStorage] = useState<LocalStorageListing | null>(null)
  const [drive, setDrive] = useState<DriveStorageListing | null>(null)
  const [driveStack, setDriveStack] = useState<Array<{ id: string; name: string }>>([{ id: 'root', name: 'My Drive' }])
  const [storageLoading, setStorageLoading] = useState(false)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [driveLoading, setDriveLoading] = useState(false)
  const [driveError, setDriveError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [pendingActions, setPendingActions] = useState<PendingAction[]>([])
  const [recentActions, setRecentActions] = useState<RecentAction[]>([])
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    getConversation().then((conversation) => setTurns(conversation.turns)).catch((cause) => setError(String(cause)))
    getConversationContext().then(setConversationContext).catch(() => setConversationContext(null))
    getPendingActions().then(setPendingActions).catch(() => setPendingActions([]))
    getRecentActions().then(setRecentActions).catch(() => setRecentActions([]))
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [turns, streamingText])

  const visibleTurns = useMemo(() => turns.filter((turn) => turn.actor === 'owner' || turn.actor === 'atlas'), [turns])

  async function openLocalStorage(path = '') {
    setView('local')
    setStorageLoading(true)
    setStorageError(null)
    try {
      setStorage(await getLocalStorage(path))
    } catch (cause) {
      setStorageError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setStorageLoading(false)
    }
  }

  async function openDriveStorage(folderId = 'root', name = 'My Drive', push = false) {
    setView('drive')
    setDriveLoading(true)
    setDriveError(null)
    try {
      setDrive(await getDriveStorage(folderId))
      if (push) setDriveStack((current) => [...current, { id: folderId, name }])
    } catch (cause) {
      setDriveError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setDriveLoading(false)
    }
  }

  async function addFiles(files: FileList | File[]) {
    const items = Array.from(files)
    if (!items.length || uploading) return
    setUploading(true)
    setStorageError(null)
    try {
      for (const file of items) await uploadLocalFile(storage?.path ?? '', file)
      await openLocalStorage(storage?.path ?? '')
    } catch (cause) {
      setStorageError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setUploading(false)
      setDragging(false)
    }
  }

  async function handleActionDecision(action: PendingAction, approve: boolean) {
    if (!action.action_id) return
    try {
      await decideAction(action.action_id, approve)
      setPendingActions(await getPendingActions())
      setRecentActions(await getRecentActions())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  async function sendMessage() {
    const text = draft.trim()
    if (!text || sending) return
    setDraft('')
    setError(null)
    setSending(true)
    setStreamingText('')
    const optimistic: Turn = {
      id: `local-${Date.now()}`,
      transcript_id: 'local',
      actor: 'owner',
      blocks: [{ type: 'text', text }],
      created_at: new Date().toISOString(),
    }
    setTurns((current) => [...current, optimistic])
    try {
      await streamMessage(text, (delta) => setStreamingText((current) => current + delta))
      const conversation = await getConversation()
      setTurns(conversation.turns)
      setConversationContext(await getConversationContext().catch(() => null))
      setPendingActions(await getPendingActions())
      setRecentActions(await getRecentActions())
      setStreamingText('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="atlas-shell">
      <header className="persistent-bar">
        <div className="brand-cluster">
          <img className="brand-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" />
          <strong>Atlas</strong><span className="version-tag">V5</span>
        </div>
        <div className="persistent-context"><span className="context-item active">Home</span><a className="context-item context-link" href="/control">Control</a></div>
        <div className="persistent-status">
          <span className="status-label"><StatusDot ok={runtimeOk} />Runtime</span>
          <span className="status-label"><StatusDot ok={databaseOk} />PostgreSQL</span>
          <span className="status-label"><StatusDot ok={providerOk} />Model</span>
          <span className="status-label transcript-status" title={conversationContext ? `${Math.round(conversationContext.pressure * 1000) / 10}% of provider context window` : 'Provider token count unavailable'}><ContextDot state={conversationContext?.state ?? null} />Transcript {conversationContext ? `${formatTokens(conversationContext.input_tokens)} / ${formatTokens(conversationContext.limit_tokens)}` : '— / 1M'}</span>
        </div>
      </header>

      <div className="atlas-body">
        <aside className="left-rail">
          <div className="rail-scroll">
            <RailSection title="Find">
              <RailItem label="Projects" active={view === 'home'} onClick={() => setView('home')} />
              <RailItem label="Local storage" active={view === 'local'} onClick={() => { void openLocalStorage(storage?.path ?? '') }} />
              <RailItem label="Drive storage" active={view === 'drive'} onClick={() => { void openDriveStorage(driveStack.at(-1)?.id ?? 'root') }} />
              <RailItem label="Project folders" /><RailItem label="Repositories" /><RailItem label="Artifacts" />
            </RailSection>
            <RailSection title="Utilities">
              <RailItem label="Normalization" /><RailItem label="Input folder" detail="not set" nested /><RailItem label="Output folder" detail="not set" nested />
            </RailSection>
          </div>
          <div className="rail-footer"><span className="rail-footer-label">Environment</span><span><StatusDot ok={runtimeOk} />{health ? health.environment : 'checking'}</span></div>
        </aside>

        <main className="main-stage">
          <section className="chat-canvas" aria-label={view === 'home' ? 'Atlas chat canvas' : view === 'local' ? 'Local storage' : 'Drive storage'}>
            <div className="canvas-head">
              <div><div className="canvas-kicker">{view === 'home' ? 'Home' : view === 'local' ? 'Find / Local storage' : 'Find / Drive storage'}</div><h1>{view === 'home' ? 'Atlas' : view === 'local' ? storageTitle(storage?.path ?? '') : (driveStack.at(-1)?.name ?? 'My Drive')}</h1></div>
              <span className="canvas-state">{view === 'home' ? (health ? `v${health.version}` : 'checking') : view === 'local' ? (storage?.display_root ?? '~/Workspace') : 'Google Drive'}</span>
            </div>

            {view === 'local' ? (
              <div className="storage-space">
                <div className="storage-toolbar">
                  <div className="storage-location">
                    <strong>Local storage</strong>
                    <span>{storage?.path ? `${storage.display_root}/${storage.path}` : (storage?.display_root ?? '~/Workspace')}</span>
                  </div>
                  <div className="storage-actions">
                    <input ref={fileInputRef} className="storage-file-input" type="file" multiple onChange={(event) => { if (event.target.files) void addFiles(event.target.files); event.currentTarget.value = '' }} />
                    <button type="button" className="storage-up" onClick={() => fileInputRef.current?.click()} disabled={uploading}>{uploading ? 'Uploading…' : 'Add files'}</button>
                    {storage?.path ? <button type="button" className="storage-up" onClick={() => { const parts = storage.path.split('/'); parts.pop(); void openLocalStorage(parts.join('/')) }}>Up one level</button> : null}
                  </div>
                </div>
                {storageLoading ? <div className="storage-empty">Reading workspace…</div> : null}
                {storageError ? <div className="chat-error storage-error">{storageError}</div> : null}
                {!storageLoading && !storageError && storage ? (
                  <div
                    className={`storage-browser${dragging ? ' dragging' : ''}`}
                    tabIndex={0}
                    onDragEnter={(event) => { event.preventDefault(); setDragging(true) }}
                    onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; setDragging(true) }}
                    onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false) }}
                    onDrop={(event) => { event.preventDefault(); setDragging(false); void addFiles(event.dataTransfer.files) }}
                    onPaste={(event) => { if (event.clipboardData.files.length) { event.preventDefault(); void addFiles(event.clipboardData.files) } }}
                  >
                    <div className="storage-drop-hint">{dragging ? 'Drop files here' : 'Drag files here, paste copied files, or use Add files'}</div>
                    <div className="storage-summary"><span>{storage.entries.length} item{storage.entries.length === 1 ? '' : 's'}</span><span>Approved local workspace</span></div>
                    <div className="storage-table" role="table" aria-label="Workspace contents">
                      <div className="storage-row storage-header" role="row"><span>Name</span><span>Type</span><span>Size</span><span>Modified</span></div>
                      {storage.entries.map((entry) => (
                        <button className="storage-row" type="button" role="row" key={entry.path} disabled={entry.kind !== 'directory'} onClick={() => { if (entry.kind === 'directory') void openLocalStorage(entry.path) }}>
                          <span className="storage-name"><span className="storage-icon">{entry.kind === 'directory' ? '▸' : '·'}</span>{entry.name}</span>
                          <span>{entry.kind}</span><span>{formatBytes(entry.size_bytes)}</span><span>{new Date(entry.modified_at).toLocaleString()}</span>
                        </button>
                      ))}
                    </div>
                    {storage.entries.length === 0 ? <div className="storage-empty">This folder is empty.</div> : null}
                  </div>
                ) : null}
              </div>
            ) : view === 'drive' ? (
              <div className="storage-space">
                <div className="storage-toolbar">
                  <div className="storage-location"><strong>Drive storage</strong><span>{driveStack.map((item) => item.name).join(' / ')}</span></div>
                  <div className="storage-actions">
                    {driveStack.length > 1 ? <button type="button" className="storage-up" onClick={() => { const next = driveStack.slice(0, -1); setDriveStack(next); void openDriveStorage(next.at(-1)?.id ?? 'root') }}>Up one level</button> : null}
                  </div>
                </div>
                {driveLoading ? <div className="storage-empty">Reading Google Drive…</div> : null}
                {driveError ? <div className="chat-error storage-error">{driveError}</div> : null}
                {!driveLoading && !driveError && drive ? (
                  <div className="storage-browser">
                    <div className="storage-summary"><span>{drive.entries.length} item{drive.entries.length === 1 ? '' : 's'}</span><span>Owner-authorized Google Drive · read only</span></div>
                    <div className="storage-table" role="table" aria-label="Google Drive contents">
                      <div className="storage-row storage-header" role="row"><span>Name</span><span>Type</span><span>Size</span><span>Modified</span></div>
                      {drive.entries.map((entry) => (
                        <button className="storage-row" type="button" role="row" key={entry.id} onClick={() => { if (entry.kind === 'directory') { void openDriveStorage(entry.id, entry.name, true) } else if (entry.web_view_link) { window.open(entry.web_view_link, '_blank', 'noopener,noreferrer') } }}>
                          <span className="storage-name"><span className="storage-icon">{entry.kind === 'directory' ? '▸' : '·'}</span>{entry.name}</span>
                          <span>{entry.kind}</span><span>{formatBytes(entry.size_bytes)}</span><span>{entry.modified_at ? new Date(entry.modified_at).toLocaleString() : '—'}</span>
                        </button>
                      ))}
                    </div>
                    {drive.entries.length === 0 ? <div className="storage-empty">This Drive folder is empty.</div> : null}
                  </div>
                ) : null}
              </div>
            ) : (
              <>
                <div className={`chat-space${visibleTurns.length ? ' has-conversation' : ''}`}>
                  {visibleTurns.length === 0 && !streamingText ? (
                    <div className="phase-message"><span className="phase-label">PHASE 1</span><h2>The model is in the seat.</h2><p>Conversation is Atlas-owned and durable. Start anywhere.</p></div>
                  ) : (
                    <div className="conversation-thread">
                      {visibleTurns.map((turn) => <article className={`chat-turn ${turn.actor}`} key={turn.id}><div className="turn-actor">{turn.actor === 'owner' ? 'You' : 'Atlas'}</div><div className="turn-body">{turnText(turn)}</div></article>)}
                      {streamingText ? <article className="chat-turn atlas streaming"><div className="turn-actor">Atlas</div><div className="turn-body">{streamingText}<span className="stream-caret" /></div></article> : null}
                      {error ? <div className="chat-error">{error}</div> : null}<div ref={bottomRef} />
                    </div>
                  )}
                </div>
                <form className="composer" onSubmit={(event) => { event.preventDefault(); void sendMessage() }}>
                  <button className="attach-button" type="button" aria-label="Attach artifact" disabled>+</button>
                  <textarea placeholder={providerOk ? 'Talk to Atlas…' : 'Model is not configured…'} rows={1} value={draft} disabled={!providerOk || sending} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void sendMessage() } }} />
                  <button className="send-button" type="submit" disabled={!providerOk || sending || !draft.trim()}>{sending ? 'Thinking…' : 'Send'}</button>
                </form>
              </>
            )}
          </section>

          <aside className="activity-rail" aria-label="Atlas activity">
            <section className="activity-section attention-section">
              <div className="activity-heading-row"><span className="activity-heading">Needs You</span><span className="activity-count">{pendingActions.length + (error ? 1 : 0)}</span></div>
              {error ? <p className="activity-empty">{error}</p> : null}
              {pendingActions.map((action) => { const args = action.detail.arguments ?? {}; const isMail = action.detail.operation === 'gmail.message.send'; return <div className="approval-widget" key={action.id}><strong>{action.title}</strong><span>{action.detail.operation ?? 'Proposed action'}</span>{isMail ? <div className="approval-mail"><span><b>To:</b> {String(args.to ?? '')}</span><span><b>Subject:</b> {String(args.subject ?? '')}</span><p>{String(args.body ?? '')}</p></div> : null}<div className="approval-actions"><button type="button" onClick={() => { void handleActionDecision(action, true) }}>Approve</button><button type="button" onClick={() => { void handleActionDecision(action, false) }}>Cancel</button></div></div> })}
              {!error && pendingActions.length === 0 ? <p className="activity-empty">Nothing needs your attention.</p> : null}
            </section>
            <div className="activity-divider" />
            <section className="activity-section">
              <div className="activity-heading-row"><span className="activity-heading">Latest</span><span className="activity-caption">recent activity</span></div>
              {recentActions.length ? <div className="activity-trace">{recentActions.map((action) => <div className="trace-row" key={action.id}><span className="latest-time">{new Date(action.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span><div><strong>{action.summary}</strong><p>{action.operation} · {action.status}</p></div></div>)}</div> : <div className="latest-empty"><span className="latest-time">—</span><div><strong>{visibleTurns.length ? 'Conversation active' : 'No recent activity yet'}</strong><p>{visibleTurns.length ? `${visibleTurns.length} persisted turns in the current transcript.` : 'External actions will appear here as Atlas uses capabilities.'}</p></div></div>}
            </section>
          </aside>
        </main>
      </div>
    </div>
  )
}

function ControlPage({ health }: { health: Health | null }) {
  const [configuration, setConfiguration] = useState<ControlConfiguration | null>(null)
  const [configurationError, setConfigurationError] = useState<string | null>(null)

  useEffect(() => {
    getControlConfiguration().then(setConfiguration).catch((cause) => setConfigurationError(String(cause)))
  }, [])

  return (
    <div className="control-shell">
      <header className="control-topbar"><div className="control-title-cluster"><img className="control-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" /><div><div className="eyebrow">ATLAS V5</div><h1>Control</h1></div></div><a className="control-link" href="/">Back to Atlas</a></header>
      <main className="control-grid">
        <section className="control-card"><div className="panel-title">Runtime</div><dl><div><dt>Version</dt><dd>{health?.version ?? 'checking'}</dd></div><div><dt>Environment</dt><dd>{health?.environment ?? 'checking'}</dd></div><div><dt>Status</dt><dd>{health?.status ?? 'checking'}</dd></div></dl></section>
        <section className="control-card"><div className="panel-title">PostgreSQL</div><p className={health?.database.ok ? 'healthy-text' : 'warning-text'}>{health?.database.ok ? 'Connected and healthy.' : 'Not connected yet.'}</p></section>
        <section className="control-card"><div className="panel-title">Model</div><dl><div><dt>Provider</dt><dd>{health?.provider.provider ?? 'checking'}</dd></div><div><dt>Model</dt><dd>{health?.provider.model ?? 'checking'}</dd></div><div><dt>Credential</dt><dd className={health?.provider.configured ? 'healthy-text' : 'warning-text'}>{health?.provider.configured ? 'Configured' : 'Missing'}</dd></div></dl></section>
        <section className="control-card control-wide"><div className="panel-title">MCP Configuration</div>{configurationError ? <p className="warning-text">{configurationError}</p> : configuration ? <div className="control-list">{configuration.mcps.map((mcp) => <div className="control-config-row" key={mcp.id}><div><strong>{mcp.label}</strong><span>{mcp.id} · {mcp.transport}</span></div><div className="control-config-meta"><span className={mcp.configured && mcp.enabled ? 'healthy-text' : 'warning-text'}>{mcp.configured && mcp.enabled ? 'Enabled' : 'Unavailable'}</span><span>{mcp.operations.length > 8 ? `${mcp.operations.length} operations discovered on demand` : (mcp.operations.length ? mcp.operations.join(', ') : 'No operations exposed')}</span></div></div>)}</div> : <p>Checking MCP configuration…</p>}</section>
        <section className="control-card control-wide"><div className="panel-title">Credentials</div><p className="control-note">Credential values are never displayed. Control only shows configuration, authentication and protection state.</p>{configuration ? <div className="control-list">{configuration.credentials.map((credential) => <div className="control-config-row" key={credential.label}><div><strong>{credential.label}</strong><span className="control-path">{credential.path ?? 'No credential storage configured'}</span></div><div className="control-config-meta"><span className={credential.configured ? 'healthy-text' : 'warning-text'}>{credential.configured ? 'Provisioned' : 'Missing'}</span>{credential.authenticated !== null ? <span className={credential.authenticated ? 'healthy-text' : 'warning-text'}>{credential.authenticated ? 'Authenticated' : 'Authentication failed'}</span> : null}{credential.scope ? <span>Scope: {credential.scope}</span> : null}<span className={credential.protected ? 'healthy-text' : 'warning-text'}>{credential.protected ? 'Protected' : 'Check permissions'}</span></div></div>)}</div> : <p>Checking credential configuration…</p>}</section>
        <section className="control-card"><div className="panel-title">Environment Registry</div><p>{health ? `${health.registry_entries} registered entries` : 'Checking…'}</p></section>
      </main>
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  useEffect(() => { getHealth().then(setHealth).catch(() => setHealth(null)) }, [])
  if (window.location.pathname.startsWith('/control')) return <ControlPage health={health} />
  return <AtlasPage health={health} />
}
