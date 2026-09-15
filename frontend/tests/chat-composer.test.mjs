import assert from 'node:assert/strict'
import test from 'node:test'
import { composerAttachmentDisabled, composerInputDisabled, composerSendDisabled } from '../src/chatComposer.ts'

test('an active Atlas turn does not lock the composer input or attachment button', () => {
  assert.equal(composerInputDisabled(true, 'chat-1'), false)
  assert.equal(composerAttachmentDisabled(true, false), false)
})

test('an active Atlas turn still gates sending the next foreground turn', () => {
  assert.equal(composerSendDisabled(true, 'chat-1', true, false, true), true)
  assert.equal(composerSendDisabled(true, 'chat-1', false, false, true), false)
})

test('provider, chat and upload state still fail closed', () => {
  assert.equal(composerInputDisabled(false, 'chat-1'), true)
  assert.equal(composerInputDisabled(true, null), true)
  assert.equal(composerAttachmentDisabled(true, true), true)
  assert.equal(composerSendDisabled(true, null, false, false, true), true)
})
