import assert from 'node:assert/strict'
import test from 'node:test'
import { scheduleSummary } from '../src/schedules.ts'

const base = { id: 't', title: 'T', prompt: 'p', schedule_value: '', timezone: 'UTC', enabled: true, next_run_at: '2026-09-14T07:00:00+00:00', last_run_at: null, last_status: null, last_result: null }

test('event schedules are shown as waiting for an event rather than a date', () => {
  assert.equal(scheduleSummary({ ...base, schedule_kind: 'event', next_run_at: '9999-12-31T00:00:00+00:00' }), 'On event · event')
})

test('paused and timed schedules keep their kind', () => {
  assert.equal(scheduleSummary({ ...base, schedule_kind: 'cron', enabled: false }), 'Paused · cron')
  const timed = scheduleSummary({ ...base, schedule_kind: 'cron' })
  assert.ok(timed.endsWith('· cron') && !timed.startsWith('On event'))
})
