#!/usr/bin/env python3
"""Starts llama-server for a profile from models.toml (foreground, Ctrl+C to stop).

Usage:
  python audit/serve.py coder-next [--port 8080] [--dry-run]

Env: LLAMA_BIN (default ~/source/repos/llama.cpp/build-cuda/bin),
     MODELS_DIR (default ~/models).
"""

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

AUDIT = Path(__file__).resolve().parent


def load_config():
    return tomllib.loads((AUDIT / 'models.toml').read_text(encoding='utf-8'))


def model_files(profile_file):
    """All files of a (possibly sharded) model; MODELS_DIR-resolved."""
    models = Path(os.environ.get('MODELS_DIR', Path.home() / 'models'))
    first = models / profile_file
    m = re.search(r'-00001-of-(\d{5})\.gguf$', first.name)
    if not m:
        return [first]
    total = int(m.group(1))
    return [first.with_name(first.name.replace('-00001-of-', f'-{i:05d}-of-'))
            for i in range(1, total + 1)]


def command(name, port='8080'):
    config = load_config()
    profile = config['profiles'][name]
    bin_dir = Path(os.environ.get('LLAMA_BIN',
                                  Path.home() / 'source/repos/llama.cpp/build-cuda/bin'))
    exe = bin_dir / ('llama-server.exe' if os.name == 'nt' else 'llama-server')
    return [str(exe), '-m', str(model_files(profile['file'])[0]), '--host', '127.0.0.1',
            '--port', port, '--alias', name, *config['server']['args'],
            *profile.get('args', [])]


def main():
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('profile', choices=sorted(config['profiles']))
    parser.add_argument('--port', default='8080')
    parser.add_argument('--dry-run', action='store_true')
    opts = parser.parse_args()

    missing = [f for f in model_files(config['profiles'][opts.profile]['file']) if not f.exists()]
    if missing:
        sys.exit(f'Model not found: {missing[0]} (see download commands in models.toml)')
    cmd = command(opts.profile, opts.port)
    print(' '.join(cmd), flush=True)
    if not opts.dry_run:
        sys.exit(subprocess.call(cmd, cwd=Path(cmd[0]).parent))


if __name__ == '__main__':
    main()
