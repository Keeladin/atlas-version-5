const CACHE = 'atlas-shell-v2'
const SHELL = ['/', '/atlas-icon.webp', '/favicon.svg', '/manifest.webmanifest']

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)))
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))))
  self.clients.claim()
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/')))
    return
  }

  event.respondWith(caches.match(request).then((cached) => cached || fetch(request).then((response) => {
    if (response.ok && ['style', 'script', 'image', 'font'].includes(request.destination)) {
      const copy = response.clone()
      caches.open(CACHE).then((cache) => cache.put(request, copy))
    }
    return response
  })))
})

// Owner notifications. The payload carries only display fields; the inbox holds the full record.
self.addEventListener('push', (event) => {
  let payload = {}
  try { payload = event.data ? event.data.json() : {} } catch { payload = { title: 'Atlas', body: event.data ? event.data.text() : '' } }
  const title = payload.title || 'Atlas'
  const quiet = Boolean(payload.quiet)
  const options = {
    body: payload.body || '',
    tag: payload.tag || payload.id || 'atlas',
    icon: '/atlas-icon.webp',
    badge: '/atlas-icon.webp',
    data: { url: payload.url || '/', id: payload.id || null },
    renotify: !quiet,
    silent: quiet,
    requireInteraction: payload.severity === 'action_required',
  }
  event.waitUntil(Promise.all([
    self.registration.showNotification(title, options),
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) client.postMessage({ type: 'notification', id: payload.id || null })
    }),
  ]))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = (event.notification.data && event.notification.data.url) || '/'
  const external = /^https?:\/\//.test(target) && !target.startsWith(self.location.origin)
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (clients) => {
    if (external) return self.clients.openWindow(target)
    const existing = clients.find((client) => 'focus' in client)
    if (existing) {
      await existing.focus()
      if ('navigate' in existing && target !== '/') return existing.navigate(target)
      return existing
    }
    return self.clients.openWindow(target)
  }))
})
