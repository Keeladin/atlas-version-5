import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { startAuthentication, startRegistration, type PublicKeyCredentialCreationOptionsJSON, type PublicKeyCredentialRequestOptionsJSON } from '@simplewebauthn/browser'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'
import { approvalFields } from './approval'
import { configureGitHubConnection, configureGoogleConnection, configureModelConnection, getOwnerCapabilities, setOwnerCapability, testControlConnection, type ControlConnection, type OwnerCapability, ForegroundConflictError, acknowledgeAction, decideAction, dismissAttention, getAuthStatus, getControlConfiguration, getConversation, getConversationContext, getConversationContextStats, getDriveStorage, getHealth, getLocalStorage, getLoginOptions, getProjectFolders, getRegistrationOptions, getRepositories, getPendingActions, getRecentActions, getScheduledTasks, logout, restartApi, streamMessage, uploadLocalFile, verifyLogin, verifyRegistration, type AuthStatus, type ControlConfiguration, type Conversation, type ConversationContext, type ConversationContextStats, type DriveStorageListing, type Health, type LocalStorageEntry, type LocalStorageListing, type RepositoryListing, type PendingAction, type RecentAction, type ScheduledTask, type Turn } from './api'

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

function MarkdownBody({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
}

function turnText(turn: Turn): string {
  return turn.blocks
    .filter((block): block is { type: 'text'; text: string } => block.type === 'text' && typeof (block as { text?: unknown }).text === 'string')
    .map((block) => block.text)
    .join('\n')
    .replace(/\n\n\[Attached local workspace files?: .*?\]$/s, '')
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

function AtlasPage({ health, onLogout }: { health: Health | null; onLogout: () => Promise<void> }) {
  const runtimeOk = Boolean(health)
  const databaseOk = Boolean(health?.database.ok)
  const providerOk = Boolean(health?.provider.configured)
  const [turns, setTurns] = useState<Turn[]>([])
  const [historyCursor, setHistoryCursor] = useState<number | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  function installConversation(conversation: Conversation) {
    setTurns(conversation.turns)
    setHistoryCursor(conversation.next_before_sequence)
  }
  async function loadOlderConversation() {
    if (!historyCursor || historyLoading) return
    setHistoryLoading(true)
    try {
      const older = await getConversation(historyCursor)
      setTurns((current) => [...older.turns, ...current.filter((turn) => !older.turns.some((old) => old.id === turn.id))])
      setHistoryCursor(older.next_before_sequence)
    } catch (cause) { setError(String(cause)) }
    finally { setHistoryLoading(false) }
  }

  const [conversationContext, setConversationContext] = useState<ConversationContext | null>(null)
  const [draft, setDraft] = useState('')
  const [streamingText, setStreamingText] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<'home' | 'local' | 'drive' | 'projects' | 'repositories'>('home')
  const [storage, setStorage] = useState<LocalStorageListing | null>(null)
  const [drive, setDrive] = useState<DriveStorageListing | null>(null)
  const [projectStorage, setProjectStorage] = useState<LocalStorageListing | null>(null)
  const [repositories, setRepositories] = useState<RepositoryListing | null>(null)
  const [driveStack, setDriveStack] = useState<Array<{ id: string; name: string }>>([{ id: 'root', name: 'My Drive' }])
  const [storageLoading, setStorageLoading] = useState(false)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [driveLoading, setDriveLoading] = useState(false)
  const [driveError, setDriveError] = useState<string | null>(null)
  const [projectLoading, setProjectLoading] = useState(false)
  const [projectError, setProjectError] = useState<string | null>(null)
  const [repositoryLoading, setRepositoryLoading] = useState(false)
  const [repositoryError, setRepositoryError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [pendingActions, setPendingActions] = useState<PendingAction[]>([])
  const [recentActions, setRecentActions] = useState<RecentAction[]>([])
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTask[]>([])
  const [mobileActivity, setMobileActivity] = useState<'needs' | 'latest' | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const composerFileInputRef = useRef<HTMLInputElement | null>(null)
  const [composerAttachments, setComposerAttachments] = useState<LocalStorageEntry[]>([])
  const [composerUploading, setComposerUploading] = useState(false)
  const bottomRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    getConversation().then(installConversation).catch((cause) => setError(String(cause)))
    getConversationContext().then(setConversationContext).catch(() => setConversationContext(null))
    getPendingActions().then(setPendingActions).catch(() => setPendingActions([]))
    getRecentActions(4).then(setRecentActions).catch(() => setRecentActions([]))
    getScheduledTasks(true).then(setScheduledTasks).catch(() => setScheduledTasks([]))
  }, [])

  const latestTurnId = turns.at(-1)?.id
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [latestTurnId, streamingText])

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

  async function openProjectFolders(path = '') {
    setView('projects')
    setProjectLoading(true)
    setProjectError(null)
    try {
      setProjectStorage(await getProjectFolders(path))
    } catch (cause) {
      setProjectError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setProjectLoading(false)
    }
  }

  async function openRepositories() {
    setView('repositories')
    setRepositoryLoading(true)
    setRepositoryError(null)
    try {
      setRepositories(await getRepositories())
    } catch (cause) {
      setRepositoryError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setRepositoryLoading(false)
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
      await decideAction(action.action_id, approve, action.detail.reviewed_target_hash)
      setPendingActions(await getPendingActions())
      setRecentActions(await getRecentActions(4))
      setScheduledTasks(await getScheduledTasks(true))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  async function handleActionAcknowledge(action: PendingAction) {
    if (!action.action_id) return
    try {
      await acknowledgeAction(action.action_id)
      setPendingActions(await getPendingActions())
      setRecentActions(await getRecentActions(4))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  async function handleInterruptionDismiss(action: PendingAction) {
    try {
      await dismissAttention(action.id)
      setPendingActions(await getPendingActions())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  async function addComposerFiles(files: FileList | File[]) {
    const items = Array.from(files)
    if (!items.length || composerUploading || sending) return
    setComposerUploading(true)
    setError(null)
    try {
      const uploaded: LocalStorageEntry[] = []
      for (const file of items) uploaded.push(await uploadLocalFile('Imports', file))
      setComposerAttachments((current) => [...current, ...uploaded])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setComposerUploading(false)
    }
  }

  async function submitOwnerMessage(requestText: string, attachments: LocalStorageEntry[], restoreOnConflict: boolean) {
    if ((!requestText && attachments.length === 0) || sending || composerUploading) return
    const attachmentPaths = attachments.map((file) => file.path)
    setError(null)
    setSending(true)
    setStreamingText('')
    const optimistic: Turn = {
      id: `local-${Date.now()}`,
      transcript_id: 'local',
      actor: 'owner',
      blocks: [{ type: 'text', text: requestText }],
      created_at: new Date().toISOString(),
    }
    setTurns((current) => [...current, optimistic])
    try {
      await streamMessage(requestText, attachmentPaths, (delta) => setStreamingText((current) => current + delta))
      const conversation = await getConversation()
      installConversation(conversation)
      setConversationContext(await getConversationContext().catch(() => null))
      setPendingActions(await getPendingActions())
      setRecentActions(await getRecentActions(4))
      setScheduledTasks(await getScheduledTasks(true))
      setStreamingText('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      if (cause instanceof ForegroundConflictError) {
        if (restoreOnConflict) {
          setDraft(requestText)
          setComposerAttachments(attachments)
        }
        setTurns((current) => current.filter((turn) => turn.id !== optimistic.id))
      }
      const conversation = await getConversation().catch(() => null)
      if (conversation) installConversation(conversation)
      const attention = await getPendingActions().catch(() => null)
      if (attention) setPendingActions(attention)
    } finally {
      setSending(false)
      setStreamingText('')
    }
  }

  async function sendMessage() {
    const text = draft.trim()
    if ((!text && composerAttachments.length === 0) || sending || composerUploading) return
    const attachments = [...composerAttachments]
    setDraft('')
    setComposerAttachments([])
    await submitOwnerMessage(text, attachments, true)
  }

  async function handleInterruptionContinue() {
    if (!providerOk || sending || composerUploading) return
    setView('home')
    setMobileActivity(null)
    await submitOwnerMessage('continue', [], false)
  }

  function renderPendingAction(action: PendingAction) {
    const args = action.detail.arguments ?? {}
    const uncertain = action.state === 'uncertain'
    if (action.state === 'staged_change') {
      return <div className="approval-widget" key={action.id}><strong>{action.title}</strong><span>{action.detail.path}</span><p>{action.detail.message}</p>
        <div className="approval-actions"><a href={action.detail.download_url} download>Download change bundle</a><button type="button" onClick={() => { void handleInterruptionDismiss(action) }}>Dismiss</button></div></div>
    }
    if (action.state === 'interrupted') {
      return (
        <div className="approval-widget uncertain" key={action.id}>
          <strong>{action.title}</strong>
          <p>{action.detail.message ?? 'Atlas work was interrupted.'}</p>
          <span>Continue creates a new owner turn. Prior actions will not be replayed automatically.</span>
          <div className="approval-actions">
            <button type="button" disabled={!providerOk || sending || composerUploading} onClick={() => { void handleInterruptionContinue() }}>Continue</button>
            <button type="button" onClick={() => { void handleInterruptionDismiss(action) }}>Dismiss</button>
          </div>
        </div>
      )
    }
    return (
      <div className={`approval-widget${uncertain ? ' uncertain' : ''}`} key={action.id}>
        <strong>{uncertain ? (action.detail.display_label ?? action.title) : action.title}</strong>
        {uncertain && action.detail.target ? <span className="attention-target">{action.detail.target}</span> : <span>{action.detail.operation ?? 'Proposed action'}</span>}
        {uncertain ? <div className="approval-mail"><p>{action.detail.message ?? 'Atlas started this action but cannot confirm whether it completed.'}</p>{action.detail.execution_started_at ? <span><b>Started:</b> {new Date(action.detail.execution_started_at).toLocaleString()}</span> : null}{action.detail.external_id ? <span><b>External ID:</b> {action.detail.external_id}</span> : null}</div> : null}
        {!uncertain && action.detail.reviewable ? <div className="approval-payload">
          {approvalFields(action.detail.operation ?? '', args).map((field) => <div key={field.key}><b>{field.label}</b><pre>{field.value === '' ? '(empty)' : field.value}</pre></div>)}
          <details><summary>Complete prepared action</summary><pre>{JSON.stringify(action.detail.proposal, null, 2)}</pre></details>
        </div> : !uncertain ? <p>{action.detail.message ?? 'Exact proposal details are unavailable. Reload before approving.'}</p> : null}
        {uncertain ? <div className="approval-actions"><button type="button" onClick={() => { void handleActionAcknowledge(action) }}>Acknowledge</button></div> : <div className="approval-actions"><button type="button" disabled={!action.detail.reviewable || !action.detail.reviewed_target_hash} onClick={() => { void handleActionDecision(action, true) }}>Approve</button><button type="button" onClick={() => { void handleActionDecision(action, false) }}>Cancel</button></div>}
      </div>
    )
  }

  return (
    <div className="atlas-shell">
      <header className="persistent-bar">
        <div className="brand-cluster">
          <a className="brand-avatar-link" href="/control" aria-label="Open Control" title="Open Control">
            <img className="brand-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" />
          </a>
          <strong>Atlas</strong><span className="version-tag">V5</span>
        </div>
        <div className="persistent-context"><span className="context-item active">Home</span><a className="context-item context-link" href="/control">Control</a></div>
        <div className="persistent-status">
          <span className="status-label"><StatusDot ok={runtimeOk} />Runtime</span>
          <span className="status-label"><StatusDot ok={databaseOk} />PostgreSQL</span>
          <span className="status-label"><StatusDot ok={providerOk} />Model</span>
          <span className="status-label transcript-status" title={conversationContext ? `${Math.round(conversationContext.pressure * 1000) / 10}% of Atlas working-context budget` : 'Working-context token count unavailable'}><ContextDot state={conversationContext?.state ?? null} />Context {conversationContext ? `${formatTokens(conversationContext.input_tokens)} / ${formatTokens(conversationContext.limit_tokens)}` : '— / 64k'}</span>
        </div>
        <div className="mobile-activity-controls" aria-label="Activity shortcuts">
          <button type="button" className={mobileActivity === 'needs' ? 'active' : ''} onClick={() => setMobileActivity((current) => current === 'needs' ? null : 'needs')}>Needs You <span>{pendingActions.length + (error ? 1 : 0)}</span></button>
          <button type="button" className={mobileActivity === 'latest' ? 'active' : ''} onClick={() => setMobileActivity((current) => current === 'latest' ? null : 'latest')}>Latest</button>
          <span className="mobile-token-count" title={conversationContext ? `${Math.round(conversationContext.pressure * 1000) / 10}% of Atlas working-context budget` : 'Working-context token count unavailable'}><ContextDot state={conversationContext?.state ?? null} />{conversationContext ? `${formatTokens(conversationContext.input_tokens)}/${formatTokens(conversationContext.limit_tokens)}` : '—/64k'}</span>
          <button type="button" className="mobile-auth-control" onClick={() => { void onLogout() }}>Log out</button>
        </div>
      </header>

      {mobileActivity ? <>
        <button className="mobile-activity-backdrop" type="button" aria-label="Close activity" onClick={() => setMobileActivity(null)} />
        <aside className="mobile-activity-panel" aria-label={mobileActivity === 'needs' ? 'Needs You' : 'Latest activity'}>
          {mobileActivity === 'needs' ? <section className="activity-section attention-section">
            <div className="activity-heading-row"><span className="activity-heading">Needs You</span><span className="activity-count">{pendingActions.length + (error ? 1 : 0)}</span></div>
            {error ? <p className="activity-empty">{error}</p> : null}
            {pendingActions.map(renderPendingAction)}
            {!error && pendingActions.length === 0 ? <p className="activity-empty">Nothing needs your attention.</p> : null}
          </section> : <section className="activity-section">
            <div className="activity-heading-row"><span className="activity-heading">Latest</span><span className="activity-caption">recent activity</span></div>
            {recentActions.length ? <div className="activity-trace">{recentActions.map((action) => <div className="trace-row" key={action.id}><span className="latest-time">{new Date(action.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span><div><strong>{action.summary}</strong><p>{action.operation} · {action.status}</p></div></div>)}</div> : <div className="latest-empty"><span className="latest-time">—</span><div><strong>{visibleTurns.length ? 'Conversation active' : 'No recent activity yet'}</strong><p>{visibleTurns.length ? `${visibleTurns.length} messages loaded from the conversation.` : 'External actions will appear here as Atlas uses capabilities.'}</p></div></div>}
          </section>}
        </aside>
      </> : null}


      <div className="atlas-body">
        <aside className="left-rail">
          <div className="rail-scroll">
            <RailSection title="Find">
              <RailItem label="Projects" active={view === 'home'} onClick={() => setView('home')} />
              <RailItem label="Local storage" active={view === 'local'} onClick={() => { void openLocalStorage(storage?.path ?? '') }} />
              <RailItem label="Drive storage" active={view === 'drive'} onClick={() => { void openDriveStorage(driveStack.at(-1)?.id ?? 'root') }} />
              <RailItem label="Project folders" active={view === 'projects'} onClick={() => { void openProjectFolders(projectStorage?.path ?? '') }} /><RailItem label="Repositories" active={view === 'repositories'} onClick={() => { void openRepositories() }} /><RailItem label="Artifacts" />
            </RailSection>
            <RailSection title="Utilities">
              <RailItem label="Normalization" /><RailItem label="Input folder" detail="not set" nested /><RailItem label="Output folder" detail="not set" nested />
            </RailSection>
          </div>
          <div className="rail-footer"><span className="rail-footer-label">Environment</span><span><StatusDot ok={runtimeOk} />{health ? health.environment : 'checking'}</span></div>
        </aside>

        <main className="main-stage">
          <section className="chat-canvas" aria-label={view === 'home' ? 'Atlas chat canvas' : view === 'local' ? 'Local storage' : view === 'drive' ? 'Drive storage' : view === 'projects' ? 'Project folders' : 'Repositories'}>
            <div className="canvas-head">
              <div><div className="canvas-kicker">{view === 'home' ? 'Home' : view === 'local' ? 'Find / Local storage' : view === 'drive' ? 'Find / Drive storage' : view === 'projects' ? 'Find / Project folders' : 'Find / Repositories'}</div><h1>{view === 'home' ? 'Atlas' : view === 'local' ? storageTitle(storage?.path ?? '') : view === 'drive' ? (driveStack.at(-1)?.name ?? 'My Drive') : view === 'projects' ? storageTitle(projectStorage?.path ?? '') : 'GitHub repositories'}</h1></div>
              <span className="canvas-state">{view === 'home' ? (health ? `v${health.version}` : 'checking') : view === 'local' ? (storage?.display_root ?? '~/Workspace') : view === 'drive' ? 'Google Drive' : view === 'projects' ? (projectStorage?.display_root ?? '/home/jaco/Projects') : (repositories ? `github.com/${repositories.owner}` : 'GitHub')}</span>
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
            ) : view === 'projects' ? (
              <div className="storage-space">
                <div className="storage-toolbar">
                  <div className="storage-location"><strong>Project folders</strong><span>{projectStorage?.path ? `${projectStorage.display_root}/${projectStorage.path}` : (projectStorage?.display_root ?? '/home/jaco/Projects')}</span></div>
                  <div className="storage-actions">{projectStorage?.path ? <button type="button" className="storage-up" onClick={() => { const parts = projectStorage.path.split('/'); parts.pop(); void openProjectFolders(parts.join('/')) }}>Up one level</button> : null}</div>
                </div>
                {projectLoading ? <div className="storage-empty">Reading project folders…</div> : null}
                {projectError ? <div className="chat-error storage-error">{projectError}</div> : null}
                {!projectLoading && !projectError && projectStorage ? (
                  <div className="storage-browser">
                    <div className="storage-summary"><span>{projectStorage.entries.length} item{projectStorage.entries.length === 1 ? '' : 's'}</span><span>Real local development directories · read only</span></div>
                    <div className="storage-table" role="table" aria-label="Project folder contents">
                      <div className="storage-row storage-header" role="row"><span>Name</span><span>Type</span><span>Size</span><span>Modified</span></div>
                      {projectStorage.entries.map((entry) => <button className="storage-row" type="button" role="row" key={entry.path} disabled={entry.kind !== 'directory'} onClick={() => { if (entry.kind === 'directory') void openProjectFolders(entry.path) }}><span className="storage-name"><span className="storage-icon">{entry.kind === 'directory' ? '▸' : '·'}</span>{entry.name}</span><span>{entry.kind}</span><span>{formatBytes(entry.size_bytes)}</span><span>{new Date(entry.modified_at).toLocaleString()}</span></button>)}
                    </div>
                    {projectStorage.entries.length === 0 ? <div className="storage-empty">No project entries here.</div> : null}
                  </div>
                ) : null}
              </div>
            ) : view === 'repositories' ? (
              <div className="storage-space">
                <div className="storage-toolbar"><div className="storage-location"><strong>Repositories</strong><span>{repositories ? `github.com/${repositories.owner}` : 'GitHub'}</span></div><div className="storage-actions"><button type="button" className="storage-up" onClick={() => { void openRepositories() }} disabled={repositoryLoading}>Refresh</button></div></div>
                {repositoryLoading ? <div className="storage-empty">Reading GitHub repositories…</div> : null}
                {repositoryError ? <div className="chat-error storage-error">{repositoryError}</div> : null}
                {!repositoryLoading && !repositoryError && repositories ? (
                  <div className="storage-browser">
                    <div className="storage-summary"><span>{repositories.repositories.length} repositor{repositories.repositories.length === 1 ? 'y' : 'ies'}</span><span>Owner-authorized GitHub · remote state</span></div>
                    <div className="storage-table repository-table" role="table" aria-label="GitHub repositories">
                      <div className="storage-row storage-header" role="row"><span>Name</span><span>Visibility</span><span>State</span><span>Branch</span></div>
                      {repositories.repositories.map((repo) => <button className="storage-row" type="button" role="row" key={repo.full_name} onClick={() => { if (repo.url) window.open(repo.url, '_blank', 'noopener,noreferrer') }}><span className="storage-name"><span className="storage-icon">⌘</span>{repo.name}</span><span>{repo.private ? 'private' : 'public'}</span><span>{repo.archived ? 'archived' : 'active'}</span><span>{repo.default_branch ?? '—'}</span></button>)}
                    </div>
                    {repositories.repositories.length === 0 ? <div className="storage-empty">No repositories returned by GitHub.</div> : null}
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
                  {historyCursor ? <button type="button" disabled={historyLoading} onClick={() => { void loadOlderConversation() }}>{historyLoading ? 'Loading…' : 'Load older conversation'}</button> : null}
                  {visibleTurns.length === 0 && !streamingText ? (
                    <div className="phase-message"><span className="phase-label">PHASE 1</span><h2>The model is in the seat.</h2><p>Conversation is Atlas-owned and durable. Start anywhere.</p></div>
                  ) : (
                    <div className="conversation-thread">
                      {visibleTurns.map((turn) => <article className={`chat-turn ${turn.actor}`} key={turn.id}><div className="turn-actor">{turn.actor === 'owner' ? 'You' : 'Atlas'}</div><div className="turn-body"><MarkdownBody text={turnText(turn)} /></div></article>)}
                      {streamingText ? <article className="chat-turn atlas streaming"><div className="turn-actor">Atlas</div><div className="turn-body"><MarkdownBody text={streamingText} /><span className="stream-caret" /></div></article> : null}
                      {error ? <div className="chat-error">{error}</div> : null}<div ref={bottomRef} />
                    </div>
                  )}
                </div>
                <form className="composer" onSubmit={(event) => { event.preventDefault(); void sendMessage() }} onDragOver={(event) => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy' } }} onDrop={(event) => { if (event.dataTransfer.files.length) { event.preventDefault(); void addComposerFiles(event.dataTransfer.files) } }}>
                  <input ref={composerFileInputRef} className="composer-file-input" type="file" multiple onChange={(event) => { if (event.target.files) void addComposerFiles(event.target.files); event.currentTarget.value = '' }} />
                  <button className="attach-button" type="button" aria-label="Add files" title="Add files" disabled={!providerOk || sending || composerUploading} onClick={() => composerFileInputRef.current?.click()}>+</button>
                  <div className="composer-entry">
                    {composerAttachments.length ? <div className="composer-attachments">{composerAttachments.map((file) => <span className="attachment-chip" key={file.path}><span>{file.name}</span><button type="button" aria-label={`Remove ${file.name}`} onClick={() => setComposerAttachments((current) => current.filter((item) => item.path !== file.path))}>×</button></span>)}</div> : null}
                    <textarea placeholder={composerUploading ? 'Adding file…' : providerOk ? 'Talk to Atlas…' : 'Model is not configured…'} rows={1} value={draft} disabled={!providerOk || sending} onChange={(event) => setDraft(event.target.value)} onPaste={(event) => { if (event.clipboardData.files.length) { event.preventDefault(); void addComposerFiles(event.clipboardData.files) } }} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void sendMessage() } }} />
                  </div>
                  <button className="send-button" type="submit" disabled={!providerOk || sending || composerUploading || (!draft.trim() && composerAttachments.length === 0)}>{sending ? 'Thinking…' : composerUploading ? 'Adding…' : 'Send'}</button>
                </form>
              </>
            )}
          </section>

          <aside className="activity-rail" aria-label="Atlas activity">
            <section className="activity-section attention-section">
              <div className="activity-heading-row"><span className="activity-heading">Needs You</span><span className="activity-count">{pendingActions.length + (error ? 1 : 0)}</span></div>
              {error ? <p className="activity-empty">{error}</p> : null}
              {pendingActions.map(renderPendingAction)}
              {!error && pendingActions.length === 0 ? <p className="activity-empty">Nothing needs your attention.</p> : null}
            </section>
            <div className="activity-divider" />
            <section className="activity-section latest-section">
              <div className="activity-heading-row"><span className="activity-heading">Latest</span><span className="activity-caption">recent activity</span></div>
              {recentActions.length ? <div className="activity-trace">{recentActions.map((action) => <div className="trace-row" key={action.id}><span className="latest-time">{new Date(action.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span><div><strong>{action.summary}</strong><p>{action.operation} · {action.status}</p></div></div>)}</div> : <div className="latest-empty"><span className="latest-time">—</span><div><strong>{visibleTurns.length ? 'Conversation active' : 'No recent activity yet'}</strong><p>{visibleTurns.length ? `${visibleTurns.length} messages loaded from the conversation.` : 'External actions will appear here as Atlas uses capabilities.'}</p></div></div>}
            </section>
            <div className="activity-divider" />
            <section className="activity-section scheduled-section">
              <div className="activity-heading-row"><span className="activity-heading">Scheduled tasks</span><span className="activity-count">{scheduledTasks.filter((task) => task.enabled).length}</span></div>
              {scheduledTasks.length ? <div className="scheduled-list">{scheduledTasks.slice(0, 4).map((task) => <div className="scheduled-row" key={task.id}><span className={`scheduled-dot${task.enabled ? ' enabled' : ''}`} aria-hidden="true" /><div><strong>{task.title}</strong><p>{task.enabled ? `Next ${new Date(task.next_run_at).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}` : 'Paused'} · {task.schedule_kind}</p></div></div>)}</div> : <p className="activity-empty">No scheduled tasks yet.</p>}
            </section>
          </aside>
        </main>
      </div>
    </div>
  )
}


function OwnerLogin({ status, onAuthenticated }: { status: AuthStatus; onAuthenticated: () => void }) {
  const [enrollmentCode, setEnrollmentCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function enroll() {
    if (!enrollmentCode.trim() || busy) return
    setBusy(true); setError(null)
    try {
      const ceremony = await getRegistrationOptions(enrollmentCode.trim())
      const credential = await startRegistration({
        optionsJSON: ceremony.options as unknown as PublicKeyCredentialCreationOptionsJSON,
      })
      await verifyRegistration(ceremony.challenge_id, enrollmentCode.trim(), credential)
      onAuthenticated()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally { setBusy(false) }
  }

  async function signIn() {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const ceremony = await getLoginOptions()
      const credential = await startAuthentication({
        optionsJSON: ceremony.options as unknown as PublicKeyCredentialRequestOptionsJSON,
      })
      await verifyLogin(ceremony.challenge_id, credential)
      onAuthenticated()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally { setBusy(false) }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <img className="auth-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" />
        <div className="eyebrow">ATLAS V5 · OWNER ACCESS</div>
        <h1>{status.enrolled ? 'Unlock Atlas' : 'Enroll owner passkey'}</h1>
        <p>{status.enrolled ? 'Use your device fingerprint, face, or secure screen lock.' : 'Enter the one-time enrollment code, then register this device with your fingerprint or face.'}</p>
        {status.enrolled ? (
          <button className="auth-primary" type="button" onClick={signIn} disabled={busy}>{busy ? 'Waiting for device…' : 'Unlock with passkey'}</button>
        ) : (
          <>
            <input className="auth-input" type="password" value={enrollmentCode} onChange={(event) => setEnrollmentCode(event.target.value)} placeholder="One-time enrollment code" autoComplete="one-time-code" />
            <button className="auth-primary" type="button" onClick={enroll} disabled={busy || !enrollmentCode.trim()}>{busy ? 'Waiting for device…' : 'Create owner passkey'}</button>
          </>
        )}
        {error ? <div className="auth-error">{error}</div> : null}
        <span className="auth-note">Biometric data never leaves your device.</span>
      </div>
    </div>
  )
}

function ControlPage({ health }: { health: Health | null }) {
  const [capabilities, setCapabilities] = useState<OwnerCapability[]>([])
  const [capabilityBusy, setCapabilityBusy] = useState<string | null>(null)
  const [capabilityError, setCapabilityError] = useState<string | null>(null)
  const [configuration, setConfiguration] = useState<ControlConfiguration | null>(null)
  const [configurationError, setConfigurationError] = useState<string | null>(null)
  const [contextStats, setContextStats] = useState<ConversationContextStats | null>(null)
  const [contextStatsError, setContextStatsError] = useState<string | null>(null)
  const [restartState, setRestartState] = useState<'idle' | 'requesting' | 'waiting' | 'error'>('idle')
  const [restartMessage, setRestartMessage] = useState<string | null>(null)
  const [setupOpen, setSetupOpen] = useState<ControlConnection['id'] | null>(null)
  const [setupBusy, setSetupBusy] = useState(false)
  const [connectionMessages, setConnectionMessages] = useState<Record<string, string>>({})
  const [modelKey, setModelKey] = useState('')
  const [modelName, setModelName] = useState(health?.provider.model ?? 'gpt-5.6-sol')
  const [githubToken, setGithubToken] = useState('')
  const [githubOwner, setGithubOwner] = useState('')
  const [googleCredential, setGoogleCredential] = useState('')

  async function refreshControl() {
    const [nextCapabilities, nextConfiguration] = await Promise.all([getOwnerCapabilities(), getControlConfiguration()])
    setCapabilities(nextCapabilities)
    setConfiguration(nextConfiguration)
    const model = nextConfiguration.connections.find((item) => item.id === 'model')
    const github = nextConfiguration.connections.find((item) => item.id === 'github')
    if (model?.model) setModelName(model.model)
    if (github?.owner) setGithubOwner(github.owner)
  }

  useEffect(() => {
    Promise.all([getOwnerCapabilities(), getControlConfiguration()]).then(([nextCapabilities, nextConfiguration]) => {
      setCapabilities(nextCapabilities)
      setConfiguration(nextConfiguration)
      const model = nextConfiguration.connections.find((item) => item.id === 'model')
      const github = nextConfiguration.connections.find((item) => item.id === 'github')
      if (model?.model) setModelName(model.model)
      if (github?.owner) setGithubOwner(github.owner)
    }).catch((cause) => setConfigurationError(String(cause)))
    getConversationContextStats().then(setContextStats).catch((cause) => setContextStatsError(String(cause)))
  }, [])

  async function toggleCapability(item: OwnerCapability) {
    setCapabilityBusy(item.id); setCapabilityError(null)
    try { await setOwnerCapability(item.id, !item.enabled); await refreshControl() }
    catch (cause) { setCapabilityError(String(cause)) }
    finally { setCapabilityBusy(null) }
  }

  async function handleRestart() {
    if (restartState === 'requesting' || restartState === 'waiting') return
    if (!window.confirm('Restart the Atlas API now? Active requests will be interrupted.')) return
    setRestartState('requesting'); setRestartMessage('Requesting restart…')
    try {
      await restartApi(); setRestartState('waiting'); setRestartMessage('Waiting for Atlas to return…')
      let sawOffline = false
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 500))
        try { await getHealth(); if (sawOffline || attempt >= 6) { window.location.reload(); return } }
        catch { sawOffline = true }
      }
      setRestartState('error'); setRestartMessage('Atlas did not return in time. Refresh to check it.')
    } catch (cause) { setRestartState('error'); setRestartMessage(cause instanceof Error ? cause.message : String(cause)) }
  }

  function showConnectionMessage(id: string, message: string) {
    setConnectionMessages((current) => ({ ...current, [id]: message }))
  }

  async function handleConnectionTest(connection: ControlConnection) {
    setSetupBusy(true); showConnectionMessage(connection.id, 'Testing connection…')
    try { const result = await testControlConnection(connection.id); showConnectionMessage(connection.id, result.detail) }
    catch (cause) { showConnectionMessage(connection.id, cause instanceof Error ? cause.message : String(cause)) }
    finally { setSetupBusy(false) }
  }

  async function handleSetupSave(id: ControlConnection['id']) {
    setSetupBusy(true); showConnectionMessage(id, 'Verifying before saving…')
    try {
      let result
      if (id === 'model') result = await configureModelConnection(modelKey, modelName)
      else if (id === 'github') result = await configureGitHubConnection(githubToken, githubOwner)
      else {
        let parsed: Record<string, unknown>
        try { parsed = JSON.parse(googleCredential) as Record<string, unknown> }
        catch { throw new Error('Paste a valid Google authorized_user JSON credential.') }
        result = await configureGoogleConnection(parsed)
      }
      await refreshControl()
      if (id === 'model') setModelKey('')
      if (id === 'github') setGithubToken('')
      if (id === 'google') setGoogleCredential('')
      showConnectionMessage(id, result.restart_required ? `${result.detail} Restart Atlas to activate this connection.` : result.detail)
      if (!result.restart_required) setSetupOpen(null)
    } catch (cause) { showConnectionMessage(id, cause instanceof Error ? cause.message : String(cause)) }
    finally { setSetupBusy(false) }
  }

  function connectionCard(connection: ControlConnection) {
    const active = setupOpen === connection.id
    const statusOk = connection.configured && connection.authenticated !== false
    return <section className={`control-card connection-card${active ? ' expanded' : ''}`} key={connection.id}>
      <div className="connection-head"><div><div className="panel-title">{connection.label}</div><strong className={statusOk ? 'healthy-text' : 'warning-text'}>{connection.configured ? (connection.authenticated === false ? 'Needs attention' : 'Connected') : 'Not configured'}</strong></div><StatusDot ok={statusOk} /></div>
      <p>{connection.detail}</p>
      {connection.operations ? <span className="connection-meta">{connection.operations} operations discovered</span> : null}
      <div className="connection-actions"><button type="button" disabled={setupBusy || !connection.configured} onClick={() => { void handleConnectionTest(connection) }}>Test</button><button type="button" onClick={() => setSetupOpen(active ? null : connection.id)}>{active ? 'Close' : connection.configured ? 'Configure' : 'Set up'}</button></div>
      {connectionMessages[connection.id] ? <p className="connection-message">{connectionMessages[connection.id]}</p> : null}
      {active ? <div className="connection-form">
        {connection.id === 'model' ? <><label>Model<input value={modelName} onChange={(event) => setModelName(event.target.value)} /></label><label>API key<input type="password" autoComplete="off" value={modelKey} placeholder="Enter a new API key" onChange={(event) => setModelKey(event.target.value)} /></label></> : null}
        {connection.id === 'github' ? <><label>GitHub owner<input value={githubOwner} onChange={(event) => setGithubOwner(event.target.value)} /></label><label>Personal access token<input type="password" autoComplete="off" value={githubToken} placeholder="Enter a new token" onChange={(event) => setGithubToken(event.target.value)} /></label><span>Atlas verifies the token against the official read-only GitHub MCP before saving it.</span></> : null}
        {connection.id === 'google' ? <><label>Authorized-user credential<textarea rows={6} value={googleCredential} placeholder='Paste Google OAuth authorized_user JSON' onChange={(event) => setGoogleCredential(event.target.value)} /></label><span>Authorize on a browser-capable machine with gws auth login --services drive,gmail,calendar, then export with gws auth export --unmasked and paste that authorized_user JSON here. Atlas verifies it before saving.</span></> : null}
        <button className="control-primary-button" type="button" disabled={setupBusy || (connection.id === 'model' && (!modelKey || !modelName)) || (connection.id === 'github' && (!githubToken || !githubOwner)) || (connection.id === 'google' && !googleCredential)} onClick={() => { void handleSetupSave(connection.id) }}>{setupBusy ? 'Verifying…' : 'Verify & save'}</button>
      </div> : null}
    </section>
  }

  return (
    <div className="control-shell">
      <header className="control-topbar"><div className="control-title-cluster"><img className="control-avatar" src="/atlas-icon.webp" alt="" aria-hidden="true" /><div><div className="eyebrow">ATLAS V5</div><h1>Control</h1></div></div><a className="control-link" href="/">Back to Atlas</a></header>
      <main className="control-grid control-dashboard">
        <section className="control-card"><div className="panel-title">Runtime</div><div className="control-hero-state"><StatusDot ok={health?.status === 'ok'} /><strong>{health?.status === 'ok' ? 'Online' : 'Checking'}</strong></div><p>v{health?.version ?? '—'} · {health?.environment ?? '—'}</p><div className="control-runtime-actions"><button className="control-restart-button" type="button" onClick={() => { void handleRestart() }} disabled={restartState === 'requesting' || restartState === 'waiting'}>{restartState === 'requesting' || restartState === 'waiting' ? 'Restarting…' : 'Restart Atlas'}</button>{restartMessage ? <span className={restartState === 'error' ? 'warning-text' : ''}>{restartMessage}</span> : null}</div></section>
        <section className="control-card"><div className="panel-title">System</div><div className="control-hero-state"><StatusDot ok={Boolean(health?.database.ok)} /><strong>{health?.database.ok ? 'Healthy' : 'Degraded'}</strong></div><p>PostgreSQL {health?.database.ok ? 'connected' : 'unavailable'} · {health?.registry_entries ?? '—'} registry entries</p></section>
        <section className="control-card"><div className="panel-title">Working context</div><div className="control-hero-state"><ContextDot state={contextStats?.state ?? null} /><strong>{contextStats ? `${formatTokens(contextStats.current_context_tokens)} / ${formatTokens(contextStats.limit_tokens)}` : 'Measuring…'}</strong></div><p>{contextStats ? `${contextStats.policy.selected_exchanges} exchanges · ${contextStats.policy.compacted_tool_turns} compacted tool turns` : 'Bounded foreground context'}</p></section>

        <div className="control-section-heading"><div><span className="eyebrow">CONNECTIONS</span><h2>Setup & integrations</h2></div><span>Configure, verify and reconnect the services Atlas depends on.</span></div>
        {configurationError ? <section className="control-card control-full"><p className="warning-text">{configurationError}</p></section> : configuration ? configuration.connections.map(connectionCard) : <section className="control-card control-full"><p>Checking connections…</p></section>}

        <section className="control-card control-full capabilities-card"><div className="control-section-inline"><div><div className="panel-title">Capabilities</div><p>Connection is not authority. Choose what Atlas may use right now.</p></div><span>{capabilities.filter((item) => item.enabled).length} enabled</span></div>
          {capabilityError ? <p className="warning-text">{capabilityError}</p> : null}
          <div className="capability-grid">{capabilities.map((item) => <button type="button" role="switch" aria-checked={item.enabled} className={`capability-switch${item.enabled ? ' enabled' : ''}`} key={item.id} disabled={capabilityBusy !== null} onClick={() => { void toggleCapability(item) }}><span className="capability-switch-copy"><strong>{item.family}</strong><small>{item.availability}</small></span><span className="capability-toggle"><i /></span></button>)}</div>
        </section>

        <details className="control-card control-full control-advanced">
          <summary><span><span className="panel-title">Advanced & diagnostics</span><small>Context analysis, credential protection and registry details</small></span><span>Expand</span></summary>
          <div className="advanced-content">
            <section><h3>Context diagnostics</h3>{contextStatsError ? <p className="warning-text">{contextStatsError}</p> : contextStats ? <><div className="context-summary-grid"><div><span>Current model input</span><strong>{formatTokens(contextStats.current_context_tokens)}</strong></div><div><span>Recent evidence</span><strong>{formatTokens(contextStats.canonical_transcript_tokens)}</strong></div><div><span>Static seat</span><strong>{formatTokens(contextStats.static_tokens)}</strong></div><div><span>Measured messages</span><strong>{contextStats.transcript.owner_messages + contextStats.transcript.atlas_messages}</strong></div></div><p className="control-note">{contextStats.measurement_scope ?? 'Bounded recent transcript window'} · {contextStats.tool_analysis.observations} tool observations · {formatTokens(contextStats.tool_analysis.projected_tokens)} projected tool tokens.</p></> : <p>Calculating…</p>}</section>
            <section><h3>Credential protection</h3><p>Secrets are never returned to Control after submission.</p>{configuration ? <div className="advanced-list">{configuration.credentials.map((credential) => <div key={credential.label}><strong>{credential.label}</strong><span className={credential.configured && credential.protected ? 'healthy-text' : 'warning-text'}>{credential.configured ? (credential.protected ? 'Protected' : 'Check permissions') : 'Not configured'}</span>{credential.scope ? <small>{credential.scope}</small> : null}</div>)}</div> : null}</section>
            <section><h3>Environment registry</h3><p>{health ? `${health.registry_entries} registered entries. Capability enablement is persisted and rechecked at dispatch.` : 'Checking…'}</p></section>
          </div>
        </details>
      </main>
    </div>
  )
}

function AuthenticatedApp({ onLogout }: { onLogout: () => Promise<void> }) {
  const [health, setHealth] = useState<Health | null>(null)
  useEffect(() => { getHealth().then(setHealth).catch(() => setHealth(null)) }, [])
  if (window.location.pathname.startsWith('/control')) return <ControlPage health={health} />
  return <AtlasPage health={health} onLogout={onLogout} />
}

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [authError, setAuthError] = useState<string | null>(null)

  async function refreshAuth() {
    try { setAuth(await getAuthStatus()); setAuthError(null) }
    catch (cause) { setAuthError(cause instanceof Error ? cause.message : String(cause)) }
  }

  async function signOut() {
    try {
      await logout()
      setAuth((current) => current ? { ...current, authenticated: false } : current)
      setAuthError(null)
    } catch (cause) {
      setAuthError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  useEffect(() => {
    getAuthStatus().then((status) => { setAuth(status); setAuthError(null) }).catch((cause) => setAuthError(cause instanceof Error ? cause.message : String(cause)))
  }, [])
  if (authError) return <div className="auth-shell"><div className="auth-card"><h1>Atlas unavailable</h1><p>{authError}</p></div></div>
  if (!auth) return <div className="auth-shell"><div className="auth-card"><div className="eyebrow">ATLAS V5</div><h1>Checking owner access…</h1></div></div>
  if (!auth.authenticated) return <OwnerLogin status={auth} onAuthenticated={() => void refreshAuth()} />
  return <AuthenticatedApp onLogout={signOut} />
}
