import assert from 'node:assert/strict'
import test from 'node:test'
import { approvalFields } from '../src/approval.ts'

test('approval includes all email recipients and untruncated body', () => {
  const args = { to: 'to@example.test', cc: 'cc@example.test', bcc: 'bcc@example.test', subject: 'Subject', body: 'x'.repeat(15000) }
  const fields = approvalFields('gmail.message.send', args)
  assert.deepEqual(fields.map((field) => field.key), Object.keys(args))
  assert.equal(fields.find((field) => field.key === 'bcc').value, args.bcc)
  assert.equal(fields.find((field) => field.key === 'body').value, args.body)
})
test('nested calendar changes and unfamiliar arguments are never omitted', () => {
  const args = { event_id: 'event', changes: { attendees: [{ email: 'person@example.test' }], summary: null }, future_field: false }
  const fields = approvalFields('calendar.event.update', args)
  assert.deepEqual(new Set(fields.map((field) => field.key)), new Set(Object.keys(args)))
  assert.deepEqual(JSON.parse(fields.find((field) => field.key === 'changes').value), args.changes)
  assert.equal(fields.find((field) => field.key === 'future_field').value, 'false')
})
test('deletion shows the exact path and expected file version', () => {
  const args = { path: 'Demo/report.txt', expected_sha256: 'a'.repeat(64) }
  const fields = approvalFields('storage.projects.delete', args)
  assert.deepEqual(fields.map((field) => field.value), Object.values(args))
})

test('approval transport carries the exact reviewed proposal hash', async () => {
  const { decideAction } = await import('../src/api.ts')
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify({ status: 'succeeded' }), { status: 200 })
  }
  try {
    await decideAction('action-id', true, 'a'.repeat(64))
    assert.equal(request.url, '/api/actions/action-id/decision')
    assert.deepEqual(JSON.parse(request.options.body), { approve: true, reviewed_target_hash: 'a'.repeat(64) })
  } finally { globalThis.fetch = original }
})
