"""Nonblocking postStart setup; failed/interrupted tasks require explicit recovery.

Only installs tools. Never launches an ACP session or contacts LLM/MCP services.
The successful source trial is reused, and new workspaces build it once.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import runtime

HERE = Path(__file__).resolve().parent
JOB = Path('/workspaces/.private/ha-xiaomi-home-source-build/setup-v1')
spec = importlib.util.spec_from_file_location('source_trial', HERE / 'source-build/trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def write_status(**fields):
    fields['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    temporary = JOB / 'status.tmp'
    temporary.write_text(json.dumps(fields, indent=2) + '\n')
    temporary.replace(JOB / 'status.json')


def clean_env():
    return {'HOME': str(JOB / 'home'), 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'CI': '1', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'XDG_CACHE_HOME': str(JOB / 'cache')}


def link_commands():
    launcher = HERE / 'launch.sh'
    launcher.chmod(0o755)
    links = {Path('/usr/local/bin') / name: launcher for name in ('acpx', 'opencode')}
    # Refuse to replace unrelated installations, including broken symlinks.
    for path, target in links.items():
        if os.path.lexists(path) and (not path.is_symlink() or path.resolve() != target.resolve()):
            raise ValueError('command path already belongs to another installation: ' + path.name)
    for path, target in links.items():
        if not os.path.lexists(path):
            path.symlink_to(target)
    runtime.prepare()


def successful_trial():
    result = json.loads((trial.ROOT / 'status.json').read_text())
    if result.get('state') != 'completed' or result.get('exit_code') != 0:
        raise ValueError('source trial incomplete; inspect its existing status, do not retry blindly')
    for name in ('trial.py', 'catalog-readiness.patch', 'catalog.test.ts'):
        if result['input_hashes'].get(name) != trial.digest(HERE / 'source-build' / name):
            raise ValueError('source trial input changed: ' + name)
    binary = trial.ROOT / 'source/packages/cli/dist/cli-linux-x64-baseline/bin/opencode'
    if result.get('binary') != str(binary) or trial.digest(binary) != result['binary_sha256']:
        raise ValueError('source trial binary mismatch')
    if trial.digest(HERE / 'source-build/catalog-readiness.patch') != trial.PATCH_SHA:
        raise ValueError('original patch mismatch')
    return binary, result['binary_sha256']


def install():
    runtime.private_directory(runtime.STATE)
    if not trial.ROOT.exists():
        trial.ROOT.mkdir(mode=0o700, parents=True)
        for name in ('home', 'tmp', 'cache'):
            (trial.ROOT / name).mkdir(mode=0o700)
        with (trial.ROOT / 'build.log').open('xb') as log:
            # Same already-tested build worker, clean environment, persistent log.
            subprocess.run([sys.executable, '-B', str(HERE / 'source-build/trial.py'), 'worker'],
                           cwd=trial.ROOT, env=trial.clean_env(), stdin=subprocess.DEVNULL,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    binary, checksum = successful_trial()
    staging = JOB / 'toolset-staging'
    staging.mkdir(mode=0o700)  # Never overwrite incomplete work from an earlier attempt.
    shutil.copytree(trial.ROOT / 'node', staging / 'node', symlinks=True)
    shutil.copy2(binary, staging / 'node/bin/opencode')
    env = clean_env()
    env['PATH'] = str(staging / 'node/bin') + ':' + env['PATH']
    print('Installing pinned acpx 0.19.3; npm lifecycle scripts disabled.', flush=True)
    subprocess.run([str(staging / 'node/bin/npm'), 'install', '--prefix', str(staging),
                    '--ignore-scripts', '--no-audit', '--no-fund', '--save-exact', 'acpx@0.19.3'],
                   cwd=JOB, env=env, stdin=subprocess.DEVNULL, check=True, timeout=300)
    manifest = {'identity': runtime.TOOL_IDENTITY, 'binarySha256': checksum,
                'acpxLockSha256': trial.digest(staging / 'package-lock.json'),
                'trialInputHashes': json.loads((trial.ROOT / 'status.json').read_text())['input_hashes']}
    (staging / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    runtime.verify_tools(staging, binary=True)
    if runtime.TOOLS.exists():
        raise ValueError('toolset appeared concurrently; refusing replacement')
    staging.rename(runtime.TOOLS)
    link_commands()


def main():
    os.umask(0o077)
    if len(sys.argv) == 2 and sys.argv[1] == 'status':
        print((JOB / 'status.json').read_text() if (JOB / 'status.json').exists() else 'Not submitted')
        return
    if len(sys.argv) == 2 and sys.argv[1] == 'worker':
        # Lock worker ownership; postStart calls cannot create parallel writers.
        with (JOB / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            write_status(state='running', pid=os.getpid())
            try:
                install()
            except Exception as exc:
                write_status(state='failed', exit_code=1, error=type(exc).__name__)
                raise
            write_status(state='completed', exit_code=0, tools=str(runtime.TOOLS))
        return
    if len(sys.argv) != 1:
        raise ValueError('expected no argument, status or worker')
    if runtime.TOOLS.exists():
        runtime.verify_tools(binary=True)
        link_commands()
        print('Cached source-built tools verified; compilation skipped.')
        return
    JOB.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        JOB.mkdir(mode=0o700)
    except FileExistsError:
        print('Setup already submitted; inspect ' + str(JOB / 'status.json'))
        return
    for name in ('home', 'cache'):
        (JOB / name).mkdir(mode=0o700)
    write_status(state='submitted')
    with (JOB / 'install.log').open('xb') as log:
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'worker'],
                                   cwd=HERE, env=clean_env(), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    (JOB / 'submission.json').write_text(json.dumps({'pid': process.pid, 'job': str(JOB)}) + '\n')
    print('Setup submitted: ' + str(JOB) + '; pid=' + str(process.pid))


if __name__ == '__main__':
    main()
