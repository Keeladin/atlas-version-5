
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
export type Turn = {
  sequence?: number
  id: string
  transcript_id: string
  actor: 'owner' | 'atlas' | 'tool' | 'system'
  blocks: Array<TextBlock | Record<string, unknown>>
  created_at: string
}

export type Conversation = {
  next_before_sequence: number | null
  transcript: { id: string; title: string | null; created_at: string; updated_at: string; closed_at: string | null }
  turns: Turn[]
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

export async function streamMessage(
  text: string,
  attachments: string[],
  onDelta: (delta: string) => void,
  chatId: string | null = null,
): Promise<void> {
  const response = await fetch('/api/conversation/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, attachments, chat_id: chatId }),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const message = body?.detail ?? `Atlas request failed (${response.status})`
    if (response.status === 409) throw new ForegroundConflictError(message)
    throw new Error(message)
  }
  if (!response.body) throw new Error('Atlas returned no response stream')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.trim()) continue
      const event = JSON.parse(line) as { type: string; text?: string; message?: string }
      if (event.type === 'delta' && event.text) onDelta(event.text)
      if (event.type === 'error') throw new Error(event.message ?? 'Provider error')
    }
    if (done) break
  }
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

export async function getRepositories(): Promise<RepositoryListing> {
  const response = await fetch('/api/repositories')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Repository load failed (${response.status})`)
  return body as RepositoryListing
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

export type ControlConfiguration = {
  connections: ControlConnection[]
  credentials: ControlCredential[]
  mcps: ControlMcp[]
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
  schedule_kind: 'once' | 'interval' | 'cron'
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
    last_attempt: MemoryReconciliationAttempt | null
  }
  recent_candidates: MemoryCandidate[]
  recent_memories: DurableMemoryInspection[]
}

export type MemoryCandidateDetail = {
  candidate: MemoryCandidate
  source: {
    chat_title: string | null
    turn_id: string
    sequence: number | null
    actor: string | null
    deleted: boolean
    text: string | null
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
