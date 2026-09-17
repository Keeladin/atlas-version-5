
export type AuthStatus = {
  required: boolean
  enrolled: boolean
  authenticated: boolean
}

export async function getAuthStatus(): Promise<AuthStatus> {
  const response = await fetch('/api/auth/status', { credentials: 'same-origin' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Auth status failed (${response.status})`)
  return body as AuthStatus
}

export async function getRegistrationOptions(enrollmentCode: string): Promise<{ challenge_id: string; options: Record<string, unknown> }> {
  const response = await fetch('/api/auth/register/options', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enrollment_code: enrollmentCode }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Passkey enrollment failed (${response.status})`)
  return body
}

export async function verifyRegistration(challengeId: string, enrollmentCode: string, credential: unknown): Promise<void> {
  const response = await fetch('/api/auth/register/verify', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ challenge_id: challengeId, enrollment_code: enrollmentCode, credential }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Passkey verification failed (${response.status})`)
}

export async function getLoginOptions(): Promise<{ challenge_id: string; options: Record<string, unknown> }> {
  const response = await fetch('/api/auth/login/options', { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Passkey login failed (${response.status})`)
  return body
}

export async function verifyLogin(challengeId: string, credential: unknown): Promise<void> {
  const response = await fetch('/api/auth/login/verify', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ challenge_id: challengeId, credential }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Passkey verification failed (${response.status})`)
}

export async function logout(): Promise<void> {
  const response = await fetch('/api/auth/logout', { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Logout failed (${response.status})`)
}

export type Health = {
  status: 'ok' | 'degraded'
  version: string
  environment: string
  database: { ok: boolean; error: string | null }
  provider: { provider: string; model: string; configured: boolean }
  registry_entries: number
}

export type TextBlock = { type: 'text'; text: string }
export type ArtifactRefBlock = { type: 'artifact_ref'; artifact_id: string; filename?: string | null; media_type?: string | null; provenance?: Record<string, unknown> }
export type Turn = {
  sequence?: number
  id: string
  transcript_id: string
  actor: 'owner' | 'atlas' | 'tool' | 'system'
  blocks: Array<TextBlock | ArtifactRefBlock | Record<string, unknown>>
  created_at: string
}

export type ForegroundRun = {
  id: string
  transcript_id: string | null
  status: string
  inference_status: string
  inference_active: boolean
  created_at: string
  finished_at: string | null
  events_url: string
}

export type Conversation = {
  next_before_sequence: number | null
  transcript: { id: string; title: string | null; created_at: string; updated_at: string; closed_at: string | null }
  turns: Turn[]
  active_run: ForegroundRun | null
}

export async function getHealth(): Promise<Health> {
  const response = await fetch('/api/health')
  const payload = (await response.json()) as Health
  return payload
}

export type Chat = {
  id: string
  title: string
  created_at: string
  updated_at: string
  active: boolean
}

export type ChatList = { active_chat_id: string; items: Chat[] }

export async function getChats(): Promise<ChatList> {
  const response = await fetch('/api/chats')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Chat list failed (${response.status})`)
  return body as ChatList
}

export async function createChat(title?: string): Promise<Chat> {
  const response = await fetch('/api/chats', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title || null }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Chat creation failed (${response.status})`)
  return body as Chat
}

export async function activateChat(chatId: string): Promise<Chat> {
  const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}/activate`, { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Chat activation failed (${response.status})`)
  return body as Chat
}

