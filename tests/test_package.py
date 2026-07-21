from __future__ import annotations

import contextlib
import io
import re
import tomllib
import unittest
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from unittest.mock import patch

from helpers import install_garminconnect_stub

install_garminconnect_stub()

from garmin_sync.cli import main  # noqa: E402 - dependency stub must be installed first


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class PackageMetadataTests(unittest.TestCase):
    def test_pyproject_defines_release_version_dependency_and_console_script(
        self,
    ) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
            pyproject = tomllib.load(pyproject_file)

        project = pyproject["project"]
        self.assertEqual(project["version"], "0.1.0")
        self.assertEqual(project["dependencies"], ["garminconnect==0.3.6"])
        self.assertEqual(project["scripts"]["garmin-sync"], "garmin_sync.cli:main")

    def test_installed_distribution_version_matches_pyproject(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
            project_version = tomllib.load(pyproject_file)["project"]["version"]
        try:
            installed_version = distribution_version("dreeve-garmin-sync")
        except PackageNotFoundError:
            self.skipTest("distribution is not installed in this test environment")

        self.assertEqual(installed_version, project_version)

    def test_locked_requirements_include_every_runtime_dependency(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
            dependencies = tomllib.load(pyproject_file)["project"]["dependencies"]

        locked_requirements = {}
        for line in (PROJECT_ROOT / "requirements.lock").read_text().splitlines():
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ \\]+)(?: \\)?", line)
            if match:
                locked_requirements[normalized_distribution_name(match.group(1))] = (
                    match.group(2)
                )

        for dependency in dependencies:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;]+)", dependency)
            self.assertIsNotNone(
                match,
                f"runtime dependency must use an exact version: {dependency}",
            )
            assert match is not None
            name, version = match.groups()
            self.assertEqual(
                locked_requirements.get(normalized_distribution_name(name)),
                version,
                f"requirements.lock is stale for {name}",
            )

    def test_version_option_uses_installed_metadata_without_loading_runtime(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            patch("garmin_sync.cli.distribution_version", return_value="0.1.0"),
            patch("garmin_sync.cli._runtime") as runtime,
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as exit_context,
        ):
            main(["--version"])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertEqual(output.getvalue(), "garmin-sync 0.1.0\n")
        runtime.assert_not_called()

    def test_every_supported_subcommand_exposes_help_without_loading_runtime(
        self,
    ) -> None:
        for command in ("auth", "daemon", "sync-once", "status", "requeue"):
            with self.subTest(command=command):
                output = io.StringIO()
                with (
                    patch("garmin_sync.cli._runtime") as runtime,
                    contextlib.redirect_stdout(output),
                    self.assertRaises(SystemExit) as exit_context,
                ):
                    main([command, "--help"])

                self.assertEqual(exit_context.exception.code, 0)
                self.assertIn("usage:", output.getvalue())
                runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
