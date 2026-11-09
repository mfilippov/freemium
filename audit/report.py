#!/usr/bin/env python3
"""Stage 3: turns verdicts into a short report of what still needs a decision.

Shows background requests (and, with --unclear, undecided "unclear" verdicts)
that have no entry in decisions.toml. Decided items reappear only when their
code changed since the decision (context hash differs) and the verdict is
still a background request.

Writes build/audit/<version>/report.md and prints a one-line summary.

Decisions whose candidate id no longer exists (code moved or renamed) are listed
too, so they can be re-keyed instead of silently reappearing as new findings.

Usage:
  python audit/report.py --model gemma4-fast [--unclear] [--min-severity medium]
"""

import argparse
import collections
import re
import tomllib
from pathlib import Path

from common import AUDIT, FINDINGS, WORK, chromium_version, read_jsonl
from triage import cache_key, load_profile

SEVERITY = ['none', 'low', 'medium', 'high']
CONFIDENCE = ['low', 'medium', 'high']


def load_decisions():
    path = AUDIT / 'decisions.toml'
    if not path.exists():
        return {}
    return {d['id']: d for d in tomllib.loads(path.read_text(encoding='utf-8')).get('decision', [])}




GATES = [g for g in tomllib.loads((AUDIT / 'gates.toml').read_text(encoding='utf-8'))['gate']
         if g.get('closes')]


def closed_by_gate(r):
    """A verified mechanism-wide gate (gates.toml `closes`) covers this item: the
    local model tends to flag such items even when shown the gate."""
    return any(r['file'].startswith(tuple(g['paths'])) and re.search(g['closes'], r['id'])
               for g in GATES)


def component(path):
    """Report group: the feature directory, e.g. chrome/browser/<x>, components/<x>."""
    parts = path.split('/')
    if parts[:2] == ['chrome', 'browser'] or parts[:2] == ['third_party', 'blink']:
        return '/'.join(parts[:3 if len(parts) > 3 else 2])
    return '/'.join(parts[:2 if len(parts) > 2 else 1])


