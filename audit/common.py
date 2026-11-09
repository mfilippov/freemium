"""Shared paths and helpers for the audit scripts (stdlib only)."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / 'audit'
SRC = ROOT / 'build' / 'chromium' / 'src'
WORK = ROOT / 'build' / 'audit'          # large, regenerable outputs (not committed)
FINDINGS = AUDIT / 'findings'            # committed model verdicts, double as the cache

# Code that never ships in our desktop builds (Windows now, Linux/macOS later).
# remoting/ is only built with enable_remoting=true (args/common.gn sets false).
EXCLUDED_DIRS = (
    'v8/', 'ios/', 'android_webview/', 'chromeos/', 'ash/', 'fuchsia_web/',
    'native_client', 'tools/', 'docs/', 'testing/', 'build/', 'buildtools/', 'infra/',
    'chromecast/', 'remoting/', 'chrome/test/', 'content/test/', 'content/shell/',
    'components/cronet/', 'weblayer/', 'out/',
)
TEST_PARTS = re.compile(
    r'(^|[/_.])(tests?|testing|test_support|unittests?|browsertests?|uitests?|fuzzers?|fuzz|'
    r'mocks?|fakes?|perftests?)([/_.]|$)', re.I)
# Platform-only code is marked by a directory or a file suffix, not by any
# path part: e.g. devtools/device/android_* and media_router/cast_* are desktop.
PLATFORM_PARTS = re.compile(
    r'(^|/)(android|chromeos|ash|fuchsia|ios|lacros)/|'
    r'_(android|chromeos|ash|fuchsia|ios|lacros)(_\w+)?\.(cc|h|mm)$')

# Third-party code scanned for URL literals; traffic annotations are collected
# from all of third_party since those mark Chromium glue code.
INCLUDED_THIRD_PARTY = (
    'third_party/blink/renderer/', 'third_party/blink/public/',
)


OUT_DIR = 'out/Release'                  # GN build dir whose inputs define "built"
_build_set = None


def build_set():
    """Source files the `chrome` target compiles in OUT_DIR, from the build graph
    (`ninja -t inputs chrome`, ~35 s, cached per build.ninja). This replaces
    guessing platform/test code from paths and makes "not built" a fact.
    Returns None when the build dir is missing (falls back to path rules)."""
    global _build_set
    if _build_set is not None:
        return _build_set or None
    ninja_file = SRC / OUT_DIR / 'build.ninja'
    if not ninja_file.exists():
        print(f'WARNING: {ninja_file} missing, using path heuristics; run gn gen first',
              file=sys.stderr)
        _build_set = set()
        return None
    cache = WORK / f'build-inputs-{OUT_DIR.replace("/", "_")}.txt'
    if not cache.exists() or cache.stat().st_mtime < ninja_file.stat().st_mtime:
        ninja = ROOT / 'build' / 'depot_tools' / ('ninja.bat' if sys.platform == 'win32' else 'ninja')
        out = subprocess.run([str(ninja), '-C', OUT_DIR, '-t', 'inputs', 'chrome'], cwd=SRC,
                             capture_output=True, text=True, check=True).stdout
        prefix = '../../'
        paths = [p[len(prefix):] for p in out.splitlines() if p.startswith(prefix)]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text('\n'.join(paths), encoding='utf-8')
    _build_set = set(cache.read_text(encoding='utf-8').splitlines())
    return _build_set


def is_built(path):
    built = build_set()
    if built is None:
        return not path.startswith(EXCLUDED_DIRS) and not PLATFORM_PARTS.search(path)
    if path in built:
        return True
    # Headers are inputs only when listed in `sources`, and header-only
    # constants (e.g. chrome/common/url_constants.h) have no .cc next to them:
    # accept a header when anything in its directory is built.
    if path.endswith('.h'):
        return path.rsplit('/', 1)[0] in _built_dirs()
    return False


_dirs = None


def _built_dirs():
    global _dirs
    if _dirs is None:
        _dirs = {p.rsplit('/', 1)[0] for p in build_set() if '/' in p}
    return _dirs


def is_relevant(path, any_third_party=False):
    if not is_built(path):
        return False
    if path.startswith('third_party/'):
        if not any_third_party and not path.startswith(INCLUDED_THIRD_PARTY):
            return False
        path = path[len('third_party/'):]
    return not TEST_PARTS.search(path)


def chromium_version():
    version = dict(line.split('=', 1) for line in (SRC / 'chrome' / 'VERSION').read_text().split())
    return '.'.join(version[k] for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH'))


def git(*args, cwd=SRC, check=True):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, check=False,
                            encoding='utf-8', errors='replace')
    if check and result.returncode not in (0, 1):  # git grep returns 1 on no match
        sys.exit(f'git {" ".join(args[:3])}... failed: {result.stderr}')
    return result.stdout


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