export async function renameChat(chatId: string, title: string): Promise<Chat> {
  const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Chat rename failed (${response.status})`)
  return body as Chat
}

export async function deleteChat(chatId: string): Promise<{ deleted_chat_id: string; active_chat: Chat }> {
  const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}`, { method: 'DELETE' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Chat deletion failed (${response.status})`)
  return body
}

export async function getConversation(chatId?: string, beforeSequence?: number): Promise<Conversation> {
  const params = new URLSearchParams()
  if (chatId) params.set('chat_id', chatId)
  if (beforeSequence) params.set('before_sequence', String(beforeSequence))
  const query = params.size ? `?${params.toString()}` : ''
  const response = await fetch(`/api/conversation${query}`)
  if (!response.ok) throw new Error(`Conversation load failed (${response.status})`)
  return (await response.json()) as Conversation
}

export type WorkingContextPolicy = {
  input_tokens: number
  token_budget: number
  exchange_limit: number
  selected_exchanges: number
  compacted_tool_turns: number
  summary_included: boolean
  budget_exceeded: boolean
  stage: 'verbatim' | 'older_tools_compacted' | 'successful_tools_compacted' | 'history_trimmed' | 'capsule_omitted'
}

export type ConversationContext = {
  input_tokens: number
  limit_tokens: number
  provider_limit_tokens: number
  pressure: number
  state: 'green' | 'amber' | 'red'
  policy: WorkingContextPolicy
}

export async function getConversationContext(chatId?: string): Promise<ConversationContext> {
  const query = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : ''
  const response = await fetch(`/api/conversation/context${query}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Context load failed (${response.status})`)
  return body as ConversationContext
}

export type ContextWindowStats = {
  exchanges: number
  input_tokens: number
  dynamic_tokens: number
  owner_messages: number
  atlas_messages: number
  tool_observations: number
  owner_characters: number
  atlas_characters: number
  largest_message_characters: number
  transcript_turns: number
}

export type ToolOperationStats = {
  operation: string
  observations: number
  projected_tokens: number
  payload_characters: number
  bands: { last_10: number; exchanges_11_15: number; exchanges_16_20: number }
}

export type HeavyToolObservation = {
  operation: string
  phase: string
  exchange_age: number
  projected_tokens: number
  payload_characters: number
}

export type ConversationContextStats = {
  transcript_id: string
  measurement_scope?: string
  static_tokens: number
  current_context_tokens: number
  canonical_transcript_tokens: number
  limit_tokens: number
  provider_limit_tokens: number
  pressure: number
  state: 'green' | 'amber' | 'red'
  policy: WorkingContextPolicy
  summary_present: boolean
  transcript: Omit<ContextWindowStats, 'exchanges' | 'input_tokens' | 'dynamic_tokens'>
  windows: ContextWindowStats[]
  tool_analysis: {
    scope_exchanges: number
    observations: number
    projected_tokens: number
    operations: ToolOperationStats[]
    largest_observations: HeavyToolObservation[]
  }
}

export async function getConversationContextStats(chatId?: string): Promise<ConversationContextStats> {
  const query = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : ''
  const response = await fetch(`/api/conversation/context/stats${query}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Context statistics load failed (${response.status})`)
  return body as ConversationContextStats
}

export class ForegroundConflictError extends Error {}
export class RunInterruptedError extends Error {}

export type StartedForegroundRun = {
  run_id: string
  transcript_id: string
  events_url: string
}

export async function startConversationRun(
  text: string,
  attachments: string[],
  chatId: string | null = null,
): Promise<StartedForegroundRun> {
  const response = await fetch('/api/conversation/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, attachments, chat_id: chatId }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const message = body?.detail ?? `Atlas request failed (${response.status})`
    if (response.status === 409) throw new ForegroundConflictError(message)
    throw new Error(message)
  }
  return body as StartedForegroundRun
}

export function observeConversationRun(runId: string, onDelta: (delta: string) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`)
    let settled = false

    const finish = (outcome: 'completed' | 'interrupted', message?: string) => {
      if (settled) return
      settled = true
      source.close()
      if (outcome === 'completed') resolve()
      else reject(new RunInterruptedError(message || 'Atlas work was interrupted; task state was retained.'))
    }

    source.addEventListener('delta', (raw) => {
      try {
        const event = JSON.parse((raw as MessageEvent).data) as { text?: string }
        if (event.text) onDelta(event.text)
      } catch (cause) {
        settled = true
        source.close()
        reject(cause instanceof Error ? cause : new Error(String(cause)))
      }
    })
    source.addEventListener('completed', () => finish('completed'))
    source.addEventListener('interrupted', (raw) => {
      let message: string | undefined
      try { message = (JSON.parse((raw as MessageEvent).data) as { message?: string }).message } catch { /* use default */ }
      finish('interrupted', message)
    })
    // EventSource reconnects automatically and supplies Last-Event-ID. A transient
    // transport loss is not an Atlas failure, so onerror intentionally does not settle.
    source.onerror = () => undefined
  })
}

export async function streamMessage(
  text: string,
  attachments: string[],
  onDelta: (delta: string) => void,
  chatId: string | null = null,
): Promise<void> {
  const run = await startConversationRun(text, attachments, chatId)
  await observeConversationRun(run.run_id, onDelta)
}

