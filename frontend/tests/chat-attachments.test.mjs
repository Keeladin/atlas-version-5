import assert from 'node:assert/strict'
import test from 'node:test'
import { isImageAttachment, mergeRestoredAttachments, turnAttachments } from '../src/chatAttachments.ts'

test('image detection prefers media type but falls back to filename', () => {
  assert.equal(isImageAttachment('image/png', 'photo.bin'), true)
  assert.equal(isImageAttachment(null, 'photo.JPG'), true)
  assert.equal(isImageAttachment('application/pdf', 'manual.pdf'), false)
})

test('turn attachments expose durable artifacts and optimistic local previews', () => {
  const turn = {
    id: 'turn-1', transcript_id: 'chat-1', actor: 'owner', created_at: '2026-09-15T00:00:00Z',
    blocks: [
      { type: 'text', text: 'See these' },
      { type: 'artifact_ref', artifact_id: 'artifact-1', filename: 'photo.png', media_type: 'image/png' },
      { type: 'local_attachment', filename: 'notes.pdf', media_type: 'application/pdf' },
    ],
  }
  const attachments = turnAttachments(turn)
  assert.equal(attachments.length, 2)
  assert.equal(attachments[0].artifact_id, 'artifact-1')
  assert.equal(attachments[1].pending, true)
})

test('restoring a foreground conflict preserves the draft prepared during the turn', () => {
  const original = [{ name: 'first.png', path: 'Imports/first.png', kind: 'file', size_bytes: 1, modified_at: 'now', preview_url: 'blob:first' }]
  const current = [
    { name: 'first.png', path: 'Imports/first.png', kind: 'file', size_bytes: 1, modified_at: 'now', preview_url: 'blob:new-first' },
    { name: 'next.pdf', path: 'Imports/next.pdf', kind: 'file', size_bytes: 2, modified_at: 'now' },
  ]
  const merged = mergeRestoredAttachments(original, current)
  assert.deepEqual(merged.map((item) => item.path), ['Imports/first.png', 'Imports/next.pdf'])
  assert.equal(merged[0].preview_url, 'blob:new-first')
})
