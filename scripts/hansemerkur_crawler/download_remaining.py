"""Download catalogued public document links previously skipped due to robots.txt.

This explicit supplemental run does not enforce robots.txt. It does not crawl
new pages or use credentials. Host allowlisting, redirect checks, size limits,
rate limiting and normal HTTP access controls remain in effect.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit

import crawl

ROOT = crawl.ROOT
WORD_EXTENSIONS = {'.doc', '.docm', '.docx', '.dot', '.dotm', '.dotx'}
OLE_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


def now():
    return datetime.now(timezone.utc).isoformat()


def fetch_public(url):
    for _ in range(8):
        if not crawl.in_scope(url):
            return {'status': 'outside_scope', 'final_url': url}
        code, headers, data = crawl.raw(url)
        if code in (301, 302, 303, 307, 308):
            target = crawl.normalize(urljoin(url, headers.get('Location', '')))
            if not target:
                raise ValueError('Invalid redirect')
            url = target
            continue
        return {'status': 'ok' if code == 200 else 'http_error',
                'http_status': code, 'final_url': url, 'headers': headers, 'data': data}
    raise ValueError('Too many redirects')


def main():
    path = ROOT / 'catalog.json'
    snapshot = ROOT / 'catalog-vor-nachdownload.json'
    if not snapshot.exists():
        with snapshot.open('x', encoding='utf-8') as f:
            f.write(path.read_text(encoding='utf-8'))
    catalog = json.loads(path.read_text(encoding='utf-8'))
    records = catalog['documents']
    targets = [url for url, r in records.items()
               if r['status'] in ('robots_blocked', 'supplement_pending')
               and crawl.is_downloadable(url, {})]
    hashes = {r['sha256']: r['local_path'] for r in records.values()
              if r.get('sha256') and r.get('local_path')
              and (ROOT / r['local_path']).is_file()}
    supplement = catalog.setdefault('supplemental_download', {
        'started_at_utc': now(), 'initial_target_count': len(targets),
        'robots_enforced': False, 'scope': 'previously_catalogued_robots_excluded_document_links',
        'requested_by_user': True, 'initial_unique_files': len(hashes),
    })
    supplement.pop('completed_at_utc', None)
    for url in targets:
        records[url].setdefault('previous_status', 'robots_blocked')
        records[url]['status'] = 'supplement_pending'
        records[url]['robots_override'] = True

    def save():
        catalog['updated_at_utc'] = now()
        catalog['scope']['robots_policy'] = 'initial_crawl_enforced; supplemental_document_download_not_enforced'
        catalog['scope']['robots_enforced'] = False
        catalog['summary'] = {
            'pages': len(catalog['pages']), 'pending_pages': len(catalog.get('pending_pages', [])),
            'document_statuses': dict(Counter(r['status'] for r in records.values())),
            'unique_files': len(hashes),
        }
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
        print(json.dumps(catalog['summary']), flush=True)

    save()
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(fetch_public, url): url for url in targets}
        for index, future in enumerate(as_completed(pending), 1):
            url = pending[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {'status': 'error', 'error': str(exc)}
            data = result.pop('data', b'')
            headers = result.pop('headers', {})
            result['checked_at_utc'] = now()
            result['content_type'] = headers.get('Content-Type', '')
            if result['status'] == 'ok':
                ext = crawl.file_extension(url, headers, data)
                is_pdf = ext == '.pdf' and data.lstrip().startswith(b'%PDF-')
                is_zip_word = ext in {'.docx', '.docm', '.dotx', '.dotm'} and data.startswith(b'PK')
                is_ole_word = ext in {'.doc', '.dot'} and data.startswith(OLE_SIGNATURE)
                if crawl.response_is_html(data, headers) or not (is_pdf or is_zip_word or is_ole_word):
                    ext = None
                if ext:
                    digest = hashlib.sha256(data).hexdigest()
                    name = re.sub(r'[^\w.-]+', '-', unquote(Path(urlsplit(url).path).stem))[:110] or 'document'
                    dest = 'originale/' + name + '--' + digest[:16] + ext
                    duplicate = digest in hashes
                    if not duplicate:
                        (ROOT / 'originale').mkdir(parents=True, exist_ok=True)
                        temp = (ROOT / dest).with_suffix(ext + '.part')
                        temp.write_bytes(data)
                        temp.replace(ROOT / dest)
                        hashes[digest] = dest
                    result.update(status='duplicate' if duplicate else 'downloaded',
                                  sha256=digest, local_path=hashes[digest], size_bytes=len(data),
                                  last_modified=headers.get('Last-Modified'), etag=headers.get('ETag'))
                else:
                    result['status'] = 'unsupported_content'
            records[url].update(result)
            if index % 20 == 0:
                save()
    supplement['completed_at_utc'] = now()
    supplement['new_unique_files'] = len(hashes) - supplement['initial_unique_files']
    supplement['outcomes'] = dict(Counter(r['status'] for r in records.values() if r.get('robots_override')))
    save()


if __name__ == '__main__':
    main()
