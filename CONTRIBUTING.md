# Contributing

Thanks for helping improve `dreeve-garmin-sync`. The project is an unofficial,
small-scope bridge between Garmin Connect and Dreeve v5 file mode. Changes should
keep that boundary explicit and preserve unattended, recoverable operation.

## Before opening an issue

- Search existing issues and check the latest prerelease notes.
- Confirm whether the failure is before or after a validated FIT reaches Dreeve's
  watch directory. Downstream Dreeve import problems belong in the Dreeve project.
- Remove tokens, email addresses, activity names and IDs, GPS data, FIT files,
  absolute personal paths, and other identifying information from logs.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

Feature requests for PyPI, systemd, Kubernetes, Windows, or NAS packages are
outside the v0.1 scope. A proposal can discuss a future direction, but a pull
request should not add a new deployment surface without prior agreement.

## Development setup

Use Python 3.12 on Linux:

```console
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run the same core checks used by CI:

```console
ruff format --check .
ruff check .
mypy src
pytest
python -m build
pip-audit --require-hashes -r requirements.lock
docker compose config --quiet
```

Tests must use fakes or local temporary data. Never use a real Garmin account,
OAuth token, downloaded activity, or a person's Dreeve data in tests, fixtures,
issues, commits, or CI.

## Change guidelines

- Preserve `pyproject.toml` as the source of the package version and dependencies.
  Regenerate the hash-locked runtime requirements when dependencies change.

  ```console
  uv pip compile pyproject.toml --generate-hashes --python-version 3.12 --universal --only-binary=:all: --no-emit-index-url --output-file requirements.lock
  ```
- Keep `auth`, `daemon`, `sync-once`, `status`, `requeue`, `--version`, the Compose
  variables, and the `/data` plus `/watch` mount contract backward compatible within
  the release line unless the change is explicitly documented as breaking.
- Keep files private, writes atomic, downloads validated, operations idempotent,
  and failures recoverable after restart.
- Treat Garmin errors and logs as potentially sensitive. Do not emit response
  bodies, tokens, passwords, or unredacted account data.
- Add focused tests for success, expected failures, restart recovery, and
  deduplication. Live Garmin access must remain outside automated tests.
- Update README or operations documentation when operator-visible behavior changes,
  and add a concise entry under `Unreleased` in `CHANGELOG.md`.

Before submitting a pull request, describe the user-visible behavior, the tests
run, any data-format or operational implications, and whether documentation needs
to change. Keep unrelated refactors out of the same pull request.

By contributing, you agree that your contribution is licensed under the project's
[MIT License](LICENSE).
