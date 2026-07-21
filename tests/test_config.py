from __future__ import annotations

import os
import stat
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from garmin_sync.config import Settings
from garmin_sync.errors import ConfigurationError


def required_environment() -> dict[str, str]:
    return {
        "DREEVE_WATCH_DIR": "/srv/dreeve/watch",
        "GARMIN_SYNC_START_DATE": "2025-12-31",
        "GARMIN_SYNC_UID": "1000",
        "GARMIN_SYNC_GID": "1000",
    }


def load_settings(environment: dict[str, str]) -> Settings:
    with (
        patch.dict(os.environ, environment, clear=True),
        patch("garmin_sync.config.os.geteuid", return_value=1000),
        patch("garmin_sync.config.os.getegid", return_value=1000),
    ):
        return Settings.from_environment()


class SettingsTests(unittest.TestCase):
    def test_from_environment_uses_documented_defaults(self) -> None:
        settings = load_settings(required_environment())

        self.assertEqual(settings.data_dir, Path("/data"))
        self.assertEqual(settings.watch_dir, Path("/watch"))
        self.assertEqual(settings.interval_minutes, 60)
        self.assertEqual(settings.initial_start_date, date(2025, 12, 31))
        self.assertEqual(settings.overlap_days, 2)
        self.assertEqual(settings.max_download_bytes, 128 * 1024 * 1024)

    def test_from_environment_requires_an_explicit_start_date(self) -> None:
        for start_date in (None, ""):
            environment = required_environment()
            if start_date is None:
                environment.pop("GARMIN_SYNC_START_DATE")
            else:
                environment["GARMIN_SYNC_START_DATE"] = start_date
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ConfigurationError, "is required"):
                    load_settings(environment)

    def test_from_environment_parses_all_supported_overrides(self) -> None:
        environment = required_environment() | {
            "GARMIN_SYNC_DATA_DIR": "/private-data",
            "GARMIN_SYNC_WATCH_DIR": "/inbox",
            "GARMIN_SYNC_INTERVAL_MINUTES": "1440",
            "GARMIN_SYNC_OVERLAP_DAYS": "7",
        }
        settings = load_settings(environment)

        self.assertEqual(settings.data_dir, Path("/private-data"))
        self.assertEqual(settings.watch_dir, Path("/inbox"))
        self.assertEqual(settings.interval_minutes, 1440)
        self.assertEqual(settings.initial_start_date, date(2025, 12, 31))
        self.assertEqual(settings.overlap_days, 7)

    def test_invalid_environment_values_are_rejected(self) -> None:
        invalid_cases = (
            ({"GARMIN_SYNC_START_DATE": "01-01-2024"}, "START_DATE"),
            ({"GARMIN_SYNC_INTERVAL_MINUTES": "hourly"}, "INTERVAL"),
            ({"GARMIN_SYNC_INTERVAL_MINUTES": "0"}, "INTERVAL"),
            ({"GARMIN_SYNC_INTERVAL_MINUTES": "10081"}, "INTERVAL"),
            ({"GARMIN_SYNC_OVERLAP_DAYS": "-1"}, "OVERLAP"),
            ({"GARMIN_SYNC_OVERLAP_DAYS": "31"}, "OVERLAP"),
        )
        for environment, expected_fragment in invalid_cases:
            with self.subTest(environment=environment):
                configured_environment = required_environment() | environment
                with self.assertRaisesRegex(ConfigurationError, expected_fragment):
                    load_settings(configured_environment)

    def test_host_path_and_non_root_identity_contract_is_enforced(self) -> None:
        invalid_cases = (
            ({"DREEVE_WATCH_DIR": "relative/watch"}, "absolute host path"),
            ({"GARMIN_SYNC_UID": "0"}, "greater than zero"),
            ({"GARMIN_SYNC_GID": "0"}, "greater than zero"),
            ({"GARMIN_SYNC_UID": "operator"}, "numeric host ID"),
            ({"GARMIN_SYNC_UID": "1001"}, "runtime identity"),
        )
        for overrides, expected_fragment in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ConfigurationError, expected_fragment):
                    load_settings(required_environment() | overrides)

    def test_path_properties_and_directory_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = Settings(
                data_dir=root / "data",
                watch_dir=root / "watch",
                interval_minutes=60,
                initial_start_date=date(2024, 1, 1),
                overlap_days=2,
            )
            settings.watch_dir.mkdir()
            settings.ensure_directories()

            expected_directories = (
                settings.data_dir,
                settings.token_dir,
                settings.state_dir,
                settings.archive_dir,
                settings.quarantine_dir,
                settings.staging_dir,
            )
            for directory in expected_directories:
                with self.subTest(directory=directory):
                    self.assertTrue(directory.is_dir())
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

            self.assertEqual(
                settings.token_file, root / "data/tokens/garmin_tokens.json"
            )
            self.assertEqual(settings.database_path, root / "data/state/sync.sqlite3")
            self.assertEqual(settings.lock_path, root / "data/state/sync.lock")

    def test_health_window_scales_for_daily_schedule(self) -> None:
        hourly = Settings(Path("/data"), Path("/watch"), 60, date.today(), 2)
        daily = Settings(Path("/data"), Path("/watch"), 1440, date.today(), 2)

        self.assertEqual(hourly.healthy_after, timedelta(hours=3))
        self.assertEqual(daily.healthy_after, timedelta(minutes=2910))


if __name__ == "__main__":
    unittest.main()
