#!/usr/bin/env python3
"""Applies our patch series to build/chromium/src.

Series files (applied in this order, see patches/series for the line format):
  patches/series             - all platforms
  patches/series.<platform>  - platform-specific

Usage (from the build env shell):
  python3 apply-patches.py                    - apply for the host platform
  python3 apply-patches.py --platform linux   - apply for another platform
  python3 apply-patches.py --reset            - revert files touched by a previous apply
  python3 apply-patches.py --args out/Release - write args.gn from args/common.gn + args/<platform>.gn

The Chromium checkout must be at CHROMIUM_TAG (git checkout <tag> && gclient sync -D).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

CHROMIUM_TAG = '157.0.8103.38'

# Upstream patch sources, referenced from series files as <name>/<path>.
# ungoogled-chromium tag N-R targets Chromium tag N; platform repo commits
# must be the ones updated to the same tag.
SOURCES = {
    'ungoogled': ('https://github.com/ungoogled-software/ungoogled-chromium.git',
                  '157.0.8103.38-1'),
    'ungoogled-windows': ('https://github.com/ungoogled-software/ungoogled-chromium-windows.git',
                          'f85bc55429c37b1c748b676b18fe79b38eee3207'),
    # Lags behind: latest is for 156.0.8078.79, not CHROMIUM_TAG.
    'ungoogled-macos': ('https://github.com/ungoogled-software/ungoogled-chromium-macos.git',
                        '31f01756a43d55b2c4211e73ca329bfab4ebfcb2'),
    # Lags behind: latest is for 156.0.8078.79, not CHROMIUM_TAG.
    'ungoogled-portablelinux': (
        'https://github.com/ungoogled-software/ungoogled-chromium-portablelinux.git',
        '6f0aa178c2394fc0a177edc0fd64e41f402a0418'),
}

PLATFORMS = ('windows', 'mac', 'linux')

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / 'build'
SRC = BUILD / 'chromium' / 'src'
PATCHES = ROOT / 'patches'
ARGS = ROOT / 'args'
STATE_FILE = BUILD / 'applied-patches.json'


def host_platform():
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform == 'darwin':
        return 'mac'
    return 'linux'


def run(*cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def git(*args, cwd):
    return run('git', *args, cwd=cwd).stdout.strip()


def find_patch_bin():
    candidates = []
    # Git for Windows ships a recent GNU patch in usr/bin, usually not on PATH;
    # prefer it over e.g. Strawberry Perl's patch 2.5.9 that is on PATH.
    # `git` on PATH may be a depot_tools wrapper; --exec-path points into the
    # real installation (<git>/mingw64/libexec/git-core).
    exec_path = run('git', '--exec-path', check=False).stdout.strip()
    if exec_path:
        candidates += [p / 'usr' / 'bin' / 'patch.exe' for p in Path(exec_path).parents]
    if shutil.which('patch'):
        candidates.append(Path(shutil.which('patch')))
    for candidate in candidates:
        if not candidate.exists():
            continue
        version = run(str(candidate), '--version', check=False).stdout.split('\n')[0].split()
        # "GNU patch 2.7.6"; older versions mishandle some ungoogled patches.
        if version[:2] == ['GNU', 'patch'] and tuple(map(int, version[2].split('.')[:2])) >= (2, 7):
            return str(candidate)
    sys.exit('GNU patch >= 2.7 not found: install Git for Windows or put it on PATH.')


def resolve(path, ref):
    result = run('git', 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}', cwd=path, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_source(name):
    """Clones the upstream source if needed, checks out its pinned ref and
    returns its patches directory."""
    url, ref = SOURCES[name]
    path = BUILD / Path(url).stem
    if not path.exists():
        print(f'Cloning {url}')
        run('git', 'clone', '-q', url, str(path))
    target = resolve(path, ref)
    if target is None:
        run('git', 'fetch', '-q', '--tags', 'origin', cwd=path)
        target = resolve(path, ref)
        if target is None:
            sys.exit(f'{ref} not found in {url}')
    if git('rev-parse', 'HEAD', cwd=path) != target:
        run('git', '-c', 'advice.detachedHead=false', 'checkout', '-q', target, cwd=path)
    return path / 'patches'


def read_series(platform):
    patches = []
    for series in (PATCHES / 'series', PATCHES / f'series.{platform}'):
        if not series.exists():
            continue
        for line in series.read_text(encoding='utf-8').splitlines():
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            source, _, rest = line.partition('/')
            if source in SOURCES:
                patches.append((line, ensure_source(source) / rest))
            else:
                patches.append((line, PATCHES / line))
    return patches


def touched_files(patch_path):
    files = set()
    for line in patch_path.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith(('+++ ', '--- ')):
            name = line[4:].split('\t')[0].strip()
            if name[:2] in ('a/', 'b/'):
                files.add(name[2:])
    return files


def check_chromium_version():
    version = dict(line.split('=', 1) for line in (SRC / 'chrome' / 'VERSION').read_text().split())
    current = '.'.join(version[k] for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))
    if current != CHROMIUM_TAG:
        sys.exit(f'Chromium checkout is {current}, expected {CHROMIUM_TAG}.\n'
                 f'Run in {SRC}: git checkout {CHROMIUM_TAG} && gclient sync -D')


def apply(platform):
    if STATE_FILE.exists():
        sys.exit(f'Patches already applied ({STATE_FILE}). Run with --reset first.')
    check_chromium_version()
    patches = read_series(platform)
    for name, path in patches:
        if not path.exists():
            sys.exit(f'Patch not found: {name} ({path})')
    patch_bin = find_patch_bin()
    state = {'platform': platform, 'patches': [], 'files': []}
    try:
        for i, (name, path) in enumerate(patches, 1):
            print(f'[{i}/{len(patches)}] {name}')
            # Record files before applying so --reset also covers a partial failure.
            state['files'] = sorted(set(state['files']) | touched_files(path))
            result = run(patch_bin, '-p1', '--forward', '--no-backup-if-mismatch', '-s',
                         '-d', str(SRC), '-i', str(path), check=False)
            if result.returncode != 0:
                print(result.stdout + result.stderr)
                sys.exit(f'FAILED: {name}\n'
                         'Fix it, then: python3 apply-patches.py --reset && python3 apply-patches.py')
            state['patches'].append(name)
    finally:
        STATE_FILE.write_text(json.dumps(state, indent=1), encoding='utf-8')
    print(f'Applied {len(patches)} patches for {platform}.')


def reset():
    if not STATE_FILE.exists():
        sys.exit('Nothing to reset.')
    state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    for name in state['files']:
        path = SRC / name
        # Patches may touch nested dependency repos (third_party/...).
        repo_dir = path.parent
        while not repo_dir.exists():
            repo_dir = repo_dir.parent
        repo = Path(git('rev-parse', '--show-toplevel', cwd=repo_dir)).resolve()
        rel = path.resolve().relative_to(repo).as_posix()
        if run('git', 'ls-files', '--error-unmatch', rel, cwd=repo, check=False).returncode == 0:
            run('git', 'checkout', '--', rel, cwd=repo)
        elif path.exists():
            path.unlink()
    STATE_FILE.unlink()
    print(f'Reverted {len(state["files"])} files.')


def write_args(platform, out_dir):
    args = ''
    for name in ('common.gn', f'{platform}.gn'):
        args += f'# --- args/{name}\n' + (ARGS / name).read_text(encoding='utf-8') + '\n'
    out = SRC / out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / 'args.gn').write_text(args, encoding='utf-8')
    print(f'Wrote {out / "args.gn"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--platform', choices=PLATFORMS, default=host_platform())
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--reset', action='store_true', help='revert previously applied patches')
    group.add_argument('--args', metavar='OUT_DIR', help='write args.gn into OUT_DIR')
    opts = parser.parse_args()
    if opts.reset:
        reset()
    elif opts.args:
        write_args(opts.platform, opts.args)
    else:
        apply(opts.platform)


if __name__ == '__main__':
    main()