export type LocalStorageEntry = {
  name: string
  path: string
  kind: 'directory' | 'file'
  size_bytes: number | null
  modified_at: string
}

export type LocalStorageListing = {
  name: string
  display_root: string
  path: string
  entries: LocalStorageEntry[]
}

export async function getLocalStorage(path = ''): Promise<LocalStorageListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  const response = await fetch(`/api/storage/local${query}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Local storage load failed (${response.status})`)
  return body as LocalStorageListing
}

export function localStorageFileUrl(path: string, download = false): string {
  const params = new URLSearchParams({ path })
  if (download) params.set('download', 'true')
  return `/api/storage/local/file?${params.toString()}`
}

export function projectStorageFileUrl(path: string, download = false): string {
  const params = new URLSearchParams({ path })
  if (download) params.set('download', 'true')
  return `/api/storage/projects/file?${params.toString()}`
}

export async function uploadLocalFile(path: string, file: File): Promise<LocalStorageEntry> {
  const form = new FormData()
  form.append('file', file)
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  const response = await fetch(`/api/storage/local/upload${query}`, { method: 'POST', body: form })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Workspace upload failed (${response.status})`)
  return body as LocalStorageEntry
}


export async function getProjectFolders(path = ''): Promise<LocalStorageListing> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  const response = await fetch(`/api/storage/projects${query}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Project folders load failed (${response.status})`)
  return body as LocalStorageListing
}

export type RepositoryEntry = {
  name: string
  full_name: string
  url: string | null
  private: boolean
  archived: boolean
  default_branch: string | null
  description: string | null
}

export type RepositoryListing = { owner: string; repositories: RepositoryEntry[] }

export type RepositoryLocalCheckout = {
  path: string
  branch: string
  sha: string
  short_sha: string
  subject: string
  dirty: boolean
  tracking_sha: string | null
  remote_tracking_current: boolean
  ahead: number | null
  behind: number | null
  relation: 'in_sync' | 'ahead' | 'behind' | 'diverged' | 'unknown'
}

export type RepositoryStatus = {
  name: string
  full_name: string
  remote: { branch: string | null; sha: string; short_sha: string; subject: string; committed_at: string | null; url: string | null }
  ci: { state: 'success' | 'failure' | 'pending' | 'none'; checks: number; statuses: number; details: Array<{ name: string; status: string | null; conclusion: string | null; url: string | null }> }
  local: RepositoryLocalCheckout[]
}

export async function getRepositories(): Promise<RepositoryListing> {
  const response = await fetch('/api/repositories')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Repository load failed (${response.status})`)
  return body as RepositoryListing
}

export async function getRepositoryStatus(repo: RepositoryEntry): Promise<RepositoryStatus> {
  const branch = repo.default_branch ? `?default_branch=${encodeURIComponent(repo.default_branch)}` : ''
  const response = await fetch(`/api/repositories/${encodeURIComponent(repo.name)}/status${branch}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Repository status failed (${response.status})`)
  return body as RepositoryStatus
}

export type DriveStorageEntry = {
  id: string
  name: string
  kind: 'directory' | 'file'
  mime_type: string
  size_bytes: number | null
  modified_at: string | null
  web_view_link: string | null
}

export type DriveStorageListing = {
  folder_id: string
  entries: DriveStorageEntry[]
}

export async function getDriveStorage(folderId = 'root'): Promise<DriveStorageListing> {
  const response = await fetch(`/api/storage/drive?folder_id=${encodeURIComponent(folderId)}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Drive storage load failed (${response.status})`)
  return body as DriveStorageListing
}

export type ControlCredential = {
  label: string
  configured: boolean
  storage: string
  path: string | null
  protected: boolean
  authenticated: boolean | null
  scope: string | null
}

export type ControlMcp = {
  id: string
  label: string
  configured: boolean
  enabled: boolean
  availability: string
  operations: string[]
  transport: string
}

export type ControlConnection = {
  id: 'model' | 'google' | 'github'
  label: string
  configured: boolean
  authenticated: boolean | null
  detail: string
  restart_required: boolean
  operations?: number
  model?: string
  owner?: string
}

