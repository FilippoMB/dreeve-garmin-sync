from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from helpers import (
    FIXED_NOW,
    FakeClient,
    activity,
    install_garminconnect_stub,
    make_fit,
    make_settings,
)

install_garminconnect_stub()

from garmin_sync.errors import (  # noqa: E402 - stub must precede service import
    ActivityUnavailable,
    AuthenticationRequired,
    CooldownActive,
    InvalidActivityPayload,
    RateLimited,
    StorageError,
    TransientGarminError,
)
from garmin_sync.service import (  # noqa: E402
    SyncResult,
    Synchronizer,
    run_daemon,
    status_is_healthy,
)
from garmin_sync.storage import ActivityFiles, Ledger  # noqa: E402


class SynchronizerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = make_settings(Path(self.temporary_directory.name))
        self.ledger = Ledger(self.settings.database_path)
        self.files = ActivityFiles(self.settings)

    def synchronizer(self, factory, *, clock=lambda: FIXED_NOW) -> Synchronizer:
        return Synchronizer(
            self.settings,
            self.ledger,
            self.files,
            client_factory=factory,
            clock=clock,
        )


class ScanWindowAndDeduplicationTests(SynchronizerTestCase):
    def test_first_scan_uses_initial_date_through_today(self) -> None:
        client = FakeClient()
        result = self.synchronizer(lambda _: client).sync_once()

        self.assertEqual(result, SyncResult())
        self.assertEqual(
            client.list_calls,
            [(date(2024, 1, 1), date(2024, 1, 6))],
        )
        self.assertEqual(self.ledger.last_successful_scan(), FIXED_NOW)

    def test_subsequent_scan_rewinds_cursor_by_overlap_without_crossing_initial_date(
        self,
    ) -> None:
        client = FakeClient()
        self.ledger.complete_scan(datetime(2024, 1, 5, 9, 0, tzinfo=UTC))
        self.synchronizer(lambda _: client).sync_once()
        self.assertEqual(client.list_calls[0][0], date(2024, 1, 3))

        other_root = Path(self.temporary_directory.name) / "second-runtime"
        settings = make_settings(other_root)
        ledger = Ledger(settings.database_path)
        files = ActivityFiles(settings)
        ledger.complete_scan(datetime(2024, 1, 2, 9, 0, tzinfo=UTC))
        second_client = FakeClient()
        Synchronizer(
            settings,
            ledger,
            files,
            client_factory=lambda _: second_client,
            clock=lambda: FIXED_NOW,
        ).sync_once()
        self.assertEqual(second_client.list_calls[0][0], date(2024, 1, 1))

    def test_duplicate_listing_downloads_and_enqueues_activity_once(self) -> None:
        duplicate = activity()
        client = FakeClient(
            activities=[duplicate, dict(duplicate)],
            downloads={"12345": make_fit()},
        )
        result = self.synchronizer(lambda _: client).sync_once()

        self.assertEqual(result.listed, 2)
        self.assertEqual(result.archived, 1)
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(client.download_calls, ["12345"])
        self.assertEqual(self.ledger.snapshot().activity_counts, {"enqueued": 1})

    def test_persisted_ledger_prevents_duplicate_work_after_runtime_restart(
        self,
    ) -> None:
        first_client = FakeClient(
            activities=[activity()],
            downloads={"12345": make_fit()},
        )
        first_result = self.synchronizer(lambda _: first_client).sync_once()
        inbox_path = self.settings.watch_dir / "garmin-12345.fit"
        original_inode = inbox_path.stat().st_ino

        restarted_settings = make_settings(Path(self.temporary_directory.name))
        restarted_ledger = Ledger(restarted_settings.database_path)
        restarted_files = ActivityFiles(restarted_settings)
        second_client = FakeClient(activities=[activity()])
        second_result = Synchronizer(
            restarted_settings,
            restarted_ledger,
            restarted_files,
            client_factory=lambda _: second_client,
            clock=lambda: FIXED_NOW + timedelta(hours=1),
        ).sync_once()

        self.assertEqual(first_result.archived, 1)
        self.assertEqual(first_result.enqueued, 1)
        self.assertEqual(second_result.listed, 1)
        self.assertEqual(second_result.archived, 0)
        self.assertEqual(second_result.enqueued, 0)
        self.assertEqual(second_client.download_calls, [])
        self.assertEqual(inbox_path.stat().st_ino, original_inode)


