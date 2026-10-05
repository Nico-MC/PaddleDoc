"""Collect linked public documents; run with Python 3 and requests. Resumable.

Scope: sitemap pages and discovered HTML links on www/k.hansemerkur.de;
document links on *.hansemerkur.de, www.hmrv.de and m.hmrv.de. Robots rules are enforced
on every URL and redirect, including wildcard rules and end anchors.
"""
from __future__ import annotations

import concurrent.futures as cf
import argparse
from collections import Counter, deque
from datetime import datetime, timezone
from email.message import Message
import gzip
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
import xml.etree.ElementTree as ET
import requests

SCRIPT_DIR = Path(__file__).resolve().parent


def default_data_dir(script_dir: Path) -> Path:
    data_folder = Path('.docs') / 'hansemerkur-oeffentlich-2026-10-04'
    if len(script_dir.parents) > 2:
        return script_dir.parents[2] / data_folder
    return Path('/app/docs/hansemerkur-oeffentlich-2026-10-04')


DEFAULT_DATA_DIR = default_data_dir(SCRIPT_DIR)
ROOT = Path(os.environ.get('HANSEMERKUR_CRAWL_DIR', DEFAULT_DATA_DIR)).expanduser().resolve()
FILES = ROOT / 'originale'
FILES.mkdir(parents=True, exist_ok=True)
APPROVED_HOSTS_FILE = ROOT / 'approved-hosts.txt'
DISCOVERY_SEEDS_FILE = ROOT / 'discovery-seeds.txt'
PAGES = {'www.hansemerkur.de', 'k.hansemerkur.de', 'www.hmrv.de'}
SITEMAPS = {
    'www.hansemerkur.de': ('/sitemap_index.xml',),
    'k.hansemerkur.de': ('/sitemap_index.xml',),
    'www.hmrv.de': ('/sitemap_index.xml', '/en/sitemap_index.xml'),
}
UA = 'HanseMerkurResearchCollector/1.0'
INTERVAL = 1.0
MAX_BYTES = 0
MAX_PAGES = 10000
DOWNLOAD_EXTENSIONS = {'.doc', '.docm', '.docx', '.dot', '.dotm', '.dotx', '.pdf'}
DOWNLOAD_CONTENT_TYPES = {
    'application/msword',
    'application/pdf',
    'application/vnd.ms-word.document.macroenabled.12',
    'application/vnd.ms-word.template.macroenabled.12',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
}
CONTENT_TYPE_EXTENSIONS = {
    'application/msword': '.doc',
    'application/pdf': '.pdf',
    'application/vnd.ms-word.document.macroenabled.12': '.docm',
    'application/vnd.ms-word.template.macroenabled.12': '.dotm',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template': '.dotx',
}
HTML_EXTENSIONS = {'.asp', '.aspx', '.cfm', '.htm', '.html', '.jspx', '.jsp', '.php', '.xhtml'}
lock = threading.RLock()
next_request = {}
robots = {}
started = datetime.now(timezone.utc).isoformat()
RUN_DEADLINE = None
RUN_STARTED_MONOTONIC = None


class CrawlDeadlineReached(Exception):
    pass


def normalize(url):
    p = urlsplit(url)
    try:
        port = p.port
    except ValueError:
        return None
    if p.scheme not in ('http', 'https') or p.username or p.password or port not in (None, 80, 443):
        return None
    netloc = p.netloc.lower()
    if (p.scheme, port) in {('https', 443), ('http', 80)}:
        netloc = p.hostname.lower()
    return urlunsplit((p.scheme, netloc, p.path or '/', p.query, ''))


def load_approved_hosts():
    source = APPROVED_HOSTS_FILE if APPROVED_HOSTS_FILE.exists() else SCRIPT_DIR / 'approved-hosts.txt'
    if not source.exists():
        return set()
    hosts = set()
    for line in source.read_text(encoding='utf-8').splitlines():
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        parsed = urlsplit(value if '://' in value else '//' + value)
        if parsed.hostname and '.' in parsed.hostname:
            hosts.add(parsed.hostname.lower())
    return hosts


