import assert from 'node:assert/strict'
import test from 'node:test'
import { getHostFilesystemScopes, setHostFilesystemScopes } from '../src/api.ts'

async function capture(exercise, body) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try { return { result: await exercise(), get: () => request } } finally { globalThis.fetch = original }
}

test('host filesystem scopes load and save through owner control', async () => {
  const scopes = { read: ['/srv'], write: ['/srv/atlas'], delete: [] }
  const loaded = await capture(() => getHostFilesystemScopes(), scopes)
  assert.equal(loaded.get().url, '/api/control/host-scopes')
  assert.deepEqual(loaded.result, scopes)
  const saved = await capture(() => setHostFilesystemScopes(scopes), scopes)
  assert.equal(saved.get().url, '/api/control/host-scopes')
  assert.equal(saved.get().options.method, 'PUT')
  assert.deepEqual(JSON.parse(saved.get().options.body), scopes)
})
