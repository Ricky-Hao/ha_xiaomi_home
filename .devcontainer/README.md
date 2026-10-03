# Patched OpenCode 2 development container

Reuses the existing prebuilt OpenCode 2.0.19 with catalog-readiness patch,
acpx 0.19.3 and Node 22.22.0. No compilation, npm replacement or patch rewrite.
Dockerfile pins the original image digest and copies only node/, node_modules/
and build.json. Verification checks toolset identity, patch, binary hash and
versions. Private HOME, config and build.log are not copied. Python base: 3.13.

## Build input
Set ACP_TOOLCHAIN_IMAGE in the builder environment to the existing image
registry/repository, without tag, digest or credentials. Dockerfile appends the
pinned digest. Registry access must already exist. Addresses stay out of Git
but can appear in private build logs/metadata. Runtime variables alone might
not reach Envbuilder; verify this mapping before rebuilding with ref ricky-dev.
Missing/mismatched tools fail closed. Push does not activate or rebuild anything.

## Copied configuration
acp/opencode.template.json preserves the nine models, settings, variants,
cost/limit metadata, provider policy and compaction buffer. Default remains
copilot/gpt-6-astra, medium. Uses the original Responses provider package,
not the v1 schema. Inherited catalog metadata is not verified pricing/availability.
Readiness uses the selected model/variant and 30000 ms. LLM_MODEL_ID is unused.

Runtime inputs (never put secrets in build args or Git):
- LLM_API_KEY and LLM_BASE_URL for the existing Responses-compatible provider.
- Prefixes CONTEXT7, FIRECRAWL, GITHUB, GITHUB_ACTIONS each accept
  <PREFIX>_MCP_URL, <PREFIX>_MCP_API_KEY, <PREFIX>_MCP_ENABLED=1.
Context7 retains Authorization: Bearer; others retain X-MCP-API-Key.
OAuth is disabled. Optional _MCP_AUTH_HEADER/_MCP_AUTH_SCHEME override auth;
empty scheme sends the raw key. Disabled servers are omitted at runtime.
Enabling a server permits authenticated discovery. Keys must be printable ASCII
without quotes/backslashes/braces. URLs require HTTPS (HTTP only on loopback),
no userinfo/query/fragment. Resolved values are not written by the launcher.

## Boundaries
State lives outside Git at /workspaces/.private/ha-xiaomi-home-acp.
Environment allowlisting and private permissions are not a same-user/root
sandbox. Logs, sessions and authorized shell tools may expose secrets.
Ignore rules do not protect tracked files/history. Do not publish derived
images without separate layer/license/content review. No task or MCP starts
automatically. acpx defaults to deny-all/noninteractive deny; the inherited
provider policy is not a tool sandbox. Wider permissions need task authority.
Queue acknowledgement is not completion/steering; cancel is not rollback or
queue clearing. No persistent task runner is introduced.

Offline checks: python3 -B -m unittest discover -s .devcontainer/acp -v;
sh -n .devcontainer/acp/launch.sh; git diff --check.
Full image build, ACP readiness/cancel, runtime injection and real LLM/MCP
access require separate verification. No rebuild or paid prompt is implied.
