"""Outbound webhook API surface: connections (owner-private, write-only
signing secret -- same shape as app/api/openwebui_routes.py's
OpenWebUIConnection) and deliveries (one row per delivery attempt, processed
by the `deliver_webhook` Celery task, enqueued here by name only -- this
module never imports the worker task module, mirroring
openwebui_routes.py's PUSH_TASK_NAME convention).

Registered in app/main.py under the same get_current_user + origin_guard
dependencies as the main job router. The WEBHOOKS_ENABLED kill-switch below
mirrors openwebui_routes._require_openwebui_enabled exactly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.routes import _require_visible
from app.core.config import settings
from app.database.session import get_db
from app.models.models import Job, JobStatus, User, WebhookConnection, WebhookDelivery
from app.schemas.webhooks import (
    WebhookConnectionCreateRequest,
    WebhookConnectionListResponse,
    WebhookConnectionResponse,
    WebhookConnectionTestResponse,
    WebhookConnectionUpdateRequest,
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookSendRequest,
)
# Module-object access (security.encrypt_webhook_secret /
# security.decrypt_webhook_secret) rather than from-imports: matches
# openwebui_routes.py's late-binding convention, keeping the helpers
# monkeypatchable in tests.
from app.services import security
# Imported by name (not `from ... import send_webhook_request as _send`) so
# tests can patch `app.api.webhook_routes.send_webhook_request` directly,
# same pattern as openwebui_routes patching test_connection/list_knowledge.
from app.services.webhooks import send_webhook_request
from app.services.security import enforce_rate_limit
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Exact Celery task name contract (see app/workers/webhook_tasks.py);
# enqueued via celery_app.send_task so the API process never imports the
# worker module.
DELIVER_TASK_NAME = 'deliver_webhook'

_TEST_COOLDOWN_KEY_PREFIX = 'webhook-test-cooldown:'


def _require_webhooks_enabled() -> None:
    # Kill-switch: with WEBHOOKS_ENABLED=false the whole /webhooks surface
    # 404s as if the feature does not exist -- mirrors
    # openwebui_routes._require_openwebui_enabled / import_routes._require_import_enabled.
    if not settings.webhooks_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')


router = APIRouter(prefix='/api/v1/webhooks', dependencies=[Depends(_require_webhooks_enabled)])


# --- Shared helpers -----------------------------------------------------------

def _validate_webhook_url(raw: str) -> str:
    """Scheme must be http/https, host present, no embedded credentials.

    Unlike import_routes._normalize_base_url / openwebui_routes._normalize_base_url,
    the path/query/fragment are left untouched and the value is returned
    verbatim (just stripped) -- a webhook URL commonly carries a meaningful
    path and/or a query-string token of its own (n8n's /webhook/<id>,
    Slack-style incoming-webhook paths, ...), so reshaping it would corrupt
    the address rather than merely normalize it.
    """
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https'):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='url must use http or https')
    if not parts.hostname:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='url must include a host')
    if parts.username or parts.password:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='url must not embed credentials')
    return value


def _dedupe_events(events: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for event in events:
        if event not in seen:
            seen.add(event)
            result.append(event)
    return result


def _get_owned_connection(db: Session, connection_id: str, user: User) -> WebhookConnection:
    """Connections are strictly owner-private (a signing secret is a
    personal credential for the receiving endpoint), mirroring
    openwebui_routes._get_owned_connection: any non-owner -- including
    admins -- gets a 404, never a 403, so cross-user connection ids are
    unprobeable."""
    connection = db.get(WebhookConnection, connection_id)
    if connection is None or connection.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Webhook connection not found')
    return connection


def _connection_to_response(connection: WebhookConnection) -> WebhookConnectionResponse:
    return WebhookConnectionResponse(
        id=connection.id,
        name=connection.name,
        url=connection.url,
        enabled=connection.enabled,
        events=list(connection.events or []),
        has_secret=connection.secret_encrypted is not None,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _check_test_cooldown(connection_id: str) -> None:
    """Redis-backed cooldown between test probes (429 + Retry-After inside
    the window), keyed per connection_id -- Redis rather than a DB column
    like ImportSource.last_test_at, since WebhookConnection carries no
    last-tested timestamp field. Reuses security._rate_limit_redis()'s
    process-wide client rather than opening a second connection pool to the
    same Redis instance; module-object access (not a from-import) keeps it
    swappable the same way tests already swap in a fake for the rate
    limiter (see tests/conftest.py). SET NX EX claims the slot atomically;
    fails open (like RedisRateLimiter) if Redis itself is unavailable."""
    key = f'{_TEST_COOLDOWN_KEY_PREFIX}{connection_id}'
    ttl_seconds = settings.webhook_test_cooldown_seconds
    client = security._rate_limit_redis()
    try:
        acquired = bool(client.set(key, '1', nx=True, ex=ttl_seconds))
    except redis_lib.RedisError:
        logger.warning('webhook test cooldown: Redis unavailable, failing open', exc_info=True)
        return
    if acquired:
        return
    try:
        retry_after = client.ttl(key) or 0
    except redis_lib.RedisError:
        retry_after = 0
    if retry_after <= 0:
        retry_after = ttl_seconds
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail='Connection was tested too recently; wait a few seconds and try again',
        headers={'Retry-After': str(retry_after)},
    )


def _delivery_to_response(delivery: WebhookDelivery) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse.model_validate(delivery)


# --- Connections ----------------------------------------------------------

@router.post('/connections', response_model=WebhookConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_webhook_connection(
    request: Request,
    payload: WebhookConnectionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookConnectionResponse:
    enforce_rate_limit(request)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='name is required')

    connection = WebhookConnection(
        owner_id=user.id,
        name=name,
        url=_validate_webhook_url(payload.url),
        events=_dedupe_events(list(payload.events)),
        enabled=payload.enabled,
        # Write-only from here on: encrypted at rest, never logged, never in
        # any response schema. Genuinely optional -- unlike OpenWebUI's
        # api_key, a connection may have no secret at all.
        secret_encrypted=security.encrypt_webhook_secret(payload.secret) if payload.secret else None,
    )
    db.add(connection)
    db.commit()
    return _connection_to_response(connection)


@router.get('/connections', response_model=WebhookConnectionListResponse)
def list_webhook_connections(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> WebhookConnectionListResponse:
    connections = db.scalars(
        select(WebhookConnection)
        .where(WebhookConnection.owner_id == user.id)
        .order_by(WebhookConnection.created_at.desc())
    ).all()
    return WebhookConnectionListResponse(items=[_connection_to_response(c) for c in connections])


@router.patch('/connections/{connection_id}', response_model=WebhookConnectionResponse)
def update_webhook_connection(
    connection_id: str,
    payload: WebhookConnectionUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookConnectionResponse:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, connection_id, user)

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='name cannot be empty')
        connection.name = name
    if payload.url is not None:
        connection.url = _validate_webhook_url(payload.url)
    if payload.events is not None:
        connection.events = _dedupe_events(list(payload.events))
    if payload.enabled is not None:
        connection.enabled = payload.enabled

    # Tri-state write-only update -- see WebhookConnectionUpdateRequest.secret's
    # docstring: omitted entirely keeps the stored secret; present-but-empty
    # clears it; present-and-non-empty rotates it.
    if 'secret' in payload.model_fields_set:
        if payload.secret:
            connection.secret_encrypted = security.encrypt_webhook_secret(payload.secret)
        else:
            connection.secret_encrypted = None

    db.commit()
    return _connection_to_response(connection)


@router.delete('/connections/{connection_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook_connection(
    connection_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, connection_id, user)
    # Deliveries keep their history: WebhookDelivery.connection_id is SET
    # NULL, and connection_name was already snapshotted at delivery-creation
    # time (see app/models/models.py's WebhookConnection docstring).
    db.delete(connection)
    db.commit()


@router.post('/connections/{connection_id}/test', response_model=WebhookConnectionTestResponse)
def test_webhook_connection(
    connection_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookConnectionTestResponse:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, connection_id, user)
    _check_test_cooldown(connection.id)

    secret: str | None = None
    if connection.secret_encrypted:
        try:
            secret = security.decrypt_webhook_secret(connection.secret_encrypted)
        except ValueError:
            return WebhookConnectionTestResponse(
                ok=False,
                detail='Stored secret could not be decrypted (SECRET_KEY changed?); the secret must be re-entered',
            )

    payload = {'event': 'test', 'timestamp': datetime.now(timezone.utc).isoformat()}
    http_status_code, error_message = send_webhook_request(
        connection.url, payload, secret,
        allowed_private_hosts=frozenset(settings.webhook_private_host_allowlist),
    )
    if error_message is not None:
        # error_message never carries headers or the secret -- see
        # app/services/webhooks.py's send_webhook_request docstring.
        return WebhookConnectionTestResponse(ok=False, detail=error_message, http_status=http_status_code or None)
    return WebhookConnectionTestResponse(ok=True, detail='Delivered', http_status=http_status_code)


# --- Deliveries -----------------------------------------------------------

@router.get('/deliveries', response_model=WebhookDeliveryListResponse)
def list_webhook_deliveries(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookDeliveryListResponse:
    # Own deliveries only, not team-wide -- a delivery is tied to the user
    # who owns the connection (or triggered the manual send), not to job
    # visibility, so there is no equivalent of openwebui_routes' job_id-scoped
    # "own + team + admin" branch here.
    deliveries = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.owner_id == user.id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    ).all()
    return WebhookDeliveryListResponse(items=[_delivery_to_response(d) for d in deliveries])


@router.post('/send', response_model=WebhookDeliveryResponse, status_code=status.HTTP_201_CREATED)
def send_webhook(
    payload: WebhookSendRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> WebhookDeliveryResponse:
    """Manual, single-job resend: creates a WebhookDelivery row and enqueues
    `deliver_webhook` for the worker to actually build the payload (event
    'job.finished' -- only a FINISHED job can be manually sent) and call
    app/services/webhooks.send_webhook_request. Mirrors
    openwebui_routes.create_openwebui_pushes's dispatch shape, but for a
    single (connection, job) pair rather than a batch."""
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, payload.connection_id, user)
    if not connection.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Webhook connection is disabled')

    job = db.get(Job, payload.job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
    _require_visible(db, job, user)
    if job.status != JobStatus.FINISHED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Job is not finished')

    # Server-side cap: an unreachable/very slow receiving endpoint must not
    # let one user queue unbounded outbound work (mirrors
    # openwebui_routes.create_openwebui_pushes's pending-count cap).
    pending_count = db.scalar(
        select(func.count()).select_from(WebhookDelivery).where(
            WebhookDelivery.owner_id == user.id,
            WebhookDelivery.status == 'pending',
        )
    ) or 0
    if pending_count + 1 > settings.webhook_max_pending_deliveries_per_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Too many pending webhook deliveries; wait for existing deliveries to finish before starting more',
        )

    delivery = WebhookDelivery(
        connection_id=connection.id,
        connection_name=connection.name,
        owner_id=user.id,
        event='job.finished',
        job_id=job.id,
        status='pending',
    )
    db.add(delivery)
    db.commit()

    celery_app.send_task(DELIVER_TASK_NAME, args=[delivery.id])
    return _delivery_to_response(delivery)
