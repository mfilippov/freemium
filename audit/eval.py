#!/usr/bin/env python3
"""Model evaluation on known answers: ungoogled-chromium's own patches.

build: runs the prefilter on the pristine tag (git HEAD of src, no patches) and
       labels candidates that ungoogled-chromium privacy patches modify as
       positives; adds a fixed random sample of the other candidates.
score: compares verdict files on that set. Recall on positives is what
       matters most (missed requests); the flag rate on the rest shows how
       noisy a model is.

Usage:
  python audit/eval.py build [--others 120]
  python audit/triage.py --model X --candidates build/audit/eval/eval.jsonl \
                         --out build/audit/eval/X.jsonl --no-cache
  python audit/eval.py score [--quick] build/audit/eval/*.jsonl
  (eval-quick.jsonl: all positives + 30 others, for a fast model comparison)
"""

import argparse
import collections
import random
import re
import subprocess
import tomllib
import sys
from pathlib import Path

from common import AUDIT, ROOT, WORK, read_jsonl, write_jsonl

UNGOOGLED = ROOT / 'build' / 'ungoogled-chromium'
EVAL = WORK / 'eval'
# Patches that fix the build or change UI only are not evidence of a request.
NOT_PRIVACY = re.compile(r'fix-build|missing-dependencies|upstream-fixes|compile|toolchain|'
                         r'clang|rust|-gn-|/gn-|flag-headers|add-flag|add-ungoogled|keyboard|'
                         r'shortcut|first-run-page|search-engine-pref|doh-changes|'
                         r'remove-unused-preferences|remove-uneeded-ui', re.I)
# Requests vanilla Chrome makes without user action in a default desktop profile
# (pristine code, no patches): a second, patch-independent source of positives.
KNOWN_BACKGROUND = {
    'metrics_report_uma', 'metrics_report_ukm', 'metrics_report_dwa',
    'metrics_report_private_metrics', 'private_metrics_encryption_key',
    'chrome_variations_service', 'update_client', 'extension_crx_fetcher',
    'extension_manifest_fetcher', 'gcm_checkin', 'gcm_registration', 'gcm_connection_factory',
    'network_time_component', 'domain_reliability_report_upload', 'intranet_redirect_detector',
    'one_google_bar_service', 'optimization_guide_model',
    'optimization_guide_model_download', 'hintsfetcher_gethintsrequest', 'autofill_query',
    'translate_url_fetcher', 'captive_portal_service',
}
# Positives that are not background requests: the patch touches the location
# for another reason (removing a link, a default extension, a UI dialog).
NOT_POSITIVE = {
    'const:components/security_interstitials/core/safe_browsing_loud_error_ui.cc:',  # help link
    'ann:webstore_installer',               # user installs; patch drops default extensions
    'ann:backdrop_collection_names_download',  # NTP customize dialog opened by the user
}
QUICK_OTHERS = 30
HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? ')


def privacy_hunks():
    """{path: [(start, end)]} old-side line ranges changed by privacy patches."""
    ranges = collections.defaultdict(list)
    series = (UNGOOGLED / 'patches' / 'series').read_text(encoding='utf-8').split()
    for name in series:
        if NOT_PRIVACY.search(name):
            continue
        path = None
        for line in (UNGOOGLED / 'patches' / name).read_text(encoding='utf-8',
                                                            errors='replace').splitlines():
            if line.startswith('--- a/'):
                path = line[6:].split('\t')[0].strip()
            elif line.startswith('--- '):
                path = None
            elif path and (m := HUNK_RE.match(line)):
                start, count = int(m.group(1)), int(m.group(2) or 1)
                ranges[path].append((start, start + count, name))
    return ranges


def build(opts):
    EVAL.mkdir(parents=True, exist_ok=True)
    pristine = EVAL / 'candidates-pristine.jsonl'
    subprocess.run([sys.executable, str(AUDIT / 'extract.py'), '--rev', 'HEAD',
                    '--out', str(pristine)], check=True)
    hunks = privacy_hunks()
    positives, others, known_found = [], [], set()
    for c in read_jsonl(pristine):
        hits = {name for s, e, name in hunks.get(c['file'], ())
                if s - 5 <= c['end'] and e + 5 >= c['start']}   # hunk context is ~3 lines
        known = KNOWN_BACKGROUND.intersection(c['annotations'])
        known_found |= known
        hits |= {f'known/{a}' for a in known}
        if hits and not any(c['id'].startswith(p) for p in NOT_POSITIVE):
            positives.append({**c, 'label': 'positive', 'patches': sorted(hits)})
        else:
            others.append({**c, 'label': 'other'})
    random.Random(157).shuffle(others)
    rows = positives + others[:opts.others]
    write_jsonl(EVAL / 'eval.jsonl', rows)
    # Quick set for comparing models fast: all positives, fewer others.
    write_jsonl(EVAL / 'eval-quick.jsonl', positives + others[:QUICK_OTHERS])
    if KNOWN_BACKGROUND - known_found:
        print('known annotations not found:', ', '.join(sorted(KNOWN_BACKGROUND - known_found)))
    by_patch = collections.Counter(p for r in positives for p in r['patches'])
    print(f'{len(positives)} positives from {len(by_patch)} patches, '
          f'{min(opts.others, len(others))} others -> {EVAL / "eval.jsonl"}')


def wilson(k, n, z=1.96):
    """95% confidence interval for a proportion: with ~35 positives it is about
    +-15 points, so smaller differences between models are noise."""
    if not n:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def failed_count(path):
    """FAILED lines in the triage log written by bakeoff.py next to the verdicts."""
    log = Path(path).with_name('triage-' + Path(path).stem + '.log')
    if not log.exists():
        return '?'
    return sum(line.lstrip().startswith('FAILED') for line in
               log.read_text(encoding='utf-8', errors='replace').splitlines())


