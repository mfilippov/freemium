#!/usr/bin/env python3
"""Stage 1: deterministic prefilter. Finds every code location in the Chromium
checkout that can make a network request and writes one candidate per location
(enclosing function or constant) with the context the model will judge.

Sources of candidates:
  ann    - network traffic annotations (Chromium requires one per request)
  unann  - MISSING_TRAFFIC_ANNOTATION / NO_TRAFFIC_ANNOTATION_YET
  url    - non-benign http(s) URL literals in code and WebUI resources

Usage:
  python audit/extract.py              - scan the working tree (patches applied)
  python audit/extract.py --rev HEAD   - scan the pristine tag (used by eval)
  python audit/extract.py --out FILE   - override output path
"""

import argparse
import collections
import json
import re
import tomllib
from pathlib import Path

from common import AUDIT, SRC, WORK, chromium_version, git, is_built, is_relevant, sha, write_jsonl, ROOT

CODE_GLOBS = ['*.cc', '*.h', '*.mm', '*.ts', '*.js']
MAX_CONTEXT_CHARS = 24000          # ~6k tokens
MAX_FUNC_LINES = 260
USES_PER_IDENT = 8
MAX_IDENT_USES = 300               # more uses than this: a generic name, not informative

ANNOTATION_RE = re.compile(
    r'\b(?:Define|DefinePartial|Complete|BranchedComplete)NetworkTrafficAnnotation\s*\(\s*"([\w.-]+)"',
    re.S)
UNANNOTATED_RE = re.compile(r'\b(MISSING_TRAFFIC_ANNOTATION|NO_TRAFFIC_ANNOTATION_YET)\b')
# Host may be a template ("https://%s.googleapis.com/...") or empty (a bare
# "https://" joined with StrCat or +); both are kept as dynamic URLs.
URL_RE = re.compile(r'''["'`](https?://([^"'`/\s:]*)[^"'`\s]*)''')
# OAuth scope identifiers look like URLs but are never fetched.
BENIGN_URL_RE = re.compile(r'googleapis\.com/auth/')
FEATURE_DEF_RE = re.compile(
    r'BASE_FEATURE\(\s*(k\w+)\s*,[^;]*?FEATURE_(ENABLED|DISABLED)_BY_DEFAULT', re.S)
FEATURE_USE_RE = re.compile(r'\b(k[A-Z]\w+)\b')
BENIGN_HOST_RE = re.compile(
    r'(^|\.)(example\.(com|org|net)|w3\.org|ietf\.org|crbug\.com|bugs\.chromium\.org|'
    r'localhost|foo\.com|bar\.com|test|invalid|whatwg\.org|schema\.org|xmlns\.com|'
    r'purl\.org|ogp\.me|mozilla\.org|unicode\.org|iana\.org|json-schema\.org)$')
COMMENT_LINE_RE = re.compile(r'^\s*(//|\*|/\*|#)')
FUNC_STOP_RE = re.compile(r'^(}|namespace\b|// -{3,}|//=+)')
FUNC_NAME_RE = re.compile(r'(~?[A-Za-z_][\w]*(?:::~?[A-Za-z_]\w*)*)\s*\(')
CONST_NAME_RE = re.compile(r'\b(k[A-Z]\w+)\b(?:\[\])?\s*(?:=|\()')
NOT_FUNCS = {'if', 'for', 'while', 'switch', 'return', 'sizeof', 'BASE_FEATURE', 'static_assert',
             'DEFINE_', 'BUILDFLAG', 'defined', 'template', 'decltype', 'alignas'}


class Source:
    """Reads files from the working tree or from a git revision."""

    def __init__(self, rev=None):
        self.rev = rev
        self._cache = {}
        # Files that differ from rev must come from git; the rest is read from disk.
        self._dirty = set()
        if rev:
            self._dirty = set(git('diff', '--name-only', rev).splitlines())
            self._dirty |= set(git('ls-files', '--others', '--exclude-standard').splitlines())

    def grep(self, *args, files_only=False):
        cmd = ['grep', '-I', '-l' if files_only else '-n', *args]
        if self.rev:
            cmd.append(self.rev)
        out = git(*cmd, '--', *CODE_GLOBS)
        prefix = f'{self.rev}:' if self.rev else ''
        for line in out.splitlines():
            yield line[len(prefix):] if prefix and line.startswith(prefix) else line

    def ls_files(self):
        if self.rev:
            out = git('ls-tree', '-r', '--name-only', self.rev, '--', *CODE_GLOBS)
        else:
            out = git('ls-files', '--', *CODE_GLOBS)
        return out.splitlines()

    def lines(self, path, cache=True):
        if path in self._cache:
            return self._cache[path]
        if path in self._dirty:
            text = git('show', f'{self.rev}:{path}', check=False)
        else:
            try:
                text = (SRC / path).read_text(encoding='utf-8', errors='replace')
            except OSError:
                return []
        lines = text.splitlines()
        if cache:
            self._cache[path] = lines
        return lines


