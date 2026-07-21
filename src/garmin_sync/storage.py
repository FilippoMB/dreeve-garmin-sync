"""Private persistence, FIT validation, and atomic Dreeve delivery."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from garmin_sync.config import Settings
from garmin_sync.errors import (
    InvalidActivityPayload,
    StorageError,
    SyncAlreadyRunning,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _from_utc_text(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _link_without_overwrite(staged: Path, destination: Path) -> bool:
    """Publish a complete file atomically, returning false when it already exists."""

    try:
        os.link(staged, destination, follow_symlinks=False)
    except FileExistsError:
        return False
    _fsync_directory(destination.parent)
    return True


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_activity_id(activity: dict[str, Any]) -> str:
    raw_id = activity.get("activityId")
    activity_id = str(raw_id) if raw_id is not None else ""
    if not activity_id.isdigit():
        raise InvalidActivityPayload("Garmin activity has no valid numeric ID")
    return activity_id


def _activity_start_text(activity: dict[str, Any]) -> str:
    for field in ("startTimeLocal", "startTimeGMT"):
        value = activity.get(field)
        if isinstance(value, str) and value:
            return value
    raise InvalidActivityPayload("Garmin activity has no start time")


def _activity_date(activity: dict[str, Any]) -> date:
    raw_value = _activity_start_text(activity).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw_value).date()
    except ValueError as error:
        raise InvalidActivityPayload(
            "Garmin activity has an invalid start time"
        ) from error


def validate_fit(payload: bytes) -> None:
    """Validate the FIT header and its declared data length."""

    if len(payload) < 12:
        raise InvalidActivityPayload("FIT payload is shorter than its header")
    header_size = payload[0]
    if header_size not in {12, 14} or len(payload) < header_size:
        raise InvalidActivityPayload("FIT payload has an unsupported header")
    if payload[8:12] != b".FIT":
        raise InvalidActivityPayload("FIT payload has no FIT signature")
    data_size = int.from_bytes(payload[4:8], byteorder="little")
    expected_size = header_size + data_size
    if len(payload) not in {expected_size, expected_size + 2}:
        raise InvalidActivityPayload("FIT payload length does not match its header")


def extract_fit_payload(download: bytes, max_bytes: int) -> bytes:
    """Return one validated FIT file from Garmin's raw or ZIP response."""

    if not download:
        raise InvalidActivityPayload("Garmin returned an empty activity download")
    if len(download) > max_bytes:
        raise InvalidActivityPayload("Garmin activity download exceeds 128 MiB")

    looks_like_fit = len(download) >= 12 and download[8:12] == b".FIT"
    if looks_like_fit:
        validate_fit(download)
        return download

    try:
        with zipfile.ZipFile(io.BytesIO(download)) as archive:
            candidates: list[zipfile.ZipInfo] = []
            for member in archive.infolist():
                path = PurePosixPath(member.filename)
                unsafe_path = path.is_absolute() or ".." in path.parts
                file_type = (member.external_attr >> 16) & 0o170000
                not_regular = file_type not in {0, stat.S_IFREG}
                encrypted = bool(member.flag_bits & 0x1)
                if unsafe_path or encrypted or (not member.is_dir() and not_regular):
                    raise InvalidActivityPayload("Garmin ZIP contains an unsafe member")
                if member.is_dir() or path.suffix.lower() != ".fit":
                    continue
                if member.file_size > max_bytes:
                    raise InvalidActivityPayload("FIT payload exceeds 128 MiB")
                candidates.append(member)

            if len(candidates) != 1:
                raise InvalidActivityPayload(
                    "Garmin ZIP must contain exactly one FIT file"
                )
            fit_payload = archive.read(candidates[0])
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise InvalidActivityPayload("Garmin download is not a readable ZIP") from error

    if len(fit_payload) > max_bytes:
        raise InvalidActivityPayload("FIT payload exceeds 128 MiB")
    validate_fit(fit_payload)
    return fit_payload


