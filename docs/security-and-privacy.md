# Security and privacy

`dreeve-garmin-sync` handles sensitive authentication and fitness data. This page
describes what the connector stores and where its responsibility ends.

## Stored data

Interactive authentication sends the entered email, password, and optional MFA
code to Garmin through the `garminconnect` client. The connector does **not** write
the email or password to `.env`, its database, or its logs. It does persist a
refreshable OAuth token at `/data/tokens/garmin_tokens.json`; treat that token as a
credential capable of accessing the Garmin account.

The connector also persists:

- activity ID, start time, name, state, checksum, and error state in SQLite;
- original validated FIT files under `/data/archive`;
- invalid raw downloads under `/data/quarantine`; and
- delivered FIT copies in Dreeve's watch directory.

FIT and quarantine files may contain GPS tracks, home/work locations, timestamps,
device identifiers, and health or training information. Activity metadata alone can
also be identifying.

## Network access and third parties

At runtime, the connector communicates with Garmin Connect to authenticate, list
activities, refresh the session, and download original activity files. Docker may
contact GitHub Container Registry only when an operator explicitly pulls an image.
The connector has no analytics or telemetry service of its own.

Garmin's consumer API is undocumented and this project is not affiliated with or
endorsed by Garmin or Dreeve. Availability, response formats, authentication, and
account policies can change without notice. The third-party `garminconnect`
dependency implements that access. Operators should review dependency updates and
Garmin's [Terms of Use](https://www.garmin.com/en-GB/legal/terms-of-use/).

## Local protections

The supplied container is designed to run as the configured non-root UID/GID with
a read-only root filesystem, no Linux capabilities, no-new-privileges, bounded
Docker logs, and writable mounts limited to `/data` and `/watch`. Connector-created
private directories use mode `0700`; the token, state, archive, quarantine, and
delivered activity files use private file permissions.

Use a UID/GID that both owns the connector data and allows Dreeve's daemon to read
delivered files. Do not solve permission errors with world-readable or
world-writable modes.

These controls do not protect against a host administrator, Docker-socket access,
malware running as the configured UID, or an unencrypted backup. Restrict host SSH
access, Docker administration, filesystem snapshots, monitoring exports, and
backup access. Encrypt backups at rest and in transit.

## Logs and support material

The adapter suppresses the upstream client's response-body logging and emits
sanitized error messages. Normal logs can still reveal activity IDs, scan dates,
timing, filenames, and personal filesystem paths. Status output contains scan times,
error summaries, and counts by activity state.

Before sharing diagnostics:

- replace activity IDs, timestamps, names, hostnames, and absolute paths;
- never attach `.env`, `garmin_tokens.json`, SQLite databases, FIT files,
  quarantine files, backups, or Docker environment dumps; and
- reproduce with synthetic data where possible.

Send suspected vulnerabilities through the private route in
[SECURITY.md](../SECURITY.md), not a public issue.

## Credential and data lifecycle

- **Reauthentication:** `docker compose run --rm -it garmin-sync auth` atomically
  replaces the stored OAuth token. It retains the ledger and archive.
- **Suspected token exposure:** stop the connector, use Garmin account security
  controls to revoke affected sessions where available, change the password when
  appropriate, remove exposed copies and backups, and authenticate again. Assume
  downloaded copies of an exposed token remain sensitive even after local deletion.
- **Backup:** stop the service and copy all of `/data` as one protected unit. Follow
  the [operations guide](operations.md#backup).
- **Uninstall:** `docker compose down` preserves bind-mounted data. Delete `/data`
  only after checking its exact path and backup decision. Never delete Dreeve's
  watch directory as part of uninstalling this connector.

No retention period or automatic deletion is imposed. The operator controls the
archive, quarantine, watch directory, backups, Docker logs, and eventual erasure.

## Responsibility boundary

The connector's supported handoff ends when a complete, validated FIT is atomically
present in the configured Dreeve watch directory. Dreeve controls import,
deduplication, dashboard storage, display, retention, and access after that point.
Garmin controls the upstream account and API. Apply each project's security and
privacy guidance to its part of the data flow.
