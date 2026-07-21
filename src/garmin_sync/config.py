"""Validated runtime configuration and private data paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from garmin_sync.errors import ConfigurationError


def _read_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _require_host_identity(name: str, actual_value: int) -> None:
    raw_value = os.getenv(name)
    if not raw_value:
        raise ConfigurationError(f"{name} is required")
    try:
        configured_value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a numeric host ID") from error
    if configured_value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    if configured_value != actual_value:
        raise ConfigurationError(
            f"{name} does not match the container runtime identity"
        )


def _validate_compose_contract() -> None:
    host_watch_dir = os.getenv("DREEVE_WATCH_DIR")
    if not host_watch_dir:
        raise ConfigurationError("DREEVE_WATCH_DIR is required")
    if not Path(host_watch_dir).is_absolute():
        raise ConfigurationError("DREEVE_WATCH_DIR must be an absolute host path")
    _require_host_identity("GARMIN_SYNC_UID", os.geteuid())
    _require_host_identity("GARMIN_SYNC_GID", os.getegid())


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration shared by the CLI and daemon."""

    data_dir: Path
    watch_dir: Path
    interval_minutes: int
    initial_start_date: date
    overlap_days: int
    max_download_bytes: int = 128 * 1024 * 1024

    @classmethod
    def from_environment(cls) -> Settings:
        start_date_value = os.getenv("GARMIN_SYNC_START_DATE")
        if not start_date_value:
            raise ConfigurationError("GARMIN_SYNC_START_DATE is required")
        try:
            initial_start_date = date.fromisoformat(start_date_value)
        except ValueError as error:
            raise ConfigurationError(
                "GARMIN_SYNC_START_DATE must use YYYY-MM-DD"
            ) from error

        _validate_compose_contract()

        return cls(
            data_dir=Path(os.getenv("GARMIN_SYNC_DATA_DIR", "/data")),
            watch_dir=Path(os.getenv("GARMIN_SYNC_WATCH_DIR", "/watch")),
            interval_minutes=_read_int(
                "GARMIN_SYNC_INTERVAL_MINUTES", 60, minimum=1, maximum=10_080
            ),
            initial_start_date=initial_start_date,
            overlap_days=_read_int(
                "GARMIN_SYNC_OVERLAP_DAYS", 2, minimum=0, maximum=30
            ),
        )

    @property
    def token_dir(self) -> Path:
        return self.data_dir / "tokens"

    @property
    def token_file(self) -> Path:
        return self.token_dir / "garmin_tokens.json"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "sync.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "sync.lock"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def staging_dir(self) -> Path:
        return self.watch_dir / ".staging"

    @property
    def healthy_after(self) -> timedelta:
        return max(
            timedelta(hours=3),
            timedelta(minutes=(2 * self.interval_minutes) + 30),
        )

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.token_dir,
            self.state_dir,
            self.archive_dir,
            self.quarantine_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        if not self.watch_dir.is_dir():
            raise ConfigurationError(
                f"Watch directory does not exist: {self.watch_dir}"
            )
        self.staging_dir.mkdir(mode=0o700, exist_ok=True)
        self.staging_dir.chmod(0o700)
