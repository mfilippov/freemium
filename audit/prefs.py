#!/usr/bin/env python3
"""Prefs track: default values of preferences that the user may want changed
(anything sent out by default, Google services, AI, promos, telemetry, and
privacy/convenience trade-offs the owner decides).

  python audit/prefs.py extract          # -> build/audit/<v>/prefs-candidates.jsonl
  python audit/serve.py gemma4-fast      # in another shell
  python audit/triage.py --track prefs --model gemma4-fast
  python audit/prefs.py report --model gemma4-fast

Candidates: every Register{Boolean,Integer,Double}Pref in a built, non-test
.cc under chrome/ and components/ (migration-only registrations skipped), with
the pref name and its comment, the default after our patches, C++ use sites
and WebUI (settings page) use sites of the pref path. Decisions go to
prefs-decisions.toml (id -> patched / keep / accepted).
"""

import argparse
import collections
import re
import tomllib

from common import (AUDIT, FINDINGS, WORK, chromium_version, is_built, is_relevant, read_jsonl,
                    sha, write_jsonl)
from features import TEST_RE, comment_above, enclosing_function, git, lines_of

REG_RE = re.compile(r'Register(Boolean|Integer|Double)Pref\(')
NAME_RE = re.compile(r'(k\w+)\s*,')
CONST_RE = re.compile(r'(?:inline\s+)?(?:constexpr|const)\s+char\s+(k\w+)\[\]\s*=\s*\n?\s*"([^"]+)"')
SKIP_FUNCS = re.compile(r'Migration|Obsolete|Deprecated', re.I)
USES = 10
WEBUI_USES = 6
MAX_CONTEXT_CHARS = 7000


def registrations():
    """(path, line_idx, end_idx, type, const, function) of pref registrations."""
    out = []
    files = git('grep', '-l', '-E', r'Register(Boolean|Integer|Double)Pref\(', '--',
                'chrome/*.cc', 'components/*.cc').splitlines()
    for path in files:
        if not is_relevant(path) or TEST_RE.search(path) or not is_built(path):
            continue
        lines = lines_of(path)
        for i, line in enumerate(lines):
            m = REG_RE.search(line)
            if not m:
                continue
            j = i
            while j < len(lines) - 1 and ');' not in lines[j] and j - i < 6:
                j += 1
            call = ' '.join(l.strip() for l in lines[i:j + 1])
            call = call[call.find(m.group(0)):]
            n = NAME_RE.search(call)
            if not n:
                continue
            fn = enclosing_function(lines, i)
            if fn and SKIP_FUNCS.search(fn):
                continue
            out.append((path, i, j + 1, m.group(1), n.group(1), fn, call))
    return out


def pref_constants():
    """const name -> [(path, line_no, pref path)] from *pref_names* / *prefs* files."""
    consts = collections.defaultdict(list)
    files = git('grep', '-l', '-E', r'char k\w+\[\] *=', '--', 'chrome/*.h', 'chrome/*.cc',
                'components/*.h', 'components/*.cc').splitlines()
    for path in files:
        if TEST_RE.search(path) or 'pref' not in path:
            continue
        text = '\n'.join(lines_of(path))
        for m in CONST_RE.finditer(text):
            consts[m.group(1)].append((path, text.count('\n', 0, m.start()) + 1, m.group(2)))
    return consts


def pick_const(consts, name, reg_path):
    """The definition of `name` closest to the registering file."""
    cands = consts.get(name, [])
    if not cands:
        return None
    parts = reg_path.split('/')
    return max(cands, key=lambda c: sum(1 for a, b in zip(parts, c[0].split('/')) if a == b))


def grep_words(patterns, globs, fixed_word=True):
    pattern_file = WORK / 'pref-patterns.txt'
    pattern_file.write_text('\n'.join(sorted(patterns)) + '\n', encoding='utf-8')
    args = ['grep', '-n', '-F', '-f', str(pattern_file)]
    if fixed_word:
        args.insert(2, '-w')
    return git(*args, '--', *globs).splitlines()


def cpp_uses(names):
    uses = collections.defaultdict(list)
    word = re.compile(r'\bk\w+\b')
    for row in grep_words(names, ['chrome/*.cc', 'chrome/*.mm', 'components/*.cc']):
        path, _, rest = row.partition(':')
        num, _, text = rest.partition(':')
        if REG_RE.search(text) or TEST_RE.search(path) or not is_built(path):
            continue
        for name in set(word.findall(text)) & names:
            uses[name].append((path, int(num), text.strip()))
    return uses


def webui_uses(paths):
    """pref path -> [(file, line, text)] in WebUI sources (settings pages bind prefs.<path>)."""
    uses = collections.defaultdict(list)
    quoted = {f'prefs.{p}' for p in paths} | {f"'{p}'" for p in paths} | {f'"{p}"' for p in paths}
    rows = grep_words(quoted, ['chrome/browser/resources/*.ts', 'chrome/browser/resources/*.html',
                               'components/*.ts', 'components/*.html'], fixed_word=False)
    for row in rows:
        path, _, rest = row.partition(':')
        num, _, text = rest.partition(':')
        if TEST_RE.search(path) or path.endswith('settings_shared/prefs/prefs.ts'):
            continue
        for p in paths:
            if f'prefs.{p}' in text or f"'{p}'" in text or f'"{p}"' in text:
                rest_char = text.split(p, 1)[1][:1]
                if rest_char in ('.', '_') and f'prefs.{p}.value' not in text:
                    continue          # a longer pref path
                uses[p].append((path, int(num), text.strip()))
    return uses


