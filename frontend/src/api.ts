
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
  id: string
  transcript_id: string
  actor: 'owner' | 'atlas' | 'tool' | 'system'
  blocks: Array<TextBlock | Record<string, unknown>>
  created_at: string
}

export type Conversation = {
  transcript: { id: string; created_at: string; closed_at: string | null }
  turns: Turn[]
}

export async function getHealth(): Promise<Health> {
  const response = await fetch('/api/health')
  const payload = (await response.json()) as Health
  return payload
}

export async function getConversation(): Promise<Conversation> {
  const response = await fetch('/api/conversation')
  if (!response.ok) throw new Error(`Conversation load failed (${response.status})`)
  return (await response.json()) as Conversation
}

export type ConversationContext = {
  input_tokens: number
  limit_tokens: number
  pressure: number
  state: 'green' | 'amber' | 'red'
}

export async function getConversationContext(): Promise<ConversationContext> {
  const response = await fetch('/api/conversation/context')
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
  static_tokens: number
  current_context_tokens: number
  canonical_transcript_tokens: number
  limit_tokens: number
  pressure: number
  state: 'green' | 'amber' | 'red'
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

export async function getConversationContextStats(): Promise<ConversationContextStats> {
  const response = await fetch('/api/conversation/context/stats')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Context statistics load failed (${response.status})`)
  return body as ConversationContextStats
}

export async function streamMessage(
  text: string,
  attachments: string[],
  onDelta: (delta: string) => void,
): Promise<void> {
  const response = await fetch('/api/conversation/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, attachments }),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(body?.detail ?? `Atlas request failed (${response.status})`)
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

export type ControlConfiguration = {
  credentials: ControlCredential[]
  mcps: ControlMcp[]
}

export async function getControlConfiguration(): Promise<ControlConfiguration> {
  const response = await fetch('/api/control/configuration')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Control configuration load failed (${response.status})`)
  return body as ControlConfiguration
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
  detail: { operation?: string; arguments?: Record<string, unknown>; message?: string; external_id?: string; execution_started_at?: string; display_label?: string; target?: string | null }
  created_at: string
}

export async function getPendingActions(): Promise<PendingAction[]> {
  const response = await fetch('/api/actions/pending')
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Pending actions load failed (${response.status})`)
  return (body?.items ?? []) as PendingAction[]
}

export async function acknowledgeAction(actionId: string): Promise<void> {
  const response = await fetch(`/api/actions/${encodeURIComponent(actionId)}/acknowledge`, { method: 'POST' })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(body?.detail ?? `Action acknowledgement failed (${response.status})`)
}

export async function decideAction(actionId: string, approve: boolean): Promise<void> {
  const response = await fetch(`/api/actions/${encodeURIComponent(actionId)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approve }),
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
