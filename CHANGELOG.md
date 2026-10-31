# Changelog

All notable changes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Update Garmin Connect to 0.3.7 and refresh the matching hashed runtime lock.
- Update development tools while retaining the established Ruff rule set.

### Fixed

- Refresh the locked certificate bundle and HTTP client to address reported
  vulnerabilities, and reject container builds with inconsistent dependencies.
- Allow the CI secret scanner to read pull-request commits.

## [0.1.0] - 2026-07-21

First public prerelease.

### Added

- Standalone Docker Compose deployment for Dreeve v5 file-import mode on Linux
  `amd64` and `arm64`.
- Interactive Garmin authentication that persists a refreshable OAuth token but
  not the account email or password.
- Scheduled and one-shot synchronization with overlap scanning, SQLite-backed
  deduplication, FIT validation, private archives, atomic watch delivery, and
  restart recovery.
- Backoff for transient failures and persisted cooldowns for Garmin rate limits.
- Status, container health checking, and archive requeue commands.
- Installation, privacy, backup/restore, update/rollback, troubleshooting, and
  uninstall documentation.

[Unreleased]: https://github.com/FilippoMB/dreeve-garmin-sync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FilippoMB/dreeve-garmin-sync/releases/tag/v0.1.0
