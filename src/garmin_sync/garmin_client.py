"""Narrow adapter around python-garminconnect's unofficial API client."""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_sync.errors import (
    ActivityUnavailable,
    AuthenticationRequired,
    RateLimited,
    StorageError,
    TransientGarminError,
)

# The dependency can include API response bodies in its own exception logs. The
# adapter emits sanitized errors, so dependency records must never reach stdout.
GARMIN_LOGGER = logging.getLogger("garminconnect")
GARMIN_LOGGER.handlers.clear()
GARMIN_LOGGER.addHandler(logging.NullHandler())
GARMIN_LOGGER.propagate = False


class ActivityClient(Protocol):
    """The Garmin operations required by the synchronization workflow."""

    def list_activities(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """Return activities in ascending start-time order."""

    def download_original(self, activity_id: str) -> bytes:
        """Return Garmin's original activity download."""


def _http_status(error: BaseException) -> int | None:
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    match = re.search(r"(?:API Error|HTTP|status[=: ]+)\s*(\d{3})", str(error), re.I)
    return int(match.group(1)) if match else None


def _translate_connection_error(error: GarminConnectConnectionError) -> Exception:
    status = _http_status(error)
    if status in {401, 403}:
        return AuthenticationRequired("Saved Garmin authentication is no longer valid")
    if status == 429:
        return RateLimited("Garmin rate limit reached")
    if status == 404:
        return ActivityUnavailable("Garmin activity download is unavailable")
    if status is not None and 400 <= status < 500:
        return ActivityUnavailable(f"Garmin rejected the activity download ({status})")
    return TransientGarminError("Garmin Connect is temporarily unavailable")


class PythonGarminClient:
    """Authenticated adapter that never retains account credentials."""

    def __init__(self, client: Garmin) -> None:
        self._client = client

    @classmethod
    def from_tokens(cls, token_dir: Path) -> PythonGarminClient:
        client = Garmin(retry_attempts=3, retry_min_wait=1, retry_max_wait=10)
        try:
            client.login(str(token_dir))
        except (FileNotFoundError, GarminConnectAuthenticationError) as error:
            raise AuthenticationRequired(
                "Garmin authentication is required; run the auth command"
            ) from error
        except GarminConnectTooManyRequestsError as error:
            raise RateLimited("Garmin rate limit reached during login") from error
        except GarminConnectConnectionError as error:
            raise _translate_connection_error(error) from error
        return cls(client)

    def list_activities(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        try:
            activities = self._client.get_activities_by_date(
                start_date.isoformat(), end_date.isoformat(), sortorder="asc"
            )
        except GarminConnectAuthenticationError as error:
            raise AuthenticationRequired(
                "Garmin authentication was rejected"
            ) from error
        except GarminConnectTooManyRequestsError as error:
            raise RateLimited("Garmin rate limit reached") from error
        except GarminConnectConnectionError as error:
            raise _translate_connection_error(error) from error
        if not isinstance(activities, list):
            raise TransientGarminError("Garmin returned an invalid activity listing")
        return activities

    def download_original(self, activity_id: str) -> bytes:
        try:
            payload = self._client.download_activity(
                activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
            )
        except GarminConnectAuthenticationError as error:
            raise AuthenticationRequired(
                "Garmin authentication was rejected"
            ) from error
        except GarminConnectTooManyRequestsError as error:
            raise RateLimited("Garmin rate limit reached") from error
        except GarminConnectConnectionError as error:
            raise _translate_connection_error(error) from error
        if not isinstance(payload, bytes):
            raise TransientGarminError("Garmin returned an invalid activity download")
        return payload


def authenticate_interactively(
    token_dir: Path,
    *,
    input_text: Callable[[str], str] = input,
    input_password: Callable[[str], str] = getpass.getpass,
) -> None:
    """Create a refreshable token without persisting account credentials."""

    email = input_text("Garmin account email: ").strip()
    if not email:
        raise AuthenticationRequired("Garmin account email is required")
    password = input_password("Garmin account password: ")
    if not password:
        raise AuthenticationRequired("Garmin account password is required")

    token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token_dir.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".auth-", dir=token_dir) as temporary:
        temporary_token_dir = Path(temporary)
        client = Garmin(
            email=email,
            password=password,
            prompt_mfa=lambda: input_text("Garmin MFA code: ").strip(),
            retry_attempts=3,
            retry_min_wait=1,
            retry_max_wait=10,
        )
        try:
            client.login(str(temporary_token_dir))
        except GarminConnectTooManyRequestsError as error:
            raise RateLimited(
                "Garmin rate limit reached during authentication"
            ) from error
        except GarminConnectAuthenticationError as error:
            raise AuthenticationRequired(
                "Garmin rejected the supplied credentials"
            ) from error
        except GarminConnectConnectionError as error:
            raise _translate_connection_error(error) from error

        new_token_file = temporary_token_dir / "garmin_tokens.json"
        _validate_new_token(new_token_file)
        _replace_token_atomically(new_token_file, token_dir / "garmin_tokens.json")


def _validate_new_token(token_file: Path) -> None:
    try:
        token_data = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StorageError("Garmin did not produce a valid OAuth token") from error
    required_fields = ("di_token", "di_refresh_token", "di_client_id")
    if any(not isinstance(token_data.get(field), str) for field in required_fields):
        raise StorageError("Garmin OAuth token is incomplete")


def _replace_token_atomically(source: Path, destination: Path) -> None:
    try:
        source.chmod(0o600)
        with source.open("rb") as token_file:
            os.fsync(token_file.fileno())
        os.replace(source, destination)
        destination.chmod(0o600)
        directory_descriptor = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise StorageError("Could not store the Garmin OAuth token") from error
