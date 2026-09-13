import type { ScheduledTask } from './api'

export function scheduleSummary(task: ScheduledTask): string {
  if (!task.enabled) return `Paused · ${task.schedule_kind}`
  if (task.schedule_kind === 'event') return 'On event · event'
  const when = new Date(task.next_run_at).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  return `${when} · ${task.schedule_kind}`
}
