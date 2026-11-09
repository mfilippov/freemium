#!/usr/bin/env python3
"""Stage 2: asks the local model (llama-server) for a verdict on each candidate.

Verdicts are appended to findings/<version>/<profile>.jsonl as they arrive, so
the run can be interrupted and resumed. A candidate is skipped when any
findings file already holds a verdict with the same context hash, model file
and prompt: on a new Chromium version only changed code is re-analyzed.

Usage:
  python audit/triage.py --model coder-next
  python audit/triage.py --model qwen36-35b --candidates FILE --out FILE --limit 50
  python audit/triage.py --model gemma4-12b --after findings/V/gemma4-12b-nothink.jsonl
"""

import argparse
import concurrent.futures
import json
import sys
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

from common import AUDIT, FINDINGS, WORK, chromium_version, read_jsonl, sha

VERDICTS = ['background_request', 'user_initiated', 'policy_only', 'not_built',
            'already_disabled', 'no_request', 'unclear']
SCHEMA = {
    'type': 'object',
    'properties': {
        'analysis': {'type': 'string', 'maxLength': 800},
        'verdict': {'enum': VERDICTS},
        'trigger': {'type': 'string', 'maxLength': 200},
        'destination': {'type': 'string', 'maxLength': 150},
        'data_sent': {'type': 'string', 'maxLength': 200},
        'default_enabled': {'enum': ['yes', 'no', 'unclear']},
        'severity': {'enum': ['none', 'low', 'medium', 'high']},
        'disable_hint': {'type': 'string', 'maxLength': 200},
        'evidence': {'type': 'string', 'maxLength': 300},
        'confidence': {'enum': ['low', 'medium', 'high']},
    },
    'required': ['analysis', 'verdict', 'trigger', 'destination', 'data_sent',
                 'default_enabled', 'severity', 'disable_hint', 'evidence', 'confidence'],
    'additionalProperties': False,
}


def load_profile(name):
    config = tomllib.loads((AUDIT / 'models.toml').read_text(encoding='utf-8'))
    if name not in config['profiles']:
        sys.exit(f'Unknown profile {name}; known: {", ".join(config["profiles"])}')
    return config['profiles'][name]


# Server flags that change throughput only, not the model's answers: profiles
# differing only in these (e.g. gemma4-fast and gemma4-fast-wide) share verdicts.
THROUGHPUT_FLAGS = {'-np': 1, '-c': 1, '--kv-unified': 0, '--no-kv-unified': 0,
                    '--kv-unified-per-slot': 1, '--fit': 1, '-ngl': 1, '--cache-ram': 1,
                    '--backend-sampling': 0, '-b': 1, '-ub': 1, '--ctx-checkpoints': 1}


def semantic_args(args):
    """Server args that can change answers (KV quantization, reasoning budget...)."""
    out, i = [], 0
    while i < len(args):
        if args[i] in THROUGHPUT_FLAGS:
            i += 1 + THROUGHPUT_FLAGS[args[i]]
        else:
            out.append(args[i])
            i += 1
    return out


def cache_key(profile, prompt):
    """Identifies what a verdict depends on besides the candidate context."""
    parts = [profile['file'], profile.get('request', {}), prompt, SCHEMA,
             semantic_args(profile.get('args', [])), profile.get('grammar', True)]
    return sha(json.dumps(parts, sort_keys=True))


def system_prompt(profile, prompt):
    """Without grammar the schema is spelled out in the prompt instead."""
    if profile.get('grammar', True):
        return prompt
    fields = '\n'.join(f'  "{k}": ' + (' | '.join(f'"{v}"' for v in spec['enum']) if 'enum' in spec
                                      else f'string (max {spec["maxLength"]} chars)')
                       for k, spec in SCHEMA['properties'].items())
    return (prompt + '\nOUTPUT FORMAT: a single JSON object with exactly these keys, in this '
            'order, and nothing else (no markdown fences):\n{\n' + fields + '\n}\n')


def parse_verdict(content):
    """Strictly validates a verdict produced without grammar; raises on any deviation
    so the request is retried."""
    start, end = content.find('{'), content.rfind('}')
    verdict = json.loads(content[start:end + 1])
    if not isinstance(verdict, dict):
        raise ValueError(f'not an object: {type(verdict).__name__}')
    for k, spec in SCHEMA['properties'].items():
        value = verdict.get(k)
        if not isinstance(value, str) or ('enum' in spec and value not in spec['enum']):
            raise ValueError(f'bad field {k}: {str(value)[:50]}')
        verdict[k] = value[:spec.get('maxLength', len(value))]
    return {k: verdict[k] for k in SCHEMA['properties']}


def load_cache(key):
    cache = {}
    for path in FINDINGS.glob('**/*.jsonl'):
        for row in read_jsonl(path):
            if row.get('key') == key:
                cache[row['context_hash']] = row
    return cache


