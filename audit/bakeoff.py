#!/usr/bin/env python3
"""Compares model profiles on the quick eval set, unattended.

For each profile: waits until its model files exist (downloads may still be
running), starts llama-server, waits for /health, runs triage.py on
build/audit/eval/eval-quick.jsonl, stops the server. Prints one line per
profile and the score table at the end.

Usage:
  python audit/bakeoff.py qwen36-35b coder-next gpt-oss-120b flash-next
  python audit/eval.py build      # first, if eval-quick.jsonl does not exist
"""

import argparse
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from common import AUDIT, WORK, read_jsonl
from serve import command, load_config, model_files

EVAL = WORK / 'eval'
PORT = '8080'


def healthy():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


class Sampler(threading.Thread):
    """Average GPU utilization and llama-server CPU cores during a run: shows
    whether the GPU or the server's sampling thread is the bottleneck."""

    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.gpu, self.stop = pid, [], threading.Event()
        self.cpu0, self.t0 = cpu_seconds(pid), time.time()

    def run(self):
        while not self.stop.wait(2):
            out = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu',
                                  '--format=csv,noheader,nounits'],
                                 capture_output=True, text=True).stdout.strip()
            if out.isdigit():
                self.gpu.append(int(out))

    def result(self):
        self.stop.set()
        cores = (cpu_seconds(self.pid) - self.cpu0) / max(1, time.time() - self.t0)
        gpu = sum(self.gpu) / max(1, len(self.gpu))
        return f'GPU {gpu:.0f}%, server CPU {cores:.1f} cores'


def cpu_seconds(pid):
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          f'(Get-Process -Id {pid}).TotalProcessorTime.TotalSeconds'],
                         capture_output=True, text=True).stdout.strip().replace(',', '.')
    try:
        return float(out)
    except ValueError:
        return 0.0


def slots(cmd):
    """Client concurrency = the last -np in the server command (later flags win)."""
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == '-np'][-1]


def wait_for_files(files, hours):
    deadline = time.time() + hours * 3600
    while not all(f.exists() for f in files):
        if time.time() > deadline:
            return False
        time.sleep(60)
    return True


def run_profile(name, opts):
    files = model_files(load_config()['profiles'][name]['file'])
    if not wait_for_files(files, opts.wait_hours):
        return f'{name}: model files not downloaded'
    if healthy():
        sys.exit(f'Port {PORT} is busy: stop the running llama-server first.')
    log = open(EVAL / f'server-{name}.log', 'w', encoding='utf-8')
    cmd = command(name, PORT)
    server = subprocess.Popen(cmd, cwd=Path(cmd[0]).parent, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while not healthy():
            if server.poll() is not None:
                return f'{name}: server exited with {server.returncode}, see {log.name}'
            if time.time() - t0 > 1800:
                return f'{name}: server not ready after 30 min'
            time.sleep(5)
        load = time.time() - t0
        out = EVAL / f'{opts.set}-{name}.jsonl'
        t1 = time.time()
        sampler = Sampler(server.pid)
        sampler.start()
        with open(EVAL / f'triage-{opts.set}-{name}.log', 'w', encoding='utf-8') as triage_log:
            subprocess.run([sys.executable, str(AUDIT / 'triage.py'), '--model', name,
                            '--candidates', str(EVAL / f'eval-{opts.set}.jsonl'), '--out', str(out),
                            '--no-cache', '--parallel', slots(cmd)],
                           stdout=triage_log, stderr=subprocess.STDOUT, check=False)
        wall = time.time() - t1
        usage = sampler.result()
        n = len(read_jsonl(out)) if out.exists() else 0
        per_min = n / wall * 60 if wall else 0
        return (f'{name}: {n} items in {wall / 60:.1f} min ({per_min:.1f}/min, '
                f'{usage}, '
                f'full run ~{1237 / max(per_min, 0.01) / 60:.1f} h)')
    finally:
        server.terminate()
        try:
            server.wait(60)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('profiles', nargs='+')
    parser.add_argument('--wait-hours', type=float, default=6)
    parser.add_argument('--set', default='quick', choices=['quick', 'perf'],
                        help='perf: 240 random real candidates, for throughput only')
    opts = parser.parse_args()
    for name in opts.profiles:
        print(run_profile(name, opts), flush=True)
    if opts.set == 'perf':
        return
    outs = [str(EVAL / f'quick-{n}.jsonl') for n in opts.profiles
            if (EVAL / f'quick-{n}.jsonl').exists()]
    subprocess.run([sys.executable, str(AUDIT / 'eval.py'), 'score', '--quick', *outs])


if __name__ == '__main__':
    main()
