import assert from 'node:assert/strict'
import test from 'node:test'
import { ForegroundConflictError, streamMessage } from '../src/api.ts'

async function withResponse(response, exercise) {
  const original = globalThis.fetch
  globalThis.fetch = async () => response
  try { await exercise() } finally { globalThis.fetch = original }
}

test('busy foreground is an explicit conflict that lets the UI preserve the draft', async () => {
  await withResponse(new Response(JSON.stringify({ detail: 'Another message is running' }), { status: 409 }), async () => {
    await assert.rejects(streamMessage('Unsent draft', ['note.txt'], () => assert.fail('No stream should start')),
      (error) => error instanceof ForegroundConflictError && error.message === 'Another message is running')
  })
})

test('server failure is not classified as an unaccepted foreground conflict', async () => {
  await withResponse(new Response(JSON.stringify({ detail: 'Server interrupted' }), { status: 500 }), async () => {
    await assert.rejects(streamMessage('Message', [], () => {}),
      (error) => error instanceof Error && !(error instanceof ForegroundConflictError))
  })
})

test('accepted stream preserves content across transport chunk boundaries', async () => {
  const data = new TextEncoder().encode('{"type":"delta","text":"héllo"}\n{"type":"delta","text":" 世界"}\n')
  const body = new ReadableStream({ start(controller) {
    for (let offset = 0; offset < data.length; offset += 3) controller.enqueue(data.slice(offset, offset + 3))
    controller.close()
  } })
  await withResponse(new Response(body), async () => {
    const chunks = []
    await streamMessage('Message', [], (chunk) => chunks.push(chunk))
    assert.equal(chunks.join(''), 'héllo 世界')
  })
})

test('an interrupted accepted stream reports interruption without classifying it as unsent', async () => {
  await withResponse(new Response('{"type":"error","message":"Task state was retained"}\n'), async () => {
    await assert.rejects(streamMessage('Message', [], () => {}),
      (error) => !(error instanceof ForegroundConflictError) && error.message === 'Task state was retained')
  })
})