def render(reg, const, uses, wuses):
    path, i, j, typ, name, fn, call = reg
    lines = lines_of(path)
    parts = [f'PREF: {name}' + (f' = "{const[2]}"' if const else ''), f'TYPE: {typ}',
             f'REGISTERED IN: {path}' + (f' in {fn}' if fn else ''), '',
             'REGISTRATION (after our patches; the default is the second argument):',
             *lines[max(0, i - 2):j]]
    if const:
        cl = lines_of(const[0])
        k = comment_above(cl, const[1] - 1)
        comment = [l for l in cl[k:const[1] - 1] if l.lstrip().startswith('//')]
        if comment:
            parts += ['', f'PREF COMMENT ({const[0]}):', *comment[-10:]]
    w = wuses.get(const[2], []) if const else []
    if w:
        parts += ['', f'SETTINGS / WEBUI ({len(w)} places, first {min(len(w), WEBUI_USES)}):']
        for p, n, _ in w[:WEBUI_USES]:
            wl = lines_of(p)
            snippet = ' '.join(l.strip() for l in wl[max(0, n - 3):n + 2])
            parts.append(f'- {p}:{n}: {snippet[:300]}')
    sites = [u for u in uses.get(name, []) if not (u[0] == path and abs(u[1] - i - 1) < 3)]
    sites = sorted(sites, key=lambda u: ('/test' in u[0], u[0]))
    parts += ['', f'USED IN ({len(sites)} places, first {min(len(sites), USES)}):']
    for p, n, text in sites[:USES]:
        f = enclosing_function(lines_of(p), n - 1)
        parts.append(f'- {p}:{n}' + (f' in {f}' if f else '') + f': {text[:160]}')
    return '\n'.join(parts)[:MAX_CONTEXT_CHARS]


def extract():
    version = chromium_version()
    regs = registrations()
    consts = pref_constants()
    picked = {r: pick_const(consts, r[4], r[0]) for r in regs}
    uses = cpp_uses({r[4] for r in regs})
    wuses = webui_uses({c[2] for c in picked.values() if c and '.' in c[2]})
    rows, seen = [], collections.Counter()
    for reg in sorted(regs):
        const = picked[reg]
        key = const[2] if const else reg[4]
        seen[key] += 1
        pid = f'pref:{key}' + (f'#{seen[key]}' if seen[key] > 1 else '')
        context = render(reg, const, uses, wuses)
        rows.append({'id': pid, 'file': reg[0], 'line': reg[1] + 1, 'name': reg[4],
                     'pref': const[2] if const else None, 'type': reg[3],
                     'context': context, 'context_hash': sha(context),
                     'code_hash': sha(reg[6])})
    out = WORK / version / 'prefs-candidates.jsonl'
    write_jsonl(out, rows)
    print(f'{len(rows)} pref registrations ({dict(collections.Counter(r["type"] for r in rows))}), '
          f'{sum(1 for r in rows if r["pref"]) } with a known path, '
          f'~{sum(len(r["context"]) for r in rows) / 4e6:.2f}M tokens -> {out}')


def report(model, show_keep):
    from triage import TRACKS, cache_key, load_profile
    version = chromium_version()
    cands = {c['id']: c for c in read_jsonl(WORK / version / 'prefs-candidates.jsonl')}
    track = TRACKS['prefs']
    key = cache_key(load_profile(model), (AUDIT / track['prompt']).read_text(encoding='utf-8'),
                    track['schema'])
    path = FINDINGS / version / track['out'].format(model=model)
    latest = {r['id']: r for r in read_jsonl(path)
              if r.get('key') == key and r['id'] in cands
              and r['context_hash'] == cands[r['id']]['context_hash']}
    dpath = AUDIT / 'prefs-decisions.toml'
    decisions = ({d['id']: d for d in tomllib.loads(dpath.read_text(encoding='utf-8'))
                  .get('decision', [])} if dpath.exists() else {})
    want = ('change', 'ask', 'unclear') + (('keep',) if show_keep else ())
    todo = [r for r in latest.values() if r['recommendation'] in want and r['id'] not in decisions]
    counts = dict(collections.Counter(r['recommendation'] for r in latest.values()))
    lines = [f'# Prefs report: Chromium {version}, model {model}', '',
             f'Candidates {len(cands)}, verdicts {len(latest)}: {counts}. '
             f'Undecided to review: {len(todo)}. Decided: {len(decisions)}.', '']
    if len(latest) < len(cands):
        lines += [f'**{len(cands) - len(latest)} candidates without a verdict: run triage.py '
                  '--track prefs first.**', '']
    conf = {'high': 0, 'medium': 1, 'low': 2}
    order = {'change': 0, 'ask': 1, 'unclear': 2, 'keep': 3}
    for r in sorted(todo, key=lambda r: (order[r['recommendation']], r['category'],
                                         conf[r['confidence']], r['id'])):
        c = cands[r['id']]
        lines.append(f'- `{c["pref"] or c["name"]}` **{r["recommendation"]}** -> '
                     f'`{r["proposed_default"]}` ({r["category"]}, {r["confidence"]}) '
                     f'`{c["file"]}:{c["line"]}`: {r["reason"]}')
    out = WORK / version / 'prefs-report.md'
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'{len(todo)} to review ({dict(collections.Counter(r["recommendation"] for r in todo))}) '
          f'-> {out}')


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
