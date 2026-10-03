"""Environment-only ACP launcher; never persist resolved credentials."""

import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

STATE = Path('/workspaces/.private/ha-xiaomi-home-acp')
TOOLS = Path('/opt/acp/node_modules/.bin')
MCP_NAMES = ('CONTEXT7', 'FIRECRAWL', 'GITHUB', 'GITHUB_ACTIONS')
LLM_KEYS = ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL_ID')
MCP_SUFFIXES = ('URL', 'API_KEY', 'ENABLED', 'AUTH_HEADER', 'AUTH_SCHEME')
ENV_KEYS = LLM_KEYS + tuple(
    f'{name}_MCP_{suffix}' for name in MCP_NAMES for suffix in MCP_SUFFIXES)


def safe_value(value):
    """Reject JSON interpolation/control characters without echoing values."""
    if not value or any(ord(char) < 32 or ord(char) > 126
                        or char in '\"\\{}' for char in value):
        raise ValueError('unsafe environment value')
    return value


def safe_url(value):
    """Require TLS, except for loopback services; reject embedded credentials."""
    parts = urlsplit(safe_value(value))
    if (not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment
            or not (parts.scheme == 'https' or (
                parts.scheme == 'http'
                and parts.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise ValueError('unsafe endpoint')
    return value


def configuration(environ):
    """Build public configuration with env references, not resolved secrets."""
    for key in LLM_KEYS:
        safe_value(environ.get(key, ''))
    safe_url(environ['LLM_BASE_URL'])
    config = {
        '$schema': 'https://opencode.ai/config.json',
        'autoupdate': False,
        'share': 'disabled',
        'permission': {'*': 'deny'},
        'enabled_providers': ['workspace'],
        'model': 'workspace/configured',
        'small_model': 'workspace/configured',
        'provider': {'workspace': {
            'npm': '@ai-sdk/openai-compatible',
            'name': 'Environment-configured provider',
            'options': {
                'baseURL': '{env:LLM_BASE_URL}',
                'apiKey': '{env:LLM_API_KEY}'},
            'models': {'configured': {
                'id': '{env:LLM_MODEL_ID}', 'name': 'Configured model'}}}},
        'mcp': {}}
    for name in MCP_NAMES:
        prefix = f'{name}_MCP_'
        enabled = environ.get(prefix + 'ENABLED', '0')
        if enabled not in ('0', '1'):
            raise ValueError('invalid MCP enable flag')
        if enabled != '1':
            continue
        safe_url(environ.get(prefix + 'URL', ''))
        safe_value(environ.get(prefix + 'API_KEY', ''))
        header = environ.get(prefix + 'AUTH_HEADER', 'Authorization')
        if not header or not all(char.isascii() and (
                char.isalnum() or char == '-') for char in header):
            raise ValueError('invalid header name')
        scheme = environ.get(prefix + 'AUTH_SCHEME', 'Bearer')
        if scheme and (not scheme.isascii() or not scheme.isalnum()):
            raise ValueError('invalid auth scheme')
        token = '{env:' + prefix + 'API_KEY}'
        config['mcp'][name.lower()] = {
            'type': 'remote', 'url': '{env:' + prefix + 'URL}',
            'oauth': False, 'enabled': True,
            'headers': {header: (scheme + ' ' if scheme else '') + token}}
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
    path = state / '.acpx/config.json'
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
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'LANG': 'C.UTF-8',
        'TERM': environ.get('TERM', 'xterm-256color'),
        'XDG_CONFIG_HOME': str(state / 'config'),
        'XDG_DATA_HOME': str(state / 'data'),
        'XDG_CACHE_HOME': str(state / 'cache'),
        'XDG_STATE_HOME': str(state / 'state'),
        'OPENCODE_DISABLE_AUTOUPDATE': 'true',
        'OPENCODE_DISABLE_SHARE': 'true'})
    return child


def main():
    """Prepare state or replace this process with the pinned executable."""
    if len(sys.argv) < 2 or sys.argv[1] not in ('prepare', 'acpx', 'opencode'):
        raise ValueError('expected prepare, acpx or opencode')
    command, arguments = sys.argv[1], sys.argv[2:]
    prepare()
    if command == 'prepare':
        missing = [key for key in LLM_KEYS if not os.environ.get(key)]
        print('ACP state ready; missing env names: ' + (', '.join(missing) or 'none'))
        return
    child = environment(os.environ)
    if command == 'opencode' and arguments not in (['--version'], ['--help']):
        child['OPENCODE_CONFIG_CONTENT'] = json.dumps(configuration(child))
    binary = TOOLS / command
    os.execve(binary, [str(binary), *arguments], child)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        print('ACP startup refused: check installation, state permissions and '
              'the documented environment variables (values hidden).', file=sys.stderr)
        sys.exit(1)
