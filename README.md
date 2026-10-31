# dreeve-garmin-sync

`dreeve-garmin-sync` is an unofficial, standalone Garmin Connect synchronizer for
[Dreeve](https://github.com/dreeveapp/dreeve). It periodically downloads original
Garmin activity files, validates and archives each FIT, and atomically places a copy
in Dreeve's file-import watch directory.

This project is not affiliated with, endorsed by, or supported by Garmin or Dreeve.
It uses Garmin's undocumented consumer API through `garminconnect`; that API can
change or stop working without notice. Review Garmin's
[Terms of Use](https://www.garmin.com/en-GB/legal/terms-of-use/) before using it.

> **Prerelease:** `v0.1.0` is intended for early adopters. Pin the exact image
> version, keep backups, and review the release notes before updating.

## What it does

- Runs an immediate scan at startup, then scans on a configurable interval.
- Refreshes a saved Garmin OAuth session without retaining the account email or
  password.
- Retries temporary failures, observes persisted rate-limit cooldowns, and resumes
  after a process or host restart.
- Deduplicates activities in a SQLite ledger and uses checksums plus no-overwrite,
  atomic delivery.
- Keeps a private FIT archive so an activity can be delivered to Dreeve again
  without another Garmin download.
- Exposes machine-readable status and a Docker health check.

Once authenticated, normal synchronization is unattended. Authentication may
occasionally need to be repeated if Garmin invalidates the saved session. Backups,
upgrades, rollback, and storage management are deliberately operator-controlled;
the project does not update or back itself up.

## Requirements

- A Linux host using `amd64` or `arm64`.
- Docker Engine with the Docker Compose plugin.
- A running Dreeve v5 installation configured with `IMPORT_MODE=files`, including
  its daemon and an existing host `watch/` directory. See Dreeve's
  [file-import documentation](https://github.com/dreeveapp/dreeve/blob/v5.0.2/docs/importing/file-import.md).
- The nonzero numeric UID and GID that should own connector data and delivered FIT
  files. Use the same non-root identity as Dreeve's daemon, or otherwise ensure
  that Dreeve can read files created with private permissions.

The supported installation is the supplied standalone Compose service. PyPI,
bare-Python/systemd, Kubernetes, Windows, and NAS-specific packages are outside the
v0.1 support scope.

## Quick start

1. Clone the repository and create the local configuration:

   ```console
   git clone https://github.com/FilippoMB/dreeve-garmin-sync.git
   cd dreeve-garmin-sync
   cp .env.example .env
   ```

2. Edit `.env`. Set all required values, especially:

   - `DREEVE_WATCH_DIR` to the **absolute host path** of Dreeve's existing
     `watch/` directory;
   - `GARMIN_SYNC_START_DATE` to the earliest date to import, in `YYYY-MM-DD`
     form; and
   - `GARMIN_SYNC_UID` and `GARMIN_SYNC_GID` to the host numeric IDs chosen above.

   `GARMIN_SYNC_DATA_PATH` defaults to `./data`. Both bind-mount source directories
   must already exist; Compose will not create them silently.

3. Create and protect the connector data directory. If the configured UID/GID are
   not your current user, use `sudo` for the ownership change:

   ```console
   mkdir -p ./data
   chmod 0700 ./data
   chown "$(id -u):$(id -g)" ./data
   test -d /absolute/path/to/dreeve/watch
   ```

   Replace the example watch path and, when needed, the `chown` IDs so they match
   `.env`. If `GARMIN_SYNC_DATA_PATH` is not `./data`, create that exact directory
   instead.

4. Validate the resolved Compose configuration, then authenticate interactively:

   ```console
   docker compose config --quiet
   docker compose run --rm -it garmin-sync auth
   ```

   The prompt accepts the Garmin account email, password, and MFA code if required.
   The email and password are used only for this authentication attempt. A
   refreshable OAuth token is stored under the protected data directory.

5. Start the daemon:

   ```console
   docker compose up -d
   ```

6. Verify both sides of the handoff:

   ```console
   docker compose ps
   docker compose exec garmin-sync garmin-sync status
   docker compose logs --tail=100 garmin-sync
   ```

   Then check Dreeve's import log (commonly `docker compose logs --tail=100 daemon`
   from the Dreeve installation) and confirm that the first activity was imported.
   Use the actual daemon service name if the Dreeve Compose file was customized.

## Choosing the start date

`GARMIN_SYNC_START_DATE` is required and controls the intentional historical
backfill. Choose either:

- the date of the earliest Garmin activity you want Dreeve to receive; or
- the date of the latest activity already imported into Dreeve, minus a small
  overlap.

The first scan covers that date through today. Later scans rewind from the last
successful scan by `GARMIN_SYNC_OVERLAP_DAYS` (two days by default) without going
earlier than the configured start date. Repeated listings do not cause repeated
downloads or deliveries: the connector records Garmin activity IDs, and Dreeve's
file importer also performs its own duplicate detection. A wider initial backfill
can take time and may increase the chance of Garmin rate limiting; let the persisted
cooldown expire instead of repeatedly forcing scans.

## Configuration

| Variable | Required | Default | Meaning |
|---|---:|---|---|
| `DREEVE_WATCH_DIR` | Yes | none | Absolute host path to Dreeve's existing file-import watch directory. |
| `GARMIN_SYNC_DATA_PATH` | No | `./data` | Connector-owned persistent host directory. |
| `GARMIN_SYNC_START_DATE` | Yes | none | Earliest scan date, exactly `YYYY-MM-DD`. |
| `GARMIN_SYNC_INTERVAL_MINUTES` | No | `60` | Minutes between successful scans (`1`–`10080`). |
| `GARMIN_SYNC_OVERLAP_DAYS` | No | `2` | Days rescanned after the first success (`0`–`30`). |
| `GARMIN_SYNC_UID` | Yes | none | Nonzero numeric runtime user and file owner. |
| `GARMIN_SYNC_GID` | Yes | none | Nonzero numeric runtime group and file group. |
| `TZ` | No | `Etc/UTC` | Container timezone for operator-facing timestamps. |
| `GARMIN_SYNC_LOG_LEVEL` | No | `INFO` | Python log level, such as `INFO` or `DEBUG`. |

Changing the start date later does not reset the persistent ledger. Existing
activity IDs remain deduplicated; only previously unseen activities are added.

## Commands

The image provides one supported CLI:

```text
garmin-sync auth
garmin-sync daemon
garmin-sync sync-once
garmin-sync status [--healthcheck]
garmin-sync requeue ACTIVITY_ID
garmin-sync --version
```

The CLI, Compose variables, and `/data` plus `/watch` mount contract are the public
interfaces. The Python modules are implementation details and do not provide a
supported library API.

With the daemon running, inspect it with:

```console
docker compose exec garmin-sync garmin-sync status
docker compose exec garmin-sync garmin-sync status --healthcheck
```

`status` prints JSON. `--healthcheck` also exits nonzero when authentication is
missing, the service is degraded or rate-limited, no scan has succeeded yet, or the
last success is stale. The container can therefore be `starting` or `unhealthy`
between authentication and its first successful scan.

The daemon is the normal operating mode. Before running `sync-once` or `requeue`
manually, stop the service to avoid the single-process lock; the exact procedures
are in the [operations guide](docs/operations.md).

## Data and support boundary

The connector owns everything under `/data` in the container: the OAuth token,
SQLite ledger, permanent FIT archive, and quarantined invalid downloads. It writes
only complete, validated `garmin-<activity-id>.fit` files and its hidden staging
directory under `/watch`.

A synchronization is successful at the connector boundary when a validated FIT is
present in Dreeve's watch directory. Dreeve owns everything after that point. If the
connector reports an activity as enqueued but it does not appear on the dashboard,
inspect Dreeve's daemon/import log and configuration.

FIT files can contain location, route, time, device, and health-related data. The
OAuth token can access the Garmin account. Protect `/data`, `/watch`, logs, and every
backup accordingly. See [Security and privacy](docs/security-and-privacy.md).

## Operations and help

- [Operations guide](docs/operations.md): backup/restore, updates, rollback,
  reauthentication, requeue, health, disk usage, and uninstall.
- [Security and privacy](docs/security-and-privacy.md): local data, network access,
  credential handling, and vulnerability reporting.
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

For connector defects, open a GitHub issue with secrets, account data, activity
files, and identifying log details removed. For security vulnerabilities, use the
private reporting route in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
