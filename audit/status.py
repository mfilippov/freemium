#!/usr/bin/env python3
"""One-screen status of the audit: model downloads, the running llama-server,
triage progress and the eval score table.

Usage: python audit/status.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from common import AUDIT, FINDINGS, WORK, read_jsonl

MODELS = Path(os.environ.get('MODELS_DIR', Path.home() / 'models'))
EVAL = WORK / 'eval'


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def ago(path):
    minutes = (time.time() - path.stat().st_mtime) / 60
    return f'{minutes:.0f} min ago' if minutes < 120 else f'{minutes / 60:.1f} h ago'


def downloads():
    partial = list(MODELS.glob('*/.cache/huggingface/download/**/*.incomplete'))
    active = [p for p in partial if time.time() - p.stat().st_mtime < 120]
    if not active:
        return 'downloads: none active'
    return 'downloads: ' + ', '.join(f'{p.relative_to(MODELS).parts[0]} {p.stat().st_size / 1e9:.1f} GB'
                                     for p in active)


def server():
    models = get('http://127.0.0.1:8080/v1/models')
    if not models:
        return 'llama-server: not running'
    name = models['data'][0]['id']
    slots = get('http://127.0.0.1:8080/slots') or []
    busy = sum(1 for s in slots if s.get('is_processing'))
    return f'llama-server: {name}, {busy}/{len(slots)} slots busy'


def progress():
    lines = []
    total = {'quick': len(read_jsonl(EVAL / 'eval-quick.jsonl'))
             if (EVAL / 'eval-quick.jsonl').exists() else 0}
    for path in sorted(EVAL.glob('quick-*.jsonl'), key=lambda p: p.stat().st_mtime):
        n = len({r['id'] for r in read_jsonl(path)})
        lines.append(f'  eval {path.stem[6:]:22} {n:>4}/{total["quick"]}  ({ago(path)})')
    for path in sorted(FINDINGS.glob('*/*.jsonl')):
        cands = WORK / path.parent.name / 'candidates.jsonl'
        n_total = len(read_jsonl(cands)) if cands.exists() else '?'
        n = len({r['id'] for r in read_jsonl(path)})
        lines.append(f'  full {path.parent.name}/{path.stem:14} {n:>4}/{n_total}  ({ago(path)})')
    return 'triage:\n' + '\n'.join(lines) if lines else 'triage: nothing yet'


def main():
    print(downloads())
    print(server())
    print(progress())
    files = [str(p) for p in sorted(EVAL.glob('quick-*.jsonl'))]
    if files:
        sys.stdout.flush()
        subprocess.run([sys.executable, str(AUDIT / 'eval.py'), 'score', '--quick', *files])


if __name__ == '__main__':
    main()
