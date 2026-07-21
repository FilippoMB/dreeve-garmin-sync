"""Synchronization workflow and long-running scheduler."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from garmin_sync.config import Settings
from garmin_sync.errors import (
    ActivityUnavailable,
    AuthenticationRequired,
    CooldownActive,
    GarminSyncError,
    InvalidActivityPayload,
    RateLimited,
    StorageError,
    TransientGarminError,
)
from garmin_sync.garmin_client import ActivityClient, PythonGarminClient
from garmin_sync.storage import (
    ActivityFiles,
    ActivityRecord,
    Ledger,
    SyncLock,
    extract_fit_payload,
    utc_now,
)

LOGGER = logging.getLogger(__name__)

ClientFactory = Callable[[Path], ActivityClient]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class SyncResult:
    listed: int = 0
    archived: int = 0
    enqueued: int = 0
    already_queued: int = 0
    invalid: int = 0
    unavailable: int = 0

    @property
    def has_terminal_errors(self) -> bool:
        return self.invalid > 0 or self.unavailable > 0


class Synchronizer:
    """Coordinate one recoverable Garmin scan and Dreeve delivery pass."""

    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        files: ActivityFiles,
        *,
        client_factory: ClientFactory = PythonGarminClient.from_tokens,
        clock: Clock = utc_now,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._files = files
        self._client_factory = client_factory
        self._clock = clock

    def sync_once(self) -> SyncResult:
        with SyncLock(self._settings.lock_path):
            started_at = self._clock()
            cooldown = self._ledger.cooldown_remaining(started_at)
            if cooldown:
                raise CooldownActive(cooldown)
            self._ledger.begin_attempt(started_at)

            try:
                result = self._run_scan(started_at)
            except AuthenticationRequired as error:
                self._ledger.record_authentication_required(str(error))
                raise
            except RateLimited:
                self._ledger.record_rate_limit(self._clock())
                raise
            except GarminSyncError as error:
                self._ledger.record_transient_error(str(error))
                raise
            except Exception:
                self._ledger.record_transient_error(
                    "Unexpected synchronization failure"
                )
                raise

            self._files.clean_stale_staging_files(self._clock())
            self._ledger.complete_scan(self._clock())
            return result

    def _run_scan(self, started_at: datetime) -> SyncResult:
        client = self._client_factory(self._settings.token_dir)
        start_date = self._scan_start_date()
        end_date = started_at.date()
        if start_date > end_date:
            start_date = end_date
        LOGGER.info(
            "Scanning Garmin activities from %s through %s", start_date, end_date
        )

        activities = client.list_activities(start_date, end_date)
        for activity in activities:
            if not isinstance(activity, dict):
                raise TransientGarminError("Garmin returned an invalid activity entry")
            try:
                self._ledger.upsert_discovered(activity, started_at)
            except InvalidActivityPayload as error:
                raise TransientGarminError(
                    "Garmin returned incomplete activity metadata"
                ) from error

        counters = {
            "listed": len(activities),
            "archived": 0,
            "enqueued": 0,
            "already_queued": 0,
            "invalid": 0,
            "unavailable": 0,
        }
        for record in self._ledger.pending_activities():
            self._process_activity(client, record, counters)
        return SyncResult(**counters)

    def _scan_start_date(self) -> date:
        last_scan = self._ledger.last_successful_scan()
        if last_scan is None:
            return self._settings.initial_start_date
        overlap = timedelta(days=self._settings.overlap_days)
        return max(self._settings.initial_start_date, (last_scan - overlap).date())

    def _process_activity(
        self,
        client: ActivityClient,
        record: ActivityRecord,
        counters: dict[str, int],
    ) -> None:
        try:
            archive = self._existing_archive(record)
        except InvalidActivityPayload as error:
            self._ledger.mark_activity_error(record.activity_id, "invalid", str(error))
            counters["invalid"] += 1
            LOGGER.warning("Archived activity %s is invalid", record.activity_id)
            return
        if archive is None:
            try:
                download = client.download_original(record.activity_id)
            except ActivityUnavailable as error:
                self._ledger.mark_activity_error(
                    record.activity_id, "unavailable", str(error)
                )
                counters["unavailable"] += 1
                LOGGER.warning("Activity %s is unavailable", record.activity_id)
                return
            except TransientGarminError as error:
                self._ledger.mark_activity_error(
                    record.activity_id, "transient_error", str(error)
                )
                raise

            try:
                fit_payload = extract_fit_payload(
                    download, self._settings.max_download_bytes
                )
                archive = self._files.store_archive(record, fit_payload)
            except InvalidActivityPayload as error:
                quarantine_path = self._files.quarantine(record.activity_id, download)
                self._ledger.mark_activity_error(
                    record.activity_id,
                    "invalid",
                    f"{error}; quarantined as {quarantine_path.name}",
                )
                counters["invalid"] += 1
                LOGGER.warning(
                    "Activity %s has an invalid download", record.activity_id
                )
                return

            self._ledger.mark_archived(
                record.activity_id, archive[0], archive[1], self._clock()
            )
            counters["archived"] += 1

        archive_path, digest = archive
        published = self._files.enqueue(record.activity_id, archive_path, digest)
        self._ledger.mark_enqueued(record.activity_id, self._clock())
        if published:
            counters["enqueued"] += 1
            LOGGER.info("Queued Garmin activity %s for Dreeve", record.activity_id)
        else:
            counters["already_queued"] += 1
            LOGGER.info("Garmin activity %s was already queued", record.activity_id)

    def _existing_archive(self, record: ActivityRecord) -> tuple[Path, str] | None:
        archive = self._files.reuse_archive(record)
        if archive is not None:
            self._ledger.mark_archived(
                record.activity_id, archive[0], archive[1], self._clock()
            )
        return archive

    def requeue(self, activity_id: str) -> bool:
        if not activity_id.isdigit():
            raise InvalidActivityPayload("Activity ID must be numeric")
        with SyncLock(self._settings.lock_path):
            record = self._ledger.find_activity(activity_id)
            if record is None:
                raise StorageError(f"Activity {activity_id} is not in the sync ledger")
            archive = self._files.reuse_archive(record)
            if archive is None:
                raise StorageError(f"Activity {activity_id} has no archived FIT file")
            published = self._files.enqueue(activity_id, archive[0], archive[1])
            self._ledger.mark_archived(
                activity_id, archive[0], archive[1], self._clock()
            )
            self._ledger.mark_enqueued(activity_id, self._clock())
            return published


def _wait_for_token_change(
    stop_event: threading.Event,
    token_file: Path,
    previous_mtime: int | None,
) -> None:
    while not stop_event.wait(60):
        try:
            current_mtime = token_file.stat().st_mtime_ns
        except FileNotFoundError:
            current_mtime = None
        if current_mtime is not None and current_mtime != previous_mtime:
            return


def run_daemon(
    synchronizer: Synchronizer,
    settings: Settings,
    ledger: Ledger,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """Run immediately and then on the configured interval until stopped."""

    event = stop_event or threading.Event()
    if stop_event is None:
        signal.signal(signal.SIGTERM, lambda *_: event.set())
        signal.signal(signal.SIGINT, lambda *_: event.set())

    transient_delays = (300, 900, 3_600)
    transient_failures = 0
    LOGGER.info(
        "Garmin sync daemon started; normal interval is %d minutes",
        settings.interval_minutes,
    )

    while not event.is_set():
        try:
            result = synchronizer.sync_once()
        except AuthenticationRequired as error:
            LOGGER.error("%s", error)
            try:
                token_mtime = settings.token_file.stat().st_mtime_ns
            except FileNotFoundError:
                token_mtime = None
            _wait_for_token_change(event, settings.token_file, token_mtime)
            continue
        except CooldownActive as error:
            LOGGER.warning("%s", error)
            event.wait(error.seconds_remaining)
            continue
        except RateLimited:
            delay = ledger.cooldown_remaining(utc_now()) or 3_600
            LOGGER.warning("Garmin rate limit reached; retrying in %d seconds", delay)
            event.wait(delay)
            continue
        except (TransientGarminError, StorageError) as error:
            delay = transient_delays[min(transient_failures, len(transient_delays) - 1)]
            transient_failures += 1
            LOGGER.error("%s; retrying in %d seconds", error, delay)
            event.wait(delay)
            continue
        except GarminSyncError as error:
            LOGGER.error("%s", error)
            event.wait(settings.interval_minutes * 60)
            continue
        except Exception:
            LOGGER.exception("Unexpected Garmin synchronization failure")
            return 1

        transient_failures = 0
        LOGGER.info(
            "Garmin scan complete: listed=%d archived=%d enqueued=%d "
            "already_queued=%d invalid=%d unavailable=%d",
            result.listed,
            result.archived,
            result.enqueued,
            result.already_queued,
            result.invalid,
            result.unavailable,
        )
        event.wait(settings.interval_minutes * 60)

    LOGGER.info("Garmin sync daemon stopped")
    return 0


def status_is_healthy(settings: Settings, ledger: Ledger, now: datetime) -> bool:
    snapshot = ledger.snapshot()
    if not settings.token_file.is_file():
        return False
    if snapshot.health_status in {
        "authentication_required",
        "not_authenticated",
        "rate_limited",
        "degraded",
    }:
        return False
    if snapshot.last_successful_scan_at is None:
        return False
    return (
        now.astimezone(UTC) - snapshot.last_successful_scan_at <= settings.healthy_after
    )
