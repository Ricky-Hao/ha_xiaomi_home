# Source-built OpenCode 2 development environment

Active path: generic Python 3.13 image -> postStart bootstrap.py -> persistent
source-built OpenCode 2.0.19 + acpx 0.19.3. No Dockerfile or OCI import is used.
The old Dockerfile/importer/tests remain inactive local files pending cleanup.
Existing root user and checkout ownership are retained; no Pod privileges change.

First setup runs asynchronously using the already-tested source-build/trial.py:
fixed upstream commit, original readiness patch, frozen Bun dependencies, four
regression tests and compilation. A successful matching trial is reused.
Node 22.22.0 and Bun 1.4.2 downloads are hash-pinned. acpx is installed with
npm lifecycle scripts disabled. Its generated lock is cached/hash-checked;
fresh installs may resolve different transitive dependencies.
Later starts verify cache/provenance and restore command links, without compiling.
Failures are nonfatal to the startup hook, NOT evidence that tools are ready.
Changed inputs, invalid cache or failed jobs require review, not automatic retry.

Commands: opencode, acpx. Inspect setup using:
python3 -B .devcontainer/acp/bootstrap.py status
Logs/status: /workspaces/.private/ha-xiaomi-home-source-build/setup-v1/
Source trial: the adjacent trial-2.0.19-01/ directory.
Tools/runtime: /workspaces/.private/ha-xiaomi-home-acp/
Stop/rebuild may interrupt workers. A Coder stop/start with the existing cache
was verified: command links recovered and source/trial/cache fingerprints stayed
unchanged. A brand-new empty-volume automatic build is not yet end-to-end tested.

Keep existing nine-model config, default copilot/gpt-6-astra / medium.
User Secrets provide `LLM_API_KEY` and `LLM_BASE_URL`.
MCP variable prefixes: `CONTEXT7`, `FIRECRAWL`, `GITHUB`, `GITHUB_ACTIONS`.
For each prefix, set the following suffixes:
- `_MCP_URL`: endpoint address.
- `_MCP_API_KEY`: runtime credential.
- `_MCP_ENABLED`: set to `1` to enable the server.
Context7 uses Bearer; other MCPs use X-MCP-API-Key. OAuth stays disabled.
LLM_MODEL_ID and ACP_TOOLCHAIN_IMAGE are unused. MCP endpoints accept HTTP
or HTTPS by explicit user authorization; HTTP transmits credentials unencrypted.
LLM endpoints still require HTTPS except loopback. URLs still reject embedded
credentials, query strings, fragments and non-HTTP(S) schemes.
Build workers get a clean environment/HOME, not user keys. This does not isolate
against root/same-user access. Never publish runtime logs, sessions or secrets.

Bootstrap starts no model request, ACP session or MCP discovery. acpx defaults
to deny-all/noninteractive deny. A minimal standalone LLM request returned the
expected text with no tool calls. MCP connections and ACP cancellation are untested.
Offline suites: test_runtime.py and test_bootstrap.py under acp/.
Version checks and cached recovery after Workspace restart pass. Home Assistant
runtime and integration-test dependencies are intentionally not installed.
Only the active source-build configuration is included in the migration commit;
obsolete local OCI recovery edits are not part of the supported startup path.
GitHub SSH host trust is container-local; after replacement, restore a verified
GitHub host key before Git SSH operations. Never disable host-key verification.
