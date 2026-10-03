"""Environment-only ACP launcher; never persist resolved credentials."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

STATE = Path('/workspaces/.private/ha-xiaomi-home-acp')
TOOLS = STATE / 'toolset'
TEMPLATE = Path(__file__).with_name('opencode.template.json')
PATCH_SHA256 = '8930ee92a7d7d732bb2175466740ac82a0b604c22ce88a9de99403438f306400'
TOOL_IDENTITY = {
    'baseVersion': '2.0.19',
    'upstreamCommit': '1fd016ef32286de9489b7b24f1029f52c49a27b3',
    'patchHash': PATCH_SHA256,
    'nodeVersion': '22.22.0', 'bunVersion': '1.4.2', 'acpxVersion': '0.19.3'}
MCP_NAMES = ('CONTEXT7', 'FIRECRAWL', 'GITHUB', 'GITHUB_ACTIONS')
LLM_KEYS = ('LLM_API_KEY', 'LLM_BASE_URL')
MCP_SUFFIXES = ('URL', 'API_KEY', 'ENABLED', 'AUTH_HEADER', 'AUTH_SCHEME')
ENV_KEYS = LLM_KEYS + tuple(
    f'{name}_MCP_{suffix}' for name in MCP_NAMES for suffix in MCP_SUFFIXES)


def safe_value(value):
    """Reject JSON interpolation/control characters without echoing values."""
    if not value or any(ord(char) < 32 or ord(char) > 126
                        or char in '\"\\{}' for char in value):
        raise ValueError('unsafe environment value')
    return value


def safe_url(value, *, allow_http=False):
    """Allow authorized HTTP for MCP only; keep other URL restrictions."""
    parts = urlsplit(safe_value(value))
    if (not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment
            or not (parts.scheme == 'https' or (
                parts.scheme == 'http'
                and (allow_http or parts.hostname in ('localhost', '127.0.0.1', '::1'))))):
        raise ValueError('unsafe endpoint')
    return value


def configuration(environ):
    """Build public configuration with env references, not resolved secrets."""
    for key in LLM_KEYS:
        safe_value(environ.get(key, ''))
    safe_url(environ['LLM_BASE_URL'])
    config = json.loads(TEMPLATE.read_text())
    servers = config['mcp']['servers']
    for name in MCP_NAMES:
        prefix = f'{name}_MCP_'
        enabled = environ.get(prefix + 'ENABLED', '0')
        if enabled not in ('0', '1'):
            raise ValueError('invalid MCP enable flag')
        server_name = name.lower().replace('_', '-')
        if enabled != '1':
            del servers[server_name]
            continue
        server = servers[server_name]
        safe_url(environ.get(prefix + 'URL', ''), allow_http=True)
        safe_value(environ.get(prefix + 'API_KEY', ''))
        default_header = next(iter(server['headers']))
        header = environ.get(prefix + 'AUTH_HEADER', default_header)
        if not header or not all(char.isascii() and (
                char.isalnum() or char == '-') for char in header):
            raise ValueError('invalid header name')
        scheme = environ.get(prefix + 'AUTH_SCHEME',
                             'Bearer' if header == 'Authorization' else '')
        if scheme and (not scheme.isascii() or not scheme.isalnum()):
            raise ValueError('invalid auth scheme')
        token = '{env:' + prefix + 'API_KEY}'
        server['disabled'] = False
        server['headers'] = {header: (scheme + ' ' if scheme else '') + token}
    return config


def private_directory(path):
    """Do not follow symlinks or adopt another user's state."""
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('unsafe state directory')
    path.chmod(0o700)


def prepare(state=STATE):
    """Create isolated tool state; the only generated config has no secrets."""
    os.umask(0o077)
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.parent.is_symlink():
        raise ValueError('unsafe state parent')
    private_directory(state)
    for name in ('.acpx', 'config', 'data', 'cache', 'state'):
        private_directory(state / name)
    config = {
        'defaultAgent': 'opencode',
        'defaultPermissions': 'deny-all',
        'nonInteractivePermissions': 'deny',
        'authPolicy': 'skip',
        'agents': {'opencode': {'argv': ['/usr/local/bin/opencode', 'acp']}}}
    safe_json(state / '.acpx/config.json', config)


