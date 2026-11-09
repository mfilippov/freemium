#!/usr/bin/env python3
"""Runtime check: lists hosts a browser contacted, from a NetLog file.

Run the build in a fresh profile without touching it, e.g.
  chrome.exe --user-data-dir=<dir> --log-net-log=netlog.json --no-first-run about:blank
then close it and run
  python audit/netlog.py netlog.json

Every host is shown with the traffic annotations of its requests (mapped back
from the hash in the NetLog via annotations.xml), the first time it was seen
(seconds after start) and a sample URL. A file cut short by a crash or kill is
read up to the last complete event.
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from common import SRC


def annotation_hash(s):
    """net/traffic_annotation/network_traffic_annotation.h ComputeAnnotationHash."""
    h = 0
    for c in s.encode():
        h = (h * 31 + c) % 138003713
    return h


def annotation_names():
    xml = (SRC / 'tools/traffic_annotation/summary/annotations.xml').read_text(encoding='utf-8')
    ids = set(re.findall(r'<item id="([^"]+)"', xml)) | {'undefined', 'test_partial',
                                                       'missing', 'test', 'net_test'}
    return {annotation_hash(i): i for i in ids}


def load(path):
    text = Path(path).read_text(encoding='utf-8', errors='replace').rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cut = text.rfind('},\n')                  # one event per line, ",\n" separated
        return json.loads(text[:cut + 1] + ']}')


def host_of(url):
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('netlog')
    opts = parser.parse_args()

    log = load(opts.netlog)
    names = annotation_names()
    events = log['events']
    t0 = min(int(e['time']) for e in events) if events else 0

    by_source = collections.defaultdict(set)          # source id -> annotation names
    for e in events:
        a = (e.get('params') or {}).get('traffic_annotation')
        if a is not None:
            by_source[e['source']['id']].add(names.get(a, f'#{a}'))

    hosts = {}
    for e in events:
        p = e.get('params') or {}
        url = p.get('url') if isinstance(p.get('url'), str) else None
        host = host_of(url) if url else p.get('host') if isinstance(p.get('host'), str) else None
        if not host:
            continue
        if '://' in host:                              # host resolver: scheme://host
            host = host_of(host) or host
        host = host.split(':')[0].strip('[]')
        if host in ('localhost', '127.0.0.1', '::1') or url and not url.startswith(('http', 'ws')):
            continue
        h = hosts.setdefault(host, {'n': 0, 'first': None, 'ann': set(), 'url': None})
        h['n'] += 1
        t = (int(e['time']) - t0) / 1000
        h['first'] = t if h['first'] is None else min(h['first'], t)
        h['ann'] |= by_source.get(e['source']['id'], set())
        if url and not h['url']:
            h['url'] = url[:120]

    print(f'{len(hosts)} hosts in {len(events)} events '
          f'({(int(events[-1]["time"]) - t0) / 1000:.0f}s)' if events else 'no events')
    for host, h in sorted(hosts.items(), key=lambda kv: kv[1]['first']):
        print(f'{h["first"]:7.1f}s  {host}  x{h["n"]}  [{", ".join(sorted(h["ann"])) or "-"}]'
              f'  {h["url"] or ""}')


if __name__ == '__main__':
    sys.exit(main())
