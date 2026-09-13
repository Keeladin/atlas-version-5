import assert from 'node:assert/strict'
import test from 'node:test'
import { getRepositoryStatus } from '../src/api.ts'

test('repository detail status uses the lazy repo endpoint and branch', async () => {
  const original = globalThis.fetch
  let requestUrl = ''
  globalThis.fetch = async (url) => {
    requestUrl = String(url)
    return new Response(JSON.stringify({
      name: 'atlas-version-5', full_name: 'Keeladin/atlas-version-5',
      remote: { branch: 'main', sha: '1234', short_sha: '1234', subject: 'test', committed_at: null, url: null },
      ci: { state: 'success', checks: 1, statuses: 0, details: [] }, local: [],
    }), { status: 200 })
  }
  try {
    const result = await getRepositoryStatus({ name: 'atlas-version-5', full_name: 'Keeladin/atlas-version-5', url: null, private: false, archived: false, default_branch: 'main', description: null })
    assert.equal(requestUrl, '/api/repositories/atlas-version-5/status?default_branch=main')
    assert.equal(result.ci.state, 'success')
  } finally { globalThis.fetch = original }
})
