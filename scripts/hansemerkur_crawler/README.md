# HanseMerkur Public Document Crawler

Standalone tooling for finding public HanseMerkur PDF and Word files for RAG evaluation. It respects each host's `robots.txt`; it does not bypass crawl exclusions.

Only source code, documentation, requirements, and blank input templates belong in Git. Downloaded documents, catalogs, reports, and robots snapshots are local data and are excluded by this folder's `.gitignore`.

## Setup

Run from the PaddleDoc repository root:

```bash
python3 -m pip install -r scripts/hansemerkur_crawler/requirements.txt
```

## Run

The default run discovers first and downloads only after its page queue is empty. Each run is capped at 10 minutes and resumes from the local catalog.

```bash
python3 scripts/hansemerkur_crawler/crawl.py
python3 scripts/hansemerkur_crawler/report.py
```

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

Adjust limits with `--max-pages`, `--max-bytes`, and `--max-seconds`.

## Inputs and Outputs

- `approved-hosts.txt`: optional, manually reviewed external domains, one per line.
- `discovery-seeds.txt`: optional URLs from public search APIs or manual research, one per line.
- `originale/`: local RAG corpus containing only PDF and Word document/template files.
- `catalog.json`: resumable discovery/download state, URL sources, statuses, and content hashes.
- Sitemap lists, CSVs, reports, and `robots-*.txt`: generated local audit data; ignored by Git.

## Scope and Limitations

Discovery starts from the sitemaps for `www.hansemerkur.de`, `k.hansemerkur.de`, and `www.hmrv.de`, follows advertised sitemap URLs and linked first-party subdomains, and scans HTML in memory for PDF/Word links. Default host scope is `*.hansemerkur.de` and `*.hmrv.de`. Other external hosts are only fetched after manual review and approval in `approved-hosts.txt`.

Only `.pdf`, `.doc`, `.docx`, `.docm`, `.dot`, `.dotx`, and `.dotm` files are downloaded. Search-engine results can be imported through `discovery-seeds.txt`. Unlinked, unindexed, JavaScript-generated, or robots-disallowed files may be missed; complete internet-wide coverage cannot be guaranteed.
