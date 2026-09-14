import { getVapidPublicKey, subscribePush, unsubscribePush } from './api'

export type PushSupport = {
  supported: boolean
  permission: NotificationPermission | 'unsupported'
  standalone: boolean
  ios: boolean
}

export function pushSupport(): PushSupport {
  const nav = typeof navigator === 'undefined' ? null : navigator
  const supported = Boolean(nav && 'serviceWorker' in nav && 'PushManager' in window && 'Notification' in window)
  const standalone = typeof window !== 'undefined' && (window.matchMedia?.('(display-mode: standalone)').matches || (nav as unknown as { standalone?: boolean })?.standalone === true)
  const ios = Boolean(nav && /iPad|iPhone|iPod/.test(nav.userAgent))
  return { supported, permission: supported ? Notification.permission : 'unsupported', standalone, ios }
}

export function urlBase64ToUint8Array(value: string): Uint8Array {
  const padding = '='.repeat((4 - (value.length % 4)) % 4)
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  const output = new Uint8Array(raw.length)
  for (let index = 0; index < raw.length; index += 1) output[index] = raw.charCodeAt(index)
  return output
}

async function serviceWorkerReady(): Promise<ServiceWorkerRegistration> {
  if (!('serviceWorker' in navigator)) throw new Error('Service workers are not available in this browser.')
  const existing = await navigator.serviceWorker.getRegistration('/')
  if (!existing) await navigator.serviceWorker.register('/sw.js')
  return navigator.serviceWorker.ready
}

/** Must be called from a user gesture: browsers only grant the permission prompt inside one. */
export async function enablePushOnThisDevice(): Promise<void> {
  const support = pushSupport()
  if (!support.supported) throw new Error('This browser does not support Web Push.')
  const vapid = await getVapidPublicKey()
  if (!vapid.configured || !vapid.public_key) throw new Error('Push is not configured on the server yet (VAPID key missing).')
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') throw new Error('Notification permission was not granted.')
  const registration = await serviceWorkerReady()
  const subscription = await registration.pushManager.getSubscription()
    ?? await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(vapid.public_key) as BufferSource })
  const json = subscription.toJSON()
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) throw new Error('The browser returned an incomplete push subscription.')
  await subscribePush({ endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth }, user_agent: navigator.userAgent.slice(0, 200) })
}

export async function disablePushOnThisDevice(): Promise<void> {
  if (!('serviceWorker' in navigator)) return
  const registration = await navigator.serviceWorker.getRegistration('/')
  const subscription = await registration?.pushManager.getSubscription()
  if (!subscription) return
  await unsubscribePush(subscription.endpoint).catch(() => undefined)
  await subscription.unsubscribe()
}

export async function currentPushEndpoint(): Promise<string | null> {
  if (!('serviceWorker' in navigator)) return null
  const registration = await navigator.serviceWorker.getRegistration('/')
  const subscription = await registration?.pushManager.getSubscription()
  return subscription?.endpoint ?? null
}