def load_discovery_seeds():
    source = DISCOVERY_SEEDS_FILE if DISCOVERY_SEEDS_FILE.exists() else SCRIPT_DIR / 'discovery-seeds.txt'
    if not source.exists():
        return []
    urls = []
    for line in source.read_text(encoding='utf-8').splitlines():
        value = line.strip()
        if value and not value.startswith('#'):
            normalized = normalize(value)
            if normalized and normalized not in urls:
                urls.append(normalized)
    return urls


APPROVED_HOSTS = load_approved_hosts()
PAGE_SCOPE = {'*.hansemerkur.de', '*.hmrv.de'} | APPROVED_HOSTS


def in_scope(url):
    h = urlsplit(url).hostname or ''
    first_party = (h == 'hansemerkur.de' or h.endswith('.hansemerkur.de')
                   or h == 'hmrv.de' or h.endswith('.hmrv.de'))
    return first_party or any(h == host or h.endswith('.' + host) for host in APPROVED_HOSTS)


def is_page_host(url):
    return in_scope(url)


def throttle(host):
    with lock:
        now = time.monotonic()
        slot = max(now, next_request.get(host, now))
        next_request[host] = slot + INTERVAL
    time.sleep(max(0, slot - time.monotonic()))


def raw(url):
    if RUN_DEADLINE is not None and time.monotonic() >= RUN_DEADLINE:
        raise CrawlDeadlineReached('Per-run time budget reached')
    host = urlsplit(url).hostname
    for result in socket.getaddrinfo(host, None):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError('Non-public address rejected')
    throttle(host)
    for attempt in range(3):
        if RUN_DEADLINE is not None and time.monotonic() >= RUN_DEADLINE:
            raise CrawlDeadlineReached('Per-run time budget reached')
        with requests.get(url, headers={'User-Agent': UA}, timeout=(10, 30),
                          allow_redirects=False, stream=True) as r:
            if r.status_code in (429, 503) and attempt < 2:
                delay = max(5 * (attempt + 1), min(120, int(r.headers.get('Retry-After', '0'))))
                with lock:
                    next_request[host] = max(next_request.get(host, 0), time.monotonic() + delay)
                throttle(host)
                continue
            chunks, size = [], 0
            for part in r.iter_content(65536):
                if RUN_DEADLINE is not None and time.monotonic() >= RUN_DEADLINE:
                    raise CrawlDeadlineReached('Per-run time budget reached')
                size += len(part)
                if MAX_BYTES and size > MAX_BYTES:
                    raise ValueError(f'Download exceeds {MAX_BYTES} bytes')
                chunks.append(part)
            return r.status_code, dict(r.headers), b''.join(chunks)


def rules_for(url):
    origin = urlunsplit((*urlsplit(url)[:2], '', '', ''))
    with lock:
        if origin in robots:
            return robots[origin]
        # Serialized once per origin so parallel requests never bypass robots.
        target = origin + '/robots.txt'
        for _ in range(6):
            code, headers, data = raw(target)
            if code not in (301, 302, 303, 307, 308):
                break
            target = normalize(urljoin(target, headers.get('Location', '')))
            if not target or not in_scope(target):
                raise ValueError('Robots redirect outside scope')
        text = data.decode('utf-8', errors='replace')
        (ROOT / ('robots-' + urlsplit(url).hostname + '.txt')).write_text(text, encoding='utf-8')
        if code in (404, 410):
            value = []
        elif code != 200 or '<html' in text.lower():
            value = [(False, '/')]
        else:
            groups, agents, rules = [], [], []
            for line in text.splitlines():
                line = line.split('#', 1)[0].strip()
                if ':' not in line:
                    continue
                key, val = (v.strip() for v in line.split(':', 1))
                key = key.lower()
                if key == 'user-agent':
                    if rules:
                        groups.append((agents, rules))
                        agents, rules = [], []
                    agents.append(val.lower())
                elif key in ('allow', 'disallow') and val:
                    rules.append((key == 'allow', val))
            groups.append((agents, rules))
            specific = [r for a, r in groups if any(x != '*' and x in UA.lower() for x in a)]
            selected = specific or [r for a, r in groups if '*' in a]
            value = [r for group in selected for r in group]
        robots[origin] = value
        return value