def enclosing(lines, idx):
    """Line range [start, end) of the top-level block (function, class or
    constant) around idx. Chromium style keeps definitions at column 0."""
    start = max(0, idx - 150)
    for i in range(idx - 1, start - 1, -1):
        if FUNC_STOP_RE.match(lines[i]):
            start = i + 1
            break
    end = min(len(lines), idx + 200)
    for j in range(idx, end):
        if lines[j].startswith('}'):
            end = j + 1
            break
    if end - start > MAX_FUNC_LINES:
        start, end = max(start, idx - 80), min(end, idx + 120)
    while start < idx and not lines[start].strip():
        start += 1
    return start, end


def block_name(lines, start, idx):
    """(kind, name) of the block: ('fn', 'Foo::Bar'), ('const', 'kUrl') or (None, None)."""
    for i in range(start, idx + 1):
        line = lines[i]
        if not line or line[0] in ' \t#/' or line.startswith(('template', 'namespace')):
            continue
        m = FUNC_NAME_RE.search(line)
        if m and m.group(1).split('::')[-1] not in NOT_FUNCS and '=' not in line[:m.start()]:
            return 'fn', m.group(1)
        break
    for i in range(idx, max(start, idx - 4) - 1, -1):
        m = CONST_NAME_RE.search(lines[i])
        if m:
            return 'const', m.group(1)
    return None, None


class Candidate:
    def __init__(self, path, start, end, idx):
        self.path, self.start, self.end, self.line = path, start, end, idx + 1
        self.kinds, self.annotations, self.urls = set(), [], []
        self.name_kind = self.name = None
        self.extra_windows = []     # (start, end) within the same file


def collect(src):
    cands = {}                      # (path, start) -> Candidate

    def at(path, idx):
        lines = src.lines(path)
        start, end = enclosing(lines, idx)
        key = (path, start)
        if key not in cands:
            cands[key] = c = Candidate(path, start, end, idx)
            c.name_kind, c.name = block_name(lines, start, idx)
        return cands[key]

    files = [p for p in src.grep('-E', 'NetworkTrafficAnnotation\\s*\\(', files_only=True)
             if is_relevant(p, any_third_party=True)]
    for path in files:
        text = '\n'.join(src.lines(path))
        for m in ANNOTATION_RE.finditer(text):
            c = at(path, text.count('\n', 0, m.start()))
            c.kinds.add('ann')
            c.annotations.append(m.group(1))

    for hit in src.grep('-E', 'MISSING_TRAFFIC_ANNOTATION|NO_TRAFFIC_ANNOTATION_YET'):
        path, lineno, text = hit.split(':', 2)
        if is_relevant(path, True) and not COMMENT_LINE_RE.match(text) and "#define" not in text:
            at(path, int(lineno) - 1).kinds.add('unann')

    for hit in src.grep('-E', '["\'`]https?://'):
        path, lineno, text = hit.split(':', 2)
        if not is_relevant(path) or COMMENT_LINE_RE.match(text):
            continue
        urls = [u for u, host in URL_RE.findall(text)
                if not BENIGN_HOST_RE.search(host.lower()) and not BENIGN_URL_RE.search(u)]
        if urls:
            c = at(path, int(lineno) - 1)
            c.kinds.add('url')
            c.urls += [u for u in urls if u not in c.urls]

    # Constants defined at namespace scope: show where they are used in the file.
    for c in cands.values():
        if c.name_kind == 'const':
            lines = src.lines(c.path)
            word = re.compile(rf'\b{re.escape(c.name)}\b')
            uses = [i for i in range(len(lines)) if not c.start <= i < c.end and word.search(lines[i])]
            for i in uses[:2]:
                c.extra_windows.append(enclosing(lines, i))
    return list(cands.values())


def feature_index(src):
    """kFeature -> 'enabled' / 'disabled' / 'varies' (several platform-specific
    definitions), as compiled in the scanned tree (i.e. after our patches)."""
    index = {}
    for path in src.grep('BASE_FEATURE(', files_only=True):
        if not is_relevant(path, True):
            continue
        for m in FEATURE_DEF_RE.finditer('\n'.join(src.lines(path, cache=False))):
            value = m.group(2).lower()
            index[m.group(1)] = value if index.get(m.group(1), value) == value else 'varies'
    return index


