# Mail Ingestion (Design)

Status: **implemented — shipped in v1.3.0** — written 2026-08-15 against PaddleDoc v1.2.1
(`main`, migration head `0008_vl_benchmarks`). This document specifies a **universal**
mail-ingestion feature: any client — an SMTP gateway or mail relay, an n8n workflow, a
script — POSTs a raw RFC-822 email, and PaddleDoc itself parses it,
processes the attachments through the existing OCR pipeline, stores everything in the
database, shows it in the UI, and exposes retrieval APIs for downstream consumers
(n8n, Amazon Bedrock AgentCore, RAG ingestors).

An earlier draft of this feature made the *sender* parse MIME and upload decoded parts.
This design inverts that — **PaddleDoc is the mail parser**, so every sender integration
collapses to "POST the raw bytes". Python's stdlib `email` package (`BytesParser` +
`policy.default`) does the heavy lifting; senders need zero mail expertise.

## Big picture

```
SMTP gateway ──────┐
n8n workflow ──────┼──► POST /api/v1/mail/messages   (raw .eml, Bearer pd_…)
any script ────────┘         │
                             │  parse MIME (stdlib email), sha256 dedup
                             ▼
                       MailMessage row (raw .eml + envelope + body markdown, all in DB)
                             │
                             ├─► body → Markdown directly (no OCR, markdownify)
                             └─► one Job per supported attachment → Celery process_job → OCR
                             ▼
        UI: Mail section (list, detail, downloads)
        API: GET message / raw .eml / parts / export.json   ◄── n8n, Bedrock AgentCore
```

Design invariants (each grounded in an existing PaddleDoc convention):

1. **DB is the source of truth for all bytes.** `Job.upload_content`, `Job.result_markdown`,
   `JobMarkdownVersion.content` and `JobArtifact.content` all live in the DB; disk under
   `storage/` is a best-effort cache the worker rehydrates from the DB when missing
   (`_resolve_upload_path`). The Helm chart does mount a shared RWX PVC by default, but no
   code path relies on it — and with persistence disabled there is no shared volume at all.
   The raw `.eml` and the body markdown follow the same rule — DB columns, never disk paths.
2. **Message hash = idempotency key.** `sha256(raw .eml bytes)` — the same primitive as
   `Job.content_sha256`, lifted to message level. Replaying a known message returns the
   existing resource (HTTP 200), nothing is reprocessed. This is what makes sender-side
   retry loops (gateway outbox, n8n retry-on-fail) trivially safe.
3. **Attachments become ordinary Jobs.** Same table, same Celery `process_job` task, same
   status lifecycle, same UI — linked to their message via a new `Job.mail_message_id` FK,
   mirroring how Confluence-import jobs carry `import_run_id`.
4. **Non-browser callers just work.** Bearer `pd_` tokens resolve via the existing
   `get_current_user`; the CSRF `origin_guard` only fires when an `Origin` header is present,
   which server-to-server clients never send.

## Data model (migration `0009_mail_ingestion`)

New table `mail_messages`:

| Column | Type | Notes |
|---|---|---|
| `id` | str(36) PK | uuid4, like `jobs.id` |
| `owner_id` | FK → users.id, SET NULL, nullable, indexed | the ingesting token's user; same visibility rules as Jobs (own + team + admin) |
| `content_sha256` | str(64), indexed | sha256 hex over the raw `.eml` bytes — **the dedup key** |
| `rfc_message_id` | str(998) nullable, indexed | parsed `Message-ID` header; lookup convenience, **not** the dedup key (spoofable, not always present) |
| `subject` | str(998), default '' | decoded header |
| `from_address` | str(998), default '' | decoded `From` |
| `recipients` | JSON | `{"to": [...], "cc": [...]}` decoded address lists |
| `sent_at` | DateTime(tz) nullable | parsed `Date` header |
| `source` | str(64), default '' | free-form client label (`mail-gateway`, `n8n`, …), from query param |
| `raw_content` | LargeBinary NOT NULL | the original `.eml`, verbatim — served by the raw-download endpoint |
| `raw_size_bytes` | int | |
| `body_format` | str(32) nullable | `text/plain` \| `text/html` — which MIME part the body markdown came from |
| `body_markdown` | Text nullable | rendered body incl. YAML frontmatter (see below) |
| `parts` | JSON | per-part manifest in MIME-tree walk order: `[{index, filename, content_type, size_bytes, outcome: 'job'\|'inline'\|'skipped', job_id?, skip_reason?}]` |
| `parse_error` | Text nullable | reserved; normal parse failures reject the request instead (see semantics) |
| `created_at` / `updated_at` | DateTime(tz) | |

