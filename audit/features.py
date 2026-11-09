#!/usr/bin/env python3
"""Features track: base::Features enabled by default that the user may want off
(AI, Google services, sign-in/sync promos, IPH/promos, ads/tracking APIs).

Kept separate from the network track: different question, different prompt.

  python audit/features.py extract       # -> build/audit/<v>/features-candidates.jsonl
  python audit/serve.py gemma4-fast      # in another shell
  python audit/triage.py --track features --model gemma4-fast
  python audit/features.py report --model gemma4-fast

Candidates: every BASE_FEATURE defined ENABLED_BY_DEFAULT (in any platform
branch) in a built, non-test file of the patched tree, with its comment,
nearby FeatureParams and up to USES use sites (function + line). Features our
patches already turned off are not candidates. Decisions go to
features-decisions.toml (id -> disable / keep).
"""

import argparse
import collections
import re
import subprocess
import tomllib

from common import (AUDIT, FINDINGS, SRC, WORK, chromium_version, is_built, is_relevant,
                    read_jsonl, sha, write_jsonl)

DEF_RE = re.compile(r'BASE_FEATURE\(\s*(k\w+)\s*,')
DEFAULT_RE = re.compile(r'FEATURE_(ENABLED|DISABLED)_BY_DEFAULT')
TEST_RE = re.compile(r'(^|[/_.])(tests?|unittests?|browsertests?|fake|mock|test_support)([/_.]|$)')
FUNC_RE = re.compile(r'^[\w:<>,*&~\s]*?(~?[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)\s*\([^;]*$')
USES = 12
MAX_CONTEXT_CHARS = 8000


def git(*args):
    return subprocess.run(['git', *args], cwd=SRC, capture_output=True, text=True,
                          encoding='utf-8', errors='replace').stdout


def lines_of(path, cache={}):
    if path not in cache:
        cache[path] = (SRC / path).read_text(encoding='utf-8', errors='replace').splitlines()
    return cache[path]


def definitions():
    """(path, start, end, name) of BASE_FEATURE blocks enabled by default in some branch."""
    out = []
    for path in git('grep', '-l', 'BASE_FEATURE(', '--', '*.cc', '*.h', '*.mm').splitlines():
        if not is_relevant(path, True) or TEST_RE.search(path) or not is_built(path):
            continue
        lines = lines_of(path)
        i = 0
        while i < len(lines):
            m = DEF_RE.search(lines[i])
            if not m:
                i += 1
                continue
            j = i
            while j < len(lines) - 1 and ');' not in lines[j]:
                j += 1
            block = '\n'.join(lines[i:j + 1])
            if 'ENABLED_BY_DEFAULT' in block:
                out.append((path, i, j + 1, m.group(1)))
            i = j + 1
    return out


def comment_above(lines, i):
    k = i
    while k > 0 and (lines[k - 1].lstrip().startswith(('//', '#if', '#else', '#elif', '#endif'))
                     or not lines[k - 1].strip()) and i - k < 20:
        k -= 1
    return k


def enclosing_function(lines, idx):
    for k in range(idx, max(-1, idx - 200), -1):
        if lines[k].startswith('}'):
            return None
        if lines[k] and not lines[k][0].isspace():
            m = FUNC_RE.match(lines[k])
            if m and m.group(1) not in ('if', 'for', 'while', 'switch', 'return', 'BASE_FEATURE'):
                return m.group(1)
    return None


def use_sites(names):
    """name -> [(path, line_no, text)] across built non-test code, one git grep."""
    pattern_file = WORK / 'feature-names.txt'
    pattern_file.write_text('\n'.join(sorted(names)) + '\n', encoding='utf-8')
    out = git('grep', '-n', '-w', '-F', '-f', str(pattern_file), '--',
              '*.cc', '*.h', '*.mm', '*.ts', '*.js')
    uses = collections.defaultdict(list)
    word = re.compile(r'\bk\w+\b')
    for row in out.splitlines():
        path, _, rest = row.partition(':')
        num, _, text = rest.partition(':')
        if 'BASE_FEATURE(' in text or TEST_RE.search(path) or not is_relevant(path, True):
            continue
        if path.endswith(('.cc', '.mm')) and not is_built(path):
            continue
        for name in set(word.findall(text)) & names:
            uses[name].append((path, int(num), text.strip()))
    return uses


FLAG_DESC_RE = re.compile(r'inline constexpr char (k\w+)\[\] =((?:\s*"(?:[^"\\]|\\.)*")+);')


def flag_strings(cache={}):
    """flag_descriptions.h constant -> text (chrome://flags names and descriptions)."""
    if not cache:
        text = (SRC / 'chrome/browser/flag_descriptions.h').read_text(encoding='utf-8')
        for m in FLAG_DESC_RE.finditer(text):
            cache[m.group(1)] = ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2)))
    return cache


def flag_entry(name, uses):
    """chrome://flags name and description of the feature, from about_flags.cc."""
    for p, n, _ in uses.get(name, []):
        if p != 'chrome/browser/about_flags.cc':
            continue
        window = '\n'.join(lines_of(p)[max(0, n - 8):n])
        consts = re.findall(r'flag_descriptions::(k\w+)', window)
        names = [c for c in consts if c.endswith('Name')]
        descs = [c for c in consts if c.endswith('Description')]
        if names and descs:
            s = flag_strings()
            return s.get(names[-1], ''), s.get(descs[-1], '')
    return None


