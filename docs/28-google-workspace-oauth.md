# Google Workspace OAuth runbook

Verified production state: **2026-09-14**.

Atlas uses OAuth 2.0 through the local `gws` bridge. The managed production bundle is owned by the `atlas-v5` service account and lives at:

```text
/var/lib/atlas-v5/control/google-workspace-config/
```

The production executable is `/opt/atlas-v5/bin/gws`. The managed bundle contains `client_secret.json`, encrypted `credentials.enc`, its encryption key, and token/cache state. `authorized_user.json` is retained as an import/legacy fallback; when encrypted credentials exist, runtime commands must prefer them and must not force `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`.

## Required OAuth scopes

The known-good authorization requests these scopes explicitly:

```text
https://mail.google.com/
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/calendar
openid
email
profile
```

`gmail.modify` is sufficient for normal message mutation but **not** Gmail's permanent-delete endpoint. Permanent delete therefore depends on `https://mail.google.com/` being present in the effective token.

## Reauthorize

Run the repository helper as the service identity so refreshed credentials keep the correct ownership:

```bash
sudo -u atlas-v5 ./deployment/authorize-google-workspace.sh
```

The helper uses the managed production config directory and explicit scopes above. Do not replace it with `gws auth login --services drive,gmail,calendar`; that selector currently yields the narrower Gmail mutation scope and can break permanent delete.

The OAuth callback listener is bound to server localhost. When opening the consent URL on another device, forward the callback port from that device to the Atlas server, for example:

```bash
ssh -L PORT:127.0.0.1:PORT jaco@<atlas-server>
```

Use the callback port printed by `gws auth login`, complete consent in the browser, then leave the tunnel once authorization finishes.

## Verify the token

```bash
sudo -u atlas-v5 env \
  GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/var/lib/atlas-v5/control/google-workspace-config \
  GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file \
  /opt/atlas-v5/bin/gws auth status
```

A healthy result has a valid refresh token and access token and includes `https://mail.google.com/`, Drive, Calendar and the identity scopes.

## Google Cloud audience

For the current single-owner personal deployment, the OAuth app is **External**, **In production**, and unverified. Do not switch it back to Testing merely to avoid the verification banner: Testing-mode authorizations for these scopes are short-lived and can force repeated reauthorization.

If Atlas later becomes a product used by other people's Google accounts, revisit Google verification and restricted-scope requirements before broadening access.

## Runtime guarantees added on 2026-09-14

- Managed startup repairs a missing `client_secret.json` from the stored authorized-user bundle when possible (`ebea0df`).
- Runtime commands prefer current encrypted `credentials.enc` over the legacy authorized-user file (`b30be0e`).
- Gmail permanent delete was acceptance-tested successfully with `https://mail.google.com/`, followed by an exact-ID read returning not found.
- Capability discovery now teaches the completion-reserve budget which operations are effecting (`056f5b2`).
- Resolved/acknowledged stale actions are pruned from active task checkpoints outside the model capability-call budget (`5bb17a0`).

## Backup

Treat the entire managed Google Workspace config directory as protected operator credential material. Back it up encrypted and separately from ordinary Atlas database/artifact snapshots. Never commit OAuth client secrets, refresh tokens, encrypted credential files, encryption keys or token caches to Git.

If Gmail read/send works but permanent delete reports `insufficient authentication scopes`, first inspect `gws auth status`. If `https://mail.google.com/` is present there but Atlas still fails, verify the runtime is not forcing `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` to a stale authorized-user token.