class RecoveryAndErrorPathTests(SynchronizerTestCase):
    def test_crash_after_atomic_publish_is_recovered_without_redownload(self) -> None:
        first_client = FakeClient(
            activities=[activity()], downloads={"12345": make_fit()}
        )
        retry_client = FakeClient(activities=[], downloads={})
        clients = iter((first_client, retry_client))
        synchronizer = self.synchronizer(lambda _: next(clients))

        with patch.object(
            self.ledger,
            "mark_enqueued",
            side_effect=RuntimeError("simulated process crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                synchronizer.sync_once()

        inbox_path = self.settings.watch_dir / "garmin-12345.fit"
        self.assertTrue(inbox_path.is_file())
        self.assertEqual(self.ledger.find_activity("12345").state, "archived")

        result = synchronizer.sync_once()
        self.assertEqual(result.already_queued, 1)
        self.assertEqual(result.archived, 0)
        self.assertEqual(retry_client.download_calls, [])
        self.assertEqual(self.ledger.find_activity("12345").state, "enqueued")

    def test_unavailable_activity_is_terminal_but_scan_completes(self) -> None:
        client = FakeClient(
            activities=[activity()],
            downloads={"12345": ActivityUnavailable("gone")},
        )
        result = self.synchronizer(lambda _: client).sync_once()

        self.assertEqual(result.unavailable, 1)
        self.assertTrue(result.has_terminal_errors)
        item = self.ledger.find_activity("12345")
        self.assertEqual(item.state, "unavailable")
        self.assertEqual(item.attempts, 1)
        self.assertEqual(self.ledger.snapshot().health_status, "healthy")

    def test_invalid_activity_is_quarantined_and_terminal(self) -> None:
        client = FakeClient(
            activities=[activity()], downloads={"12345": b"not a FIT or ZIP"}
        )
        result = self.synchronizer(lambda _: client).sync_once()

        self.assertEqual(result.invalid, 1)
        item = self.ledger.find_activity("12345")
        self.assertEqual(item.state, "invalid")
        quarantined = list(self.settings.quarantine_dir.rglob("*.bin"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"not a FIT or ZIP")

    def test_transient_download_failure_remains_pending_for_next_scan(self) -> None:
        first_client = FakeClient(
            activities=[activity()],
            downloads={"12345": TransientGarminError("temporary outage")},
        )
        retry_client = FakeClient(activities=[], downloads={"12345": make_fit()})
        clients = iter((first_client, retry_client))
        synchronizer = self.synchronizer(lambda _: next(clients))

        with self.assertRaisesRegex(TransientGarminError, "temporary outage"):
            synchronizer.sync_once()
        item = self.ledger.find_activity("12345")
        self.assertEqual(item.state, "transient_error")
        self.assertEqual(item.attempts, 1)
        self.assertEqual(self.ledger.snapshot().health_status, "degraded")

        result = synchronizer.sync_once()
        self.assertEqual(result.archived, 1)
        self.assertEqual(result.enqueued, 1)
        self.assertEqual(retry_client.download_calls, ["12345"])
        self.assertEqual(self.ledger.find_activity("12345").state, "enqueued")

    def test_authentication_failure_is_persisted_without_network_retry(self) -> None:
        calls = 0

        def unauthenticated_factory(_):
            nonlocal calls
            calls += 1
            raise AuthenticationRequired("reauthenticate")

        synchronizer = self.synchronizer(unauthenticated_factory)
        with self.assertRaises(AuthenticationRequired):
            synchronizer.sync_once()

        snapshot = self.ledger.snapshot()
        self.assertEqual(calls, 1)
        self.assertEqual(snapshot.health_status, "authentication_required")
        self.assertEqual(snapshot.last_error, "reauthenticate")

    def test_rate_limit_persists_cooldown_and_blocks_immediate_retry(self) -> None:
        calls = 0

        def rate_limited_factory(_):
            nonlocal calls
            calls += 1
            raise RateLimited("slow down")

        synchronizer = self.synchronizer(rate_limited_factory)
        with self.assertRaises(RateLimited):
            synchronizer.sync_once()

        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.health_status, "rate_limited")
        self.assertEqual(snapshot.rate_limit_count, 1)
        self.assertEqual(self.ledger.cooldown_remaining(FIXED_NOW), 3600)
        with self.assertRaises(CooldownActive) as context:
            synchronizer.sync_once()
        self.assertEqual(context.exception.seconds_remaining, 3600)
        self.assertEqual(calls, 1)

    def test_invalid_activity_listing_is_treated_as_transient(self) -> None:
        client = FakeClient(activities=[activity(), "not-a-dictionary"])
        with self.assertRaises(TransientGarminError):
            self.synchronizer(lambda _: client).sync_once()
        self.assertEqual(self.ledger.snapshot().health_status, "degraded")


class RequeueTests(SynchronizerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ledger.upsert_discovered(activity(), FIXED_NOW)
        archive_path, digest = self.files.store_archive(
            self.ledger.find_activity("12345"), make_fit()
        )
        self.ledger.mark_archived("12345", archive_path, digest, FIXED_NOW)
        self.ledger.mark_enqueued("12345", FIXED_NOW)
        self.synchronizer_under_test = self.synchronizer(
            lambda _: self.fail("requeue must not create a Garmin client")
        )

    def test_requeue_delivers_archive_and_is_idempotent(self) -> None:
        self.assertTrue(self.synchronizer_under_test.requeue("12345"))
        self.assertEqual(
            (self.settings.watch_dir / "garmin-12345.fit").read_bytes(), make_fit()
        )
        self.assertFalse(self.synchronizer_under_test.requeue("12345"))
        self.assertEqual(self.ledger.find_activity("12345").state, "enqueued")

    def test_requeue_rejects_invalid_unknown_and_unarchived_ids(self) -> None:
        with self.assertRaises(InvalidActivityPayload):
            self.synchronizer_under_test.requeue("not-numeric")
        with self.assertRaisesRegex(StorageError, "not in the sync ledger"):
            self.synchronizer_under_test.requeue("999")

        self.ledger.upsert_discovered(activity("456"), FIXED_NOW)
        with self.assertRaisesRegex(StorageError, "no archived FIT"):
            self.synchronizer_under_test.requeue("456")


class HealthTests(SynchronizerTestCase):
    def test_health_requires_token_recent_success_and_healthy_state(self) -> None:
        self.assertFalse(status_is_healthy(self.settings, self.ledger, FIXED_NOW))
        self.settings.token_file.write_text("test token", encoding="utf-8")
        self.assertFalse(status_is_healthy(self.settings, self.ledger, FIXED_NOW))

        self.ledger.complete_scan(FIXED_NOW - timedelta(hours=2))
        self.assertTrue(status_is_healthy(self.settings, self.ledger, FIXED_NOW))
        self.assertFalse(
            status_is_healthy(
                self.settings,
                self.ledger,
                FIXED_NOW + self.settings.healthy_after + timedelta(seconds=1),
            )
        )

    def test_degraded_authentication_and_rate_limited_states_are_unhealthy(
        self,
    ) -> None:
        self.settings.token_file.write_text("test token", encoding="utf-8")
        mutations = (
            lambda: self.ledger.record_transient_error("network"),
            lambda: self.ledger.record_authentication_required("login"),
            lambda: self.ledger.record_rate_limit(FIXED_NOW),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.ledger.complete_scan(FIXED_NOW)
                mutate()
                self.assertFalse(
                    status_is_healthy(self.settings, self.ledger, FIXED_NOW)
                )


class RecordingEvent:
    def __init__(self, stop_after_waits: int) -> None:
        self.stop_after_waits = stop_after_waits
        self.waits: list[float] = []
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after_waits:
            self.stopped = True
        return self.stopped


class ScriptedSynchronizer:
    def __init__(self, outcomes) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0

    def sync_once(self):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DaemonRetryTests(SynchronizerTestCase):
    def test_transient_retries_use_five_fifteen_and_sixty_minutes(self) -> None:
        synchronizer = ScriptedSynchronizer(
            [
                TransientGarminError("one"),
                TransientGarminError("two"),
                TransientGarminError("three"),
                SyncResult(),
            ]
        )
        event = RecordingEvent(stop_after_waits=4)

        result = run_daemon(synchronizer, self.settings, self.ledger, stop_event=event)

        self.assertEqual(result, 0)
        self.assertEqual(synchronizer.calls, 4)
        self.assertEqual(event.waits, [300, 900, 3600, 3600])

    def test_authentication_failure_waits_for_token_change_without_relogin_loop(
        self,
    ) -> None:
        synchronizer = ScriptedSynchronizer([AuthenticationRequired("run auth")])
        event = RecordingEvent(stop_after_waits=1)

        result = run_daemon(synchronizer, self.settings, self.ledger, stop_event=event)

        self.assertEqual(result, 0)
        self.assertEqual(synchronizer.calls, 1)
        self.assertEqual(event.waits, [60])


if __name__ == "__main__":
    unittest.main()