Constraints: `UniqueConstraint(owner_id, content_sha256)` (dedup scope = owning user;
Postgres/SQLite treat NULLs as distinct, so rows orphaned by user deletion never collide).

Changes to existing tables: `jobs.mail_message_id` — FK → mail_messages.id, SET NULL,
nullable, indexed. Added via `op.batch_alter_table('jobs')` exactly like
`0008_vl_benchmarks` added `benchmark_run_id`.

Migration rules (repo convention, enforced by `backend/tests/test_migrations.py`): file
`backend/alembic/versions/0009_mail_ingestion.py`, `down_revision='0008_vl_benchmarks'`,
sqlite-compatible DDL only (`op.create_table`/`op.batch_alter_table`,
`if_not_exists=True` on create_table/create_index, no native Postgres enums), model class
added to `backend/app/models/models.py` in the same change.

Blob discipline: define `_MAIL_BLOB_DEFER_OPTIONS = (defer(MailMessage.raw_content),)` and
apply it to **every** list/lookup query except the raw-download endpoint — same rule as
`_JOB_BLOB_DEFER_OPTIONS` / `_ARTIFACT_BLOB_DEFER_OPTIONS`.

## Ingestion endpoint

```
POST /api/v1/mail/messages
Authorization: Bearer pd_<token>
Content-Type: message/rfc822        (raw .eml bytes as the request body)
```

Query parameters (all optional): `profile_id` (OCR profile for attachment jobs, default =
server default profile), `folder` / `subfolder` (storage folder for attachment jobs, via the
existing `_sanitize_storage_path`; default `mail`), `tags` (comma-separated, applied to
attachment jobs), `source` (client label).

Also accepted: `multipart/form-data` with a single `file` part containing the `.eml` and the
same fields as form fields — convenience for `curl -F` and n8n's form mode. Same semantics.

### Request handling (backend, synchronous — no OCR happens here)

1. **Stream with a hard cap.** There is no body-size middleware anywhere in the stack
   (uvicorn is started bare; `await request.body()` buffers unboundedly), so the raw-body
   handler MUST read `request.stream()` chunk-wise, accumulating bytes, computing the
   sha256 incrementally, and aborting with 413 once the total exceeds the cap. This is
   genuinely **new code with no in-repo precedent** — nothing reads `request.stream()`
   today; `save_upload` only chunk-reads an `UploadFile` that Starlette has already fully
   spooled. For the same reason the multipart convenience mode *cannot* get a mid-stream
   abort (FastAPI parses and spools the whole body before the handler runs): that branch
   rejects on the declared `Content-Length` up front and re-checks the actual spooled size
   afterwards — a weaker, post-hoc cap, same as uploads have today. New setting
   `max_mail_message_bytes: int` in `core/config.py`, default `max_upload_bytes` (100 MiB),
   env `MAX_MAIL_MESSAGE_BYTES`. (Ops note: the Helm chart sets no `proxy-body-size`
   ingress annotation by default — same documented operator duty as for uploads.)
2. **Dedup check.** Look up `content_sha256` within the caller's *visibility scope* (the
   same own + team + admin filter the retrieval API uses — matching how
   `_find_predecessor_job` scopes upload dedup today). Hit → **200** with the existing
   message resource (full response shape below, `replayed: true`); nothing new is stored.
   The replay is not purely passive: it re-dispatches any of that message's attachment
   jobs still sitting in PENDING — this is the crash recovery, see step 6. A concurrent
   duplicate that trips the unique constraint on commit is caught (IntegrityError →
   rollback → re-fetch → 200). The `UniqueConstraint(owner_id, content_sha256)` is only
   the race backstop within one owner; same-team duplicates (e.g. after rotating the
   ingest token to a different service user) are caught by the scoped lookup, not the
   constraint. Miss → continue, respond **201**.
3. **Parse.** `email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)` —
   stdlib, no new dependency. Unparseable input → **422** `"Unable to parse message"`,
   nothing stored (mirrors the importer's setup-failure-vs-per-item-failure split: a broken
   message is a setup failure; a broken attachment is a per-item skip). Decode headers
   (subject, from, to/cc, date, Message-ID) via the policy API. A missing `Message-ID` is
   fine — `rfc_message_id` stays NULL; identity rests on the hash.
