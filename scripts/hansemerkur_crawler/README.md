# HanseMerkur Public Document Crawler

Standalone tooling for finding public HanseMerkur PDF and Word files for RAG evaluation. The normal crawl respects each host's `robots.txt`.

Only source code, documentation, requirements, and blank input templates belong in Git. Crawl outputs and downloaded documents stay outside this repository in the neighboring `.docs/hansemerkur-oeffentlich-2026-10-04/` data folder.
The normal crawler respects `robots.txt`. The optional supplemental script below is different: it retries already catalogued PDF/Word links even when their path is disallowed by `robots.txt`. Use it only when you have authorization to retrieve those files; it does not bypass authentication or HTTP access controls.

## Setup

Run from the PaddleDoc repository root:

```bash
python3 -m pip install -r scripts/hansemerkur_crawler/requirements.txt
```

## Run

The default run has no time limit. It first downloads PDF/Word files listed directly in official sitemaps, then discovers deeper linked pages, and finally downloads the additional document links found there. If interrupted, it resumes from the local catalog. Use `--max-seconds` only when you explicitly want a time cap.

```bash
python3 scripts/hansemerkur_crawler/crawl.py
python3 scripts/hansemerkur_crawler/report.py
```

If you have authorization to retrieve the catalogued Robots-blocked documents, use one command to run the normal crawl and then the supplemental pass:

```bash
python3 scripts/hansemerkur_crawler/crawl.py --include-robots-blocked
python3 scripts/hansemerkur_crawler/report.py
```

The supplemental pass intentionally ignores `robots.txt` only for already catalogued PDF/Word URLs. It does not crawl new pages or bypass authentication or HTTP access controls. Use it only with explicit authorization.

To discover without downloading:

```bash
python3 scripts/hansemerkur_crawler/crawl.py --discover-only
python3 scripts/hansemerkur_crawler/report.py
```

To download already catalogued files without more discovery:

```bash
python3 scripts/hansemerkur_crawler/crawl.py --download-pending-only
python3 scripts/hansemerkur_crawler/report.py
```

The supplemental pass can also be started separately; it retries only already catalogued, robots-blocked PDF/Word URLs and does not discover new pages:

```bash
python3 scripts/hansemerkur_crawler/download_remaining.py
python3 scripts/hansemerkur_crawler/report.py
```

By default there is no per-response size limit. The UI's optional size override is off; turn it on to set a cap. The CLI equivalent is `--max-bytes N` (`0` means unlimited). Adjust crawl pages with `--max-pages` (the UI accepts up to 100,000 HTML pages); this is separate from document-list pagination. Add `--max-seconds 600` for an optional 10-minute cap.

## Inputs and Outputs

- `approved-hosts.txt`: optional, manually reviewed external domains, one per line.
- `discovery-seeds.txt`: optional URLs from public search APIs or manual research, one per line.
- `../.docs/hansemerkur-oeffentlich-2026-10-04/originale/`: local RAG corpus containing only PDF and Word document/template files.
- `../.docs/hansemerkur-oeffentlich-2026-10-04/catalog.json`: resumable discovery/download state, URL sources, statuses, and content hashes.
- Sitemap lists, CSVs, reports, and `robots-*.txt` are also generated in that local data folder, not in Git.

Local host/seed files in the data folder take precedence; otherwise the crawler reads the blank templates next to the source. Set `HANSEMERKUR_CRAWL_DIR` to use a different data location.

## Scope and Limitations

Discovery starts from the sitemaps for `www.hansemerkur.de`, `k.hansemerkur.de`, and `www.hmrv.de`, follows advertised sitemap URLs and linked first-party subdomains, and scans HTML in memory for PDF/Word links. Default host scope is `*.hansemerkur.de` and `*.hmrv.de`. Other external hosts are only fetched after manual review and approval in `approved-hosts.txt`.

Only `.pdf`, `.doc`, `.docx`, `.docm`, `.dot`, `.dotx`, and `.dotm` files are downloaded. Search-engine results can be imported through `discovery-seeds.txt`. Unlinked, unindexed, JavaScript-generated, or robots-disallowed files may be missed; complete internet-wide coverage cannot be guaranteed.
