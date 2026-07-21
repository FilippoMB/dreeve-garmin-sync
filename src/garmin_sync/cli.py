"""Command-line interface for setup, operation, and container health."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import version as distribution_version
from typing import Any

from garmin_sync.config import Settings
from garmin_sync.errors import GarminSyncError
from garmin_sync.garmin_client import authenticate_interactively
from garmin_sync.service import Synchronizer, run_daemon, status_is_healthy
from garmin_sync.storage import ActivityFiles, Ledger


class _PackageVersionAction(argparse.Action):
    """Resolve the installed package version only when it is requested."""

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        print(f"{parser.prog} {distribution_version('dreeve-garmin-sync')}")
        parser.exit()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-sync",
        description="Synchronize original Garmin activity files into Dreeve",
    )
    parser.add_argument(
        "--version",
        action=_PackageVersionAction,
        help="Show the installed package version and exit",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("auth", help="Create or replace the saved Garmin token")
    subcommands.add_parser("sync-once", help="Run one Garmin synchronization")
    subcommands.add_parser("daemon", help="Run immediately and then periodically")
    status = subcommands.add_parser("status", help="Show persistent sync status")
    status.add_argument(
        "--healthcheck",
        action="store_true",
        help="Return non-zero unless the daemon is healthy",
    )
    requeue = subcommands.add_parser(
        "requeue", help="Deliver one archived FIT to Dreeve again"
    )
    requeue.add_argument("activity_id")
    return parser


def _runtime() -> tuple[Settings, Ledger, ActivityFiles, Synchronizer]:
    settings = Settings.from_environment()
    settings.ensure_directories()
    ledger = Ledger(settings.database_path)
    files = ActivityFiles(settings)
    return settings, ledger, files, Synchronizer(settings, ledger, files)


def _run_command(arguments: argparse.Namespace) -> int:
    settings, ledger, files, synchronizer = _runtime()

    if arguments.command == "auth":
        authenticate_interactively(settings.token_dir)
        if not settings.token_file.is_file():
            raise GarminSyncError("Authentication succeeded but no token was stored")
        settings.token_file.chmod(0o600)
        ledger.mark_authenticated()
        print("Garmin authentication succeeded; only the OAuth token was stored.")
        return 0

    if arguments.command == "sync-once":
        result = synchronizer.sync_once()
        print(
            "Garmin scan complete: "
            f"listed={result.listed} archived={result.archived} "
            f"enqueued={result.enqueued} already_queued={result.already_queued} "
            f"invalid={result.invalid} unavailable={result.unavailable}"
        )
        return 2 if result.has_terminal_errors else 0

    if arguments.command == "daemon":
        return run_daemon(synchronizer, settings, ledger)

    if arguments.command == "status":
        print(files.status_as_json(ledger.snapshot()))
        if arguments.healthcheck:
            return 0 if status_is_healthy(settings, ledger, datetime.now(UTC)) else 1
        return 0

    if arguments.command == "requeue":
        published = synchronizer.requeue(arguments.activity_id)
        if published:
            print(f"Requeued Garmin activity {arguments.activity_id} for Dreeve.")
        else:
            print(f"Garmin activity {arguments.activity_id} is already in the inbox.")
        return 0

    raise AssertionError(f"Unhandled command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    logging.basicConfig(
        level=os.getenv("GARMIN_SYNC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = _parser().parse_args(argv)
    try:
        return _run_command(arguments)
    except GarminSyncError as error:
        print(f"garmin-sync: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("garmin-sync: interrupted", file=sys.stderr)
        return 130
