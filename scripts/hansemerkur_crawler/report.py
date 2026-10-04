"""Validate downloaded originals and export readable CSV/Markdown reports."""
import csv
from collections import Counter
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
catalog = json.loads((root / 'catalog.json').read_text(encoding='utf-8'))
documents = list(catalog['documents'].values())
pages = catalog.get('pages', {})
unique = {}
for doc in documents:
    if doc.get('local_path'):
        unique.setdefault(doc['local_path'], []).append(doc)

total_bytes = 0
for local_path, rows in unique.items():
    data = (root / local_path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == rows[0]['sha256'], local_path
    assert len(data) == rows[0]['size_bytes'], local_path
    total_bytes += len(data)

fields = ['status', 'local_path', 'url', 'final_url', 'sha256', 'size_bytes',
          'content_type', 'checked_at_utc', 'last_modified', 'http_status',
          'previous_status', 'robots_override', 'error', 'sources']
with (root / 'dokumentkatalog.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    for doc in documents:
        row = {k: doc.get(k, '') for k in fields}
        row['sources'] = json.dumps(doc.get('sources', []), ensure_ascii=False)
        writer.writerow(row)

with (root / 'dateiliste.csv').open('w', encoding='utf-8-sig', newline='') as f:
    fields = ['datei', 'bytes', 'sha256', 'titel', 'urls', 'fundseiten']
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    for path, rows in sorted(unique.items()):
        sources = [s for r in rows for s in r.get('sources', [])]
        writer.writerow({'datei': path, 'bytes': rows[0]['size_bytes'], 'sha256': rows[0]['sha256'],
                         'titel': ' | '.join(sorted({s['link_text'] for s in sources if s['link_text']})),
                         'urls': ' | '.join(r['url'] for r in rows),
                         'fundseiten': ' | '.join(sorted({s['page'] for s in sources}))})

page_fields = ['url', 'status', 'final_url', 'content_type', 'checked_at_utc',
               'http_status', 'error']
with (root / 'seitenkatalog.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=page_fields)
    writer.writeheader()
    for url, page in sorted(pages.items()):
        row = {k: page.get(k, '') for k in page_fields if k != 'url'}
        row['url'] = url
        writer.writerow(row)

external_documents = [d for d in documents if d.get('status') == 'outside_scope']
external_fields = ['url', 'final_url', 'content_type', 'sources']
with (root / 'externe-kandidaten.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=external_fields)
    writer.writeheader()
    for doc in external_documents:
        row = {k: doc.get(k, '') for k in external_fields}
        row['sources'] = json.dumps(doc.get('sources', []), ensure_ascii=False)
        writer.writerow(row)

external_pages = list(catalog.get('external_page_candidates', {}).values())
with (root / 'externe-seitenkandidaten.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['url', 'sources'])
    writer.writeheader()
    for page in external_pages:
        writer.writerow({'url': page.get('url', ''),
                         'sources': json.dumps(page.get('sources', []), ensure_ascii=False)})

statuses = Counter(d['status'] for d in documents)
page_statuses = Counter(p['status'] for p in catalog['pages'].values())
summary = {
    'unique_files': len(unique), 'total_bytes': total_bytes,
    'document_statuses': dict(statuses), 'page_statuses': dict(page_statuses),
    'external_candidates': len(external_documents),
    'external_page_candidates': len(external_pages),
    'pending_pages': len(catalog.get('pending_pages', [])),
    'sitemap_errors': catalog.get('sitemap_errors', []),
    'discovery_limit_hits': catalog.get('summary', {}).get('discovery_limit_hits', 0),
    'elapsed_seconds': catalog.get('summary', {}).get('elapsed_seconds', 0),
    'time_budget_exhausted': catalog.get('summary', {}).get('time_budget_exhausted', False),
    'all_downloaded_hashes_verified': True,
    'supplemental_download': catalog.get('supplemental_download'),
}
(root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
lines = [
    '# Ergebnis der Dokumentensammlung', '',
    f"Katalogstand (UTC): {catalog['updated_at_utc']}", '',
    f'- **{len(unique)} unterschiedliche Originaldateien**, insgesamt {total_bytes / 1024**2:.1f} MiB.',
    f'- {len(documents)} entdeckte Dokument-URLs.',
    f'- {len(pages)} bearbeitete Seiten-URLs; {len(catalog.get("pending_pages", []))} noch ausstehend.',
    f"- Laufzeit: {catalog.get('summary', {}).get('elapsed_seconds', 0)} Sekunden; Zeitlimit erreicht: {catalog.get('summary', {}).get('time_budget_exhausted', False)}.",
    f'- {len(external_documents)} externe Dokumentkandidaten in `externe-kandidaten.csv` (nicht heruntergeladen).',
    f'- {len(external_pages)} externe Seitenkandidaten in `externe-seitenkandidaten.csv` (nicht gecrawlt).',
    '- SHA-256 und Dateigröße aller gespeicherten Dateien geprüft.', '',
    '| Dokumentstatus | Anzahl |', '|---|---:|',
    *[f'| {key} | {value} |' for key, value in sorted(statuses.items())], '',
    '## Dateien und Herkunft', '',
    'Die Originale liegen in `originale/`. `dateiliste.csv` enthält eine Zeile pro Datei mit '
    'allen zugehörigen URLs und Fundseiten. HTML-Seiten werden nur während des Crawl-Vorgangs '
    'im Speicher ausgewertet; `seitenkatalog.csv` enthält nur Besuchsstatus und URLs. '
    '`externe-kandidaten.csv` '
    'listet gefundene Links außerhalb der freigegebenen Hosts; sie werden nicht heruntergeladen.', '',
    '`dokumentkatalog.csv` und `catalog.json` enthalten auch robots-gesperrte, externe oder '
    'fehlgeschlagene Dokumentlinks.', '',
    'Die erfassten Originaldateien bilden einen Snapshot innerhalb des in README.md beschriebenen '
    'Crawl-Bereichs. Nicht öffentlich abrufbare und unverlinkte Dateien sind nicht Teil des Downloads.', '',
]
if catalog.get('summary', {}).get('discovery_limit_hits'):
    lines.extend([f"Seitenlimit erreicht: {catalog['summary']['discovery_limit_hits']} weitere interne Seitenlinks wurden nicht eingeplant.", ''])
if catalog.get('sitemap_errors'):
    lines.extend([f"Sitemap-Fehler: {len(catalog['sitemap_errors'])}; Details stehen in `catalog.json`.", ''])
if catalog.get('supplemental_download'):
    supplement = catalog['supplemental_download']
    lines.extend(['## Ergänzender Download', '',
                  'Auf ausdrücklichen Nutzerwunsch wurden die zunächst wegen robots.txt ausgelassenen '
                  'öffentlichen Dokumentlinks direkt abgerufen. Der ursprüngliche Katalog liegt in '
                  '`catalog-vor-nachdownload.json`.', '',
                  f"Zusätzliche unterschiedliche Dateien: {supplement.get('new_unique_files', 'Lauf noch nicht abgeschlossen')}", '',
                  f"Ergebnisse der erneut geprüften Links: {json.dumps(supplement.get('outcomes', {}), ensure_ascii=False)}", ''])
if page_statuses.get('error') or page_statuses.get('http_error'):
    lines.extend(['Einige Seiten konnten nicht erfolgreich abgerufen werden. Ihre URLs und '
                  'Fehler stehen unter `pages` in `catalog.json`.', ''])
(root / 'ERGEBNIS.md').write_text('\n'.join(lines), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