def safe_json(path, config):
    """Write references only, without following links or adopting foreign files."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise ValueError('unsafe config file')
        os.fchmod(stream.fileno(), 0o600)
        stream.truncate(0)
        json.dump(config, stream, indent=2)
        stream.write('\n')


def environment(environ, state=STATE):
    """Do not pass Coder credentials, Git identity, or unrelated keys to ACP."""
    child = {key: environ[key] for key in ENV_KEYS if key in environ}
    child.update({
        'HOME': str(state),
        'PATH': f'{TOOLS}/node/bin:{TOOLS}/node_modules/.bin:/usr/local/bin:/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'TERM': environ.get('TERM', 'xterm-256color'),
        'XDG_CONFIG_HOME': str(state / 'config'),
        'XDG_DATA_HOME': str(state / 'data'),
        'XDG_CACHE_HOME': str(state / 'cache'),
        'XDG_STATE_HOME': str(state / 'state'),
        'OPENCODE_DISABLE_AUTOUPDATE': 'true',
        'OPENCODE_DISABLE_SHARE': 'true'})
    return child


def verify_tools(tools=TOOLS, binary=False):
    """Verify source provenance and, when requested, local build hashes/versions.

    Locally compiled binaries need not be byte-identical to the old OCI build.
    This is cache validation, not protection from same-user manifest tampering.
    """
    manifest = json.loads((tools / 'build.json').read_text())
    if manifest.get('identity') != TOOL_IDENTITY:
        raise ValueError('source-built toolset identity mismatch')
    for name in ('trial.py', 'catalog-readiness.patch', 'catalog.test.ts'):
        with Path(__file__).with_name('source-build').joinpath(name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest.get('trialInputHashes', {}).get(name):
                raise ValueError('source-build inputs changed; review before rebuilding')
    if not binary:
        return
    for name, field in (('node/bin/opencode', 'binarySha256'), ('package-lock.json', 'acpxLockSha256')):
        with (tools / name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest.get(field):
                raise ValueError('installed toolset checksum mismatch')
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        env = {'HOME': home, 'PATH': f'{tools}/node/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        for executable, expected in (
                ('node/bin/node', 'v22.22.0'),
                ('node/bin/opencode', 'opencode v2.0.19'),
                ('node_modules/.bin/acpx', '0.19.3')):
            result = subprocess.run([str(tools / executable), '--version'], env=env,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode or result.stdout.strip() != expected:
                raise ValueError('reused tool version/ABI mismatch')
            print(expected)


def readiness_environment(config):
    """Same catalog-readiness contract as the existing patched launcher."""
    selected = config['model']
    return {
        'OPENCODE_ACP_REQUIRED_MODEL': selected['providerID'] + '/' + selected['model'],
        'OPENCODE_ACP_REQUIRED_VARIANT': selected.get('variant', ''),
        'OPENCODE_ACP_CATALOG_TIMEOUT_MS': '30000'}


def main():
    """Prepare state or replace this process with the pinned executable."""
    if len(sys.argv) < 2 or sys.argv[1] not in ('prepare', 'verify', 'acpx', 'opencode'):
        raise ValueError('expected prepare, acpx or opencode')
    command, arguments = sys.argv[1], sys.argv[2:]
    verify_tools(binary=command == 'verify')
    if command == 'verify':
        return
    prepare()
    if command == 'prepare':
        missing = [key for key in LLM_KEYS if not os.environ.get(key)]
        print('ACP state ready; missing env names: ' + (', '.join(missing) or 'none'))
        return
    child = environment(os.environ)
    if command == 'opencode' and arguments not in (['--version'], ['--help']):
        config = configuration(child)
        private_directory(STATE / 'config/opencode')
        safe_json(STATE / 'config/opencode/opencode.json', config)
        child.update(readiness_environment(config))
    binary = TOOLS / ('node/bin/opencode' if command == 'opencode' else 'node_modules/.bin/acpx')
    os.execve(binary, [str(binary), *arguments], child)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        print('ACP startup refused: check installation, state permissions and '
              'the documented environment variables (values hidden).', file=sys.stderr)
        sys.exit(1)
