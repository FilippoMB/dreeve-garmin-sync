from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import garmin_sync.storage as storage_module
from garmin_sync.errors import (
    InvalidActivityPayload,
    StorageError,
    SyncAlreadyRunning,
)
from garmin_sync.storage import (
    ActivityFiles,
    ActivityRecord,
    Ledger,
    SyncLock,
    extract_fit_payload,
    validate_fit,
)

from helpers import FIXED_NOW, activity, make_fit, make_settings


def zip_payload(entries: list[tuple[str, bytes]], *, compressed: bool = False) -> bytes:
    output = io.BytesIO()
    compression = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return output.getvalue()


def record(
    activity_id: str = "12345",
    *,
    start_time: str = "2024-01-02T08:30:00",
    state: str = "discovered",
    archive_path: str | None = None,
    digest: str | None = None,
) -> ActivityRecord:
    return ActivityRecord(
        activity_id=activity_id,
        start_time=start_time,
        name="Morning run",
        state=state,
        archive_path=archive_path,
        sha256=digest,
        attempts=0,
        last_error=None,
    )


class FitPayloadTests(unittest.TestCase):
    def test_accepts_raw_fit_and_single_fit_inside_zip(self) -> None:
        fit = make_fit()

        self.assertEqual(extract_fit_payload(fit, 1024), fit)
        self.assertEqual(
            extract_fit_payload(zip_payload([("nested/activity.FIT", fit)]), 1024),
            fit,
        )

    def test_rejects_unsafe_zip_path(self) -> None:
        payload = zip_payload([("../escaped.fit", make_fit())])

        with self.assertRaisesRegex(InvalidActivityPayload, "unsafe"):
            extract_fit_payload(payload, 4096)

    def test_rejects_multiple_or_missing_fit_members(self) -> None:
        cases = (
            zip_payload([("one.fit", make_fit()), ("two.fit", make_fit(b"two"))]),
            zip_payload([("readme.txt", b"not an activity")]),
        )
        for payload in cases:
            with self.subTest(size=len(payload)):
                with self.assertRaisesRegex(InvalidActivityPayload, "exactly one"):
                    extract_fit_payload(payload, 4096)

    def test_rejects_corrupt_or_empty_download(self) -> None:
        for payload in (b"", b"not a zip archive", b"PK\x03\x04truncated"):
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidActivityPayload):
                    extract_fit_payload(payload, 4096)

    def test_rejects_oversize_raw_download_and_expanded_zip_member(self) -> None:
        with self.assertRaisesRegex(InvalidActivityPayload, "exceeds"):
            extract_fit_payload(b"x" * 513, 512)

        expanded_fit = make_fit(b"x" * 1000)
        compressed = zip_payload([("activity.fit", expanded_fit)], compressed=True)
        self.assertLess(len(compressed), 500)
        with self.assertRaisesRegex(InvalidActivityPayload, "exceeds"):
            extract_fit_payload(compressed, 500)

    def test_validate_fit_rejects_bad_signature_header_and_declared_length(
        self,
    ) -> None:
        malformed = (
            b"too short",
            bytes([13]) + b"\0" * 20,
            make_fit().replace(b".FIT", b"NOPE", 1),
            make_fit() + b"unexpected",
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidActivityPayload):
                    validate_fit(payload)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = make_settings(Path(self.temporary_directory.name))
        self.ledger = Ledger(self.settings.database_path)

    def test_database_is_private_and_activity_lifecycle_is_persistent(self) -> None:
        self.assertEqual(
            stat.S_IMODE(self.settings.database_path.stat().st_mode), 0o600
        )
        self.assertEqual(self.ledger.snapshot().health_status, "not_authenticated")

        item = activity()
        self.ledger.upsert_discovered(item, FIXED_NOW)
        discovered = self.ledger.find_activity("12345")
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.state, "discovered")
        self.assertEqual(
            [entry.activity_id for entry in self.ledger.pending_activities()], ["12345"]
        )

        archive_path = self.settings.archive_dir / "2024/01/garmin-12345.fit"
        self.ledger.mark_archived("12345", archive_path, "abc123", FIXED_NOW)
        self.assertEqual(self.ledger.find_activity("12345").state, "archived")
        self.ledger.mark_enqueued("12345", FIXED_NOW)
        self.assertEqual(self.ledger.find_activity("12345").state, "enqueued")
        self.assertEqual(self.ledger.pending_activities(), [])
        self.assertEqual(self.ledger.snapshot().activity_counts, {"enqueued": 1})

        reopened = Ledger(self.settings.database_path)
        self.assertEqual(reopened.find_activity("12345").state, "enqueued")

    def test_upsert_deduplicates_metadata_without_resetting_state(self) -> None:
        self.ledger.upsert_discovered(activity(name="Old name"), FIXED_NOW)
        self.ledger.mark_enqueued("12345", FIXED_NOW)
        self.ledger.upsert_discovered(
            activity(name="Updated name"), FIXED_NOW + timedelta(minutes=5)
        )

        item = self.ledger.find_activity("12345")
        self.assertEqual(item.name, "Updated name")
        self.assertEqual(item.state, "enqueued")
        self.assertEqual(self.ledger.snapshot().activity_counts, {"enqueued": 1})

    def test_error_states_control_pending_retries(self) -> None:
        ids_and_states = (
            ("1", "transient_error"),
            ("2", "invalid"),
            ("3", "unavailable"),
        )
        for activity_id, state in ids_and_states:
            self.ledger.upsert_discovered(activity(activity_id), FIXED_NOW)
            self.ledger.mark_activity_error(activity_id, state, f"{state} message")

        self.assertEqual(
            [item.activity_id for item in self.ledger.pending_activities()], ["1"]
        )
        for activity_id, state in ids_and_states:
            item = self.ledger.find_activity(activity_id)
            self.assertEqual(item.state, state)
            self.assertEqual(item.attempts, 1)
            self.assertEqual(item.last_error, f"{state} message")

    def test_scan_cursor_and_rate_limit_cooldown_transitions(self) -> None:
        self.ledger.begin_attempt(FIXED_NOW)
        self.assertEqual(self.ledger.snapshot().health_status, "running")
        completed_at = FIXED_NOW + timedelta(minutes=1)
        self.ledger.complete_scan(completed_at)
        self.assertEqual(self.ledger.last_successful_scan(), completed_at)
        self.assertEqual(self.ledger.snapshot().health_status, "healthy")

        self.assertEqual(self.ledger.record_rate_limit(FIXED_NOW), 3600)
        self.assertEqual(self.ledger.cooldown_remaining(FIXED_NOW), 3600)
        self.assertEqual(self.ledger.record_rate_limit(FIXED_NOW), 7200)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.health_status, "rate_limited")
        self.assertEqual(snapshot.rate_limit_count, 2)
        self.assertEqual(self.ledger.cooldown_remaining(FIXED_NOW), 7200)
        self.assertEqual(
            self.ledger.cooldown_remaining(FIXED_NOW + timedelta(hours=2)), 0
        )

        self.ledger.complete_scan(FIXED_NOW + timedelta(hours=3))
        snapshot = self.ledger.snapshot()
        self.assertIsNone(snapshot.cooldown_until)
        self.assertEqual(snapshot.rate_limit_count, 0)

    def test_rejects_unknown_activity_error_state(self) -> None:
        self.ledger.upsert_discovered(activity(), FIXED_NOW)
        with self.assertRaises(ValueError):
            self.ledger.mark_activity_error("12345", "mystery", "bad state")


class ActivityFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = make_settings(Path(self.temporary_directory.name))
        self.files = ActivityFiles(self.settings)
        self.fit = make_fit()

    def test_archive_is_private_idempotent_and_never_overwritten(self) -> None:
        archive_path, digest = self.files.store_archive(record(), self.fit)
        original_inode = archive_path.stat().st_ino

        self.assertEqual(
            archive_path,
            self.settings.archive_dir / "2024/01/garmin-12345.fit",
        )
        self.assertEqual(digest, hashlib.sha256(self.fit).hexdigest())
        self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o600)

        same_path, same_digest = self.files.store_archive(record(), self.fit)
        self.assertEqual(same_path.stat().st_ino, original_inode)
        self.assertEqual(same_digest, digest)
        with self.assertRaisesRegex(StorageError, "Archive collision"):
            self.files.store_archive(record(), make_fit(b"different activity"))
        self.assertEqual(archive_path.read_bytes(), self.fit)

    def test_enqueue_publishes_complete_file_atomically_and_is_idempotent(self) -> None:
        archive_path, digest = self.files.store_archive(record(), self.fit)
        observed_staged_payloads: list[bytes] = []

        def inspect_and_link(staged: Path, destination: Path) -> bool:
            self.assertFalse(destination.exists())
            observed_staged_payloads.append(staged.read_bytes())
            os.link(staged, destination)
            return True

        with patch.object(storage_module, "_link_without_overwrite", inspect_and_link):
            self.assertTrue(self.files.enqueue("12345", archive_path, digest))

        final_path = self.settings.watch_dir / "garmin-12345.fit"
        self.assertEqual(observed_staged_payloads, [self.fit])
        self.assertEqual(final_path.read_bytes(), self.fit)
        self.assertEqual(list(self.settings.staging_dir.iterdir()), [])
        self.assertFalse(self.files.enqueue("12345", archive_path, digest))

    def test_enqueue_refuses_inbox_collision_and_bad_archive_digest(self) -> None:
        archive_path, digest = self.files.store_archive(record(), self.fit)
        final_path = self.settings.watch_dir / "garmin-12345.fit"
        final_path.write_bytes(make_fit(b"other"))
        with self.assertRaisesRegex(StorageError, "inbox collision"):
            self.files.enqueue("12345", archive_path, digest)

        final_path.unlink()
        archive_path.write_bytes(make_fit(b"tampered"))
        with self.assertRaisesRegex(StorageError, "checksum mismatch"):
            self.files.enqueue("12345", archive_path, digest)

    def test_enqueue_failure_cleans_staging_without_partial_destination(self) -> None:
        archive_path, digest = self.files.store_archive(record(), self.fit)
        with patch.object(
            storage_module, "_link_without_overwrite", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.files.enqueue("12345", archive_path, digest)

        self.assertFalse((self.settings.watch_dir / "garmin-12345.fit").exists())
        self.assertEqual(list(self.settings.staging_dir.iterdir()), [])

    def test_reuse_archive_validates_existing_file(self) -> None:
        self.assertIsNone(self.files.reuse_archive(record()))
        path = self.files.archive_path(record())
        path.parent.mkdir(parents=True)
        path.write_bytes(b"invalid")
        with self.assertRaises(InvalidActivityPayload):
            self.files.reuse_archive(record())

    def test_reuse_archive_rejects_valid_fit_with_changed_checksum(self) -> None:
        path, digest = self.files.store_archive(record(), self.fit)
        path.write_bytes(make_fit(b"silently modified activity"))

        with self.assertRaisesRegex(InvalidActivityPayload, "checksum mismatch"):
            self.files.reuse_archive(
                record(state="enqueued", archive_path=str(path), digest=digest)
            )

    def test_reuse_archive_uses_persisted_path_after_metadata_date_changes(
        self,
    ) -> None:
        path, digest = self.files.store_archive(record(), self.fit)
        changed_metadata = record(
            start_time="2024-02-03T08:30:00",
            state="enqueued",
            archive_path=str(path),
            digest=digest,
        )

        self.assertEqual(self.files.reuse_archive(changed_metadata), (path, digest))

    def test_reuse_archive_recovers_unrecorded_file_after_metadata_date_changes(
        self,
    ) -> None:
        path, digest = self.files.store_archive(record(), self.fit)
        changed_metadata = record(start_time="2024-02-03T08:30:00")

        self.assertEqual(self.files.reuse_archive(changed_metadata), (path, digest))

    def test_reuse_archive_rejects_a_persisted_path_outside_the_archive(self) -> None:
        outside_path = Path(self.temporary_directory.name) / "other.fit"
        outside_path.write_bytes(self.fit)

        with self.assertRaisesRegex(InvalidActivityPayload, "outside"):
            self.files.reuse_archive(
                record(state="enqueued", archive_path=str(outside_path))
            )


class SyncLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_reusable_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "sync.lock"
            with SyncLock(lock_path):
                with self.assertRaises(SyncAlreadyRunning):
                    with SyncLock(lock_path):
                        self.fail("second lock must not be acquired")
            with SyncLock(lock_path):
                pass


if __name__ == "__main__":
    unittest.main()
