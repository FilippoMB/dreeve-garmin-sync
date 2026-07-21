"""Shared offline test fixtures for the Garmin synchronization sidecar."""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


def install_garminconnect_stub() -> None:
    """Allow service tests to import without installing the network client."""

    try:
        __import__("garminconnect")
        return
    except ModuleNotFoundError:
        pass

    module = types.ModuleType("garminconnect")

    class GarminConnectAuthenticationError(Exception):
        pass

    class GarminConnectConnectionError(Exception):
        pass

    class GarminConnectTooManyRequestsError(Exception):
        pass

    class Garmin:
        class ActivityDownloadFormat:
            ORIGINAL = "original"

    module.Garmin = Garmin
    module.GarminConnectAuthenticationError = GarminConnectAuthenticationError
    module.GarminConnectConnectionError = GarminConnectConnectionError
    module.GarminConnectTooManyRequestsError = GarminConnectTooManyRequestsError
    sys.modules["garminconnect"] = module


def make_fit(data: bytes = b"activity-data", *, header_size: int = 12) -> bytes:
    """Build the smallest FIT-shaped payload accepted by the production validator."""

    header = bytearray(header_size)
    header[0] = header_size
    header[4:8] = len(data).to_bytes(4, byteorder="little")
    header[8:12] = b".FIT"
    return bytes(header) + data


def activity(
    activity_id: str = "12345",
    *,
    start_time: str = "2024-01-02T08:30:00",
    name: str = "Morning run",
) -> dict[str, Any]:
    return {
        "activityId": int(activity_id),
        "startTimeLocal": start_time,
        "activityName": name,
    }


class FakeClient:
    """In-memory ActivityClient with programmable listing and downloads."""

    def __init__(
        self,
        activities: list[dict[str, Any]] | Exception | None = None,
        downloads: dict[str, bytes | Exception] | None = None,
    ) -> None:
        self.activities = [] if activities is None else activities
        self.downloads = {} if downloads is None else downloads
        self.list_calls: list[tuple[date, date]] = []
        self.download_calls: list[str] = []

    def list_activities(self, start_date: date, end_date: date):
        self.list_calls.append((start_date, end_date))
        if isinstance(self.activities, Exception):
            raise self.activities
        return list(self.activities)

    def download_original(self, activity_id: str) -> bytes:
        self.download_calls.append(activity_id)
        result = self.downloads[activity_id]
        if isinstance(result, Exception):
            raise result
        return result


FIXED_NOW = datetime(2024, 1, 6, 12, 0, tzinfo=UTC)


def make_settings(root: Path, **overrides: Any):
    """Create Settings whose private and inbox roots live below a temp directory."""

    from garmin_sync.config import Settings

    defaults = {
        "data_dir": root / "data",
        "watch_dir": root / "watch",
        "interval_minutes": 60,
        "initial_start_date": date(2024, 1, 1),
        "overlap_days": 2,
    }
    defaults.update(overrides)
    settings = Settings(**defaults)
    settings.watch_dir.mkdir(parents=True, exist_ok=True)
    settings.ensure_directories()
    return settings
