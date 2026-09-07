import assert from 'node:assert/strict'
import test from 'node:test'
import { configureGitHubConnection, configureGoogleConnection, configureModelConnection, discoverModelModels, testControlConnection } from '../src/api.ts'

async function capture(exercise) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify({ ok: true, detail: 'verified' }), { status: 200 })
  }
  try { await exercise() } finally { globalThis.fetch = original }
  return request
}

test('model setup sends credential only to the dedicated owner control endpoint', async () => {
  const request = await capture(() => configureModelConnection('sk-secret', 'gpt-model'))
  assert.equal(request.url, '/api/control/connections/model')
  assert.equal(request.options.method, 'PUT')
  assert.deepEqual(JSON.parse(request.options.body), { provider: 'openai', model: 'gpt-model', api_key: 'sk-secret' })
})

test('GitHub and Google setup use separate connection endpoints', async () => {
  const github = await capture(() => configureGitHubConnection('github-secret', 'owner'))
  assert.equal(github.url, '/api/control/connections/github')
  const google = await capture(() => configureGoogleConnection({ type: 'authorized_user', refresh_token: 'refresh' }))
  assert.equal(google.url, '/api/control/connections/google')
})
test('connection test never needs credential material from the browser', async () => {
  const request = await capture(() => testControlConnection('github'))
  assert.equal(request.url, '/api/control/connections/github/test')
  assert.equal(request.options.method, 'POST')
  assert.equal(request.options.body, undefined)
})


test('model discovery verifies the submitted key and asks the provider for its catalog', async () => {
  const request = await capture(() => discoverModelModels('sk-new'))
  assert.equal(request.url, '/api/control/connections/model/models')
  assert.equal(request.options.method, 'POST')
  assert.deepEqual(JSON.parse(request.options.body), { provider: 'openai', api_key: 'sk-new' })
})

test('configured model discovery can refresh with the protected server-side key', async () => {
  const request = await capture(() => discoverModelModels())
  assert.deepEqual(JSON.parse(request.options.body), { provider: 'openai' })
})
