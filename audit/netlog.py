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


# Requests a page (or an extension, or the user opening DevTools / downloading)
# makes; everything else is the browser acting on its own and is listed.
PAGE_ANNOTATIONS = {
    'navigation_url_loader', 'blink_resource_loader', 'blink_extension_resource_loader',
    'websocket_stream', 'websocket_basic_stream', 'webrtc_peer_connection',
    'webrtc_rtc_transport', 'service_worker_navigation_preload', 'service_worker_script_load',
    'service_worker_update_checker', 'service_worker_race_network_request', 'worker_script_load',
    'early_hints_preload', 'speculation_rules_prefetch', 'speculation_rules_prefetch_probe',
    'keepalive_fetch_retry', 'blink_fetch_later_manager', 'preload_activation_beacon',
    'back_navigation_cache_query', 'favicon_loader', 'pdf_plugin_placeholder',
    'renderer_initiated_download', 'download_web_contents_frame', 'download_manager_resume',
    'parallel_download_job', 'drag_download_file', 'download_bubble_retry_download',
    'downloads_dom_handler', 'devtools_network_resource',
    # Accepted 2026-11-14: OpenSearch description a visited page links to; images
    # (search engine favicons) shown by an open chrome:// page such as settings.
    'open_search', 'sanitized_image_source',
}


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
    parser.add_argument('--all', action='store_true', help='also list hosts only pages contacted')
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
        h = hosts.setdefault(host, {'n': 0, 'first': None, 'ann': {}, 'url': None})
        h['n'] += 1
        t = (int(e['time']) - t0) / 1000
        h['first'] = t if h['first'] is None else min(h['first'], t)
        for a in by_source.get(e['source']['id'], ()):
            h['ann'].setdefault(a, url[:120] if url else None)
            if url and not h['ann'][a]:
                h['ann'][a] = url[:120]
        if url and not h['url']:
            h['url'] = url[:120]

    span = (int(events[-1]['time']) - t0) / 1000 if events else 0
    browser = {k: v for k, v in hosts.items()
               if any(a not in PAGE_ANNOTATIONS for a in v['ann'])}
    dns_only = {k: v for k, v in hosts.items() if not v['ann']}
    page = len(hosts) - len(browser) - len(dns_only)
    print(f'{len(hosts)} hosts in {len(events)} events ({span / 3600:.1f}h): '
          f'{len(browser)} with requests not made by pages, {len(dns_only)} DNS/connect only, '
          f'{page} only from pages')
    if browser:
        print('\nNOT MADE BY PAGES (check these):')
        for host, h in sorted(browser.items(), key=lambda kv: kv[1]['first']):
            for a, u in sorted(h['ann'].items()):
                if a not in PAGE_ANNOTATIONS:
                    print(f'{fmt_time(h["first"])}  {host}  [{a}]  {u or ""}')
    if dns_only:
        print('\nDNS/CONNECT ONLY (preconnect by a page, or a request without annotation):')
        for host, h in sorted(dns_only.items(), key=lambda kv: kv[1]['first']):
            print(f'{fmt_time(h["first"])}  {host}  x{h["n"]}')
    if opts.all and page:
        print('\nONLY FROM PAGES:')
        for host, h in sorted(hosts.items(), key=lambda kv: kv[1]['first']):
            if host not in browser and host not in dns_only:
                print(f'{fmt_time(h["first"])}  {host}  [{", ".join(sorted(h["ann"]))}]')


def fmt_time(s):
    return f'{int(s // 3600):3d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d}'


if __name__ == '__main__':
    sys.exit(main())