def ask(server, profile, prompt, cand, timeout):
    grammar = profile.get('grammar', True)
    body = {
        'messages': [{'role': 'system', 'content': system_prompt(profile, prompt)},
                     {'role': 'user', 'content': cand['context']}],
        'max_tokens': 8000,   # thinking models need room before the JSON
        **profile.get('request', {}),
    }
    # Grammar-constrained decoding runs on one CPU thread for all slots; without
    # it the server can sample on the GPU (backend_sampling), see models.toml.
    if grammar:
        body['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'verdict', 'strict': True, 'schema': SCHEMA}}
    req = urllib.request.Request(f'{server}/v1/chat/completions', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    content = data['choices'][0]['message']['content']
    if grammar:
        return json.loads(content), data.get('usage', {})
    try:
        return parse_verdict(content), data.get('usage', {})
    except ValueError:
        # ~1% of free-form answers are invalid JSON (unescaped quotes in quoted
        # code) and greedy retries repeat them: redo this one request with the
        # grammar, which requires CPU sampling for it.
        return ask(server, {**profile, 'grammar': True,
                            'request': {**profile.get('request', {}), 'backend_sampling': False}},
                   prompt, cand, timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--model', required=True, help='profile from models.toml')
    parser.add_argument('--candidates', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--server', default='http://127.0.0.1:8080')
    parser.add_argument('--parallel', type=int, default=4, help='match llama-server -np')
    parser.add_argument('--limit', type=int, help='only the first N candidates (smoke test)')
    parser.add_argument('--no-cache', action='store_true', help='ignore verdicts from other files')
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--after', type=Path, metavar='FINDINGS',
                        help='second stage: skip candidates this earlier (fast) run '
                             'confidently cleared, see --cleared')
    parser.add_argument('--cleared', default='no_request',
                        help='stage-1 verdicts that, with high confidence, skip stage 2')
    opts = parser.parse_args()

    version = chromium_version()
    profile = load_profile(opts.model)
    prompt = (AUDIT / 'prompt.md').read_text(encoding='utf-8')
    key = cache_key(profile, prompt)
    cands = read_jsonl(opts.candidates or WORK / version / 'candidates.jsonl')[:opts.limit]
    if opts.after:
        cleared_verdicts = set(opts.cleared.split(','))
        cleared = {r['context_hash'] for r in read_jsonl(opts.after)
                   if r['verdict'] in cleared_verdicts and r['confidence'] == 'high'}
        before = len(cands)
        cands = [c for c in cands if c['context_hash'] not in cleared]
        print(f'stage 2: {before - len(cands)} of {before} cleared by {opts.after.name}',
              flush=True)
    out = opts.out or FINDINGS / version / f'{opts.model}.jsonl'
    out.parent.mkdir(parents=True, exist_ok=True)

    # Resume by (id, context_hash): a candidate whose code changed (e.g. after a new
    # patch) is asked again even within the same version and output file.
    done = ({(r['id'], r['context_hash']) for r in read_jsonl(out) if r.get('key') == key}
            if out.exists() else set())
    cache = {} if opts.no_cache else load_cache(key)
    todo, reused = [], []
    for c in cands:
        if (c['id'], c['context_hash']) in done:
            continue
        hit = cache.get(c['context_hash'])
        if hit:
            reused.append({**hit, 'id': c['id'], 'file': c['file'], 'name': c['name'],
                           'reused': True})
        else:
            todo.append(c)
    print(f'{len(cands)} candidates: {len(done)} done, {len(reused)} reused from cache, '
          f'{len(todo)} to ask {opts.model}', flush=True)

    lock = threading.Lock()
    with open(out, 'a', encoding='utf-8', newline='\n') as f:
        def save(row):
            with lock:
                # ASCII escapes: models sometimes emit lone surrogates UTF-8 cannot encode.
                f.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + '\n')
                f.flush()

        for row in reused:
            save(row)

        def work(c):
            start = time.time()
            for attempt in range(3):
                try:
                    verdict, usage = ask(opts.server, profile, prompt, c, opts.timeout)
                    break
                except Exception as e:      # bad JSON, timeout, server restart
                    error = f'{type(e).__name__}: {e}'
                    time.sleep(5)
            else:
                return c, None, error, time.time() - start
            save({'id': c['id'], 'file': c['file'], 'name': c['name'], 'key': key,
                  'context_hash': c['context_hash'], 'model': opts.model,
                  'prompt_tokens': usage.get('prompt_tokens'),
                  'completion_tokens': usage.get('completion_tokens'),
                  'seconds': round(time.time() - start, 1), **verdict})
            return c, verdict, None, time.time() - start

        t0, n, failed, tokens = time.time(), 0, 0, 0
        with concurrent.futures.ThreadPoolExecutor(opts.parallel) as pool:
            for c, verdict, error, _ in pool.map(work, todo):
                n += 1
                if error:
                    failed += 1
                    print(f'  FAILED {c["id"]}: {error[:200]}', flush=True)
                tokens += len(c['context']) // 4
                if n % 25 == 0 or n == len(todo):
                    rate = tokens / (time.time() - t0)
                    eta = (len(todo) - n) * (time.time() - t0) / n / 60
                    print(f'  {n}/{len(todo)} ~{rate:.0f} ctx tok/s, ETA {eta:.0f} min, '
                          f'{failed} failed', flush=True)
    print(f'Done -> {out}')


if __name__ == '__main__':
    main()