export type ControlNotificationSettings = {
  push_configured: boolean
  push_subject: string
  push_repeat_minutes: number
  rdc_monitor_enabled: boolean
  rdc_monitor_unit: string
}

export type ControlConfiguration = {
  connections: ControlConnection[]
  credentials: ControlCredential[]
  mcps: ControlMcp[]
  notifications?: ControlNotificationSettings
}

export async function getControlConfiguration(): Promise<ControlConfiguration> {
  const response = await fetch('/api/control/configuration')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Control configuration load failed (${response.status})`)
  return body as ControlConfiguration
}


export type ConnectionResult = { ok: boolean; detail: string; configured?: boolean; restart_required?: boolean; model?: string; owner?: string; provider?: string }
export type ModelCatalogResult = ConnectionResult & { provider: string; models: string[] }

async function connectionRequest(path: string, init: RequestInit): Promise<ConnectionResult> {
  const response = await fetch(path, init)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Connection request failed (${response.status})`)
  return body as ConnectionResult
}

export async function testControlConnection(id: 'model' | 'google' | 'github'): Promise<ConnectionResult> {
  return connectionRequest(`/api/control/connections/${id}/test`, { method: 'POST' })
}

export async function discoverModelModels(apiKey?: string, provider = 'openai'): Promise<ModelCatalogResult> {
  const payload: Record<string, string> = { provider }
  if (apiKey) payload.api_key = apiKey
  return connectionRequest('/api/control/connections/model/models', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }) as Promise<ModelCatalogResult>
}

export async function configureModelConnection(apiKey: string | undefined, model: string, provider = 'openai'): Promise<ConnectionResult> {
  const payload: Record<string, string> = { provider, model }
  if (apiKey) payload.api_key = apiKey
  return connectionRequest('/api/control/connections/model', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
}

export async function configureGitHubConnection(token: string, owner: string): Promise<ConnectionResult> {
  return connectionRequest('/api/control/connections/github', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token, owner }) })
}

export async function configureGoogleConnection(credentials: Record<string, unknown>): Promise<ConnectionResult> {
  return connectionRequest('/api/control/connections/google', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ credentials }) })
}

export async function restartApi(): Promise<void> {
  const response = await fetch('/api/control/restart', { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `API restart failed (${response.status})`)
}

export type PendingAction = {
  id: string
  action_id: string | null
  state: string
  title: string
  detail: { download_url?: string; path?: string; reviewable?: boolean; reviewed_target_hash?: string; proposal?: Record<string, unknown>; operation?: string; arguments?: Record<string, unknown>; message?: string; external_id?: string; execution_started_at?: string; display_label?: string; target?: string | null }
  created_at: string
}

export async function getPendingActions(): Promise<PendingAction[]> {
  const response = await fetch('/api/actions/pending')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Pending actions load failed (${response.status})`)
  return (body?.items ?? []) as PendingAction[]
}

export async function dismissAttention(attentionId: string): Promise<void> {
  const response = await fetch(`/api/attention/${encodeURIComponent(attentionId)}/dismiss`, { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Attention dismissal failed (${response.status})`)
}

export async function acknowledgeAction(actionId: string): Promise<void> {
  const response = await fetch(`/api/actions/${encodeURIComponent(actionId)}/acknowledge`, { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Action acknowledgement failed (${response.status})`)
}

export async function decideAction(actionId: string, approve: boolean, reviewedTargetHash?: string): Promise<void> {
  const response = await fetch(`/api/actions/${encodeURIComponent(actionId)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approve, reviewed_target_hash: reviewedTargetHash }),
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Action decision failed (${response.status})`)
}

export type ScheduledTask = {
  id: string
  title: string
  prompt: string
  schedule_kind: 'once' | 'interval' | 'cron' | 'event'
  schedule_value: string
  timezone: string
  enabled: boolean
  next_run_at: string
  last_run_at: string | null
  last_status: string | null
  last_result: string | null
}

export async function getScheduledTasks(includeDisabled = true): Promise<ScheduledTask[]> {
  const response = await fetch(`/api/schedules?include_disabled=${includeDisabled ? 'true' : 'false'}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Scheduled tasks load failed (${response.status})`)
  return (body?.items ?? []) as ScheduledTask[]
}

export type RecentAction = {
  id: string
  operation: string
  status: string
  summary: string
  created_at: string
}

export async function getRecentActions(limit = 8): Promise<RecentAction[]> {
  const response = await fetch(`/api/actions/recent?limit=${limit}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Recent actions load failed (${response.status})`)
  return (body?.items ?? []) as RecentAction[]
}

export type OwnerCapability = { id: string; family: string; description: string; enabled: boolean; provisioned: boolean; availability: string }
export async function getOwnerCapabilities(): Promise<OwnerCapability[]> {
  const response = await fetch('/api/control/capabilities')
  if (!response.ok) throw new Error('Could not load capability settings')
  return (await response.json()).items
}
export async function setOwnerCapability(id: string, enabled: boolean): Promise<void> {
  const response = await fetch(`/api/control/capabilities/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
  })
  if (!response.ok) throw new Error('Capability setting was not saved')
}

