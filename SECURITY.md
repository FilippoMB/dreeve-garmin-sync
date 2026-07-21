# Security policy

## Supported versions

`dreeve-garmin-sync` is prerelease software. Security fixes are provided on a
best-effort basis for the latest published `0.1.x` release only. There is no support
SLA and no promise of indefinite maintenance.

| Version | Security fixes |
|---|---|
| Latest `0.1.x` prerelease | Best effort |
| Older or unreleased versions | No |

Pin an exact container version, review release notes, and update manually. Images
are not updated automatically.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/FilippoMB/dreeve-garmin-sync/security/advisories/new)
and include:

- the affected version or image digest and host architecture;
- a minimal reproduction and the expected security boundary;
- the likely impact; and
- any suggested mitigation.

Remove Garmin credentials, OAuth tokens, FIT files, GPS coordinates, activity
names, account identifiers, and other personal data. If a secret was exposed,
rotate or revoke it immediately rather than sending it with the report.

Reports will be acknowledged and assessed as maintainer availability permits. Do
not publicly disclose an unpatched issue until a coordinated disclosure date has
been agreed.

## Security model

The container runs as the configured non-root UID/GID with a read-only root
filesystem, dropped Linux capabilities, no-new-privileges, and private data-file
permissions. The only writable persistent mounts are connector data and Dreeve's
watch directory.

This hardening does not make an OAuth token or activity data harmless. Anyone who
can read the data directory, its backups, the Docker socket, or the running
container may be able to access sensitive account or fitness data. Host security,
Docker administration, backup encryption, and Dreeve's own security remain the
operator's responsibility.

The connector relies on an unofficial Garmin consumer API and the third-party
`garminconnect` package. API changes, account controls, or upstream vulnerabilities
can affect availability and security. Review Garmin's
[Terms of Use](https://www.garmin.com/en-GB/legal/terms-of-use/) and each release's
dependency changes.

For the complete data inventory and safe handling guidance, see
[Security and privacy](docs/security-and-privacy.md).
