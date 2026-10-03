# Development container

Public-only Python 3.13 development image with Node 22.22.0,
`acpx@0.19.3` and `opencode-ai@1.18.34`. Tool releases are pinned, but
base-image tags and transitive npm dependencies are not content-addressed.
This is the public npm OpenCode v1 configuration, not a private patched v2
build. Custom model-catalog/readiness patches are intentionally not included.
No private registry, SSH identity, internal URL or credentials are required to build.
Home Assistant is not deployed and project test dependencies are not installed.

## Runtime environment variables

Inject these through Coder into the **running container / agent environment**.
Do not put values in this repository, devcontainer `containerEnv` / `remoteEnv`,
Dockerfile `ARG` / `ENV`, image build secrets, a committed `.env`, or shell history.
The JSON deliberately has no `${localEnv:...}` secret expansion: resolved
container metadata and build logs must not become credential storage.
An interactive shell-only export is not sufficient for an existing ACP process.
How Coder persists these values is template-dependent; use its protected
runtime-secret mechanism rather than a public/plaintext template parameter.

Required for OpenCode model operations:

| Variable | Meaning |
| --- | --- |
| `LLM_API_KEY` | LLM API credential |
| `LLM_BASE_URL` | OpenAI-compatible API base URL, including `/v1` if required |
| `LLM_MODEL_ID` | Actual model ID accepted by that endpoint |

The local model alias is `workspace/configured`; both normal and small-model
operations use that configured model. There is no fallback to another provider.
This adapter uses the OpenAI-compatible API; a provider requiring a different
protocol needs a reviewed configuration change. Token values must be printable
ASCII without quotes, backslashes or braces. Endpoints must use HTTPS (HTTP is
allowed only for loopback), with no userinfo, query string or fragment.

Optional remote MCP slots (nothing connects by default):

| MCP | URL variable | Key variable | Explicit connection opt-in |
| --- | --- | --- | --- |
| Context7 | `CONTEXT7_MCP_URL` | `CONTEXT7_MCP_API_KEY` | `CONTEXT7_MCP_ENABLED=1` |
| Firecrawl | `FIRECRAWL_MCP_URL` | `FIRECRAWL_MCP_API_KEY` | `FIRECRAWL_MCP_ENABLED=1` |
| GitHub | `GITHUB_MCP_URL` | `GITHUB_MCP_API_KEY` | `GITHUB_MCP_ENABLED=1` |
| GitHub Actions | `GITHUB_ACTIONS_MCP_URL` | `GITHUB_ACTIONS_MCP_API_KEY` | `GITHUB_ACTIONS_MCP_ENABLED=1` |

Each server uses `Authorization: Bearer <key>` and OAuth is disabled.
If required by a server, set `<NAME>_MCP_AUTH_HEADER` to its header name and
`<NAME>_MCP_AUTH_SCHEME` to an empty string for a raw API-key header.
For example, direct Context7 endpoints may require the header `CONTEXT7_API_KEY`
instead of Bearer authentication; confirm against your actual endpoint.
Enabling a slot permits an authenticated connection and tool discovery when
OpenCode starts. Tool execution remains denied by the default permission policy.
Do not enable servers or broaden tool permissions just to test setup.

## Activation and commands

The current fallback container does not change just because this branch is pushed.
Before a separately authorized rebuild, select `ricky-dev` as the repository ref
using the template's supported branch mechanism and inject the runtime variables.
Do not assume restarting an existing cached main-branch image applies these files.
The `postStartCommand` creates private runtime state without requiring keys,
connecting to any MCP, starting an agent task, or issuing a paid prompt.

After the image is built:

```sh
cd /workspaces/ha_xiaomi_home
acpx --version
opencode --version
acpx config show
```

After credentials and permissions have been explicitly approved for a task:

```sh
acpx opencode sessions new
acpx --format json opencode --no-wait 'the authorized task'
acpx opencode status
acpx opencode sessions show
acpx opencode cancel
```

`--no-wait` acknowledges queue submission, not completion or steering. Record
the session identifier and retain output/exit status outside Git. `status` shows
local process/lease state, not task success. Cancellation is cooperative and does
not roll back edits or clear queued turns. An idle `nothing to cancel` response
is not a cancellation test. Workspace stop/rebuild can kill active work.
No custom persistent submit/observe/cancel runner is included or validated here.
Default OpenCode and acpx permissions are deny-all; a future coding task must
explicitly review and grant its necessary tools. This is not an OS sandbox.

## Credential and privacy boundary

The wrappers isolate HOME and XDG state under
`/workspaces/.private/ha-xiaomi-home-acp`, outside the repository (directories
0700, generated acpx config 0600). Only documented LLM/MCP environment names
are forwarded; unrelated Coder credentials and Git identity variables are not.
OpenCode config contains `{env:...}` references supplied in memory, never resolved
keys written by the launcher. No auth file, provider login or key copying is used.
Use the wrappers on PATH, not raw `/opt/acp/node_modules/.bin` executables.

Environment variables are **not** a vault: same-user/root processes, debugging,
child tools, crash dumps, container administrators and provider/MCP services may
see credentials or request content. OpenCode's resolved debug config, tool output,
session databases and transcripts can contain secrets. Never publish these or
run `opencode debug config`, `env`, `printenv` or shell tracing with real keys.
Permission changes and local configs can weaken these defaults. `.gitignore`
only prevents accidental new staging; it cannot protect tracked files, forced
adds, history, image layers, logs, or uploads. Do not copy another workspace's
private configuration, patches, registry metadata, SSH keys or known_hosts.
Use GitHub noreply author/committer emails when publishing; environment identity
variables override repository-local Git config. Existing upstream authors remain.

## Offline checks and limitations

```sh
python3 -B -m unittest discover -s .devcontainer/acp -p 'test_*.py' -v
sh -n .devcontainer/acp/launch.sh
git diff --check
```

Tests use dummy credentials and make no service requests. A passed test or
schema check is not proof of image build, Coder secret injection, model responses,
MCP connectivity, patched-v2 equivalence, or active cancellation. Validate those
separately after rebuilding, using the intended ref and explicit task authority.
Public references: [OpenCode config](https://opencode.ai/docs/config/),
[acpx config](https://github.com/openclaw/acpx/blob/v0.19.3/docs/config.md),
[acpx CLI](https://github.com/openclaw/acpx/blob/v0.19.3/docs/CLI.md).