def verified_labels():
    """Hand-verified answers from audit/eval-labels.toml: id -> 'yes'/'no'/'unsure'."""
    path = AUDIT / 'eval-labels.toml'
    if not path.exists():
        return {}
    return {l['id']: l['answer'] for l in tomllib.loads(path.read_text(encoding='utf-8'))['label']}


def score(opts):
    labels = {r['id']: r for r in read_jsonl(EVAL / ('eval-quick.jsonl' if opts.quick
                                                      else 'eval.jsonl'))}
    # Verified labels override the patch-adjacency guesses: yes -> positive,
    # no -> negative (measures false positives), unsure -> excluded.
    verified = verified_labels()
    for i, r in labels.items():
        answer = verified.get(i)
        if answer:
            r['label'] = {'yes': 'positive', 'no': 'negative', 'unsure': 'skip'}[answer]
        elif r['label'] == 'positive':
            r['label'] = 'unverified'
    labels = {i: r for i, r in labels.items() if r['label'] != 'skip'}
    n_pos = sum(r['label'] == 'positive' for r in labels.values())
    n_neg = sum(r['label'] == 'negative' for r in labels.values())
    n_oth = sum(r['label'] == 'other' for r in labels.values())
    print(f'{"model":26} {"recall [95% CI]":>16} {"+unclear":>8} {"FP on neg":>9} '
          f'{"flag%":>5} {"out tok":>7} {"s/item":>6} {"done":>7} {"failed":>6}')
    for path in opts.files:
        rows = {r['id']: r for r in read_jsonl(path) if r['id'] in labels}
        pos = [r for i, r in rows.items() if labels[i]['label'] == 'positive']
        neg = [r for i, r in rows.items() if labels[i]['label'] == 'negative']
        oth = [r for i, r in rows.items() if labels[i]['label'] == 'other']
        bg = lambda rs: sum(r['verdict'] == 'background_request' for r in rs)
        unc = lambda rs: sum(r['verdict'] == 'unclear' for r in rs)
        # Hard miss: called the request user-driven or absent (soft: disabled/not built).
        hard = lambda rs: sum(r['verdict'] in ('user_initiated', 'no_request') for r in rs)
        # Denominators are the full label counts: a missing verdict (timeout,
        # invalid JSON) is a miss, otherwise failing models look better.
        pct = lambda k, rs: k / max(1, n_pos if rs is pos else n_oth if rs is oth
                                    else n_neg if rs is neg else len(rs))
        lo, hi = wilson(bg(pos), n_pos)
        secs = sum(r.get('seconds', 0) for r in rows.values()) / max(1, len(rows))
        # Output tokens show whether a model thinks (~2k) or answers directly (~200).
        out_tok = sum(r.get('completion_tokens') or 0 for r in rows.values()) / max(1, len(rows))
        failed = failed_count(path)
        recall = f'{pct(bg(pos), pos):.0%} [{lo:.0%}-{hi:.0%}]'
        print(f'{Path(path).stem:26} {recall:>16} {pct(bg(pos) + unc(pos), pos):8.0%} '
              f'{pct(bg(neg), neg):9.0%} {pct(bg(oth), oth):5.0%} {out_tok:7.0f} '
              f'{secs:6.0f} {len(rows):>3}/{len(labels):<3} {failed:>6}')
        if opts.misses:
            for r in pos:
                if r['verdict'] not in ('background_request', 'unclear'):
                    print(f'    miss {r["verdict"]:16} {r["id"][:70]}  '
                          f'[{", ".join(p.split("/")[-1] for p in labels[r["id"]]["patches"])}]')
            for r in neg:
                if r['verdict'] == 'background_request':
                    print(f'    false positive {r["id"][:70]}')
    print(f'({n_pos} verified positives, {n_neg} verified negatives, {n_oth} unverified others; '
          'recall = background_request on positives; "FP on neg" = background_request on '
          'negatives)')


def agree(opts):
    """Verdict agreement between two runs: the noise floor for model comparisons."""
    a = {r['id']: r['verdict'] for r in read_jsonl(opts.a)}
    b = {r['id']: r['verdict'] for r in read_jsonl(opts.b)}
    ids = sorted(set(a) & set(b))
    n = len(ids)
    same = sum(a[i] == b[i] for i in ids)
    # Cohen's kappa: agreement corrected for chance.
    ca, cb = collections.Counter(a[i] for i in ids), collections.Counter(b[i] for i in ids)
    chance = sum(ca[v] * cb[v] for v in ca) / max(1, n * n)
    kappa = (same / max(1, n) - chance) / max(1e-9, 1 - chance)
    bg_a = {i for i in ids if a[i] == 'background_request'}
    bg_b = {i for i in ids if b[i] == 'background_request'}
    print(f'{n} common ids: {same} same verdict ({same / max(1, n):.0%}), kappa {kappa:.2f}; '
          f'background_request only in A: {len(bg_a - bg_b)}, only in B: {len(bg_b - bg_a)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--others', type=int, default=120)
    s = sub.add_parser('score')
    s.add_argument('files', nargs='+')
    s.add_argument('--quick', action='store_true', help='score on the quick set only')
    s.add_argument('--misses', action='store_true', help='list missed positives')
    g = sub.add_parser('agree', help='verdict agreement between two runs (noise floor)')
    g.add_argument('a')
    g.add_argument('b')
    opts = parser.parse_args()
    {'build': build, 'score': score, 'agree': agree}[opts.cmd](opts)


if __name__ == '__main__':
    main()
