#!/usr/bin/env python3
"""One stage of the GitHub Actions build (.github/workflows/stage.yml).

A GitHub-hosted job runs at most 6 hours, a full build on its 4 cores takes several times
that. So the build is a chain of stages, as in ungoogled-chromium-windows: each stage continues
from the state the previous one saved (build/chromium and build/ungoogled-chromium without
.git, restored by the workflow before this script) and interrupts ninja at --deadline; the
workflow then saves the state for the next stage. The stage that finishes the build packs the
zip (package.py) and collects the PE files to sign.

Writes `finished=true|false` (and the zip's name and SHA-256 when finished) to $GITHUB_OUTPUT.
Exits non-zero only on failure.

Usage:
  python ci/stage.py --cpu x64|arm64 --deadline <unix time>
  python ci/stage.py --list-unsigned DIR    - print PE files in DIR without a signature
"""

import argparse
import hashlib
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / 'build'
DEPOT_TOOLS = BUILD / 'depot_tools'
CHROMIUM = BUILD / 'chromium'
SRC = CHROMIUM / 'src'
DIST = BUILD / 'dist'

DEPOT_TOOLS_URL = 'https://chromium.googlesource.com/chromium/tools/depot_tools.git'
# The revision the local build used; depot_tools does not update itself in CI.
DEPOT_TOOLS_REV = '97da8489e4b7e9dc78fa2efb846871a7b423d933'

PE_SUFFIXES = ('.exe', '.dll')


def log(msg):
    print(f'[stage {time.strftime("%H:%M:%S")}] {msg}', flush=True)


def run(*cmd, cwd=None, env=None):
    log(' '.join(map(str, cmd)))
    subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, check=True)


def chromium_tag():
    text = (ROOT / 'apply-patches.py').read_text(encoding='utf-8')
    return re.search(r"^CHROMIUM_TAG = '([^']+)'", text, re.M).group(1)


def build_env():
    env = dict(os.environ)
    env['PATH'] = str(DEPOT_TOOLS) + os.pathsep + env['PATH']
    env['DEPOT_TOOLS_UPDATE'] = '0'
    env['DEPOT_TOOLS_WIN_TOOLCHAIN'] = '0'  # Visual Studio of the runner
    env['DEPOT_TOOLS_METRICS'] = '0'
    return env


def ensure_depot_tools(env):
    """depot_tools at the pinned revision, bootstrapped (Python, CIPD) once per job: it is not
    part of the saved state."""
    if (DEPOT_TOOLS / 'gclient.py').exists():
        return
    DEPOT_TOOLS.mkdir(parents=True)
    run('git', 'init', '-q', cwd=DEPOT_TOOLS)
    run('git', 'fetch', '-q', '--depth', '1', DEPOT_TOOLS_URL, DEPOT_TOOLS_REV, cwd=DEPOT_TOOLS)
    run('git', '-c', 'advice.detachedHead=false', 'checkout', '-q', 'FETCH_HEAD', cwd=DEPOT_TOOLS)
    run('cmd', '/c', DEPOT_TOOLS / 'bootstrap' / 'win_tools.bat', cwd=DEPOT_TOOLS, env=env)


def sync(env):
    """Chromium at CHROMIUM_TAG with its dependencies, no history (first stage only)."""
    tag = chromium_tag()
    CHROMIUM.mkdir(parents=True, exist_ok=True)
    (CHROMIUM / '.gclient').write_text(
        'solutions = [{\n'
        '  "name": "src",\n'
        f'  "url": "https://chromium.googlesource.com/chromium/src.git@refs/tags/{tag}",\n'
        '  "managed": False,\n'
        '  "custom_deps": {},\n'
        '  "custom_vars": {},\n'
        '}]\n', encoding='utf-8')
    run('cmd', '/c', 'gclient', 'sync', '--no-history', '--shallow', '-D', '-j', '8',
        cwd=CHROMIUM, env=env)


def out_dir(cpu):
    return 'out/Arm64' if cpu == 'arm64' else 'out/Release'


def configure(cpu, env):
    out = out_dir(cpu)
    args = [sys.executable, ROOT / 'apply-patches.py', '--args', out]
    if cpu != 'x64':
        args += ['--cpu', cpu]
    run(*args, env=env)
    # Plain ninja instead of siso: it is interrupted and resumed across stages.
    with (SRC / out / 'args.gn').open('a', encoding='utf-8') as f:
        f.write('\n# --- CI (ci/stage.py)\nuse_siso = false\n')
    run('cmd', '/c', 'gn', 'gen', out, cwd=SRC, env=env)


