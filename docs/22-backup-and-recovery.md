# Offline backup and recovery

Atlas durability requires PostgreSQL **and** immutable artifacts, project checkpoints/change bundles, and the owner enrollment marker from the same stopped-runtime interval. Copying the code checkout is not a state backup. The implementation is `backend/atlas/maintenance/backup.py`; it is an operator CLI and is not a model capability.

## Create a snapshot

Use the deployed version's CLI and PostgreSQL client utilities compatible with the server. Stop **all** Atlas processes using this database and state. The host wrapper checks the production systemd service is inactive; a manual/development process must be stopped separately. Keep Atlas stopped until the command succeeds or the failed snapshot has been inspected.

```bash
sudo systemctl stop atlas-v5.service
sudo install -d -o atlas-v5 -g atlas-v5 -m 0700 /var/lib/atlas-v5/backups
sudo bash deployment/backup-host.sh /var/lib/atlas-v5/backups/NEW-SNAPSHOT
```

Choose a new directory name for every snapshot. The command refuses an existing output directory and overlap with source state. It writes a custom-format PostgreSQL dump, copies regular state files, checks every canonical artifact's size and SHA-256 against the database, and writes a checksummed manifest. `COMPLETE` is published only after the snapshot files and directories have been flushed. Missing `COMPLETE`, changed content, unlisted files and symlinks make a snapshot unrestorable. Hashes detect accidental damage; the snapshot itself still needs protected storage.

Separately back up `/etc/atlas-v5/config`, `/etc/atlas-v5/secrets`, and the Google Workspace credential configuration selected by `ATLAS_GWS_CONFIG_DIR` into protected encrypted operator storage. Record the source commit/release and PostgreSQL major version with that operator backup. These credentials are deliberately excluded from ordinary state snapshots. Owner Workspace and Projects source trees are also external inputs: back them up through the owner's normal file/VCS backup process. Observed evidence bytes and pending Atlas change bundles are included in the Atlas snapshot.

Copy the completed snapshot to protected storage outside the host, and periodically rehearse restoration into disposable targets. Restart the unchanged runtime when backup maintenance is finished:

```bash
sudo systemctl start atlas-v5.service
```

## Restore into empty targets

Keep the target runtime stopped. Restore using the same Atlas release first; apply newer migrations only after verifying the restored release. Create a new empty PostgreSQL database and a protected file containing its connection URL. Select new, separate artifact/checkpoint directories and enrollment-marker path, writable by the operator running the CLI. Never use production targets for a rehearsal.

With the matching release's virtual environment and protected configuration loaded, run:

```bash
/opt/atlas-v5/venv/bin/python -m atlas.maintenance.backup restore /path/to/SNAPSHOT \
  --offline \
  --database-url-file /protected/restore-database-url \
  --artifact-dir /restore/atlas/artifacts \
  --checkpoint-dir /restore/atlas/project-checkpoints \
  --enrolled-marker /restore/atlas/auth/enrolled
```

Run as the identity owning those new targets; do not put the database URL or passwords on the command line. The CLI validates the entire manifest before touching the database, refuses populated database/state targets, restores PostgreSQL in one transaction, copies exact file bytes, and verifies canonical artifact references again. It preserves passkeys and execution records and does not start inference or dispatch effects. A failure after database restoration can leave a partial **new** target; discard/recreate only that rehearsal target before retrying. Restore is deliberately not an overwrite or in-place rollback command.

Validate conversation/task state, pending and uncertain actions, schedules, owner capability settings, artifact hashes and change bundles while offline. Restore protected configuration/credentials separately. For a real recovery, deliberately point runtime configuration at the verified restored targets before starting the matching release. Startup reconciliation retains ambiguous effect truth; queued schedules may run once scheduling and their capabilities are enabled. For a rehearsal, leave the runtime stopped or isolate its network and disable scheduling before starting it. A historic snapshot cannot know about effects performed after its capture: reconcile that interval with external providers before resuming operational work.

## Deploy maintenance boundary

`deploy-host.sh` stops the service before changing the installed application, interpreter, dependencies or migrations. A failure leaves the service stopped for inspection. It does not automatically roll back code or a database migration. Take a completed offline snapshot and retain the matching release before an upgrade requiring recovery coverage. Restoring that snapshot requires the empty-target procedure above.

The repository integration test creates two disposable PostgreSQL databases, snapshots task state and exact artifact/bundle bytes, restores them, checks identity and content, and rejects populated/corrupt targets. It does not simulate power loss, copy backups off-host, or exercise live provider credentials.