def permitted(url):
    p = urlsplit(url)
    path = p.path + ('?' + p.query if p.query else '')
    matches = []
    for allow, pattern in rules_for(url):
        end = pattern.endswith('$')
        body = pattern[:-1] if end else pattern
        regex = '^' + '.*'.join(re.escape(v) for v in body.split('*')) + ('$' if end else '')
        if re.search(regex, path):
            matches.append((len(body.replace('*', '')), allow))
    return max(matches)[1] if matches else True


def sitemaps_for(host):
    origin = 'https://' + host
    rules_for(origin + '/')
    robots_file = ROOT / ('robots-' + host + '.txt')
    urls = []
    if robots_file.exists():
        for line in robots_file.read_text(encoding='utf-8', errors='replace').splitlines():
            key, separator, value = line.partition(':')
            if separator and key.strip().lower() == 'sitemap':
                url = normalize(urljoin(origin, value.strip()))
                if url and in_scope(url):
                    urls.append(url)
    if not urls:
        urls = [normalize(origin + path) for path in SITEMAPS.get(host, ('/sitemap.xml', '/sitemap_index.xml'))]
    return list(dict.fromkeys(url for url in urls if url))


def discover_sitemap_pages(initial_hosts, discovered_hosts, seen_sitemaps, errors):
    host_queue = deque(initial_hosts)
    pages = []
    while host_queue and len(discovered_hosts) < 100:
        host = host_queue.popleft()
        if host in discovered_hosts:
            continue
        discovered_hosts.add(host)
        try:
            host_sitemaps = sitemaps_for(host)
        except CrawlDeadlineReached as exc:
            errors.append({'host': host, 'error': str(exc)})
            break
        except Exception as exc:
            errors.append({'host': host, 'error': str(exc)})
            continue
        for sitemap_url in host_sitemaps:
            try:
                sitemap_pages = collect_sitemap(sitemap_url, seen_sitemaps, errors)
            except CrawlDeadlineReached as exc:
                errors.append({'url': sitemap_url, 'error': str(exc)})
                return pages
            pages.extend(sitemap_pages)
            for page_url in sitemap_pages:
                normalized_url = normalize(page_url)
                page_host = urlsplit(normalized_url).hostname if normalized_url else None
                if (normalized_url and is_page_host(normalized_url) and page_host
                        and page_host not in discovered_hosts and page_host not in host_queue):
                    host_queue.append(page_host)
    if host_queue:
        errors.append({'error': 'Sitemap host discovery capped at 100 hosts'})
    return pages


def fetch(url):
    for _ in range(8):
        if not in_scope(url):
            return {'status': 'outside_scope', 'final_url': url}
        if not permitted(url):
            return {'status': 'robots_blocked', 'final_url': url}
        code, headers, data = raw(url)
        if code in (301, 302, 303, 307, 308):
            target = normalize(urljoin(url, headers.get('Location', '')))
            if not target:
                raise ValueError('Invalid redirect')
            url = target
            continue
        return {'status': 'ok' if code == 200 else 'http_error', 'http_status': code,
                'final_url': url, 'headers': headers, 'data': data}
    raise ValueError('Too many redirects')


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current, self.label = [], None, []
        self.jsonld, self.jsonld_active, self.jsonld_parts = [], False, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self.jsonld_active, self.jsonld_parts = True, []
        if tag == 'a':
            self.current, self.label = attrs.get('href'), []
        if tag == 'area' and attrs.get('href'):
            self.links.append((attrs['href'], attrs.get('alt', '')))
        if tag in ('iframe', 'embed', 'object'):
            self.links.append((attrs.get('src') or attrs.get('data') or '', attrs.get('title', '')))
        if tag in ('img', 'script', 'source', 'video', 'audio'):
            self.links.append((attrs.get('src', ''), attrs.get('alt') or attrs.get('title') or tag))
        if tag == 'link':
            self.links.append((attrs.get('href', ''), attrs.get('title') or attrs.get('rel') or tag))
        if tag == 'form' and attrs.get('action') and attrs.get('method', 'get').lower() == 'get':
            self.links.append((attrs['action'], attrs.get('name', 'form')))
        for attribute in ('data-download-url', 'data-file-url', 'data-href', 'data-url'):
            if attrs.get(attribute):
                self.links.append((attrs[attribute], attrs.get('title') or attribute))
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh':
            match = re.search(r'url\s*=\s*[\'"]?([^\'"]+)', attrs.get('content', ''), re.I)
            if match:
                self.links.append((match.group(1).strip(), 'meta refresh'))

    def handle_data(self, data):
        if self.jsonld_active:
            self.jsonld_parts.append(data)
        elif self.current:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.jsonld_active:
            self.jsonld.append(''.join(self.jsonld_parts))
            self.jsonld_active, self.jsonld_parts = False, []
            return
        if tag == 'a' and self.current:
            self.links.append((self.current, ' '.join(' '.join(self.label).split())))
            self.current = None


