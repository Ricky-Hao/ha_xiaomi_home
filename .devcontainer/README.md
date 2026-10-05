# Xiaomi Home project development environment

This devcontainer uses the Python 3.13 Bookworm image, the existing root user and
workspace folder, and the Python/Pylance editor extensions. It installs project
test dependencies once through `postCreateCommand`; there is no per-start hook.
The image is used directly, without a Dockerfile or source-build toolchain.
`.dockerignore` retains the Envbuilder generated-file allowlist unchanged.

## Project dependencies

`requirements-dev.txt` mirrors the unit-test packages in
`.github/workflows/test.yaml`: pytest and its asyncio/dependency plugins, plus the
libraries needed to import and test the integration. Versions are unpinned just
as in that workflow; this is a development dependency list, not a lockfile.

The create hook then installs **`homeassistant==2024.4.4` with `--no-deps`**, matching
CI. `test/test_cloud_poll_integration.py` reads the original `multi_select`
validator from that distribution and mocks the remaining HA runtime imports.
Keep this installation separate from the requirements list so pip does not pull
the full HA service dependency tree. This environment does not start Home Assistant
or install all production dependencies from the integration manifest. CI's separate
lint, HACS, hassfest and service-setup jobs are not reproduced by this test setup.

The create hook needs package-index access. A failed install remains visible as a
failed hook. To install or refresh the same dependencies manually from the repo root:

```sh
python -m pip install -r .devcontainer/requirements-dev.txt
python -m pip install --no-deps homeassistant==2024.4.4
```

## Tests

Focused offline project checks, using synthetic I/O and the HA validator fixture:

```sh
python -B -m pytest -m github test/check_rule_format.py test/test_common.py test/test_cloud_poll.py test/test_cloud_poll_integration.py
python -B -m unittest discover -s .devcontainer/tests -v
```

Run tests in a disposable checkout when local test data must be preserved:
`test/conftest.py` creates and removes `test/miot`, `test/test_cache` and
`test/manifest.json`. Avoid unfiltered test runs for this offline workflow; other
tests can require real cloud, network or device access.

The repository devcontainer owns project dependencies only. Workspace-level coding
tools and their configuration/state are supplied separately by the workspace
infrastructure. Keep local credentials, sessions and caches outside Git; the existing
`.gitignore` protections remain in effect.