class SyncLock:
    """Non-blocking process lock shared by daemon and manual commands."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> SyncLock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self._descriptor = os.open(self._path, flags, 0o600)
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self._descriptor)
            self._descriptor = None
            raise SyncAlreadyRunning(
                "Another Garmin synchronization is running"
            ) from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    activity_id: str
    start_time: str
    name: str
    state: str
    archive_path: str | None
    sha256: str | None
    attempts: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    health_status: str
    last_attempt_at: datetime | None
    last_successful_scan_at: datetime | None
    last_error: str | None
    cooldown_until: datetime | None
    rate_limit_count: int
    activity_counts: dict[str, int]


class Ledger:
    """SQLite ledger for scan recovery and at-least-once delivery."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS SyncStatus (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    healthStatus TEXT NOT NULL,
                    lastAttemptAt TEXT,
                    lastSuccessfulScanAt TEXT,
                    lastError TEXT,
                    cooldownUntil TEXT,
                    rateLimitCount INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO SyncStatus (singleton, healthStatus)
                VALUES (1, 'not_authenticated');

                CREATE TABLE IF NOT EXISTS ActivitySync (
                    activityId TEXT PRIMARY KEY,
                    startTime TEXT NOT NULL,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    archivePath TEXT,
                    sha256 TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lastError TEXT,
                    discoveredAt TEXT NOT NULL,
                    archivedAt TEXT,
                    enqueuedAt TEXT
                );
                """
            )
        self._database_path.chmod(0o600)

    def begin_attempt(self, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE SyncStatus
                SET healthStatus = 'running', lastAttemptAt = ?, lastError = NULL
                WHERE singleton = 1
                """,
                (_to_utc_text(now),),
            )

    def last_successful_scan(self) -> datetime | None:
        with self._connect() as connection:
            value = connection.execute(
                "SELECT lastSuccessfulScanAt FROM SyncStatus WHERE singleton = 1"
            ).fetchone()[0]
        return _from_utc_text(value)

    def cooldown_remaining(self, now: datetime) -> int:
        with self._connect() as connection:
            value = connection.execute(
                "SELECT cooldownUntil FROM SyncStatus WHERE singleton = 1"
            ).fetchone()[0]
        cooldown_until = _from_utc_text(value)
        if cooldown_until is None or cooldown_until <= now:
            return 0
        return max(1, int((cooldown_until - now).total_seconds()))

    def upsert_discovered(self, activity: dict[str, Any], now: datetime) -> None:
        activity_id = _safe_activity_id(activity)
        start_time = _activity_start_text(activity)
        name = str(activity.get("activityName") or "Untitled activity")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ActivitySync (
                    activityId, startTime, name, state, discoveredAt
                ) VALUES (?, ?, ?, 'discovered', ?)
                ON CONFLICT(activityId) DO UPDATE SET
                    startTime = excluded.startTime,
                    name = excluded.name
                """,
                (activity_id, start_time, name, _to_utc_text(now)),
            )

    def pending_activities(self) -> list[ActivityRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT activityId, startTime, name, state, archivePath, sha256,
                       attempts, lastError
                FROM ActivitySync
                WHERE state IN ('discovered', 'transient_error', 'archived')
                ORDER BY startTime ASC, activityId ASC
                """
            ).fetchall()
        return [ActivityRecord(*row) for row in rows]

    def find_activity(self, activity_id: str) -> ActivityRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT activityId, startTime, name, state, archivePath, sha256,
                       attempts, lastError
                FROM ActivitySync WHERE activityId = ?
                """,
                (activity_id,),
            ).fetchone()
        return ActivityRecord(*row) if row else None

    def mark_archived(
        self, activity_id: str, archive_path: Path, digest: str, now: datetime
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ActivitySync
                SET state = 'archived', archivePath = ?, sha256 = ?,
                    archivedAt = ?, lastError = NULL
                WHERE activityId = ?
                """,
                (str(archive_path), digest, _to_utc_text(now), activity_id),
            )

    def mark_enqueued(self, activity_id: str, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ActivitySync
                SET state = 'enqueued', enqueuedAt = ?, lastError = NULL
                WHERE activityId = ?
                """,
                (_to_utc_text(now), activity_id),
            )

    def mark_activity_error(self, activity_id: str, state: str, message: str) -> None:
        if state not in {"transient_error", "unavailable", "invalid"}:
            raise ValueError(f"Unsupported activity state: {state}")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ActivitySync
                SET state = ?, attempts = attempts + 1, lastError = ?
                WHERE activityId = ?
                """,
                (state, message, activity_id),
            )

    def complete_scan(self, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE SyncStatus
                SET healthStatus = 'healthy', lastSuccessfulScanAt = ?,
                    lastError = NULL, cooldownUntil = NULL, rateLimitCount = 0
                WHERE singleton = 1
                """,
                (_to_utc_text(now),),
            )

    def mark_authenticated(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE SyncStatus
                SET healthStatus = 'ready', lastError = NULL,
                    cooldownUntil = NULL, rateLimitCount = 0
                WHERE singleton = 1
                """
            )

    def record_authentication_required(self, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE SyncStatus
                SET healthStatus = 'authentication_required', lastError = ?
                WHERE singleton = 1
                """,
                (message,),
            )

    def record_transient_error(self, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE SyncStatus
                SET healthStatus = 'degraded', lastError = ?
                WHERE singleton = 1
                """,
                (message,),
            )

    def record_rate_limit(self, now: datetime) -> int:
        with self._connect() as connection:
            current_count = int(
                connection.execute(
                    "SELECT rateLimitCount FROM SyncStatus WHERE singleton = 1"
                ).fetchone()[0]
            )
            next_count = current_count + 1
            delay_seconds = int(min(3_600 * (2 ** (next_count - 1)), 86_400))
            cooldown_until = now + timedelta(seconds=delay_seconds)
            connection.execute(
                """
                UPDATE SyncStatus
                SET healthStatus = 'rate_limited', lastError = ?,
                    cooldownUntil = ?, rateLimitCount = ?
                WHERE singleton = 1
                """,
                (
                    "Garmin rate limit reached",
                    _to_utc_text(cooldown_until),
                    next_count,
                ),
            )
        return delay_seconds

    def snapshot(self) -> StatusSnapshot:
        with self._connect() as connection:
            status_row = connection.execute(
                """
                SELECT healthStatus, lastAttemptAt, lastSuccessfulScanAt,
                       lastError, cooldownUntil, rateLimitCount
                FROM SyncStatus WHERE singleton = 1
                """
            ).fetchone()
            count_rows = connection.execute(
                "SELECT state, COUNT(*) FROM ActivitySync GROUP BY state"
            ).fetchall()
        return StatusSnapshot(
            health_status=status_row[0],
            last_attempt_at=_from_utc_text(status_row[1]),
            last_successful_scan_at=_from_utc_text(status_row[2]),
            last_error=status_row[3],
            cooldown_until=_from_utc_text(status_row[4]),
            rate_limit_count=status_row[5],
            activity_counts={row[0]: row[1] for row in count_rows},
        )


