"""OpenWebUI push API surface: connections (owner-private, write-only API
key -- same shape as app/api/import_routes.py's ImportSource) and pushes
(one row per job-push attempt, processed by the `push_openwebui` Celery
task, enqueued here by name only -- this module never imports the worker
task module, mirroring import_routes.py's IMPORT_TASK_NAME convention).

Registered in app/main.py under the same get_current_user + origin_guard
dependencies as the main job router. The OPENWEBUI_ENABLED kill-switch below
mirrors import_routes._require_import_enabled exactly.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.routes import _owner_visible, _require_visible, _resolve_markdown_content
from app.core.config import settings
from app.database.session import get_db
from app.models.models import Job, JobStatus, OpenWebUIConnection, OpenWebUIPush, User
from app.schemas.openwebui import (
    OpenWebUIConnectionCreateRequest,
    OpenWebUIConnectionListResponse,
    OpenWebUIConnectionResponse,
    OpenWebUIConnectionTestResponse,
    OpenWebUIConnectionUpdateRequest,
    OpenWebUIKnowledgeItem,
    OpenWebUIKnowledgeListResponse,
    OpenWebUIPushCreateRequest,
    OpenWebUIPushListResponse,
    OpenWebUIPushResponse,
)
# Module-object access (security.encrypt_openwebui_api_key /
# security.decrypt_openwebui_api_key) rather than from-imports: matches
# import_routes.py's late-binding convention, keeping the helpers
# monkeypatchable in tests.
from app.services import security
from app.services.openwebui import OpenWebUIError, list_knowledge, test_connection
from app.services.security import enforce_rate_limit
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Exact Celery task name contract (see app/workers/openwebui_tasks.py);
# enqueued via celery_app.send_task so the API process never imports the
# worker module.
PUSH_TASK_NAME = 'push_openwebui'

_TEST_COOLDOWN_KEY_PREFIX = 'openwebui-test-cooldown:'
# Second cooldown key, keyed by user rather than connection_id -- a
# connection-only cooldown is trivially bypassed by creating a fresh
# throwaway connection for every probe (each gets its own connection_id, so
# each starts with a clean cooldown window), turning the test endpoint into
# an unrate-limited reachability oracle. Keyed on the same TTL and checked
# alongside the connection key (see _check_test_cooldown).
_TEST_COOLDOWN_USER_KEY_PREFIX = 'openwebui-test-cooldown-user:'


def _require_openwebui_enabled() -> None:
    # Kill-switch: with OPENWEBUI_ENABLED=false the whole /openwebui surface
    # 404s as if the feature does not exist -- mirrors _require_import_enabled.
    if not settings.openwebui_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Not found')


router = APIRouter(prefix='/api/v1/openwebui', dependencies=[Depends(_require_openwebui_enabled)])


# --- Shared helpers -----------------------------------------------------------

def _normalize_base_url(raw: str) -> str:
    """Same shape rules as import_routes._normalize_base_url /
    auth._normalize_vl_base_url (scheme must be http/https, host present, no
    embedded credentials, no query/fragment, trailing slash stripped) --
    duplicated locally rather than imported, to avoid a cross-feature import
    into this otherwise unrelated router module (same reasoning as
    auth._normalize_vl_base_url)."""
    value = raw.strip()
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https'):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must use http or https')
    if not parts.hostname:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must include a host')
    if parts.username or parts.password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must not embed credentials'
        )
    if parts.query or parts.fragment:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='base_url must not contain a query or fragment'
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _get_owned_connection(db: Session, connection_id: str, user: User) -> OpenWebUIConnection:
    """Connections are strictly owner-private (an API key is a personal
    OpenWebUI credential), mirroring import_routes._get_owned_source: any
    non-owner -- including admins -- gets a 404, never a 403, so cross-user
    connection ids are unprobeable."""
    connection = db.get(OpenWebUIConnection, connection_id)
    if connection is None or connection.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='OpenWebUI connection not found')
    return connection


def _connection_to_response(connection: OpenWebUIConnection) -> OpenWebUIConnectionResponse:
    return OpenWebUIConnectionResponse.model_validate(connection)


def _check_test_cooldown(connection_id: str, user_id: str) -> None:
    """Redis-backed cooldown between test probes (429 + Retry-After inside
    the window) -- Redis rather than a DB column like ImportSource.
    last_test_at, since OpenWebUIConnection carries no last-tested timestamp
    field. Reuses security._rate_limit_redis()'s process-wide client rather
    than opening a second connection pool to the same Redis instance;
    module-object access (not a from-import) keeps it swappable the same way
    tests already swap in a fake for the rate limiter (see tests/conftest.py).

    Two independent keys are checked -- one per connection_id, one per
    user_id -- and either already being held blocks the probe: a
    connection-only cooldown is bypassable by creating a fresh throwaway
    connection for every probe (a new connection_id starts with a clean
    cooldown window every time), turning this endpoint into an
    unrate-limited internal-network reachability oracle. The per-user key
    closes that. SET NX EX claims each slot atomically; fails open (like
    RedisRateLimiter) if Redis itself is unavailable."""
    connection_key = f'{_TEST_COOLDOWN_KEY_PREFIX}{connection_id}'
    user_key = f'{_TEST_COOLDOWN_USER_KEY_PREFIX}{user_id}'
    ttl_seconds = settings.openwebui_test_cooldown_seconds
    client = security._rate_limit_redis()
    try:
        # Short-circuiting `and`: if the connection slot is already held,
        # the user slot is left untouched rather than being consumed by a
        # probe that was going to be rejected anyway.
        acquired = bool(client.set(connection_key, '1', nx=True, ex=ttl_seconds)) and bool(
            client.set(user_key, '1', nx=True, ex=ttl_seconds)
        )
    except redis_lib.RedisError:
        logger.warning('openwebui test cooldown: Redis unavailable, failing open', exc_info=True)
        return
    if acquired:
        return
    try:
        retry_after = max(client.ttl(connection_key) or 0, client.ttl(user_key) or 0)
    except redis_lib.RedisError:
        retry_after = 0
    if retry_after <= 0:
        retry_after = ttl_seconds
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail='Connection was tested too recently; wait a few seconds and try again',
        headers={'Retry-After': str(retry_after)},
    )


def _push_to_response(db: Session, push: OpenWebUIPush) -> OpenWebUIPushResponse:
    # content_stale is derived, never stored: compare what was actually
    # pushed against the job's CURRENT markdown at read time (see
    # OpenWebUIPush's docstring in app/models/models.py). Only meaningful
    # once a push has actually finished -- pending/running/failed pushes
    # have no pushed_content_sha256 yet and read as not-stale.
    content_stale = False
    if push.pushed_content_sha256:
        job = db.get(Job, push.job_id)
        content = _resolve_markdown_content(job) if job is not None else None
        if content is not None:
            content_stale = hashlib.sha256(content.encode('utf-8')).hexdigest() != push.pushed_content_sha256
    return OpenWebUIPushResponse(
        id=push.id,
        job_id=push.job_id,
        connection_id=push.connection_id,
        connection_name=push.connection_name,
        knowledge_id=push.knowledge_id,
        knowledge_name=push.knowledge_name,
        status=push.status,
        error_message=push.error_message,
        openwebui_file_id=push.openwebui_file_id,
        content_stale=content_stale,
        created_at=push.created_at,
        updated_at=push.updated_at,
    )


_INELIGIBLE_DETAIL = {
    'not_found': 'Job not found',
    'not_ready': 'Job is not finished, or has no markdown content to push',
}


def _job_for_push(db: Session, job_id: str, user: User) -> tuple[Job | None, str | None]:
    """Eligibility check for one requested job_id: visible to the caller,
    FINISHED, and has resolvable markdown. Returns (job, None) when
    eligible; (None, 'not_found') for a missing/invisible job (mirrors
    routes._require_visible's 404-not-403 discipline); (job, 'not_ready')
    for a visible job that isn't push-eligible yet."""
    job = db.get(Job, job_id)
    if job is None or not _owner_visible(db, job.owner_id, user):
        return None, 'not_found'
    if job.status != JobStatus.FINISHED:
        return job, 'not_ready'
    if not _resolve_markdown_content(job):
        return job, 'not_ready'
    return job, None


# --- Connections ----------------------------------------------------------

@router.post('/connections', response_model=OpenWebUIConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_openwebui_connection(
    request: Request,
    payload: OpenWebUIConnectionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OpenWebUIConnectionResponse:
    enforce_rate_limit(request)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='name is required')

    connection = OpenWebUIConnection(
        owner_id=user.id,
        name=name,
        base_url=_normalize_base_url(payload.base_url),
        # Write-only from here on: encrypted at rest, never logged, never in
        # any response schema.
        api_key_encrypted=security.encrypt_openwebui_api_key(payload.api_key),
    )
    db.add(connection)
    db.commit()
    return _connection_to_response(connection)


@router.get('/connections', response_model=OpenWebUIConnectionListResponse)
def list_openwebui_connections(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> OpenWebUIConnectionListResponse:
    connections = db.scalars(
        select(OpenWebUIConnection)
        .where(OpenWebUIConnection.owner_id == user.id)
        .order_by(OpenWebUIConnection.created_at.desc())
    ).all()
    return OpenWebUIConnectionListResponse(items=[_connection_to_response(c) for c in connections])


@router.patch('/connections/{connection_id}', response_model=OpenWebUIConnectionResponse)
def update_openwebui_connection(
    connection_id: str,
    payload: OpenWebUIConnectionUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OpenWebUIConnectionResponse:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, connection_id, user)

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='name cannot be empty')
        connection.name = name
    if payload.base_url is not None:
        connection.base_url = _normalize_base_url(payload.base_url)
    # Write-only update contract: omitted or empty api_key keeps the stored one.
    if payload.api_key:
        connection.api_key_encrypted = security.encrypt_openwebui_api_key(payload.api_key)

    db.commit()
    return _connection_to_response(connection)


@router.delete('/connections/{connection_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_openwebui_connection(
    connection_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, connection_id, user)
    # Pushes keep their history: OpenWebUIPush.connection_id is SET NULL, and
    # connection_name was already snapshotted at push time (see
    # app/models/models.py's OpenWebUIConnection docstring).
    db.delete(connection)
    db.commit()


@router.post('/connections/{connection_id}/test', response_model=OpenWebUIConnectionTestResponse)
def test_openwebui_connection(
    connection_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OpenWebUIConnectionTestResponse:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, connection_id, user)
    _check_test_cooldown(connection.id, user.id)

    try:
        api_key = security.decrypt_openwebui_api_key(connection.api_key_encrypted)
    except ValueError:
        return OpenWebUIConnectionTestResponse(
            ok=False,
            detail='Stored API key could not be decrypted (SECRET_KEY changed?); the key must be re-entered',
        )

    try:
        test_connection(
            connection.base_url, api_key,
            allowed_private_hosts=frozenset(settings.openwebui_private_host_allowlist),
        )
    except OpenWebUIError as exc:
        # OpenWebUIError messages carry URLs/statuses, never headers or the
        # API key.
        return OpenWebUIConnectionTestResponse(ok=False, detail=str(exc))
    return OpenWebUIConnectionTestResponse(ok=True, detail='Connected')


@router.get('/connections/{connection_id}/knowledge', response_model=OpenWebUIKnowledgeListResponse)
def get_openwebui_connection_knowledge(
    connection_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> OpenWebUIKnowledgeListResponse:
    connection = _get_owned_connection(db, connection_id, user)
    try:
        api_key = security.decrypt_openwebui_api_key(connection.api_key_encrypted)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Stored API key could not be decrypted (SECRET_KEY changed?); the key must be re-entered',
        ) from exc

    try:
        items = list_knowledge(
            connection.base_url, api_key,
            allowed_private_hosts=frozenset(settings.openwebui_private_host_allowlist),
        )
    except OpenWebUIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return OpenWebUIKnowledgeListResponse(items=[OpenWebUIKnowledgeItem(**item) for item in items])


# --- Pushes -------------------------------------------------------------------

@router.post('/pushes', response_model=OpenWebUIPushListResponse, status_code=status.HTTP_201_CREATED)
def create_openwebui_pushes(
    payload: OpenWebUIPushCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OpenWebUIPushListResponse:
    enforce_rate_limit(request)
    connection = _get_owned_connection(db, payload.connection_id, user)

    knowledge_id = payload.knowledge_id.strip()
    knowledge_name = payload.knowledge_name.strip()
    if not knowledge_id or not knowledge_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='knowledge_id/knowledge_name are required')

    now = datetime.now(timezone.utc)
    seen_ids: set[str] = set()
    # (job_id, job|None, ineligibility reason|None), in request order --
    # preserved through to the response so the caller can zip it back
    # against the job_ids it sent.
    plan: list[tuple[str, Job | None, str | None]] = []
    for job_id in payload.job_ids:
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        job, reason = _job_for_push(db, job_id, user)
        plan.append((job_id, job, reason))

    eligible = [(job_id, job) for job_id, job, reason in plan if reason is None]
    if not eligible:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No requested job is eligible for an OpenWebUI push (not found, not finished, or has no markdown content)',
        )

    # Server-side cap: a wedged/very active OpenWebUI instance must not let
    # one user queue unbounded outbound work (mirrors
    # import_routes.create_import_run's active-run cap in spirit, though
    # without that endpoint's TOCTOU re-check -- pushes are cheap/frequent
    # rather than a single long-lived exclusive slot, so a plain count here
    # is an acceptable, much lower-stakes race).
    pending_count = db.scalar(
        select(func.count()).select_from(OpenWebUIPush).where(
            OpenWebUIPush.owner_id == user.id,
            OpenWebUIPush.status.in_(['pending', 'running']),
        )
    ) or 0
    if pending_count + len(eligible) > settings.openwebui_push_max_pending_per_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Too many pending OpenWebUI pushes; wait for existing pushes to finish before starting more',
        )

    created_by_job_id: dict[str, OpenWebUIPush] = {}
    for job_id, _job in eligible:
        push = OpenWebUIPush(
            connection_id=connection.id,
            connection_name=connection.name,
            job_id=job_id,
            knowledge_id=knowledge_id,
            knowledge_name=knowledge_name,
            status='pending',
            owner_id=user.id,
        )
        db.add(push)
        created_by_job_id[job_id] = push
    db.commit()

    for push in created_by_job_id.values():
        celery_app.send_task(PUSH_TASK_NAME, args=[push.id])

    items: list[OpenWebUIPushResponse] = []
    for job_id, _job, reason in plan:
        if reason is None:
            items.append(_push_to_response(db, created_by_job_id[job_id]))
        else:
            # Not persisted -- there is no Job row an invalid job_id could
            # legally hang a Push row's CASCADE FK off of, so this entry
            # exists only in the response, never in openwebui_pushes.
            items.append(
                OpenWebUIPushResponse(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    connection_id=connection.id,
                    connection_name=connection.name,
                    knowledge_id=knowledge_id,
                    knowledge_name=knowledge_name,
                    status='failed',
                    error_message=_INELIGIBLE_DETAIL[reason],
                    openwebui_file_id=None,
                    content_stale=False,
                    created_at=now,
                    updated_at=now,
                )
            )
    return OpenWebUIPushListResponse(items=items)


@router.get('/pushes', response_model=OpenWebUIPushListResponse)
def list_openwebui_pushes(
    job_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OpenWebUIPushListResponse:
    if job_id is not None:
        # Visibility follows the JOB (own + team + admin, via
        # routes._require_visible) -- multiple teammates may have pushed the
        # same job to different knowledge bases, and all of that history
        # should be visible to anyone who can see the job.
        job = db.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found')
        _require_visible(db, job, user)
        query = select(OpenWebUIPush).where(OpenWebUIPush.job_id == job_id)
    else:
        # Without job_id: the caller's OWN recent pushes only, not
        # team-wide -- there is no single job to scope visibility against.
        query = select(OpenWebUIPush).where(OpenWebUIPush.owner_id == user.id)

    pushes = db.scalars(query.order_by(OpenWebUIPush.created_at.desc()).limit(limit)).all()
    return OpenWebUIPushListResponse(items=[_push_to_response(db, push) for push in pushes])
