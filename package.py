#!/usr/bin/env python3
"""Packs a Windows build (build/chromium/src/out/Release, or out/Arm64 with --arm64)
into a portable zip.

Same file list as ungoogled-chromium-windows/package.py (Chromium's
chrome/tools/build/win/FILES.cfg, official build entries, no .pdb, no installer).
Output: build/dist/chromium_<version>_<commit>_windows_<x64|arm64>.zip. Unpack anywhere
and run chrome.exe; nothing is installed.

Usage: uv run --no-project python package.py [--arm64]
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'build' / 'chromium' / 'src'

sys.path.insert(0, str(ROOT / 'build' / 'ungoogled-chromium' / 'utils'))
import filescfg  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--arm64', action='store_true', help='pack out/Arm64')
    arm64 = parser.parse_args().arm64
    OUT = SRC / 'out' / ('Arm64' if arm64 else 'Release')
    cpu = 'arm64' if arm64 else 'x64'
    version = dict(line.split('=') for line in
                   (SRC / 'chrome' / 'VERSION').read_text().split())
    version = '{MAJOR}.{MINOR}.{BUILD}.{PATCH}'.format(**version)
    commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    dist = ROOT / 'build' / 'dist'
    dist.mkdir(parents=True, exist_ok=True)
    output = dist / f'chromium_{version}_{commit}_windows_{cpu}.zip'
    excluded = {Path(p) for p in ('mini_installer.exe', 'mini_installer_exe_version.rc',
                                  'setup.exe', 'chrome.packed.7z')}
    # Version manifests left in out/ by earlier versions of the checkout.
    excluded |= {m.relative_to(OUT) for m in OUT.glob('*.manifest') if m.stem != version}
    files = filescfg.filescfg_generator(SRC / 'chrome/tools/build/win/FILES.cfg', OUT,
                                        'arm' if arm64 else '64bit', excluded)
    filescfg.create_archive(files, (), OUT, output)
    print(f'{output} ({output.stat().st_size / 2**20:.0f} MiB)')


if __name__ == '__main__':
    main()