def load_verdicts(profile, version, current):
    """Latest verdict per id for the current candidates, made with this profile's
    current model, prompt and schema; stale rows in the findings file are ignored."""
    path = FINDINGS / version / f'{profile}.jsonl'
    if not path.exists():
        return {}
    key = cache_key(load_profile(profile), (AUDIT / 'prompt.md').read_text(encoding='utf-8'))
    return {r['id']: r for r in read_jsonl(path)
            if r.get('key') == key and (r['id'], r['context_hash']) in current}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--model', required=True)
    parser.add_argument('--second', metavar='PROFILE',
                        help='second-opinion verdicts (e.g. gemma4-think) shown per item')
    parser.add_argument('--agreed-only', action='store_true',
                        help='with --second: hide items the second opinion clears')
    parser.add_argument('--hide-gated', action='store_true',
                        help='hide items a verifier (findings/<v>/verify.jsonl) cleared with a '
                             'cited gate or evidence, and items closed by gates.toml')
    parser.add_argument('--unclear', action='store_true', help='also list unclear verdicts')
    parser.add_argument('--min-severity', choices=SEVERITY, default='low')
    opts = parser.parse_args()

    version = chromium_version()
    cands = read_jsonl(WORK / version / 'candidates.jsonl')
    current = {(c['id'], c['context_hash']) for c in cands}
    current_code = {(c['id'], c['code_hash']) for c in cands}
    latest = load_verdicts(opts.model, version, current)
    second = load_verdicts(opts.second, version, current) if opts.second else {}
    # Verifier verdicts (a stronger model checking each finding in code) count
    # only for unchanged code (the candidate's own code, not the context around
    # it) and only with a cited gate when they clear an item.
    verify_path = FINDINGS / version / 'verify.jsonl'
    verified = {r['id']: r for r in (read_jsonl(verify_path) if verify_path.exists() else [])
                if (r['id'], r.get('code_hash')) in current_code}
    missing = len(current) - len(latest)
    decisions = load_decisions()
    current_ids = {i for i, _ in current}
    orphaned = sorted(i for i in decisions if i not in current_ids)
    min_sev = SEVERITY.index(opts.min_severity)

    new, changed, cleared, gated = [], [], 0, 0
    for r in latest.values():
        bg = r['verdict'] == 'background_request' and SEVERITY.index(r['severity']) >= min_sev
        if not (bg or (opts.unclear and r['verdict'] == 'unclear')):
            continue
        s = second.get(r['id'])
        if opts.agreed_only and s and s['verdict'] not in ('background_request', 'unclear'):
            cleared += 1
            continue
        v = verified.get(r['id'])
        if opts.hide_gated and ((v and v['verdict'] not in ('background_request', 'unclear')
                                 and (v.get('gate') or v.get('evidence'))) or closed_by_gate(r)):
            gated += 1
            continue
        d = decisions.get(r['id'])
        if d is None:
            new.append(r)
        elif bg and d.get('context_hash') and d['context_hash'] != r['context_hash']:
            changed.append((r, d))

    def sev(r):
        return SEVERITY.index(r['severity'])

    groups = collections.defaultdict(list)
    for r in new:
        groups[component(r['file'])].append(r)
    ordered = sorted(groups.items(), key=lambda g: (-max(map(sev, g[1])), -len(g[1]), g[0]))

    lines = [f'# Audit report: Chromium {version}, model {opts.model}'
             + (f', second opinion {opts.second}' if opts.second else ''), '',
             f'Verdicts: {dict(collections.Counter(r["verdict"] for r in latest.values()))}', '',
             f'Undecided: {len(new)} in {len(groups)} groups. Decided but code changed: '
             f'{len(changed)}.' + (f' Hidden as cleared by {opts.second}: {cleared}.'
                                   if opts.agreed_only else '')
             + (f' Hidden as gated off (verifier or gates.toml): {gated}.' if opts.hide_gated else '')
             + f' Verified by a stronger model: {len(verified)} '
             f'({sum(1 for r in new if r["id"] in verified)} of them still listed).', '',
             'Decide per group where possible (one patch usually covers a group); record '
             'decisions per id in audit/decisions.toml.', '']
    if missing:
        lines += [f'**{missing} current candidates have no verdict with this model and '
                  'prompt: run triage.py first, this report is incomplete.**', '']
    if orphaned:
        lines += ['## Decisions for ids that no longer exist', '',
                  'The code moved or was renamed: find the new id and update decisions.toml.', '']
        lines += [f'- `{i}` ({decisions[i]["status"]})' for i in orphaned] + ['']

    lines += ['## Groups', '', '| group | items | high | medium | low | destinations |',
              '|---|---|---|---|---|---|']
    for name, items in ordered:
        c = collections.Counter(r['severity'] for r in items)
        dests = ', '.join(sorted({r['destination'][:40] for r in items})[:3])
        lines.append(f'| `{name}` | {len(items)} | {c["high"]} | {c["medium"]} | {c["low"]} '
                     f'| {dests.replace("|", "/")} |')
    lines.append('')

    def item(r, prev=None):
        s = second.get(r['id'])
        out = [f'#### `{r["id"]}`',
               f'- **{r["severity"]}** ({r["confidence"]}), default on: {r["default_enabled"]}'
               + (f'; second opinion: **{s["verdict"]}** ({s["confidence"]})' if s else ''),
               f'- `{r["file"]}`' + (f' `{r["name"]}`' if r.get('name') else ''),
               f'- trigger: {r["trigger"]}',
               f'- destination: {r["destination"]}; data: {r["data_sent"]}',
               f'- disable: {r["disable_hint"]}',
               f'- evidence: `{r["evidence"].replace("`", "")}`',
               f'- analysis: {r["analysis"]}']
        if s and s['verdict'] != r['verdict']:
            out.append(f'- second opinion analysis: {s["analysis"]}')
        v = verified.get(r['id'])
        if v:
            out.append(f'- **verified: {v["verdict"]}** ({v["confidence"]}, {v["verifier"]})'
                       + (f'; gate: `{v["gate"]}`' if v.get('gate') else '')
                       + f'; {v["evidence"]}' + (f'; patch: {v["patch_hint"]}'
                                                 if v.get('patch_hint') else ''))
        if prev:
            out.append(f'- previous decision: {prev["status"]} {prev.get("note", "")}')
        return out + ['']

    for name, items in ordered:
        lines += [f'## `{name}` ({len(items)})', '']
        for r in sorted(items, key=lambda r: (-sev(r), r['id'])):
            lines += item(r)
    if changed:
        lines += ['## Code changed since decision', '']
        for r, d in changed:
            lines += item(r, d)

    out = WORK / version / 'report.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines), encoding='utf-8')
    counts = collections.Counter(r['severity'] for r in new)
    print(f'{len(new)} undecided in {len(groups)} groups ({dict(counts)}), '
          f'{len(changed)} changed since decision, {len(orphaned)} orphaned decisions, '
          f'{missing} candidates without verdict'
          + (f', {cleared} cleared by {opts.second}' if opts.agreed_only else '') + f' -> {out}')


if __name__ == '__main__':
    main()