export type MemoryReconciliationAttempt = {
  id: string
  attempt_number: number
  status: string
  semantic_decision: string | null
  target_memory_id: string | null
  operation_id: string | null
  evaluated_memory_revision: number | null
  evaluated_source_revision: number | null
  evidence_refs: Record<string, unknown>
  result: Record<string, unknown>
  error: string | null
  started_at: string
  completed_at: string | null
}

export type MemoryCandidate = {
  id: string
  status: string
  kind: string
  content: string | null
  scope: string
  scope_key: string | null
  confidence: number
  durability: string
  proposed_action: string
  subject: string | null
  namespace: string | null
  evidence: string | null
  proposer_model: string | null
  intake_path: string
  attempt_count: number
  decision: Record<string, unknown>
  review_after: string | null
  expires_at: string | null
  invalidated_at: string | null
  created_at: string
  processed_at: string | null
  source_transcript_id: string
  source_turn_id: string
  source_provider_evidence_id: string | null
  source_chat_title: string | null
  source_sequence: number | null
  latest_attempt: MemoryReconciliationAttempt | null
}

export type DurableMemoryInspection = {
  id: string
  status: string
  authority: 'owner' | 'derived'
  record_kind: string
  memory_kind: string | null
  scope: string
  scope_key: string | null
  durability: string
  subject: string | null
  namespace: string | null
  content: string | null
  suppresses_recall: boolean
  embedded: boolean
  embedding_model: string | null
  source_transcript_id: string | null
  source_turn_id: string | null
  supersedes_id: string | null
  superseded_by_id: string | null
  valid_from: string | null
  valid_to: string | null
  retired_at: string | null
  deleted_at: string | null
  created_at: string
  updated_at: string
}

export type MemoryObservability = {
  stream_token: string
  reconciliation: {
    enabled: boolean
    model: string
    batch_size: number
    lease_seconds: number
    max_attempts: number
    short_term_review_hours: number
    short_term_expiry_days: number
  }
  summary: {
    candidate_counts: Record<string, number>
    memory_counts: Record<string, number>
    authority_counts: { owner: number; derived: number }
    grounding_counts: Record<string, number>
    obligation_counts: Record<string, number>
    proposal_verifier: {
      eligible: number
      agree: number
      disagree: number
      disagreement_rate: number | null
      review_excluded: number
      same_model: number
      cross_model: number
      unknown_model_pair: number
    }
    last_attempt: MemoryReconciliationAttempt | null
  }
  recent_candidates: MemoryCandidate[]
  recent_memories: DurableMemoryInspection[]
}

export type MemoryTraceTurn = {
  chat_title?: string | null
  turn_id: string
  span_ref?: string
  principal?: string
  sequence: number | null
  actor: string | null
  deleted: boolean
  text: string | null
}

export type MemoryCandidateDetail = {
  candidate: MemoryCandidate
  trigger: MemoryTraceTurn
  source: MemoryTraceTurn
  evidence_sources: MemoryTraceTurn[]
  verification: {
    independent_readings: Array<Record<string, unknown>>
    comparisons: Array<Record<string, unknown>>
    reconciliations: Array<Record<string, unknown>>
    policies: Array<Record<string, unknown>>
    obligations: Array<Record<string, unknown>>
    conflicts: Array<Record<string, unknown>>
  }
  attempts: MemoryReconciliationAttempt[]
  linked_memories: Array<DurableMemoryInspection & { provenance: Array<Record<string, unknown>> }>
  reason_retained: boolean
}