def find_uses(src, cands):
    """Repo-wide use sites of each candidate's function/constant (one git grep)."""
    idents = {}
    for c in cands:
        if c.name:
            ident = c.name.split('::')[-1]
            if len(ident) >= 6 and not ident.startswith('~'):
                idents.setdefault(ident, []).append(c)
    if not idents:
        return {}
    # git grep -w -F -f with hundreds of patterns is pathologically slow;
    # tokenizing each file once and intersecting with the ident set is fast.
    uses = collections.defaultdict(list)
    counts = collections.Counter()
    word = re.compile(r'[A-Za-z_]\w{5,}')
    names = idents.keys()
    for path in src.ls_files():
        if not is_relevant(path):
            continue
        lines = src.lines(path, cache=False)
        if not set(word.findall('\n'.join(lines))) & names:
            continue
        for n, text in enumerate(lines, 1):
            for ident in set(word.findall(text)) & names:
                counts[ident] += 1
                if len(uses[ident]) < 40:
                    uses[ident].append(f'{path}:{n}:{text.strip()[:200]}')
    result = {}
    for ident, cs in idents.items():
        for c in cs:
            own = f'{c.path}:'
            others = [u for u in uses[ident] if not u.startswith(own)]
            if others and counts[ident] <= MAX_IDENT_USES:
                result[id(c)] = (counts[ident], others[:USES_PER_IDENT])
    return result


# Checks that are always false in this build, so code behind them does not run.
# Requests are often guarded far from the code that sends them; these hints let
# the model see the guard (prompt.md explains each fact).
GATES = [
    (r'\b(HasAPIKeyConfigured|IsGoogleAPIKeyUsable|HasOAuthClientConfigured)\b',
     'Google API key check: false, this build has no API keys'),
    (r'\b(HasPrimaryAccount|GetPrimaryAccountId|GetPrimaryAccountInfo|'
     r'PrimaryAccountAccessTokenFetcher|IsSyncFeatureEnabled|IsSyncFeatureActive)\b',
     'needs a signed-in Google account: impossible in this build'),
    (r'\b(IsMetricsConsentGiven|IsMetricsReportingEnabled|IsMetricsAndCrashReportingEnabled|'
     r'IsUsageStatsUploadEnabled|IsCrashReportingEnabled)\b',
     'metrics/crash reporting consent: off by default'),
    (r'\b(kUrlKeyedAnonymizedDataCollectionEnabled|UrlKeyedDataCollectionConsentHelper|'
     r'IsUrlKeyedAnonymizedDataCollectionEnabled)\b',
     '"make searches and browsing better" consent: off by default'),
    (r'BUILDFLAG\(GOOGLE_CHROME_BRANDING\)',
     'GOOGLE_CHROME_BRANDING is false: code under #if is not compiled'),
]
GATE_RE = re.compile('|'.join(f'({p})' for p, _ in GATES))
MAX_GATE_HINTS = 8
MAX_GATE_SPREAD = 25               # a helper name in more files is too generic