class ActivityFiles:
    """Permanent archive plus atomic, no-overwrite Dreeve handoff."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _validated_archive_path(self, path: Path, activity_id: str) -> Path:
        try:
            path.resolve().relative_to(self._settings.archive_dir.resolve())
        except (OSError, ValueError) as error:
            raise InvalidActivityPayload(
                "Stored archive path is outside the archive directory"
            ) from error
        if path.name != f"garmin-{activity_id}.fit":
            raise InvalidActivityPayload(
                "Stored archive path does not match the activity ID"
            )
        return path

    def archive_path(self, activity: dict[str, Any] | ActivityRecord) -> Path:
        if isinstance(activity, ActivityRecord):
            activity_id = activity.activity_id
            if activity.archive_path is not None:
                return self._validated_archive_path(
                    Path(activity.archive_path), activity_id
                )
            raw_date = activity.start_time
            try:
                activity_day = datetime.fromisoformat(
                    raw_date.replace("Z", "+00:00")
                ).date()
            except ValueError as error:
                raise InvalidActivityPayload(
                    "Stored activity has an invalid start time"
                ) from error
        else:
            activity_id = _safe_activity_id(activity)
            activity_day = _activity_date(activity)
        return (
            self._settings.archive_dir
            / str(activity_day.year)
            / f"{activity_day.month:02d}"
            / f"garmin-{activity_id}.fit"
        )

    def store_archive(
        self, activity: dict[str, Any] | ActivityRecord, fit_payload: bytes
    ) -> tuple[Path, str]:
        validate_fit(fit_payload)
        destination = self.archive_path(activity)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.parent.chmod(0o700)
        digest = _sha256(fit_payload)

        if destination.exists():
            existing_payload = destination.read_bytes()
            if _sha256(existing_payload) != digest:
                raise StorageError(f"Archive collision for {destination.name}")
            return destination, digest

        staged = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
        try:
            _write_private_file(staged, fit_payload)
            if not _link_without_overwrite(staged, destination):
                existing_payload = destination.read_bytes()
                if _sha256(existing_payload) != digest:
                    raise StorageError(f"Archive collision for {destination.name}")
        finally:
            staged.unlink(missing_ok=True)
        destination.chmod(0o600)
        return destination, digest

    def reuse_archive(self, activity: ActivityRecord) -> tuple[Path, str] | None:
        path = self.archive_path(activity)
        if not path.is_file():
            matches = list(
                self._settings.archive_dir.glob(
                    f"*/*/garmin-{activity.activity_id}.fit"
                )
            )
            if not matches:
                return None
            if len(matches) > 1:
                raise StorageError(
                    f"Multiple archived FIT files exist for {activity.activity_id}"
                )
            path = self._validated_archive_path(matches[0], activity.activity_id)
        payload = path.read_bytes()
        validate_fit(payload)
        digest = _sha256(payload)
        if activity.sha256 is not None and digest != activity.sha256:
            raise InvalidActivityPayload(
                f"Archived FIT checksum mismatch for {activity.activity_id}"
            )
        return path, digest

    def quarantine(self, activity_id: str, download: bytes) -> Path:
        now = utc_now()
        destination_dir = (
            self._settings.quarantine_dir / str(now.year) / f"{now.month:02d}"
        )
        destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination_dir.chmod(0o700)
        destination = destination_dir / (
            f"garmin-{activity_id}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}.bin"
        )
        _write_private_file(destination, download)
        _fsync_directory(destination_dir)
        return destination

    def enqueue(self, activity_id: str, archive_path: Path, digest: str) -> bool:
        payload = archive_path.read_bytes()
        if _sha256(payload) != digest:
            raise StorageError(f"Archived FIT checksum mismatch for {activity_id}")
        validate_fit(payload)

        final_path = self._settings.watch_dir / f"garmin-{activity_id}.fit"
        if final_path.exists():
            if _sha256(final_path.read_bytes()) != digest:
                raise StorageError(f"Dreeve inbox collision for {final_path.name}")
            return False

        staged = self._settings.staging_dir / (
            f"garmin-{activity_id}-{uuid.uuid4().hex}.part"
        )
        try:
            _write_private_file(staged, payload)
            published = _link_without_overwrite(staged, final_path)
            if not published and final_path.exists():
                if _sha256(final_path.read_bytes()) != digest:
                    raise StorageError(f"Dreeve inbox collision for {final_path.name}")
            return published
        finally:
            staged.unlink(missing_ok=True)

    def clean_stale_staging_files(self, now: datetime) -> None:
        cutoff = now.timestamp() - timedelta(hours=24).total_seconds()
        for candidate in self._settings.staging_dir.glob("garmin-*.part"):
            try:
                if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except FileNotFoundError:
                continue

    @staticmethod
    def status_as_json(snapshot: StatusSnapshot) -> str:
        return json.dumps(
            {
                "health": snapshot.health_status,
                "last_attempt_at": (
                    _to_utc_text(snapshot.last_attempt_at)
                    if snapshot.last_attempt_at
                    else None
                ),
                "last_successful_scan_at": (
                    _to_utc_text(snapshot.last_successful_scan_at)
                    if snapshot.last_successful_scan_at
                    else None
                ),
                "last_error": snapshot.last_error,
                "cooldown_until": (
                    _to_utc_text(snapshot.cooldown_until)
                    if snapshot.cooldown_until
                    else None
                ),
                "rate_limit_count": snapshot.rate_limit_count,
                "activities": snapshot.activity_counts,
            },
            indent=2,
            sort_keys=True,
        )
