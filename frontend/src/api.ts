export type Health = {
  status: 'ok' | 'degraded'
  version: string
  environment: string
  database: { ok: boolean; error: string | null }
  registry_entries: number
}

export async function getHealth(): Promise<Health> {
  const response = await fetch('/api/health')
  const payload = (await response.json()) as Health
  return payload
}