def iter_json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_strings(item)


def collect_sitemap(url, seen=None, errors=None):
    seen = seen if seen is not None else set()
    if url in seen:
        return []
    seen.add(url)
    result = fetch(url)
    if result['status'] != 'ok':
        if errors is not None:
            errors.append({'url': url, 'error': f'Sitemap unavailable: {result["status"]}'})
        return []
    data = result['data']
    try:
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        tree = ET.fromstring(data)
    except Exception as exc:
        if errors is not None:
            errors.append({'url': url, 'error': str(exc)})
        return []
    urls = [n.text.strip() for n in tree.iter() if n.tag.endswith('}loc') and n.text]
    if tree.tag.endswith('sitemapindex'):
        pages = []
        for child_url in urls:
            try:
                pages.extend(collect_sitemap(child_url, seen, errors))
            except CrawlDeadlineReached as exc:
                if errors is not None:
                    errors.append({'url': child_url, 'error': str(exc)})
                break
        return pages
    return urls


def response_is_html(data, headers):
    content_type = headers.get('Content-Type', '').lower()
    prefix = data[:512].lstrip().lower()
    return 'text/html' in content_type or prefix.startswith((b'<!doctype html', b'<html'))


def is_downloadable(url, headers, data=b''):
    path_extension = Path(urlsplit(url).path).suffix.lower()
    disposition = Message()
    disposition['content-disposition'] = headers.get('Content-Disposition', '')
    disposition_extension = Path(disposition.get_filename() or '').suffix.lower()
    if path_extension in DOWNLOAD_EXTENSIONS or disposition_extension in DOWNLOAD_EXTENSIONS or data.startswith(b'%PDF-'):
        return True
    if path_extension and path_extension not in HTML_EXTENSIONS:
        return False
    content_type = headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    return content_type in DOWNLOAD_CONTENT_TYPES


def is_html_candidate(url):
    extension = Path(urlsplit(url).path).suffix.lower()
    return not extension or extension in HTML_EXTENSIONS


def file_extension(url, headers, data=b''):
    if data.startswith(b'%PDF-'):
        return '.pdf'
    extension = Path(urlsplit(url).path).suffix.lower()
    if extension not in DOWNLOAD_EXTENSIONS:
        extension = ''
    if not extension:
        disposition = Message()
        disposition['content-disposition'] = headers.get('Content-Disposition', '')
        suggested = disposition.get_filename()
        extension = Path(suggested or '').suffix.lower()
        if extension not in DOWNLOAD_EXTENSIONS:
            extension = ''
    if not extension:
        content_type = headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        extension = CONTENT_TYPE_EXTENSIONS.get(content_type, mimetypes.guess_extension(content_type)) or '.bin'
    return extension


