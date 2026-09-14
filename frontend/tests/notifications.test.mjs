import assert from 'node:assert/strict'
import test from 'node:test'
import { deletePushSubscription, getNotifications, getVapidPublicKey, markAllNotificationsRead, markNotificationRead, resolveNotification, sendTestPush, subscribePush, unsubscribePush } from '../src/api.ts'

async function capture(exercise, body = { items: [], unread: 0 }) {
  const original = globalThis.fetch
  let request
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  try { return { request, result: await exercise(), get: () => request } } finally { globalThis.fetch = original }
}

test('inbox listing asks for recent items and returns the unread count', async () => {
  const { get, result } = await capture(() => getNotifications(20), { items: [{ id: 'n1', title: 't' }], unread: 3 })
  assert.equal(get().url, '/api/notifications?limit=20&include_superseded=false')
  assert.equal(result.unread, 3)
  assert.equal(result.items[0].id, 'n1')
})

test('read, read-all and resolve post to the notification resource', async () => {
  const read = await capture(() => markNotificationRead('n1'), { id: 'n1' })
  assert.equal(read.get().url, '/api/notifications/n1/read')
  assert.equal(read.get().options.method, 'POST')
  const all = await capture(() => markAllNotificationsRead(), { updated: 2 })
  assert.equal(all.get().url, '/api/notifications/read-all')
  const resolved = await capture(() => resolveNotification('n1'), { id: 'n1' })
  assert.equal(resolved.get().url, '/api/notifications/n1/resolve')
})

test('push subscription sends the browser keys as JSON and never anything else', async () => {
  const subscription = { endpoint: 'https://push.example/abc', keys: { p256dh: 'p', auth: 'a' }, user_agent: 'phone' }
  const { get } = await capture(() => subscribePush(subscription), { id: 's1', host: 'push.example' })
  assert.equal(get().url, '/api/push/subscribe')
  assert.equal(get().options.method, 'POST')
  assert.deepEqual(JSON.parse(get().options.body), subscription)
  const gone = await capture(() => unsubscribePush('https://push.example/abc'), { removed: true })
  assert.deepEqual(JSON.parse(gone.get().options.body), { endpoint: 'https://push.example/abc' })
  const removed = await capture(() => deletePushSubscription('s1'), { removed: true })
  assert.equal(removed.get().url, '/api/push/subscriptions/s1')
  assert.equal(removed.get().options.method, 'DELETE')
})

test('vapid key lookup and test push need no body', async () => {
  const key = await capture(() => getVapidPublicKey(), { configured: true, public_key: 'BAAA', subject: 'mailto:x' })
  assert.equal(key.get().url, '/api/push/vapid-public-key')
  assert.equal(key.result.public_key, 'BAAA')
  const sent = await capture(() => sendTestPush(), { push_status: 'sent', results: {} })
  assert.equal(sent.get().url, '/api/push/test')
  assert.equal(sent.get().options.method, 'POST')
  assert.equal(sent.get().options.body, undefined)
})
