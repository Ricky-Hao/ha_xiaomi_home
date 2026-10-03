"""Explicit source-build trial; original patch/commands, no automatic startup hook.

Usage: python3 -B trial.py submit | status
Outputs live outside Git. A task directory is never reused or retried implicitly.
A stopped/rebuilt Workspace can interrupt the task; inspect status before recovery.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

INPUT = Path(__file__).resolve().parent
ROOT = Path('/workspaces/.private/ha-xiaomi-home-source-build/trial-2.0.19-01')
COMMIT = '1fd016ef32286de9489b7b24f1029f52c49a27b3'
PATCH_SHA = '8930ee92a7d7d732bb2175466740ac82a0b604c22ce88a9de99403438f306400'
SOURCE_SHA = '6bf0088eb036df98cf7d569dc259ac27daee869602ce7ba4180e5807a8aa6119'
LOCK_SHA = 'dd67fbf0ba9cb58beda9f59faee3b423795ba0b775b02af210aaa205bf449f81'
NODE_SHA = '9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37'
BUN_SHA = 'c678040f14fe0440eb839d37cbd0ce4c051a32da72806ac97de6a6aab6bf728f'
STATE = {}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def clean_env():
    return {'HOME': str(ROOT / 'home'), 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'CI': '1', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'TMPDIR': str(ROOT / 'tmp'),
            'XDG_CACHE_HOME': str(ROOT / 'cache')}


def record(**values):
    STATE.update(values, updated_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    temp = ROOT / 'status.tmp'
    temp.write_text(json.dumps(STATE, indent=2) + '\n')
    temp.replace(ROOT / 'status.json')


def run(label, args, env, cwd=None, timeout=300):
    record(step=label)
    print('STEP:', label, flush=True)
    child = subprocess.Popen([str(a) for a in args], cwd=cwd or ROOT, env=env,
                             start_new_session=True)
    record(command_pid=child.pid)
    try:
        rc = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise subprocess.CalledProcessError(124, label)
    if rc:
        raise subprocess.CalledProcessError(rc, label)
    record(command_pid=None)


def download(name, url, checksum, env):
    dest = ROOT / name
    run('download ' + name, ['curl', '-fsSL', '--connect-timeout', '20',
                            '--max-time', '240', url, '-o', dest], env)
    if digest(dest) != checksum:
        raise ValueError(name + ' checksum mismatch')
    return dest


def build():
    env = clean_env()
    patch = INPUT / 'catalog-readiness.patch'
    if digest(patch) != PATCH_SHA:
        raise ValueError('original patch checksum mismatch')
    record(input_hashes={p.name: digest(p) for p in
                         (Path(__file__), patch, INPUT / 'catalog.test.ts')})
    node = download('node.tar.xz',
                    'https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.xz', NODE_SHA, env)
    (ROOT / 'node').mkdir()
    run('extract Node', ['tar', '-xJf', node, '--strip-components=1', '-C', ROOT / 'node'], env)
    bun = download('bun.zip',
                   'https://github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64-baseline.zip', BUN_SHA, env)
    run('extract Bun', ['unzip', '-q', bun, '-d', ROOT], env)
    env['PATH'] = f'{ROOT}/bun-linux-x64-baseline:{ROOT}/node/bin:' + env['PATH']
    for name, expected in (('node', 'v22.22.0'), ('bun', '1.4.2')):
        actual = subprocess.check_output([name, '--version'], env=env, cwd=ROOT, timeout=20).decode().strip()
        if actual != expected:
            raise ValueError(name + ' version mismatch')
        print(name, actual, flush=True)
    archive = download('source.tar.gz',
                       'https://codeload.github.com/anomalyco/opencode/tar.gz/' + COMMIT, SOURCE_SHA, env)
    source = ROOT / 'source'
    source.mkdir()
    run('extract pinned source', ['tar', '-xzf', archive, '--strip-components=1', '-C', source], env)
    if digest(source / 'bun.lock') != LOCK_SHA:
        raise ValueError('upstream lock mismatch')
    run('check original patch', ['git', 'apply', '--check', patch], env, source)
    run('apply original patch', ['git', 'apply', patch], env, source)
    run('install frozen upstream dependencies',
        ['bun', 'install', '--filter', '@opencode/cli', '--frozen-lockfile', '--ignore-scripts'],
        env, source, timeout=900)
    if digest(source / 'bun.lock') != LOCK_SHA:
        raise ValueError('upstream lock changed')
    cli = source / 'packages/cli'
    shutil.copyfile(INPUT / 'catalog.test.ts', cli / 'test/acp/network-readiness.test.ts')
    run('original catalog readiness regression', ['bun', 'test', './test/acp/network-readiness.test.ts'],
        env, cli, timeout=180)
    env.update(OPENCODE_VERSION='2.0.19', OPENCODE_CHANNEL='latest')
    run('compile patched OpenCode 2',
        ['bun', 'run', 'script/build.ts', '--target=opencode-linux-x64-baseline',
         '--skip-install', '--skip-web-ui'], env, cli, timeout=1200)
    binary = cli / 'dist/cli-linux-x64-baseline/bin/opencode'
    version = subprocess.check_output([str(binary), '--version'], env=env, cwd=ROOT,
                                      timeout=30).decode().strip()
    if version != 'opencode v2.0.19':
        raise ValueError('compiled version mismatch')
    record(binary=str(binary), binary_sha256=digest(binary), version=version)
    print('Compiled and version-verified:', version, flush=True)


def main():
    os.umask(0o077)
    command = sys.argv[1] if len(sys.argv) == 2 else ''
    if command == 'status':
        if not (ROOT / 'status.json').exists():
            print('No worker status; inspect submission.json before any retry.')
            return
        result = json.loads((ROOT / 'status.json').read_text())
        print(json.dumps(result, indent=2))
        return
    if command == 'submit':
        # Creation is exclusive: even an interrupted or failed task is not overwritten.
        ROOT.mkdir(parents=True, mode=0o700, exist_ok=False)
        for name in ('home', 'tmp', 'cache'):
            (ROOT / name).mkdir(mode=0o700)
        with (ROOT / 'build.log').open('xb') as log:
            proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'worker'],
                                    cwd=ROOT, env=clean_env(), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
        submission = {'pid': proc.pid, 'task': ROOT.name, 'root': str(ROOT),
                      'command': 'source-build trial; no model/MCP calls'}
        (ROOT / 'submission.json').write_text(json.dumps(submission, indent=2) + '\n')
        print(json.dumps(submission))
        return
    if command != 'worker':
        raise ValueError('use submit or status')
    record(state='running', pid=os.getpid(), task=ROOT.name,
           boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
           process_start_ticks=Path('/proc/self/stat').read_text().split()[21],
           started_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    code = 0
    try:
        build()
    except subprocess.CalledProcessError as exc:
        code = exc.returncode
        record(error='command failed', failed_command=str(exc.cmd))
    except Exception as exc:
        code = 1
        record(error=type(exc).__name__ + ': ' + str(exc))
    finally:
        record(state='completed' if code == 0 else 'failed', exit_code=code, command_pid=None)
    sys.exit(code if code >= 0 else 128 - code)


if __name__ == '__main__':
    main()
