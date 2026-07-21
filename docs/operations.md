# Operations guide

This guide covers the supported Docker Compose deployment. Run connector commands
from the directory containing `compose.yaml` and `.env`.

The daemon synchronizes automatically after one-time authentication, including
normal token refresh, retries, rate-limit cooldowns, and recovery after restart.
Backups, updates, rollbacks, storage changes, and occasional reauthentication are
manual operations.

## Know the persistent data

The host directory configured by `GARMIN_SYNC_DATA_PATH` is mounted as `/data` and
contains:

| Path | Purpose | Consequence if lost |
|---|---|---|
| `tokens/garmin_tokens.json` | Refreshable Garmin OAuth session | Authentication must be repeated. |
| `state/sync.sqlite3` and `state/sync.lock` | Scan status, deduplication ledger, checksums, and process lock | Historical activities may be relisted and downloaded. |
| `archive/YYYY/MM/*.fit` | Original validated FIT archive | Activities cannot be requeued without downloading again. |
| `quarantine/YYYY/MM/*.bin` | Invalid or unexpected Garmin downloads | Diagnostic evidence is lost. |

SQLite may create short-lived journal or sidecar files. Always stop the service
before copying `/data`; copying a live database is not a supported backup.

The Dreeve watch directory is a separate bind mount. It is shared infrastructure,
not connector-owned data, and must not be deleted during connector maintenance.

## Backup

Use an encrypted backup destination with access restricted at least as tightly as
the source. The archive contains detailed fitness/location data and a usable OAuth
token.

1. Record the exact running image and resolved paths:

   ```console
   docker compose config --images
   docker compose config
   ```

2. Stop the daemon so the SQLite database and files form one consistent snapshot:

   ```console
   docker compose stop garmin-sync
   ```

3. Create a permission-preserving archive. Replace both absolute paths with the
   configured data directory and a protected destination:

   ```console
   sudo tar --create --numeric-owner --file /absolute/path/to/backup/dreeve-garmin-sync-data.tar --directory /absolute/path/to/data .
   sudo chmod 0600 /absolute/path/to/backup/dreeve-garmin-sync-data.tar
   sudo sha256sum /absolute/path/to/backup/dreeve-garmin-sync-data.tar | sudo tee /absolute/path/to/backup/dreeve-garmin-sync-data.tar.sha256 >/dev/null
   sudo chmod 0600 /absolute/path/to/backup/dreeve-garmin-sync-data.tar.sha256
   ```

   Save a copy of `compose.yaml`, `.env`, the displayed image version/digest, and
   the checksum alongside the encrypted backup. `.env` normally contains paths and
   numeric IDs rather than Garmin credentials, but still treat deployment details
   as private.

4. Restart and verify:

   ```console
   docker compose start garmin-sync
   docker compose ps
   docker compose exec garmin-sync garmin-sync status
   ```

Test restores periodically on a separate host or isolated directory. A backup is
not proven until it has been restored and inspected.

## Restore

Restore the complete data directory as one unit. Do not combine a ledger from one
backup with tokens or archives from another.

1. Stop and remove the connector container without deleting bind-mounted data:

   ```console
   docker compose down
   ```

2. Set task-specific shell variables to exact, safe paths and the numeric IDs from
   `.env`. The restore path and safety-copy path must not already exist:

   ```console
   SYNC_DATA_PATH=/absolute/path/to/data
   SYNC_RESTORE_PATH=/absolute/path/to/data.restore
   SYNC_UID_VALUE=1000
   SYNC_GID_VALUE=1000
   ```

3. Validate the archive checksum, extract into the new private directory, and set
   the configured ownership:

   ```console
   sha256sum --check /absolute/path/to/backup/dreeve-garmin-sync-data.tar.sha256
   sudo install -d -m 0700 -o "$SYNC_UID_VALUE" -g "$SYNC_GID_VALUE" "$SYNC_RESTORE_PATH"
   sudo tar --extract --numeric-owner --preserve-permissions --file /absolute/path/to/backup/dreeve-garmin-sync-data.tar --directory "$SYNC_RESTORE_PATH"
   sudo chown -R "$SYNC_UID_VALUE:$SYNC_GID_VALUE" "$SYNC_RESTORE_PATH"
   ```

   If the checksum was recorded separately instead of in a `.sha256` file, compare
   the new `sha256sum` output manually.

