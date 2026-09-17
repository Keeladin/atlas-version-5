import assert from 'node:assert/strict'
import test from 'node:test'
import { ForegroundConflictError, RunInterruptedError, dismissAttention, dismissInformationalAttention, streamMessage } from '../src/api.ts'

let eventScript = () => {}

class FakeEventSource {
  constructor(url) {
    this.url = url
    this.listeners = new Map()
    this.closed = false
    queueMicrotask(() => eventScript(this))
  }
  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) ?? []
    callbacks.push(callback)
    this.listeners.set(type, callbacks)
  }
  emit(type, data = '{}') {
    for (const callback of this.listeners.get(type) ?? []) callback({ data })
  }
  close() { this.closed = true }
  fail() { this.onerror?.(new Event('error')) }
}

async function withTransport(response, script, exercise) {
  const originalFetch = globalThis.fetch
  const originalEventSource = globalThis.EventSource
  globalThis.fetch = async () => response
  globalThis.EventSource = FakeEventSource
  eventScript = script
  try { await exercise() } finally {
    globalThis.fetch = originalFetch
    globalThis.EventSource = originalEventSource
  }
}

const accepted = () => new Response(JSON.stringify({
  run_id: 'run-1', transcript_id: 'chat-1', events_url: '/api/runs/run-1/events',
}), { status: 202, headers: { 'Content-Type': 'application/json' } })

test('busy foreground is an explicit conflict that lets the UI preserve the draft', async () => {
  await withTransport(new Response(JSON.stringify({ detail: 'Another message is running' }), { status: 409 }), () => {}, async () => {
    await assert.rejects(streamMessage('Unsent draft', ['note.txt'], () => assert.fail('No stream should start')),
      (error) => error instanceof ForegroundConflictError && error.message === 'Another message is running')
  })
})

test('server failure before run acceptance is not classified as a foreground conflict', async () => {
  await withTransport(new Response(JSON.stringify({ detail: 'Server unavailable' }), { status: 500 }), () => {}, async () => {
    await assert.rejects(streamMessage('Message', [], () => {}),
      (error) => error instanceof Error && !(error instanceof ForegroundConflictError))
  })
})

test('accepted run is observed through SSE until durable completion', async () => {
  const chunks = []
  await withTransport(accepted(), (source) => {
    assert.equal(source.url, '/api/runs/run-1/events')
    source.emit('delta', JSON.stringify({ text: 'héllo' }))
    source.emit('delta', JSON.stringify({ text: ' 世界' }))
    source.emit('completed', JSON.stringify({ transcript_id: 'chat-1' }))
  }, async () => {
    await streamMessage('Message', [], (chunk) => chunks.push(chunk))
    assert.equal(chunks.join(''), 'héllo 世界')
  })
})

test('transient SSE network errors do not redefine the Atlas run as failed', async () => {
  const chunks = []
  await withTransport(accepted(), (source) => {
    source.fail()
    source.emit('delta', JSON.stringify({ text: 'reconnected' }))
    source.emit('completed')
  }, async () => {
    await streamMessage('Message', [], (chunk) => chunks.push(chunk))
    assert.equal(chunks.join(''), 'reconnected')
  })
})

test('durable interrupted event reports task retention without classifying the message as unsent', async () => {
  await withTransport(accepted(), (source) => {
    source.emit('interrupted', JSON.stringify({ message: 'Task state was retained' }))
  }, async () => {
    await assert.rejects(streamMessage('Message', [], () => {}),
      (error) => error instanceof RunInterruptedError && !(error instanceof ForegroundConflictError)
        && error.message === 'Task state was retained')
  })
})

test('interruption dismissal targets the durable attention item', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    assert.equal(input, '/api/attention/attention%20id/dismiss')
    assert.equal(init?.method, 'POST')
    return new Response('{}')
  }
  try { await dismissAttention('attention id') } finally { globalThis.fetch = original }
})


test('informational attention cleanup uses the protected bulk-dismiss route', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async (input, init) => {
    assert.equal(input, '/api/attention/informational/dismiss-all')
    assert.equal(init?.method, 'POST')
    return new Response(JSON.stringify({ count: 7 }), { headers: { 'Content-Type': 'application/json' } })
  }
  try { assert.equal(await dismissInformationalAttention(), 7) } finally { globalThis.fetch = original }
})
