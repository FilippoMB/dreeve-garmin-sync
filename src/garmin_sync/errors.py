"""Application-level errors with safe, user-facing messages."""


class GarminSyncError(Exception):
    """Base class for expected synchronization failures."""


class ConfigurationError(GarminSyncError):
    """Raised when environment configuration is invalid."""


class AuthenticationRequired(GarminSyncError):
    """Raised when the saved Garmin session must be recreated."""


class RateLimited(GarminSyncError):
    """Raised when Garmin asks the client to slow down."""


class TransientGarminError(GarminSyncError):
    """Raised for temporary Garmin or network failures."""


class ActivityUnavailable(GarminSyncError):
    """Raised when Garmin no longer provides an activity download."""


class InvalidActivityPayload(GarminSyncError):
    """Raised when a Garmin download is not one safe, valid FIT payload."""


class StorageError(GarminSyncError):
    """Raised when persistent data cannot be stored safely."""


class SyncAlreadyRunning(GarminSyncError):
    """Raised when another synchronization process owns the state lock."""


class CooldownActive(GarminSyncError):
    """Raised while a persisted Garmin rate-limit cooldown is active."""

    def __init__(self, seconds_remaining: int) -> None:
        super().__init__(f"Garmin cooldown active for {seconds_remaining} more seconds")
        self.seconds_remaining = seconds_remaining
