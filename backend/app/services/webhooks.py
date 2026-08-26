"""Outbound webhook delivery transport.

Kept FastAPI-free (mirrors app/services/openwebui.py's shape) so both
app/api/webhook_routes.py (the synchronous POST .../test probe) and
app/workers/webhook_tasks.py (the `deliver_webhook` Celery task, with its own
retry/backoff loop) call the exact same function for the exact same wire
format -- there must be only one place that builds headers/signs the body.

Every outbound request goes through app.services.safe_fetch.safe_fetch --
the same SSRF protection Confluence/OpenWebUI get (private-IP block with the
admin-managed `allowed_private_hosts` exemption, unconditional cloud-metadata
block, DNS pinning, redirect revalidation). Webhook receivers (e.g. n8n) are
typically self-hosted on a private LAN, exactly like the OpenWebUI case this
mirrors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.config import settings
from app.models.models import Tag, job_tags
from app.services.safe_fetch import SafeFetchError, safe_fetch

if TYPE_CHECKING:
    from app.models.models import ImportRun, Job

_REQUEST_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 64 * 1024
_ERROR_DETAIL_MAX_CHARS = 500


def _sign_body(body: bytes, secret: str) -> str:
    """HMAC-SHA256 over the raw request body, hex-encoded, in the
    'sha256=<hex>' shape sent as X-PaddleDoc-Signature."""
    mac = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return f'sha256={mac}'


def send_webhook_request(
    url: str,
    payload: dict,
    secret: str | None,
    allowed_private_hosts: frozenset[str],
) -> tuple[int, str | None]:
    """POST `payload` as JSON to `url`.

    Always sets 'X-PaddleDoc-Event: <payload['event']>'. When `secret` is
    set, also signs the raw request body and sends it as
    'X-PaddleDoc-Signature: sha256=<hex-hmac-sha256>'.

    Returns (http_status, error_message): error_message is None for any 2xx
    response, otherwise a short human-readable detail (never headers or the
    secret). http_status is 0 for a transport-level failure (SSRF block, DNS
    failure, timeout, connection refused, ...) that never produced a real
    HTTP response, in which case error_message is always set.
    """
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-PaddleDoc-Event': str(payload.get('event', '')),
    }
    if secret:
        headers['X-PaddleDoc-Signature'] = _sign_body(body, secret)

    try:
        response = safe_fetch(
            url,
            method='POST',
            headers=headers,
            body=body,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            max_bytes=_MAX_RESPONSE_BYTES,
            allowed_private_hosts=allowed_private_hosts,
        )
    except SafeFetchError as exc:
        return 0, str(exc)[:_ERROR_DETAIL_MAX_CHARS]

    if 200 <= response.status_code < 300:
        return response.status_code, None

    detail = response.body.decode('utf-8', errors='replace').strip()
    if not detail:
        detail = f'HTTP {response.status_code}'
    return response.status_code, detail[:_ERROR_DETAIL_MAX_CHARS]


def _job_download_url(job_id: str) -> str:
    return f"{settings.public_api_url.rstrip('/')}/api/v1/jobs/{job_id}/download"


def build_job_payload(db, job: 'Job', event: str, include_markdown: bool) -> dict:
    """Build the JSON payload for a 'job.finished'/'job.failed' event, or for
    a manual /webhooks/send (which always builds with event='job.finished'
    -- see app/api/webhook_routes.send_webhook and
    app/workers/webhook_tasks.deliver_webhook).

    `db` is accepted (rather than reading job.tags' lazy-loaded relationship
    implicitly) so the caller's own session is always what runs the tags
    query -- job may be a fresh db.get() result from a short-lived worker
    session, and this keeps the query explicit rather than relying on
    whichever session the ORM instance happens to still be attached to.

    `include_markdown` is the caller's call, not derived from `event` here:
    a manual send is always event='job.finished' with include_markdown=True
    (POST /webhooks/send only accepts a FINISHED job), the automatic
    job.finished hook likewise passes True, and the job.failed hook passes
    False -- see those call sites for the actual event/include_markdown
    pairing. `error_message` is only ever non-null for event='job.failed'.
    """
    tag_names = db.scalars(
        select(Tag.name).join(job_tags, job_tags.c.tag_id == Tag.id).where(job_tags.c.job_id == job.id)
    ).all()

    info = job.processing_info if isinstance(job.processing_info, dict) else {}
    job_settings = info.get('settings') if isinstance(info.get('settings'), dict) else {}
    profile_id = job_settings.get('profile_id') if isinstance(job_settings.get('profile_id'), str) else None
    folder = job_settings.get('folder') if isinstance(job_settings.get('folder'), str) else ''
    subfolder = job_settings.get('subfolder') if isinstance(job_settings.get('subfolder'), str) else ''

    return {
        'event': event,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'job': {
            'id': job.id,
            'filename': job.original_filename,
            'status': job.status.value if hasattr(job.status, 'value') else str(job.status),
            'folder': folder,
            'subfolder': subfolder,
            'tags': sorted(tag_names),
            'profile_id': profile_id,
            'document_version': job.document_version,
            'content_sha256': job.content_sha256,
        },
        'markdown': job.result_markdown if include_markdown else None,
        'error_message': job.error_message if event == 'job.failed' else None,
        'download_url': _job_download_url(job.id),
    }


def build_run_payload(run: 'ImportRun') -> dict:
    """Build the JSON payload for an 'import_run.finished' event."""
    return {
        'event': 'import_run.finished',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'run': {
            'id': run.id,
            'scope_type': run.scope_type,
            'scope_value': run.scope_value,
            'pages_imported': run.pages_imported,
            'pages_failed': run.pages_failed,
        },
    }