export async function getMemoryObservability(limit = 40): Promise<MemoryObservability> {
  const response = await fetch(`/api/control/memory?limit=${limit}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Memory observability load failed (${response.status})`)
  return body as MemoryObservability
}

export async function getMemoryCandidateDetail(candidateId: string): Promise<MemoryCandidateDetail> {
  const response = await fetch(`/api/control/memory/candidates/${encodeURIComponent(candidateId)}`)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Memory candidate load failed (${response.status})`)
  return body as MemoryCandidateDetail
}

export type MemoryStreamState = 'connecting' | 'live' | 'reconnecting'

export function streamMemoryObservability(
  onSnapshot: (snapshot: MemoryObservability) => void,
  onState: (state: MemoryStreamState) => void,
  limit = 40,
  since?: string,
): () => void {
  const params = new URLSearchParams({ limit: String(limit) })
  if (since) params.set('since', since)
  const source = new EventSource(`/api/control/memory/stream?${params.toString()}`)
  onState('connecting')
  source.onopen = () => onState('live')
  source.onerror = () => onState('reconnecting')
  source.addEventListener('snapshot', (event) => {
    const snapshot = JSON.parse((event as MessageEvent<string>).data) as MemoryObservability
    onSnapshot(snapshot)
  })
  return () => source.close()
}

// Owner notifications: the awareness ledger and the Web Push channel.
export type NotificationSeverity = 'info' | 'warning' | 'action_required' | 'critical' | 'resolved'

export type OwnerNotification = {
  id: string
  source: string
  kind: string
  severity: NotificationSeverity
  title: string
  body: string
  detail: { url?: string; code?: string; expires_at?: string; open_url?: string; message?: string; download_url?: string; [key: string]: unknown }
  sensitive_fields: string[]
  thread_key: string | null
  status: 'open' | 'superseded' | 'resolved'
  read: boolean
  created_at: string | null
  resolved_at: string | null
  run_id: string | null
  push_status: string
}

export type NotificationInbox = { items: OwnerNotification[]; unread: number }

async function notificationRequest<T>(url: string, init: RequestInit | undefined, label: string): Promise<T> {
  const response = await fetch(url, init)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `${label} failed (${response.status})`)
  return body as T
}

export function getNotifications(limit = 30, includeSuperseded = false): Promise<NotificationInbox> {
  return notificationRequest(`/api/notifications?limit=${limit}&include_superseded=${includeSuperseded ? 'true' : 'false'}`, undefined, 'Notifications load')
}

export function markNotificationRead(notificationId: string): Promise<OwnerNotification> {
  return notificationRequest(`/api/notifications/${encodeURIComponent(notificationId)}/read`, { method: 'POST' }, 'Notification update')
}

export function markAllNotificationsRead(): Promise<{ updated: number }> {
  return notificationRequest('/api/notifications/read-all', { method: 'POST' }, 'Notification update')
}

export function resolveNotification(notificationId: string): Promise<OwnerNotification> {
  return notificationRequest(`/api/notifications/${encodeURIComponent(notificationId)}/resolve`, { method: 'POST' }, 'Notification update')
}

export type VapidInfo = { configured: boolean; public_key: string | null; subject: string }

export function getVapidPublicKey(): Promise<VapidInfo> {
  return notificationRequest('/api/push/vapid-public-key', undefined, 'Push configuration load')
}

export type PushSubscriptionSummary = {
  id: string
  host: string
  user_agent: string
  created_at: string | null
  last_success_at: string | null
  failure_count: number
  disabled: boolean
}

export async function getPushSubscriptions(): Promise<PushSubscriptionSummary[]> {
  const body = await notificationRequest<{ items: PushSubscriptionSummary[] }>('/api/push/subscriptions', undefined, 'Push devices load')
  return body.items ?? []
}

export type PushSubscriptionPayload = { endpoint: string; keys: { p256dh: string; auth: string }; user_agent?: string }

export function subscribePush(subscription: PushSubscriptionPayload): Promise<PushSubscriptionSummary> {
  return notificationRequest('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(subscription) }, 'Push subscription')
}

export function unsubscribePush(endpoint: string): Promise<{ removed: boolean }> {
  return notificationRequest('/api/push/unsubscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ endpoint }) }, 'Push unsubscribe')
}

export function deletePushSubscription(subscriptionId: string): Promise<{ removed: boolean }> {
  return notificationRequest(`/api/push/subscriptions/${encodeURIComponent(subscriptionId)}`, { method: 'DELETE' }, 'Push device removal')
}

export type PushTestResult = { push_status: string | null; results: Record<string, { status: number; at: string }>; configured: boolean }

export function sendTestPush(): Promise<PushTestResult> {
  return notificationRequest('/api/push/test', { method: 'POST' }, 'Test notification')
}

// Per-operation authority: the owner's decision overrides every default.
export type OperationAuthorityValue = 'auto' | 'approval_required' | 'forbidden'

export type OperationAuthority = {
  id: string
  capability_id: string
  family: string
  description: string
  effect: string
  trust: string
  default_authority: OperationAuthorityValue
  override: OperationAuthorityValue | null
  effective_authority: OperationAuthorityValue
  argument_rules: boolean
  enabled: boolean
}

export async function getOperationAuthorities(): Promise<OperationAuthority[]> {
  const body = await notificationRequest<{ items: OperationAuthority[] }>('/api/control/operations', undefined, 'Operation authority load')
  return body.items ?? []
}

export function setOperationAuthority(operationId: string, authority: OperationAuthorityValue | null): Promise<OperationAuthority> {
  return notificationRequest(`/api/control/operations/${encodeURIComponent(operationId)}`,
    { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ authority }) }, 'Operation authority update')
}

export type HostFilesystemScopes = { read: string[]; write: string[]; delete: string[] }

export function getHostFilesystemScopes(): Promise<HostFilesystemScopes> {
  return notificationRequest('/api/control/host-scopes', undefined, 'Host filesystem scope load')
}

export function setHostFilesystemScopes(scopes: HostFilesystemScopes): Promise<HostFilesystemScopes> {
  return notificationRequest('/api/control/host-scopes',
    { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(scopes) }, 'Host filesystem scope update')
}

export type WorkspaceTaskFilter = 'all' | 'active' | 'terminal'
export type WorkspaceTask = {
  transcript_id: string
  task_id: string
  project_id: string
  title: string
  status: 'active' | 'complete' | 'cancelled'
  controller_state: string
  objective: string
  scope: string[]
  authority_grants: string[]
  acceptance_criteria: { id: string; text: string; status: 'pending' | 'passed' | 'failed'; evidence_refs: string[] }[]
  checkpoints: { id: string; text: string; status: string }[]
  progress: { percent?: number; current_checkpoint?: string | null; completed_checkpoints?: string[]; coding_session_id?: string | null }
  next_step: string | null
  findings: string[]
  completion_rejected: string | null
  cancel_reason: string | null
  pending_actions: { action_id: string; operation: string; phase: string; evidence_id?: string }[]
  retry_count: number
  transient_retry_count: number
  next_wake_at: string | null
  created_at: string | null
  updated_at: string | null
  cleanup?: { coding_session_id: string | null; coding_session: string; cancelled_action_ids: string[]; interrupted_run_ids: string[]; warnings: string[] }
}
export type WorkspaceTaskList = { items: WorkspaceTask[]; next_offset: number | null }

async function workspaceRequest<T>(path: string, method = 'GET'): Promise<T> {
  const response = await fetch(`/api/workspace/tasks${path}`, { method, credentials: 'same-origin' })
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = typeof body?.detail === 'string' ? body.detail : `Workspace request failed (${response.status})`
    throw new Error(detail)
  }
  return body as T
}

export function listWorkspaceTasks(status: WorkspaceTaskFilter = 'all', offset = 0): Promise<WorkspaceTaskList> {
  return workspaceRequest(`?${new URLSearchParams({ status, offset: String(offset), limit: '20' })}`)
}

export function getWorkspaceTask(taskId: string): Promise<WorkspaceTask> {
  return workspaceRequest(`/${encodeURIComponent(taskId)}`)
}

export function cancelWorkspaceTask(taskId: string): Promise<WorkspaceTask> {
  return workspaceRequest(`/${encodeURIComponent(taskId)}/cancel`, 'POST')
}

export function resumeWorkspaceTask(taskId: string): Promise<WorkspaceTask> {
  return workspaceRequest(`/${encodeURIComponent(taskId)}/resume`, 'POST')
}