4. **Body → Markdown, no OCR.** Pick `msg.get_body(preferencelist=('html', 'plain'))`.
   HTML → reuse the `confluence_markdown` BeautifulSoup + markdownify pipeline
   (both libraries are already in `backend/requirements.txt`, importable in the backend
   image — this runs in the API process, not the worker). Plain text → taken verbatim.
   Prepend YAML frontmatter (`yaml.safe_dump`, third frontmatter shape alongside the RAG
   and Confluence ones): `source: mail`, `subject`, `from`, `to`, `date`, `message_id`,
   `content_sha256`, `ingested_by` (source label), `ingested_at`. Store in
   `body_markdown`. A body-less or attachment-only message is valid (`body_markdown` NULL).
5. **Part manifest via a full MIME-tree walk — NOT bare `iter_attachments()`.**
   `iter_attachments()` only inspects the immediate children of the top-level part. On a
   `multipart/signed` (S/MIME) message it yields the inner `multipart/mixed` container as
   one unclassifiable pseudo-attachment — the real PDF inside would be silently lost — and
   inline `Content-ID` images are surfaced or hidden depending on the exact nesting. The
   ingest service therefore runs its own deterministic depth-first walk of the MIME tree
   and classifies every *leaf* part:
   - **Body candidates** — the best `text/html`/`text/plain` alternative (chosen once,
     `get_body`-style) becomes the body; not listed in `parts`.
   - **Inline parts** (disposition `inline` with a `Content-ID` — typically signature
     images and logos) — recorded as `{outcome: 'inline'}`: downloadable via the
     part-content endpoint, but **no OCR job by default** (setting `ocr_inline_images`,
     default false, so worker time is not burnt on logos). `cid:` references in the body
     HTML are replaced with `![inline attachment: <filename>]` placeholders before
     markdownify (v1; rewriting them to part-content URLs would require extending
     `MarkdownView`'s allowed URL schemes).
   - **Attachments** — sanitized filename (fallback `part-<index><ext-from-mime>`),
     extension + MIME validated against the existing `ALLOWED_EXTENSIONS` /
     `_EXTENSION_TO_MIME_TYPES` tables via a **non-raising** check: an unsupported or
     oversized (> `max_upload_bytes`) part is recorded as `{outcome: 'skipped',
     skip_reason: 'unsupported_type' | 'too_large'}` and **never fails the request** (a
     mail with one PDF and one `.zip` still ingests the PDF).
   - **Attached messages** (`message/rfc822`, forwarded mail) — v1 does not recurse into
     them: `{outcome: 'skipped', skip_reason: 'nested_message'}`. Their bytes stay
     downloadable; a consumer can re-POST them as a message of their own.
   - **Unclassifiable containers** — recorded as skipped with a distinct reason, never
     silently dropped.

   `parts[].index` is the position in this walk order. The stored manifest is
   authoritative; the part-content endpoint re-runs the same walk and cross-checks
   filename/content_type against the manifest entry before serving.

   Each supported attachment becomes a Job, following the Confluence attachment-child
   pattern: `status=PENDING`, `upload_content=<bytes>`, `content_sha256`,
   `owner_id=current user`, `mail_message_id=<message id>`, tags applied, and
   `processing_info.settings = {mode: 'mail_attachment', profile_id, folder,
   storage_folder: '<sanitized-folder>/<job_id>', mail: {mail_message_id, part_index,
   rfc_message_id}}`. `storage_folder` is **load-bearing, not decoration**: the worker's
   `_resolve_upload_path` / `_resolve_result_path` read exactly that key and fall back to
   `inbox`/`single` when it is absent — which is why the Confluence import children set it
   too. The synthetic `upload_path` is built from it (only its suffix matters — the worker
   rehydrates the bytes from `upload_content`).
   **Version-chain bypass:** `document_version=1`, `previous_job_id=None`, no
   `_find_predecessor_job` lookup — the benchmark path already sets this precedent.
   Rationale: message-hash dedup already blocks true duplicates, and chaining
   `invoice.pdf` from unrelated senders into one document's version history would be wrong.
6. **Commit, then dispatch.** One transaction for the `mail_messages` row + all Job rows
   (a crash cannot half-ingest a message and then reject the retry). After commit, per
   child: `process_job.delay(job_id, profile_id, 'mail_attachment', '', None)` — the
   direct-import + `.delay()` convention every API-side dispatch uses today (`send_task`
   by string name appears only inside `import_tasks.py`, as a circular-import workaround,
   not as an architecture). Crash-window recovery — API pod dies between commit and the
   dispatch loop, stranding PENDING jobs: **the replay path is the backstop.** The sender
   that never received a response retries; the dedup hit (step 2) re-dispatches the
   message's still-PENDING jobs. Belt-and-braces: the detail/export endpoints re-dispatch
   PENDING mail-attachment jobs older than a few minutes when polled. Do NOT hang this on
   `worker_ready` (`requeue_running_jobs_after_restart`) — that fires only when a worker
   pod restarts, which in stable production may be weeks after the API crash, and it only
   touches RUNNING jobs today. There is no Celery beat/cron in this deployment to sweep
   periodically.
7. **Rate limiting:** skip `enforce_rate_limit` for this handler, following the
   collection-bulk-upload precedent — a gateway flushing its outbox from one IP would
   otherwise burn the shared 60/min IP bucket. Auth + size cap + dedup are the guards.

### Response — 201 first ingest, 200 idempotent replay (same shape)

```json
{
  "id": "3f9b3d6c-…",
  "replayed": false,
  "content_sha256": "9c1185a5c5e9fc54612808977ee8f548b2258d31…",
  "rfc_message_id": "<20260815091200.abc@partner.example>",
  "subject": "Quartalsbericht Q3",
  "from_address": "alice@partner.example",
  "recipients": { "to": ["billing@firma.example"], "cc": [] },
  "sent_at": "2026-08-15T09:12:00Z",
  "source": "mail-gateway",
  "raw_size_bytes": 482133,
  "body_format": "text/html",
  "has_body": true,
  "parts": [
    { "index": 0, "filename": "bericht-q3.pdf", "content_type": "application/pdf",
      "size_bytes": 401210, "outcome": "job", "job_id": "a1b2…" },
    { "index": 1, "filename": "logo.png", "content_type": "image/png",
      "size_bytes": 8123, "outcome": "inline" },
    { "index": 2, "filename": "archiv.zip", "content_type": "application/zip",
      "size_bytes": 52110, "outcome": "skipped", "skip_reason": "unsupported_type" }
  ],
  "created_at": "2026-08-15T10:02:11Z"
}
```

Errors: `401` bad/expired token; `413` over `max_mail_message_bytes`; `422` unparseable
message. Uniform short-string `detail`, no internals leaked — house style.

## Retrieval API (what n8n / Bedrock AgentCore consume)

All endpoints: `Depends(get_current_user)`, visibility via a `_visible_mail_filter`
(own + team + admin — the `_visible_job_filter` logic pointed at `mail_messages.owner_id`),
invisible resources → **404**, never 403 (no existence leaks).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/mail/messages` | List. Filters: `q` (subject/from substring), `message_id` (exact `rfc_message_id`), `sha256` (exact `content_sha256`), `source`, `from_date`/`to_date`; `limit` (≤500)/`offset` + real `total` — the `/search` convention. Blob columns deferred; `parts` included so job states are one join away. |
| `GET /api/v1/mail/messages/{id}` | Detail: full envelope, `parts` enriched with each job's current `status`/`error_message`, `has_body`. |
| `GET /api/v1/mail/messages/{id}/body` | Body markdown as `text/plain` (mirrors `GET /jobs/{id}/preview`). 404 if no body. |
| `GET /api/v1/mail/messages/{id}/raw` | The original `.eml`, verbatim. `Content-Type: message/rfc822`, `Content-Disposition: attachment; filename="<id>.eml"`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, max-age=3600` — the `JobArtifact` content-endpoint conventions. This is new surface: no endpoint serves original upload bytes today. |
| `GET /api/v1/mail/messages/{id}/parts/{index}/content` | Original bytes of one part (incl. inline and skipped ones), extracted **on the fly** from `raw_content` by re-running the same deterministic tree walk — no double storage. The result is cross-checked against the stored `parts` manifest (filename/content_type) before serving. Visibility-checked; index out of range or manifest mismatch → 404. Same download headers as `/raw`. |
| `GET /api/v1/mail/messages/{id}/export.json` | **The aggregation consumers want.** Schema `paddledoc.mail-export/1`, see below. |
| `DELETE /api/v1/mail/messages/{id}?delete_jobs=bool` | Remove a message; optionally its attachment jobs. Without `delete_jobs`, issue an explicit `UPDATE jobs SET mail_message_id = NULL WHERE mail_message_id = :id` before deleting — do **not** rely on the FK's SET NULL: SQLite (dev + the whole test suite) runs without `PRAGMA foreign_keys`, so the cascade is inert there. `delete_import_run` does exactly this, for exactly this reason; also mirror its bulk blob-row deletion without loading blobs. |

### `export.json` (`paddledoc.mail-export/1`)

```json
{
  "schema": "paddledoc.mail-export/1",
  "message": { "id", "content_sha256", "rfc_message_id", "subject", "from_address",
               "recipients", "sent_at", "source", "created_at" },
  "body": { "format": "text/html", "markdown": "…full body markdown…" },
  "attachments": [
    { "index": 0, "filename": "bericht-q3.pdf", "content_type": "application/pdf",
      "size_bytes": 401210, "outcome": "job", "job_id": "a1b2…",
      "job_status": "FINISHED", "content_sha256": "…",
      "markdown": "…full OCR markdown (result_markdown, DB-first)…" },
    { "index": 1, "filename": "logo.zip", "outcome": "skipped",
      "skip_reason": "unsupported_type" }
  ],
  "complete": true
}
```

`complete` is true when every `outcome:'job'` attachment is FINISHED or FAILED (FAILED
attachments carry `"error_message"` instead of `"markdown"`; a message with no job parts —
body-only, or all parts inline/skipped — is trivially `complete`). Consumers may fetch
export.json at any time; polling loop = "GET detail until `complete`, then GET export".
No webhooks in v1 (PaddleDoc has no webhook machinery; polling matches how the UI itself
tracks imports). If a push is ever needed, it belongs in the consumer (n8n Schedule/Poll).

### Consumer walkthroughs

**n8n** — HTTP Request node #1: `POST {base}/api/v1/mail/messages?source=n8n`, auth header
`Bearer pd_…`, body = binary `.eml` (e.g. straight from n8n's IMAP/Email-Trigger node's
raw output), content type `message/rfc822`. Take `id` from the response (200 and 201 are
both success — 200 just means "already known"). Wait/loop on
`GET …/mail/messages/{id}` until `complete`-equivalent (all parts terminal), then
`GET …/{id}/export.json` and feed `body.markdown` + `attachments[*].markdown` onward.

**Bedrock AgentCore** (or any agent framework) — give the agent tool wrappers around
`GET /mail/messages?message_id=…` / `?sha256=…` (resolve a known mail to PaddleDoc's id)
and `GET /mail/messages/{id}/export.json` (fetch the processed content). Read-only tokens
don't exist (a `pd_` token is full-user), so point agents at a dedicated low-privilege
user whose team scope contains only what they may read.

**SMTP gateway / mail relay** — see below.

## UI (frontend)

A first-class **Mail** section — deliberately *not* the low-visibility Imports pattern
(Imports has no sidebar entry; mail messages are a primary browsing surface here):

- `sidebar-nav.tsx`: add `{href: '/mail', label: 'Mail', icon: Mail}` (lucide-react) to the
  `links` array.
- `frontend/src/lib/mail.ts`: types mirroring the response schemas field-for-field +
  status-chip helpers — the `lib/imports.ts` template.
- `app/mail/page.tsx`: list (subject, from, date, source, parts summary, per-message status
  chip), filter bar (`q`, source, date range), pagination against the `total`-returning
  list endpoint.
- `app/mail/[id]/page.tsx`: envelope card; body rendered with the existing sanitized
  `MarkdownView` (it already strips frontmatter); parts table with per-job status chips
  linking to `/jobs/{job_id}`; download buttons for raw `.eml` and each part using the
  **fetch-as-blob** pattern (`apiFetch` → blob → synthesized `<a download>` — the proven
  artifact/folder pattern, not the plain-`<a href>` one). Poll every 2.5 s while any part
  job is PENDING/RUNNING (`imports/[id]` pattern, `useVisiblePolling`/manual tick).
- `document-browser.tsx` / `jobs/[id]/page.tsx`: the `mode === 'import'` sentinel branches
  get a `mode === 'mail_attachment'` case — show a "from mail" chip linking back to
  `/mail/{mail_message_id}`; Restart stays allowed (these are normal `process_job` jobs
  with `upload_content` present, unlike import page jobs).

## Sender side: SMTP gateway / mail relay (context)

The universal endpoint keeps every sender integration thin — a sender never parses MIME:

1. Hook in wherever the full raw RFC-822 message is available in the mail flow (an SMTP
   smarthost's processing pipeline, a milter, a relay hook). The hook must not block mail
   delivery: enqueue `{rawMessage, receivedAt}` into a sender-side outbox and return.
2. An outbox worker filters by configured recipients and POSTs the **raw bytes verbatim**
   to `POST /api/v1/mail/messages?source=<gateway-name>&folder=mail`, with retry +
   exponential backoff. 200 and 201 are both success; only transport errors and 5xx
   retry. Server-side hash dedup makes every retry safe — the sender needs no delivery
   bookkeeping beyond "not yet acknowledged".
3. Config: PaddleDoc base URL + `pd_` token in the sender's environment; HTTPS; token
   rotation documented. No `Origin` header is sent, so the CSRF guard never fires.

Any source that can get at raw RFC-822 bytes (procmail hook, Postfix `smtpd_proxy`, an
n8n IMAP poller, a commercial gateway's extension point) follows the identical contract —
that is the point of the universal design.

## Implementation checklist (PaddleDoc)

- [x] `MailMessage` model + `jobs.mail_message_id` + migration `0009_mail_ingestion`
      (sqlite-compatible, `test_migrations.py` green)
- [x] `backend/app/services/mail_ingest.py`: streaming reader with cap + incremental
      sha256 (new `request.stream()` code, no in-repo precedent); MIME parse; header
      decode; body→markdown (reusing `confluence_markdown` internals, `cid:` placeholder
      rewriting); deterministic MIME-tree walk handling `multipart/signed`, nested
      containers, inline `Content-ID` parts and `message/rfc822`, with non-raising
      validation
- [x] `backend/app/api/mail_routes.py` (`/api/v1/mail` router, registered in `main.py`
      with `get_current_user` + `origin_guard` like the others): ingest + list + detail +
      body + raw + part-content + export.json + delete; `_visible_mail_filter`;
      `_MAIL_BLOB_DEFER_OPTIONS`
- [x] Commit-then-dispatch via `process_job.delay` with mode `mail_attachment` and
      correct `storage_folder`; replay-path + poll-path re-dispatch of stranded PENDING
      mail jobs
- [x] `max_mail_message_bytes` setting + Helm `values.yaml` env passthrough + README
      proxy-body-size note
- [x] Frontend: `lib/mail.ts`, `app/mail/**`, sidebar entry, `mail_attachment` sentinel
      branches (incl. manual `.eml` upload on `/mail`, added after the initial ingestion
      landing)
- [x] Tests: idempotent replay (incl. concurrent-duplicate race and PENDING re-dispatch),
      mixed supported/skipped parts, `multipart/signed` (S/MIME) and nested-multipart
      topologies, inline-image classification (incl. top-level `multipart/related`),
      body-only and attachment-only messages, oversize 413, unparseable 422,
      part-content extraction fidelity, visibility (team/admin/foreign 404), export.json
      completeness states, DELETE leaves no dangling `mail_message_id` on SQLite
- [ ] Tests: charset-odd bodies (non-UTF-8 / mismatched declared charsets) — not yet
      covered; every ingest fixture so far declares `charset="utf-8"`
- [x] Docs: API reference section + n8n example

## Open points (decide during implementation)

- **Charset robustness:** stdlib decoding trusts declared charsets; no chardet/
  charset-normalizer is in the dependency tree. Real-world mail with lying charset headers
  may mis-decode — add `charset-normalizer` as a fallback decoder if that matters day one.
- **Attachment-job queue pressure:** all Celery tasks share the single default queue; a
  mail burst delays interactive OCR jobs equally. If that hurts, introduce `task_routes`
  + a second queue later (nothing exists today).
- **Retention:** no automatic pruning is specified; raw `.eml` blobs accumulate in
  Postgres. If volume becomes real, add a retention setting + pruning job. Moving
  `raw_content` onto the chart's shared
  RWX PVC instead would be cheaper per byte but diverges from the DB-first convention and
  breaks persistence-disabled deployments — retention is the cleaner lever.
- **Forwarded mail (`message/rfc822`):** v1 skips nested messages with a distinct
  `skip_reason`. If ingesting forwarded attachments matters, v2 can recurse one level and
  ingest them as linked child messages — the manifest model already leaves room.