def gate_label(text):
    m = GATE_RE.search(text)
    return GATES[[i for i, g in enumerate(m.groups()) if g][0] // 2][1] if m else None


def gate_index(src):
    """{function short name: (path, line, code, label)} for functions that check a
    gate (level 1) or call such a function (level 2, label says via which).
    Only specific names count: a name found in many files (IsEnabled, Initialize,
    AddObserver) or a constructor would attach the gate to unrelated code."""
    word = re.compile(r'[A-Za-z_]\w{7,}')
    files, spread = [], collections.Counter()
    for path in src.ls_files():
        if path.endswith(('.cc', '.mm', '.h')) and is_relevant(path, True):
            files.append(path)
            spread.update(set(word.findall('\n'.join(src.lines(path, cache=False)))))

    def specific(name):
        parts = name.split('::')
        short = parts[-1]
        return (len(short) >= 8 and spread[short] <= MAX_GATE_SPREAD
                and not (len(parts) > 1 and parts[-2] == short) and not short.startswith('~'))

    level1 = {}
    # git grep ERE has no portable word boundary: grep a superset, filter with GATE_RE.
    for hit in src.grep("-E", GATE_RE.pattern.replace(r"\b", "")):
        path, lineno, text = hit.split(':', 2)
        if not path.endswith(('.cc', '.mm')) or not is_relevant(path, True) \
                or COMMENT_LINE_RE.match(text) or not GATE_RE.search(text):
            continue
        lines = src.lines(path)
        idx = int(lineno) - 1
        kind, name = block_name(lines, enclosing(lines, idx)[0], idx)
        if kind == 'fn' and specific(name):
            level1.setdefault(name.split('::')[-1],
                              (path, int(lineno), text.strip()[:160], gate_label(text)))
    level2 = {}
    names = level1.keys()
    for path in files:
        if not path.endswith(('.cc', '.mm')):
            continue
        lines = src.lines(path, cache=False)
        if not set(word.findall('\n'.join(lines))) & names:
            continue
        for idx, text in enumerate(lines):
            for callee in set(word.findall(text)) & names:
                kind, name = block_name(lines, enclosing(lines, idx)[0], idx)
                if kind != 'fn' or not specific(name):
                    continue
                short = name.split('::')[-1]
                if short != callee and short not in level1:
                    p, n, code, label = level1[callee]
                    level2.setdefault(short, (path, idx + 1, text.strip()[:120],
                                              f'calls {callee}, which checks `{code}` '
                                              f'({p}:{n}): {label}'))
    return {**level2, **level1}


VERIFIED_GATES = []                # from gates.toml, filled by main()


def load_verified_gates(src):
    """gates.toml entries; fails when a cited check no longer matches the code, so a
    stale fact cannot clear findings on a new version."""
    gates = tomllib.loads((AUDIT / 'gates.toml').read_text(encoding='utf-8'))['gate']
    stale = [chk for g in gates for chk in g.get('check', [])
             if chk.split(':', 1)[1] not in '\n'.join(src.lines(chk.split(':', 1)[0]))]
    stale += [f'{p} is built now' for g in gates for p in g.get('not_built', []) if is_built(p)]
    if stale:
        raise SystemExit('gates.toml: cited code changed, re-verify these gates:\n  '
                         + '\n  '.join(stale))
    return gates


def gate_hints(src, c, uses, gates):
    """Gate checks in the candidate's code, in the functions it calls and in the
    callers shown under OTHER USES."""
    scopes = [(c.path, c.start, c.end)] + [(c.path, s, e) for s, e in c.extra_windows]
    for site in (uses.get(id(c), (0, []))[1]):
        path, lineno, _ = site.split(':', 2)
        lines = src.lines(path)
        if path.endswith(('.cc', '.mm')) and lines:
            s, e = enclosing(lines, int(lineno) - 1)
            scopes.append((path, s, e))
    own = c.name.split('::')[-1] if c.name else None
    hints, seen = [], set()
    word = re.compile(r'[A-Za-z_]\w{7,}')
    for path, s, e in scopes:
        lines = src.lines(path)
        for idx in range(s, e):
            text = lines[idx]
            if COMMENT_LINE_RE.match(text):
                continue
            label = gate_label(text)
            if label and (path, idx) not in seen:
                seen.add((path, idx))
                hints.append(f'- {path}:{idx + 1}: `{text.strip()[:160]}` -> {label}')
            for callee in set(word.findall(text)) & gates.keys():
                if callee != own and callee not in seen:
                    seen.add(callee)
                    p, n, code, label = gates[callee]
                    hints.append(f'- {path}:{idx + 1} calls {callee} ({p}:{n}: `{code}`) '
                                 f'-> {label}')
    return hints[:MAX_GATE_HINTS]


def patch_diff(path, applied_files):
    if path not in applied_files:
        return ''
    diff = git('diff', '--no-color', '-U2', 'HEAD', '--', path)
    lines = diff.splitlines()[4:]           # drop the diff header
    return '\n'.join(lines[:80]) + ('\n...' if len(lines) > 80 else '')


def render(src, c, uses, applied_files, gates):
    lines = src.lines(c.path)
    parts = [f'FILE: {c.path}']
    if c.annotations:
        parts.append('TRAFFIC ANNOTATIONS: ' + ', '.join(c.annotations))
    parts.append('CODE:\n' + '\n'.join(lines[c.start:c.end]))
    for start, end in c.extra_windows:
        parts.append(f'USED IN THE SAME FILE:\n' + '\n'.join(lines[start:end]))
    if id(c) in uses:
        total, sites = uses[id(c)]
        parts.append(f'OTHER USES OF {c.name.split("::")[-1]} ({total} total):\n' + '\n'.join(sites))
    shown = '\n'.join(lines[c.start:c.end] + [l for s, e in c.extra_windows for l in lines[s:e]])
    features = sorted({f for f in FEATURE_USE_RE.findall(shown) if f in FEATURES})
    if features:
        parts.append('FEATURE DEFAULTS (as compiled): ' +
                     ', '.join(f'{f}={FEATURES[f]}' for f in features[:20]))
    hints = gate_hints(src, c, uses, gates)
    if hints:
        parts.append('GATES SEEN NEAR THIS CODE (false or off by default in this build; check '
                     'that they guard the request):\n' + '\n'.join(hints))
    facts = [g['text'] for g in VERIFIED_GATES if c.path.startswith(tuple(g['paths']))]
    if facts:
        parts.append('VERIFIED FACTS ABOUT THIS MECHANISM:\n' + '\n'.join(facts))
    diff = patch_diff(c.path, applied_files)
    if diff:
        parts.append('ALREADY PATCHED IN THIS FILE (diff vs upstream):\n' + diff)
    text = '\n\n'.join(parts)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + '\n[...truncated]'
    return text


def candidate_id(c, taken):
    if c.annotations:
        base = 'ann:' + c.annotations[0]
    elif c.name:
        base = f'{c.name_kind}:{c.path}:{c.name}'
    elif c.urls:
        base = f'url:{c.path}:{c.urls[0]}'
    else:
        base = f'unann:{c.path}'
    cid, n = base, 1
    while cid in taken:
        n += 1
        cid = f'{base}#{n}'
    taken.add(cid)
    return cid


FEATURES = {}                      # kFeature -> default, filled by main()
COVERAGE_EXEMPT = ('remoting/', 'content/shell/')   # not built, see common.EXCLUDED_DIRS


def check_coverage(src, rows):
    """Every desktop annotation listed in Chromium's annotations.xml must be a
    candidate; catches prefilter rules that silently drop real code."""
    xml = '\n'.join(src.lines('tools/traffic_annotation/summary/annotations.xml'))
    found = {a for r in rows for a in r['annotations']}
    missing = []
    for m in re.finditer(r'<item id="([^"]+)"[^>]*os_list="([^"]*)"[^>]*file_path="([^"]+)"', xml):
        aid, oses, path = m.groups()
        desktop = {'windows', 'linux'} & set(oses.split(','))
        if desktop and aid not in found and not path.startswith(COVERAGE_EXEMPT) \
                and not TEST_PATH_RE.search(path) and is_built(path):
            missing.append(f'{aid} ({path})')
    return missing


TEST_PATH_RE = re.compile(r'(^|[/_.])(tests?|unittests?|browsertests?|fake|mock)([/_.]|$)')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rev', help='scan a git revision of src instead of the working tree')
    parser.add_argument('--out', type=Path)
    opts = parser.parse_args()

    src = Source(opts.rev)
    version = chromium_version()
    out = opts.out or WORK / version / ('candidates.jsonl' if not opts.rev else 'candidates-pristine.jsonl')
    applied_files = set()
    state = ROOT / 'build' / 'applied-patches.json'
    if not opts.rev and state.exists():
        applied_files = set(json.loads(state.read_text(encoding='utf-8'))['files'])

    cands = collect(src)
    uses = find_uses(src, cands)
    FEATURES.update(feature_index(src))
    gates = gate_index(src)
    VERIFIED_GATES.extend(load_verified_gates(src))
    taken, rows = set(), []
    for c in sorted(cands, key=lambda c: (c.path, c.start)):
        context = render(src, c, uses, applied_files, gates)
        rows.append({
            'id': candidate_id(c, taken), 'kinds': sorted(c.kinds), 'file': c.path,
            'line': c.line, 'start': c.start + 1, 'end': c.end, 'name': c.name,
            'annotations': c.annotations, 'urls': c.urls[:20],
            'context': context, 'context_hash': sha(context),
            # The candidate's own code only: verifier results stay valid when
            # the context around it (hints, uses) changes.
            'code_hash': sha('\n'.join(src.lines(c.path)[c.start:c.end])),
        })
    write_jsonl(out, rows)
    kinds = collections.Counter(k for r in rows for k in r['kinds'])
    tokens = sum(len(r['context']) for r in rows) // 4
    print(f'{len(rows)} candidates ({dict(kinds)}), ~{tokens / 1e6:.1f}M tokens -> {out}')
    missing = check_coverage(src, rows)
    if missing:
        print(f'COVERAGE: {len(missing)} desktop annotations from annotations.xml are not '
              'candidates; fix the prefilter rules:\n  ' + '\n  '.join(missing))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