def ninja(cpu, deadline, jobs, env):
    """Builds chrome until done or deadline. Returns True when the build finished."""
    cmd = [str(SRC / 'third_party' / 'ninja' / 'ninja.exe'), '-C', out_dir(cpu), 'chrome']
    if jobs:
        cmd[1:1] = ['-j', str(jobs)]
    log(' '.join(cmd) + f' (until {time.strftime("%H:%M:%S", time.localtime(deadline))})')
    proc = subprocess.Popen(cmd, cwd=SRC, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors='replace',
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    last = ['']

    def pump():
        for line in proc.stdout:
            sys.stdout.write(line)
            if line.startswith('['):
                last[0] = line.split(']', 1)[0] + ']'
        sys.stdout.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    interrupted = False
    try:
        proc.wait(max(0, deadline - time.time()))
    except subprocess.TimeoutExpired:
        interrupted = True
        log('deadline reached, interrupting ninja')
        # ninja stops, waits for running commands and deletes their partial outputs.
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            proc.wait(600)
        except subprocess.TimeoutExpired:
            log('ninja did not stop, killing it')
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)])
            proc.wait()
    reader.join(30)
    summary(f'{cpu}: ninja stopped at {last[0] or "?"}' if interrupted else
            f'{cpu}: ninja finished at {last[0] or "?"}')
    if not interrupted and proc.returncode != 0:
        sys.exit(f'ninja failed with exit code {proc.returncode}')
    return not interrupted


def is_signed(data):
    """True if the PE image has an Authenticode signature (a security directory)."""
    if data[:2] != b'MZ':
        raise ValueError('not a PE file')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('not a PE file')
    opt = pe + 24
    magic = struct.unpack_from('<H', data, opt)[0]
    dirs = opt + {0x10b: 96, 0x20b: 112}[magic]
    size = struct.unpack_from('<I', data, dirs + 4 * 8 + 4)[0]  # entry 4: security
    return size != 0


def collect_unsigned(cpu, package):
    """PE files of the zip without a signature, for signing, under DIST/unsigned/<cpu>/ with
    their paths in the zip. Files that are already signed (D3DCompiler_47.dll by Microsoft)
    keep their signature."""
    dest = DIST / 'unsigned' / cpu
    shutil.rmtree(dest, ignore_errors=True)
    kept = []
    with zipfile.ZipFile(package) as z:
        for info in z.infolist():
            if not info.filename.lower().endswith(PE_SUFFIXES):
                continue
            data = z.read(info)
            if is_signed(data):
                kept.append(info.filename)
                continue
            path = dest / info.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    count = sum(1 for p in dest.rglob('*') if p.is_file())
    summary(f'{cpu}: {count} PE files to sign; already signed: {", ".join(kept) or "none"}')


def package(cpu, env):
    for old in DIST.glob(f'chromium_*_windows_{cpu}.zip'):
        old.unlink()
    args = [sys.executable, ROOT / 'package.py'] + (['--arm64'] if cpu == 'arm64' else [])
    run(*args, env=env)
    (zip_path,) = DIST.glob(f'chromium_*_windows_{cpu}.zip')
    collect_unsigned(cpu, zip_path)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    summary(f'{cpu}: `{zip_path.name}` sha256 `{digest}`')
    output(finished='true', package=zip_path.name, sha256=digest)


def summary(line):
    log(line)
    if 'GITHUB_STEP_SUMMARY' in os.environ:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(line + '\n\n')


def output(**values):
    if 'GITHUB_OUTPUT' in os.environ:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            for key, value in values.items():
                f.write(f'{key}={value}\n')


def list_unsigned(directory):
    bad = [p for p in sorted(Path(directory).rglob('*'))
           if p.is_file() and p.suffix.lower() in PE_SUFFIXES and not is_signed(p.read_bytes())]
    for p in bad:
        print(p)
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cpu', choices=('x64', 'arm64'))
    parser.add_argument('--deadline', type=float, help='unix time to interrupt ninja at')
    parser.add_argument('-j', dest='jobs', type=int, help='ninja -j (default: ninja\'s)')
    parser.add_argument('--list-unsigned', metavar='DIR')
    opts = parser.parse_args()
    if opts.list_unsigned:
        sys.exit(list_unsigned(opts.list_unsigned))
    if not opts.cpu or not opts.deadline:
        parser.error('--cpu and --deadline are required')

    env = build_env()
    ensure_depot_tools(env)
    # Each step is skipped when the restored state already has its result.
    if not (SRC / 'chrome' / 'VERSION').exists():
        sync(env)
    if not (BUILD / 'applied-patches.json').exists():
        run(sys.executable, ROOT / 'apply-patches.py', env=env)
    if not (SRC / out_dir(opts.cpu) / 'build.ninja').exists():
        configure(opts.cpu, env)
    if ninja(opts.cpu, opts.deadline, opts.jobs, env):
        package(opts.cpu, env)
    else:
        output(finished='false')


if __name__ == '__main__':
    main()
