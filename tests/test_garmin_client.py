from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import install_garminconnect_stub

install_garminconnect_stub()

from garminconnect import (  # noqa: E402 - optional dependency stub is installed first
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_sync.errors import (  # noqa: E402
    ActivityUnavailable,
    AuthenticationRequired,
    RateLimited,
    StorageError,
    TransientGarminError,
)
from garmin_sync.garmin_client import (  # noqa: E402
    PythonGarminClient,
    authenticate_interactively,
)


class TokenAuthenticationTests(unittest.TestCase):
    @patch("garmin_sync.garmin_client.Garmin")
    def test_unattended_client_uses_only_the_token_store(
        self, garmin_class: Mock
    ) -> None:
        garmin = garmin_class.return_value
        token_dir = Path("/private/tokens")

        result = PythonGarminClient.from_tokens(token_dir)

        self.assertIsInstance(result, PythonGarminClient)
        garmin_class.assert_called_once_with(
            retry_attempts=3, retry_min_wait=1, retry_max_wait=10
        )
        garmin.login.assert_called_once_with(str(token_dir))

    @patch("garmin_sync.garmin_client.Garmin")
    def test_missing_or_rejected_token_requires_interactive_auth(
        self, garmin_class: Mock
    ) -> None:
        garmin_class.return_value.login.side_effect = GarminConnectAuthenticationError(
            "rejected"
        )

        with self.assertRaises(AuthenticationRequired):
            PythonGarminClient.from_tokens(Path("/tokens"))

    @patch("garmin_sync.garmin_client.Garmin")
    def test_rate_limit_during_token_login_is_preserved(
        self, garmin_class: Mock
    ) -> None:
        garmin_class.return_value.login.side_effect = GarminConnectTooManyRequestsError(
            "429"
        )

        with self.assertRaises(RateLimited):
            PythonGarminClient.from_tokens(Path("/tokens"))


class InteractiveAuthenticationTests(unittest.TestCase):
    @patch("garmin_sync.garmin_client.Garmin")
    def test_credentials_and_mfa_are_supplied_only_to_interactive_login(
        self, garmin_class: Mock
    ) -> None:
        prompts = iter(("athlete@example.org", "654321"))
        input_text = Mock(side_effect=lambda _: next(prompts))
        input_password = Mock(return_value="one-time-password")
        captured_mfa: list[str] = []

        def login(token_path: str) -> None:
            temporary_token_dir = Path(token_path)
            self.assertNotEqual(temporary_token_dir, token_dir)
            self.assertEqual(temporary_token_dir.parent, token_dir)
            callback = garmin_class.call_args.kwargs["prompt_mfa"]
            captured_mfa.append(callback())
            (temporary_token_dir / "garmin_tokens.json").write_text(
                json.dumps(
                    {
                        "di_token": "new-access-token",
                        "di_refresh_token": "new-refresh-token",
                        "di_client_id": "client-id",
                    }
                ),
                encoding="utf-8",
            )

        garmin_class.return_value.login.side_effect = login
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_dir = Path(temporary_directory) / "tokens"
            token_dir.mkdir()
            final_token = token_dir / "garmin_tokens.json"
            final_token.write_text(
                json.dumps(
                    {
                        "di_token": "old-access-token",
                        "di_refresh_token": "old-refresh-token",
                    }
                ),
                encoding="utf-8",
            )
            authenticate_interactively(
                token_dir,
                input_text=input_text,
                input_password=input_password,
            )

            saved_token = json.loads(final_token.read_text(encoding="utf-8"))
            self.assertEqual(saved_token["di_token"], "new-access-token")
            self.assertEqual(saved_token["di_refresh_token"], "new-refresh-token")
            self.assertEqual(saved_token["di_client_id"], "client-id")
            self.assertEqual(
                [path for path in token_dir.iterdir() if path.is_dir()], []
            )

        self.assertEqual(captured_mfa, ["654321"])
        input_password.assert_called_once_with("Garmin account password: ")
        self.assertEqual(garmin_class.call_args.kwargs["email"], "athlete@example.org")
        self.assertEqual(garmin_class.call_args.kwargs["password"], "one-time-password")

    @patch("garmin_sync.garmin_client.Garmin")
    def test_incomplete_refresh_token_does_not_replace_existing_token(
        self, garmin_class: Mock
    ) -> None:
        def login(token_path: str) -> None:
            (Path(token_path) / "garmin_tokens.json").write_text(
                json.dumps(
                    {
                        "di_token": "new-access-token",
                        "di_refresh_token": "new-refresh-token",
                    }
                ),
                encoding="utf-8",
            )

        garmin_class.return_value.login.side_effect = login
        with tempfile.TemporaryDirectory() as temporary_directory:
            token_dir = Path(temporary_directory) / "tokens"
            token_dir.mkdir()
            final_token = token_dir / "garmin_tokens.json"
            final_token.write_text("old-token", encoding="utf-8")

            with self.assertRaises(StorageError):
                authenticate_interactively(
                    token_dir,
                    input_text=Mock(return_value="athlete@example.org"),
                    input_password=Mock(return_value="one-time-password"),
                )

            self.assertEqual(final_token.read_text(encoding="utf-8"), "old-token")

    def test_dependency_logs_do_not_propagate_response_bodies(self) -> None:
        dependency_logger = logging.getLogger("garminconnect")

        self.assertFalse(dependency_logger.propagate)
        self.assertTrue(
            any(
                isinstance(handler, logging.NullHandler)
                for handler in dependency_logger.handlers
            )
        )


class GarminApiAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.garmin = Mock()
        self.client = PythonGarminClient(self.garmin)

    def test_activity_listing_and_original_download_use_expected_api(self) -> None:
        self.garmin.get_activities_by_date.return_value = [{"activityId": 123}]
        self.garmin.download_activity.return_value = b"download"

        activities = self.client.list_activities(date(2024, 1, 1), date(2024, 1, 2))
        download = self.client.download_original("123")

        self.assertEqual(activities, [{"activityId": 123}])
        self.assertEqual(download, b"download")
        self.garmin.get_activities_by_date.assert_called_once_with(
            "2024-01-01", "2024-01-02", sortorder="asc"
        )
        self.garmin.download_activity.assert_called_once_with(
            "123", dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
        )

    def test_invalid_shapes_and_http_failures_are_sanitized(self) -> None:
        self.garmin.get_activities_by_date.return_value = {"not": "a list"}
        with self.assertRaises(TransientGarminError):
            self.client.list_activities(date(2024, 1, 1), date(2024, 1, 2))

        self.garmin.download_activity.side_effect = GarminConnectConnectionError(
            "API Error 404 - missing"
        )
        with self.assertRaises(ActivityUnavailable):
            self.client.download_original("123")


if __name__ == "__main__":
    unittest.main()
