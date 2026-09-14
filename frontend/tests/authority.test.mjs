import assert from 'node:assert/strict'
import test from 'node:test'
import { getOperationAuthorities, setOperationAuthority } from '../src/api.ts'

async function capture(exercise, body) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try { return { result: await exercise(), get: () => request } } finally { globalThis.fetch = original }
}

test('operation authority listing reads the control endpoint', async () => {
  const { get, result } = await capture(() => getOperationAuthorities(), { items: [{ id: 'host.systemd.change_unit_state', override: null }] })
  assert.equal(get().url, '/api/control/operations')
  assert.equal(result[0].id, 'host.systemd.change_unit_state')
})

test('setting authority sends the decision, and null restores the default', async () => {
  const set = await capture(() => setOperationAuthority('host.systemd.change_unit_state', 'forbidden'), { id: 'x', override: 'forbidden' })
  assert.equal(set.get().url, '/api/control/operations/host.systemd.change_unit_state')
  assert.equal(set.get().options.method, 'PUT')
  assert.deepEqual(JSON.parse(set.get().options.body), { authority: 'forbidden' })
  const reset = await capture(() => setOperationAuthority('a.b', null), { id: 'a.b', override: null })
  assert.deepEqual(JSON.parse(reset.get().options.body), { authority: null })
})