def file_name(url, headers, digest):
    disposition = Message()
    disposition['content-disposition'] = headers.get('Content-Disposition', '')
    suggested = disposition.get_filename()
    path_name = Path(urlsplit(url).path).name
    name = Path(unquote(suggested or path_name)).stem
    name = re.sub(r'[^\w.-]+', '-', name)[:110] or 'file'
    return f'{name}--{digest[:16]}'


def main():
    global MAX_BYTES, RUN_DEADLINE, RUN_STARTED_MONOTONIC
    parser = argparse.ArgumentParser(description='Discover and collect public HanseMerkur RAG sources.')
    parser.add_argument('--discover-only', action='store_true', help='Scan pages and catalog candidate files without downloading them.')
    parser.add_argument('--download-pending-only', action='store_true', help='Download catalogued pending files without scanning more pages.')
    parser.add_argument('--include-robots-blocked', action='store_true',
                        help='After the normal crawl, attempt catalogued PDF/Word URLs disallowed by robots.txt.')
    parser.add_argument('--max-pages', type=int, default=MAX_PAGES, help='Maximum HTML pages to scan (default: %(default)s).')
    parser.add_argument('--max-bytes', type=int, default=MAX_BYTES,
                        help='Maximum bytes per response; 0 means unlimited (default).')
    parser.add_argument('--max-seconds', type=int, default=0,
                        help='Optional wall-clock limit in seconds; 0 means no limit (default).')
    args = parser.parse_args()
    if args.discover_only and args.download_pending_only:
        parser.error('--discover-only and --download-pending-only cannot be used together')
    if args.discover_only and args.include_robots_blocked:
        parser.error('--include-robots-blocked cannot be used with --discover-only')
    if args.max_pages < 1:
        parser.error('--max-pages must be positive')
    if args.max_bytes < 0:
        parser.error('--max-bytes must be zero (unlimited) or positive')
    if args.max_seconds < 0:
        parser.error('--max-seconds must be zero (unlimited) or positive')
    MAX_BYTES = args.max_bytes
    RUN_STARTED_MONOTONIC = time.monotonic()
    RUN_DEADLINE = RUN_STARTED_MONOTONIC + args.max_seconds if args.max_seconds else None

    state_file = ROOT / 'catalog.json'
    old = json.loads(state_file.read_text()) if state_file.exists() else {}
    if args.download_pending_only and not old:
        parser.error('--download-pending-only requires an existing catalog.json')
    records = {}
    for url, record in old.get('documents', {}).items():
        key = normalize(url)
        if not key:
            continue
        record['url'] = key
        local_path = record.get('local_path')
        if local_path and not (ROOT / local_path).is_file():
            for field in ('local_path', 'sha256', 'size_bytes'):
                record.pop(field, None)
            if record.get('status') in {'downloaded', 'duplicate'}:
                record['status'] = ('pending' if in_scope(key) and is_downloadable(key, {})
                                    else 'outside_scope' if not in_scope(key) else 'ignored_type')
        if key not in records:
            records[key] = record
        else:
            other = records[key]
            sources = other['sources'] + [s for s in record['sources'] if s not in other['sources']]
            if record.get('local_path') or other['status'] == 'pending':
                records[key] = record
            records[key]['sources'] = sources
    for url, record in records.items():
        if record['status'] == 'outside_scope' and in_scope(url):
            record['status'] = 'pending'
    previous_pages = old.get('pages', {})
    rescan_previous_pages = set(old.get('scope', {}).get('page_hosts', [])) != PAGE_SCOPE
    queued_pages = list(old.get('pending_pages', []))
    pages = {}
    for page_url, page in previous_pages.items():
        normalized_url = normalize(page_url)
        if not normalized_url:
            continue
        if not rescan_previous_pages:
            pages[normalized_url] = {k: v for k, v in page.items()
                                     if k not in {'local_path', 'sha256', 'size_bytes', 'duplicate',
                                                  'title', 'text_path', 'text_sha256',
                                                  'text_size_bytes', 'duplicate_text'}}
    if rescan_previous_pages:
        queued_pages = [u for u, r in previous_pages.items() if r.get('status') != 'document'] + queued_pages
    normalized_pages = []
    for queued_url in queued_pages:
        normalized_url = normalize(queued_url)
        if (normalized_url and normalized_url not in pages
                and is_html_candidate(normalized_url) and not is_downloadable(normalized_url, {})):
            normalized_pages.append(normalized_url)
    page_queue = deque() if args.download_pending_only else deque(dict.fromkeys(normalized_pages))
    sitemap_errors = list(old.get('sitemap_errors', [])) if args.download_pending_only else []
    discovered_sitemap_hosts = set(old.get('scope', {}).get('sitemap_hosts', [])) if args.download_pending_only else set()
    seen_sitemaps = set()
    manual_seeds = [] if args.download_pending_only else load_discovery_seeds()
    seeds = []
    if not args.download_pending_only:
        root_hosts = set(PAGES) | APPROVED_HOSTS
        root_hosts.update(urlsplit(url).hostname for url in manual_seeds if is_page_host(url))
        seeds = [normalize(u) for u in discover_sitemap_pages(
            sorted(root_hosts), discovered_sitemap_hosts, seen_sitemaps, sitemap_errors)]
        seeds.extend(manual_seeds)
        seeds = list(dict.fromkeys(url for url in seeds if url))
        (ROOT / 'sitemap-urls.json').write_text(json.dumps(seeds, indent=2), encoding='utf-8')
    external_page_candidates = (old.get('external_page_candidates', {}) if args.download_pending_only else {
        url: {'url': url, 'sources': [{'page': 'discovery-seeds.txt', 'link_text': 'manual/search seed'}]}
        for url in manual_seeds if not is_page_host(url) and not is_downloadable(url, {})
    })
    known_pages = set(pages) | set(page_queue)
    limit_pages = set(old.get('limit_pending_pages', []))
    for url in ([] if args.download_pending_only else sorted(limit_pages.copy())):
        if is_page_host(url) and is_html_candidate(url) and url not in known_pages:
            if len(known_pages) < args.max_pages:
                known_pages.add(url)
                page_queue.append(url)
                limit_pages.discard(url)
    for url in seeds:
        if not url:
            continue
        if is_downloadable(url, {}):
            record = records.setdefault(url, {
                'url': url, 'status': 'pending' if in_scope(url) else 'outside_scope', 'sources': []})
            sitemap_source = {'page': 'robots-sitemap', 'link_text': 'Sitemap'}
            if sitemap_source not in record['sources']:
                record['sources'].append(sitemap_source)
            if url in manual_seeds:
                source = {'page': 'discovery-seeds.txt', 'link_text': 'manual/search seed'}
                if source not in record['sources']:
                    record['sources'].append(source)
        elif is_page_host(url) and is_html_candidate(url) and url not in known_pages:
            if len(known_pages) < args.max_pages:
                known_pages.add(url)
                page_queue.append(url)
            else:
                limit_pages.add(url)
        elif is_html_candidate(url) and not is_page_host(url):
            external_page_candidates.setdefault(url, {'url': url, 'sources': []})
    pending_documents = []
    for url, record in records.items():
        if record['status'] != 'pending':
            continue
        if not in_scope(url):
            record['status'] = 'outside_scope'
        elif is_downloadable(url, {}):
            host = urlsplit(url).hostname or ''
            host_priority = (0 if host == 'www.hansemerkur.de' else
                             1 if host == 'k.hansemerkur.de' else
                             2 if host == 'www.hmrv.de' else 3)
            is_sitemap_document = any(source.get('page') == 'robots-sitemap'
                                      for source in record.get('sources', []))
            pending_documents.append((host_priority, not is_sitemap_document, url))
        else:
            record['status'] = 'ignored_type'
    ordered_documents = sorted(pending_documents)
    sitemap_doc_queue = deque(url for _, is_non_sitemap, url in ordered_documents if not is_non_sitemap)
    doc_queue = deque(url for _, is_non_sitemap, url in ordered_documents if is_non_sitemap)
    hashes = {r['sha256']: r['local_path'] for r in records.values() if r.get('sha256')}
    futures = {}
    count = 0

    def save():
        pending_pages = (list(old.get('pending_pages', [])) if args.download_pending_only
                         else list(page_queue) + [u for u, kind in futures.values() if kind == 'page'])
        elapsed_seconds = int(time.monotonic() - RUN_STARTED_MONOTONIC)
        budget_exhausted = RUN_DEADLINE is not None and time.monotonic() >= RUN_DEADLINE
        out = {'started_at_utc': old.get('started_at_utc', started),
               'updated_at_utc': datetime.now(timezone.utc).isoformat(),
                               'scope': {'page_hosts': sorted(PAGE_SCOPE), 'document_hosts': '*.hansemerkur.de, *.hmrv.de',
                                         'approved_external_hosts': sorted(APPROVED_HOSTS),
                                         'discovery_only': args.discover_only,
                                         'download_pending_only': args.download_pending_only,
                                                 'sitemap_hosts': sorted(discovered_sitemap_hosts),
                                                 'download_extensions': sorted(DOWNLOAD_EXTENSIONS), 'robots_enforced': True,
                                                 'max_pages': args.max_pages, 'max_bytes': MAX_BYTES,
                                                 'max_seconds': args.max_seconds,
                                                 'request_interval_seconds': INTERVAL},
               'summary': {'pages': len(pages), 'pending_pages': len(pending_pages),
                           'document_statuses': dict(Counter(r['status'] for r in records.values())),
                                                       'unique_files': len(hashes),
                                                     'discovery_limit_hits': len(limit_pages),
                                                     'elapsed_seconds': elapsed_seconds,
                                                     'time_budget_exhausted': budget_exhausted},
                             'sitemap_errors': sitemap_errors, 'limit_pending_pages': sorted(limit_pages),
                               'pending_pages': pending_pages, 'documents': records, 'pages': pages,
                               'external_page_candidates': external_page_candidates}
        temp = state_file.with_suffix('.tmp')
        temp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(state_file)
        print(json.dumps(out['summary']), flush=True)

    def add_document(url, source, title):
        queue = sitemap_doc_queue if source == 'robots-sitemap' else doc_queue
        if url not in records:
            status = 'pending' if in_scope(url) else 'outside_scope'
            records[url] = {'url': url, 'status': status, 'sources': []}
            if status == 'pending':
                queue.append(url)
        elif (in_scope(url) and is_downloadable(url, {})
              and records[url].get('status') in {'ignored_type', 'outside_scope', 'unsupported_content'}):
            records[url]['status'] = 'pending'
            if url not in sitemap_doc_queue and url not in doc_queue:
                queue.append(url)
        if source == 'robots-sitemap' and url in doc_queue:
            doc_queue.remove(url)
            sitemap_doc_queue.append(url)
        source_entry = {'page': source, 'link_text': title}
        if source_entry not in records[url]['sources']:
            records[url]['sources'].append(source_entry)

    def queue_page(url):
        if url in known_pages or url in pages or not is_page_host(url) or not is_html_candidate(url):
            return
        if len(known_pages) < args.max_pages:
            known_pages.add(url)
            page_queue.append(url)
        else:
            limit_pages.add(url)

    def add_host_sitemaps(host):
        new_urls = discover_sitemap_pages([host], discovered_sitemap_hosts, seen_sitemaps, sitemap_errors)
        for raw_url in new_urls:
            candidate = normalize(raw_url)
            if not candidate:
                continue
            seeds.append(candidate)
            if is_downloadable(candidate, {}):
                add_document(candidate, 'robots-sitemap', 'Sitemap')
            else:
                queue_page(candidate)
        (ROOT / 'sitemap-urls.json').write_text(json.dumps(seeds, indent=2), encoding='utf-8')

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        while page_queue or ((sitemap_doc_queue or doc_queue) and not args.discover_only) or futures:
            while (len(futures) < 4
                   and (RUN_DEADLINE is None or time.monotonic() < RUN_DEADLINE)):
                if sitemap_doc_queue and not args.discover_only:
                    kind = 'sitemap_document'
                elif any(future_kind == 'sitemap_document' for _, future_kind in futures.values()):
                    break
                elif page_queue:
                    kind = 'page'
                elif any(future_kind == 'page' for _, future_kind in futures.values()):
                    break
                elif doc_queue and not args.discover_only:
                    kind = 'document'
                else:
                    break
                queue = (page_queue if kind == 'page' else
                         sitemap_doc_queue if kind == 'sitemap_document' else doc_queue)
                url = queue.popleft()
                if kind == 'page' and url in pages:
                    continue
                futures[pool.submit(fetch, url)] = (url, kind)
            if not futures:
                break
            done, _ = cf.wait(futures, return_when=cf.FIRST_COMPLETED)
            for future in done:
                url, kind = futures.pop(future)
                try:
                    result = future.result()
                except CrawlDeadlineReached:
                    retry_queue = (page_queue if kind == 'page' else
                                   sitemap_doc_queue if kind == 'sitemap_document' else doc_queue)
                    retry_queue.appendleft(url)
                    continue
                except Exception as exc:
                    result = {'status': 'error', 'error': str(exc)}
                data = result.pop('data', b'')
                headers = result.pop('headers', {})
                result['checked_at_utc'] = datetime.now(timezone.utc).isoformat()
                result['content_type'] = headers.get('Content-Type', '')
                is_html = response_is_html(data, headers)
                downloadable = is_downloadable(result.get('final_url', url), headers, data)
                if result['status'] == 'ok' and downloadable and not is_html:
                    if kind == 'page':
                        records.setdefault(url, {'url': url, 'sources': []})
                        pages[url] = {'status': 'document'}
                    digest = hashlib.sha256(data).hexdigest()
                    ext = file_extension(url, headers, data)
                    name = file_name(url, headers, digest)
                    dest = 'originale/' + name + ext
                    duplicate = digest in hashes
                    if not duplicate:
                        FILES.mkdir(parents=True, exist_ok=True)
                        (ROOT / dest).write_bytes(data)
                        hashes[digest] = dest
                    records[url].update(result, status='duplicate' if duplicate else 'downloaded',
                                        sha256=digest, local_path=hashes[digest], size_bytes=len(data),
                                        last_modified=headers.get('Last-Modified'), etag=headers.get('ETag'))
                elif kind in {'document', 'sitemap_document'}:
                    records[url].update(result)
                    if result['status'] == 'ok':
                        records[url]['status'] = 'unsupported_content' if is_html else 'ignored_type'
                else:
                    pages[url] = result if result['status'] != 'ok' or is_html else dict(result, status='ignored_type')
                    if result['status'] == 'ok' and is_html:
                        parser = Links()
                        parser.feed(data.decode('utf-8', errors='replace'))
                        for structured_blob in parser.jsonld:
                            try:
                                structured_data = json.loads(structured_blob)
                            except json.JSONDecodeError:
                                continue
                            for value in iter_json_strings(structured_data):
                                structured_url = normalize(urljoin(result.get('final_url', url), value))
                                if structured_url and is_downloadable(structured_url, {}):
                                    parser.links.append((structured_url, 'JSON-LD'))
                        for href, title in parser.links:
                            link = normalize(urljoin(result.get('final_url', url), href))
                            if not link:
                                continue
                            link_host = urlsplit(link).hostname
                            if is_page_host(link) and link_host not in discovered_sitemap_hosts:
                                add_host_sitemaps(link_host)
                            if is_downloadable(link, {}):
                                add_document(link, url, title)
                            elif is_page_host(link):
                                queue_page(link)
                            elif is_html_candidate(link):
                                candidate = external_page_candidates.setdefault(
                                    link, {'url': link, 'sources': []})
                                source = {'page': url, 'link_text': title}
                                if source not in candidate['sources']:
                                    candidate['sources'].append(source)
                count += 1
                if count % 20 == 0:
                    save()
        save()
    if (args.include_robots_blocked
            and (RUN_DEADLINE is None or time.monotonic() < RUN_DEADLINE)):
        from download_remaining import main as download_robots_blocked

        download_robots_blocked()


if __name__ == '__main__':
    main()
