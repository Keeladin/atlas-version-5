import assert from 'node:assert/strict'
import test from 'node:test'
import { getMemoryCandidateDetail, getMemoryObservability, streamMemoryObservability } from '../src/api.ts'

async function captureJson(payload, exercise) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify(payload), { status: 200 })
  }
  try { await exercise() } finally { globalThis.fetch = original }
  return request
}

test('memory observability loads a bounded read-only projection', async () => {
  const request = await captureJson({
    reconciliation: { enabled: true, model: 'gpt-5.6-sol' },
    summary: { candidate_counts: {}, memory_counts: {}, authority_counts: { owner: 0, derived: 0 }, grounding_counts: { legacy_unverified: 0 }, obligation_counts: { memory_review: 0, memory_conflict: 0 }, last_attempt: null },
    recent_candidates: [],
    recent_memories: [],
  }, () => getMemoryObservability(50))

  assert.equal(request.url, '/api/control/memory?limit=50')
  assert.equal(request.options, undefined)
})

test('candidate trace uses the dedicated encoded detail endpoint', async () => {
  const request = await captureJson({ candidate: {}, source: {}, attempts: [], linked_memories: [], reason_retained: false }, () => getMemoryCandidateDetail('candidate/id'))

  assert.equal(request.url, '/api/control/memory/candidates/candidate%2Fid')
  assert.equal(request.options, undefined)
})

test('memory observability stream reconnects from the snapshot token and emits live snapshots', () => {
  const original = globalThis.EventSource
  let source
  class FakeEventSource {
    constructor(url) { this.url = url; this.listeners = {}; this.closed = false; source = this }
    addEventListener(name, handler) { this.listeners[name] = handler }
    close() { this.closed = true }
  }
  globalThis.EventSource = FakeEventSource
  const states = []
  const snapshots = []
  try {
    const close = streamMemoryObservability(
      (snapshot) => snapshots.push(snapshot),
      (state) => states.push(state),
      50,
      'a'.repeat(64),
    )
    assert.equal(source.url, `/api/control/memory/stream?limit=50&since=${'a'.repeat(64)}`)
    assert.deepEqual(states, ['connecting'])
    source.onopen()
    source.listeners.snapshot({ data: JSON.stringify({ stream_token: 'b'.repeat(64), recent_candidates: [] }) })
    source.onerror()
    assert.deepEqual(states, ['connecting', 'live', 'reconnecting'])
    assert.equal(snapshots[0].stream_token, 'b'.repeat(64))
    close()
    assert.equal(source.closed, true)
  } finally {
    globalThis.EventSource = original
  }
})

