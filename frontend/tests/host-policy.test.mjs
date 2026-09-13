import assert from 'node:assert/strict'
import test from 'node:test'
import { applyHostPolicy, getHostPolicy, validateHostPolicy } from '../src/api.ts'

async function capture(exercise, body) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try { return { result: await exercise(), get: () => request } } finally { globalThis.fetch = original }
}

test('host policy status, validation and apply use the control endpoints', async () => {
  const status = await capture(() => getHostPolicy(), { policy: { exists: true }, pending: null })
  assert.equal(status.get().url, '/api/control/host')
  const verdict = await capture(() => validateHostPolicy({ policy: '[shell]' }), { policy: { ok: true }, servers: null })
  assert.equal(verdict.get().url, '/api/control/host/validate')
  assert.deepEqual(JSON.parse(verdict.get().options.body), { policy: '[shell]' })
  const apply = await capture(() => applyHostPolicy({ servers: '[[servers]]' }), { pending: { kinds: ['servers'] } })
  assert.equal(apply.get().url, '/api/control/host')
  assert.equal(apply.get().options.method, 'PUT')
  assert.deepEqual(JSON.parse(apply.get().options.body), { servers: '[[servers]]' })
})
