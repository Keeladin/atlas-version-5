# Atlas V5 Owner Notifications

## Principle

Atlas keeps the owner in the loop without making the owner part of the runtime loop. Routine observation is silent, bounded recovery inside a defined envelope happens without asking, state changes the owner would want to know about are announced, and approval is reserved for consequential effects (see `10-authority-and-control.md`).

Notifications are an **awareness ledger**. The existing `owner_attention` rows remain the **actionable ledger** (proposals, uncertain effects, interruptions, staged changes); every attention row is mirrored into the awareness ledger by an ORM flush hook, in the same transaction, and resolves there when the owner acts on it.

## Model

```
event (source, kind, severity, title, body, detail, sensitive_fields, thread_key)
    ↓ emit (transactional, inside the caller's session)
notifications row  →  inbox (Updates section, Control)
    ↓ policy
push outbox (push_status = pending)  →  dispatcher loop  →  every enabled device
```

Severities: `info`, `warning`, `action_required`, `critical`, `resolved`.

Policy (`backend/atlas/notifications/policy.py`):

- `info` stays in the inbox.
- `warning`, `action_required`, `critical` are pushed.
- `resolved` is pushed only when the thread it closes was pushed before, so the owner never hears "fixed" about something they were never told.
- A superseding event on a thread pushed within `ATLAS_PUSH_REPEAT_MINUTES` (default 60) is still delivered but marked `quiet`; the service worker uses the thread key as the notification `tag`, so the phone replaces the earlier notification silently instead of buzzing again.

Threads (`thread_key`): a new event on an open thread supersedes the previous open rows; an identical open event is deduplicated; a `resolved` event closes the thread and is only inserted when something was open. The owner can resolve a thread from the inbox; runtime resolves attention threads when the owner decides, acknowledges or dismisses the underlying item.

Delivery is an outbox, never inline: `emit` writes the row with the caller's transaction, and `push_delivery_loop` drains `push_status = pending` rows under `FOR UPDATE SKIP LOCKED`, sending through pywebpush in a worker thread. Failures are recorded per device as status codes only; `404`/`410` disable the subscription. Without a VAPID key or without devices, rows are marked `skipped`.

## Model access and redaction

The capability family `atlas.notifications` exposes:

- `notifications.list` (read, auto): recent items with every key named in `sensitive_fields` replaced by `[redacted]` (`body` may be listed too). The model sees that something was withheld.
- `notifications.emit` (create, auto): `info` or `warning` only, rate limited per hour, attributed to the current run. This is how a scheduled task can push its result to the owner.

The owner API (`/api/notifications`, `/api/push/*`) is always owner audience and sits behind the owner session boundary. Push payloads carry only title, body, tag, url, severity and the quiet flag, never the full detail.

Like every capability, `atlas.notifications` is inserted **disabled** by the registry and must be switched on in Control. The runtime monitor and the attention bridge do not go through the capability runtime and work regardless.

## Web Push

PWA Web Push with VAPID. The key is a runtime secret (`ATLAS_PUSH_VAPID_PRIVATE_KEY_FILE`, provisioned by `deployment/bootstrap-push-vapid.sh`); the browser receives only the public key. The Control page shows push support, permission and installed-app state, enables or disables the current device, sends a test, and lists registered devices by push-service host. On iPhone and iPad the app must be installed to the Home Screen before Safari delivers push.

## First consumer: the Desktop Commander connector monitor

`backend/atlas/monitors/rdc.py` watches the owner's `desktop-commander.service` user unit. The journal of that unit proved that **every restart invalidates the persisted refresh token** (`Persisted session invalid: Invalid Refresh Token: Already Used`), after which the connector requests a device code, exits when the code expires, and is restarted by systemd with a new code every ~17 minutes until the owner enters one (75 such flows between 29 Aug and 13 Sep 2026, including a 14 hour run overnight). Restarting is therefore the cause, not a recovery. **The monitor has no restart authority and no `systemctl` access by design.** It:

- reads the journal with a fixed argv (`journalctl -o json … --after-cursor`), keeping only `MESSAGE` (truncated), `__CURSOR` and `__REALTIME_TIMESTAMP`, and matches only known lifecycle lines; the journal also carries large tool outputs that are never persisted;
- checks liveness from `/proc`;
- persists cursor and state in `host_monitor_state` in the same transaction as the events it emits, so a line is announced exactly once and a restart of Atlas never re-announces an expired code.

| Journal observation | State | Event |
| --- | --- | --- |
| `Starting MCP Device` | starting | — |
| `Persisted session invalid` | reason recorded | — |
| `Device code received` → url → code → `Code expires in N minutes` | auth_required | `action_required` on `rdc.auth:<device_id>` with url, code, expiry (code and body are sensitive) |
| another code while auth_required | auth_required | same thread again (superseded; quiet push inside the repeat window) |
| `Device marked as online` / `Device ready` after auth_required | online | `resolved` on the auth thread |
| `Session restored` then online after starting | online | `info` on `rdc.restart` |
| process absent longer than the grace period | — | `warning` on `rdc.process`; `resolved` when it returns |

Journal access is granted by adding the runtime user to the `systemd-journal` group (journal files are `root:systemd-journal 0640`); the unit keeps `ProtectHome=yes`.

The monitor stays read-only. Acting on what it reports is the job of event-driven schedules through governed MCP servers under owner policy (`27-host-operations.md`); the generic unit health monitor (`runtime.units`) covers any listed unit the same way.

Acceptance without provoking the defect: `python -m atlas.monitors.rdc --replay tests/fixtures/rdc_journal_auth_flow.jsonl` feeds recorded lines (timestamps rebased to now, code replaced) through the real parser, ledger and outbox under `rdc.replay.*` threads with a `[replay test]` prefix. A deliberate `systemctl --user restart desktop-commander.service` is a destructive test of the known upstream defect and is never part of normal validation.

Upstream: the rotated refresh token is not written back to `~/.desktop-commander-device/device.json` after a successful authorization (confirmed in `dist/remote-device/device.js`, which saves once at startup, and `remote-channel.js`, which rotates every 45 minutes in memory). `deployment/host/desktop-commander/` carries a local patch and the upstream issue is drafted in `20-implementation-notes.md`. The monitor stays useful for genuine outages either way.