4. Preserve the current directory as an immediate rollback copy, then put the
   restored directory at the exact configured path:

   ```console
   sudo mv -- "$SYNC_DATA_PATH" "${SYNC_DATA_PATH}.before-restore"
   sudo mv -- "$SYNC_RESTORE_PATH" "$SYNC_DATA_PATH"
   ```

5. Start the pinned image and inspect the first scan:

   ```console
   docker compose up -d
   docker compose ps
   docker compose logs --tail=100 garmin-sync
   docker compose exec garmin-sync garmin-sync status
   ```

Keep `.before-restore` until the connector completes a successful scan and Dreeve
imports a delivered file. Then protect it as another backup or remove it using the
same deliberate deletion checks described under [Uninstall](#uninstall).

## Update to an exact version

There are no automatic updates. Read `CHANGELOG.md` and the GitHub release notes,
then update only to an explicit SemVer image tag—never a moving tag.

1. Take a complete backup using the procedure above.
2. Change the `image:` value in `compose.yaml`, for example from one exact version
   to `ghcr.io/filippomb/dreeve-garmin-sync:0.1.0`.
3. Confirm and pull exactly what will run:

   ```console
   docker compose config --images
   docker compose pull garmin-sync
   docker compose images garmin-sync
   ```

4. Replace only the connector service and inspect it:

   ```console
   docker compose up -d --no-deps garmin-sync
   docker compose ps
   docker compose logs --tail=100 garmin-sync
   docker compose exec garmin-sync garmin-sync --version
   docker compose exec garmin-sync garmin-sync status
   ```

5. Confirm a successful scan and the next Dreeve import before retiring the backup.

Pulling an image is not a backup and does not alter the bind-mounted data. Do not
run unattended tools that rewrite the image tag.

## Roll back

Treat persistent data as version-coupled even if a particular release note says no
schema changed. Use the backup taken immediately before that update.

1. Run `docker compose down`.
2. Restore the matching pre-update `/data` backup with the restore procedure above.
3. Put the previous exact image tag back in `compose.yaml`.
4. Run `docker compose pull garmin-sync` and `docker compose up -d`.
5. Verify the CLI version, connector status, logs, and a Dreeve import.

This restores both code and state. Simply changing the image while retaining state
written by a newer release is not a supported rollback unless that release's notes
explicitly say it is safe.

## Reauthenticate

Reauthentication is required when status reports `authentication_required`, logs
say the saved authentication is invalid, or account security changes revoke the
session.

Run the interactive command against the same data mount:

```console
docker compose run --rm -it garmin-sync auth
```

This atomically replaces only the OAuth token; it does not erase the ledger or FIT
archive. The running daemon checks for a changed token while waiting and should
resume within about one minute. If it does not, restart and inspect status:

```console
docker compose restart garmin-sync
docker compose logs --tail=100 garmin-sync
docker compose exec garmin-sync garmin-sync status
```

Do not place the email, password, MFA code, or resulting token in `.env` or a
command line. If authentication repeatedly fails, stop retrying, verify the account
directly with Garmin, and allow any rate-limit cooldown to expire.

## Requeue an archived activity

Use the numeric Garmin activity ID shown in the archive filename
`garmin-<activity-id>.fit`. Requeue never contacts Garmin; it validates and copies
the existing archive into Dreeve's watch directory.

To avoid racing the daemon's process lock:

```console
docker compose stop garmin-sync
docker compose run --rm garmin-sync requeue ACTIVITY_ID
docker compose start garmin-sync
```

If the same file and checksum are already in the watch directory, the command is a
successful no-op. A file with the same name and different content is a collision:
preserve and investigate it rather than overwriting it. Check Dreeve's import log
after requeueing.

## Run a manual scan

The daemon runs immediately at startup, so a restart is usually sufficient. For a
foreground diagnostic scan:

```console
docker compose stop garmin-sync
docker compose run --rm garmin-sync sync-once
docker compose start garmin-sync
```

`sync-once` exits `2` when one or more activities were terminally invalid or
unavailable, even if the rest of the scan completed. It exits `1` for an operational
failure. Repeated manual scans do not bypass a persisted Garmin cooldown and can
make rate limiting worse.

## Health and troubleshooting

Start with these read-only checks:

```console
docker compose config --quiet
docker compose ps
docker compose logs --tail=200 garmin-sync
docker compose exec garmin-sync garmin-sync status
docker compose exec garmin-sync garmin-sync status --healthcheck
```

If the service is not running, obtain persisted status with a one-off container:

```console
docker compose run --rm garmin-sync status
```

Health requires a token, a successful scan, a non-error state, and a recent last
success. “Recent” means within the larger of three hours or twice the configured
interval plus 30 minutes. The initial `ready` state is not healthy until the first
scan succeeds.

Common conditions:

| Symptom | Action |
|---|---|
| Compose reports a required variable | Fill every required `.env` value; the watch path must be absolute and both mount sources must exist. |
| `Permission denied` under `/data` or `/watch` | Make the source directory writable by `GARMIN_SYNC_UID:GARMIN_SYNC_GID`; ensure Dreeve can read the delivered private files. Do not make activity data world-readable. |
| `authentication_required` | Follow [Reauthenticate](#reauthenticate). |
| `rate_limited` with `cooldown_until` | Wait until the reported UTC time. The cooldown grows on repeated rate limits and persists across restarts. Do not loop manual scans or authentication. |
| `degraded` or a transient network error | The daemon retries after increasing delays. Check host networking and Garmin availability, then let it recover. |
| `invalid` activity count or quarantine file | Preserve the quarantine payload privately, note the activity ID from sanitized logs, and file a redacted issue if reproducible. Other activities continue. |
| `unavailable` activity count | Garmin rejected or could not provide that original download. The item is terminal; other activities continue. |
| `Another Garmin synchronization is running` | A scan or requeue owns the lock. Wait, or stop the daemon before a manual operation. |
| Archive or inbox checksum collision | Do not overwrite either file. Back them up, determine which copy belongs to the activity ID, and report a redacted defect if the connector created conflicting data. |
| Enqueued in connector but absent from dashboard | Inspect Dreeve's daemon/import log, `IMPORT_MODE=files`, watch mount, and duplicate handling. This is downstream of connector delivery. |

Logs intentionally suppress upstream Garmin response details. Raising
`GARMIN_SYNC_LOG_LEVEL` can add connector diagnostics, but do not publish logs
without reviewing them for personal paths, times, and activity IDs.

## Disk usage and moving data

The FIT archive grows with every downloaded activity and is retained indefinitely;
quarantine also grows when invalid downloads occur. There is no automatic pruning.
Measure both connector-owned storage and the Dreeve-owned inbox:

```console
du -sh /absolute/path/to/data
du -sh /absolute/path/to/data/archive /absolute/path/to/data/quarantine
du -sh /absolute/path/to/dreeve/watch
```

Do not delete the token or ledger to free space. Removing archive files makes those
activities impossible to requeue from local data and can impair recovery. Prefer
moving the complete data directory to a larger protected filesystem:

1. Back up and run `docker compose down`.
2. Copy the complete directory while preserving ownership and permissions.
3. Change `GARMIN_SYNC_DATA_PATH` in `.env` to the new host path.
4. Run `docker compose config --quiet`, `docker compose up -d`, and the standard
   status/log checks.
5. Remove the old copy only after a successful scan and verified backup.

Dreeve decides when to consume or remove watch files. Manage that directory under
Dreeve's documented retention behavior, not as part of connector archive cleanup.

## Uninstall

Stop and remove only this project's container and network:

```console
docker compose down
```

Because `/data` and `/watch` are bind mounts, this command preserves both. To keep
the connector data, retain its directory or make a final protected backup. You can
remove the cloned repository while pointing the backup record at its Compose file,
`.env`, exact image tag/digest, and data archive.

To permanently delete connector data:

1. Make and verify a final backup, or explicitly decide that the OAuth token,
   ledger, archive, and quarantine are no longer needed.
2. Resolve the configured data path with `realpath` and verify that it is the
   connector-owned directory—not `/`, a home directory, the repository root, or
   Dreeve's watch directory.
3. Use an explicit absolute target with an interactive deletion command, for
   example `sudo rm -rI -- /verified/absolute/path/to/connector-data`.

Deletion is irreversible without a backup. Never recursively delete
`DREEVE_WATCH_DIR`: it belongs to Dreeve and may contain unrelated imports. Removing
the local container image is optional and does not remove persistent bind-mounted
data.