def render(path, start, end, name, uses):
    lines = lines_of(path)
    top = comment_above(lines, start)
    parts = [f'FEATURE: {name}', f'DEFINED IN: {path}', '', 'DEFINITION (after our patches):',
             *lines[top:end]]
    for p, n, text in uses.get(name, []):
        if p.endswith('.h') and 'DECLARE_FEATURE' in text:
            hl = lines_of(p)
            k = comment_above(hl, n - 1)
            comment = [l for l in hl[k:n - 1] if l.lstrip().startswith('//')]
            if comment:
                parts += ['', f'DECLARATION COMMENT ({p}):', *comment[-12:]]
            break
    flag = flag_entry(name, uses)
    if flag:
        parts += ['', f'CHROME://FLAGS ENTRY: {flag[0]}: {flag[1]}']
    params = [l.strip() for l in lines if name in l and 'FeatureParam' in l][:5]
    if params:
        parts += ['', 'PARAMS:', *params]
    sites = [u for u in uses.get(name, []) if not u[0].endswith('.h')
             and u[0] != 'chrome/browser/about_flags.cc' and 'FeatureParam' not in u[2]
             and not (u[0] == path and f'&{name}' in u[2])]
    sites = sorted(sites, key=lambda u: (u[0].startswith(('third_party', 'content/')), u[0]))
    parts += ['', f'USED IN ({len(sites)} places, first {min(len(sites), USES)}):']
    for p, n, text in sites[:USES]:
        fn = enclosing_function(lines_of(p), n - 1) if p.endswith(('.cc', '.mm')) else None
        parts.append(f'- {p}:{n}' + (f' in {fn}' if fn else '') + f': {text[:160]}')
    return '\n'.join(parts)[:MAX_CONTEXT_CHARS]


def extract():
    version = chromium_version()
    defs = definitions()
    uses = use_sites({d[3] for d in defs})
    rows, seen = [], collections.Counter()
    for path, start, end, name in sorted(defs):
        seen[name] += 1
        fid = f'feat:{name}' + (f'#{seen[name]}' if seen[name] > 1 else '')
        context = render(path, start, end, name, uses)
        rows.append({'id': fid, 'file': path, 'line': start + 1, 'name': name,
                     'context': context, 'context_hash': sha(context),
                     'code_hash': sha('\n'.join(lines_of(path)[start:end]))})
    out = WORK / version / 'features-candidates.jsonl'
    write_jsonl(out, rows)
    print(f'{len(rows)} enabled-by-default features in built code, '
          f'~{sum(len(r["context"]) for r in rows) / 4e6:.1f}M tokens -> {out}')


def report(model, show_keep):
    from triage import TRACKS, cache_key, load_profile
    version = chromium_version()
    cands = {c['id']: c for c in read_jsonl(WORK / version / 'features-candidates.jsonl')}
    track = TRACKS['features']
    key = cache_key(load_profile(model), (AUDIT / track['prompt']).read_text(encoding='utf-8'),
                    track['schema'])
    path = FINDINGS / version / track['out'].format(model=model)
    latest = {r['id']: r for r in read_jsonl(path)
              if r.get('key') == key and r['id'] in cands
              and r['context_hash'] == cands[r['id']]['context_hash']}
    dpath = AUDIT / 'features-decisions.toml'
    decisions = ({d['id']: d for d in tomllib.loads(dpath.read_text(encoding='utf-8'))
                  .get('decision', [])} if dpath.exists() else {})
    want = ('disable', 'unclear') if not show_keep else ('disable', 'unclear', 'keep')
    todo = [r for r in latest.values() if r['recommendation'] in want and r['id'] not in decisions]
    groups = collections.defaultdict(list)
    for r in todo:
        groups[r['category']].append(r)
    conf = {'high': 0, 'medium': 1, 'low': 2}
    lines = [f'# Features report: Chromium {version}, model {model}', '',
             f'Candidates {len(cands)}, verdicts {len(latest)}: '
             f'{dict(collections.Counter(r["recommendation"] for r in latest.values()))}. '
             f'Undecided to review: {len(todo)}. Decided: {len(decisions)}.', '']
    if len(latest) < len(cands):
        lines += [f'**{len(cands) - len(latest)} candidates without a verdict: run triage.py '
                  '--track features first.**', '']
    lines += ['| category | disable | unclear |', '|---|---|---|']
    for cat, items in sorted(groups.items(), key=lambda g: -len(g[1])):
        c = collections.Counter(r['recommendation'] for r in items)
        lines.append(f'| {cat} | {c["disable"]} | {c["unclear"]} |')
    for cat, items in sorted(groups.items(), key=lambda g: -len(g[1])):
        lines += ['', f'## {cat} ({len(items)})', '']
        for r in sorted(items, key=lambda r: (r['recommendation'] != 'disable',
                                              conf[r['confidence']], r['id'])):
            lines.append(f'- `{r["name"]}` **{r["recommendation"]}** ({r["confidence"]}, '
                         f'visible: {r["user_visible"]}, google: {r["google_dependency"]}) '
                         f'`{r["file"]}`: {r["reason"]}')
    out = WORK / version / 'features-report.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'{len(todo)} to review in {len(groups)} categories '
          f'({dict(collections.Counter(r["category"] for r in todo).most_common())}) -> {out}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('extract')
    r = sub.add_parser('report')
    r.add_argument('--model', required=True)
    r.add_argument('--keep', action='store_true', help='also list "keep" recommendations')
    opts = parser.parse_args()
    extract() if opts.cmd == 'extract' else report(opts.model, opts.keep)


if __name__ == '__main__':
    main()
