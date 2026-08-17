"""Confluence-refresh scheduler: a self-re-enqueuing Celery task
(`confluence_refresh_tick`) that periodically starts a fresh crawl run for
every ImportSource with refresh_enabled=true whose interval has elapsed.

There is deliberately no Celery Beat in this deployment, so the recurring
tick is a chain of self-re-enqueued tasks: each execution re-sends itself
via `self.app.send_task(..., countdown=confluence_refresh_tick_seconds)`.
The chain is kicked off by the existing `worker_ready` hook in
app/workers/tasks.py (`celery_app.send_task('confluence_refresh_tick')`, by
name only -- this module is registered with the Celery app the same way
import_tasks.py/openwebui_tasks.py are, via an explicit import at the bottom
of tasks.py).

Singleton discipline (no Beat means nothing else guarantees only one chain
is ever running) is a Redis SET-NX-EX lock, styled after
tasks._try_acquire_recovery_lock -- but held and renewed for the chain's
entire lifetime rather than released after one startup action, since a
short-lived lock would let every later, unrelated worker restart start a
second competing chain even while the first one is perfectly healthy. The
winning token is threaded through the self-re-enqueue chain (the same way
import_confluence threads `chunk_seq`) and renewed once per tick
(_renew_refresh_lock, mirroring import_tasks._commit_owned's lease-guard: a
failed renewal means this execution was superseded, or the lock lapsed with
nobody holding it, and it must stop re-enqueuing itself either way). Every
`confluence_refresh_tick()` call -- both the token-less kickstart from
worker_ready and every self-re-enqueued continuation -- goes through the
same acquire-or-renew check, so a redundant kickstart from a simultaneous
multi-replica startup, or from a worker restarting years into an
already-healthy chain, is a harmless no-op rather than a second chain.

The refresh run itself reuses the existing `import_confluence` chunked crawl
task completely unchanged -- it is enqueued exactly like a user-started run
(see app/api/import_routes.create_import_run), just with
`options['is_refresh'] = True`. ImportRun has no dedicated "is this a
refresh" column (none was added by the schema change this feature builds
on); `options` is already an arbitrary JSON settings-snapshot per the model
docstring, so that is the "existing ImportRun pattern" this flag piggybacks
on instead of a new migration. The per-page diff against ImportPageState
(skip unchanged pages, version-chain changed ones) lives in
app/workers/import_tasks.py, gated on that same flag; the
last_refresh_at/last_refresh_error bookkeeping on ImportSource lives there
too (set when a refresh run reaches a terminal state).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from redis import Redis
from sqlalchemy import select

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.models import ImportRun, ImportRunStatus, ImportSource
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

TICK_TASK_NAME = 'confluence_refresh_tick'

_REFRESH_LOCK_KEY = 'worker:confluence-refresh:tick-lock'
# A generous multiple of the tick interval: must comfortably outlive a
# single tick (including a slow due-sources scan) so a healthy chain's own
# next renewal always lands well before expiry, while still reclaiming a
# genuinely dead chain (holder crashed and redelivery also failed) in
# bounded time rather than never.
_REFRESH_LOCK_TTL_MULTIPLIER = 3
_REFRESH_LOCK_TTL_FLOOR_SECONDS = 60


def _refresh_lock_ttl_seconds() -> int:
    return max(settings.confluence_refresh_tick_seconds * _REFRESH_LOCK_TTL_MULTIPLIER, _REFRESH_LOCK_TTL_FLOOR_SECONDS)


def _try_acquire_refresh_lock() -> str | None:
    """SET NX EX -- same lock idiom as tasks._try_acquire_recovery_lock.
    Returns the winning token, or None if another live chain already holds
    it (in which case the caller must stand down, not start a second
    chain)."""
    token = str(uuid.uuid4())
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    acquired = client.set(_REFRESH_LOCK_KEY, token, nx=True, ex=_refresh_lock_ttl_seconds())
    return token if acquired else None


def _renew_refresh_lock(token: str) -> bool:
    """Per-tick heartbeat for the chain's leadership, mirroring
    import_tasks._commit_owned's lease-guard: only extends the TTL while
    `token` is still the current holder. False means this execution was
    superseded (the lock lapsed and either nobody or a newer chain took
    over) -- the caller must stop re-enqueuing so at most one chain ever
    survives."""
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    if client.get(_REFRESH_LOCK_KEY) != token:
        return False
    client.expire(_REFRESH_LOCK_KEY, _refresh_lock_ttl_seconds())
    return True


def _acquire_or_renew(lock_token: str | None) -> str | None:
    """Returns the token this execution owns the lock with going forward, or
    None when it must stand down (do the dispatch work only if issued a
    token; never re-enqueue without one)."""
    if lock_token is None:
        return _try_acquire_refresh_lock()
    return lock_token if _renew_refresh_lock(lock_token) else None


def _aware_utc(value: datetime) -> datetime:
    # Local copy of app.api.deps._aware_utc's naive-vs-aware sqlite fixup --
    # workers must not import the API module (same reasoning as
    # import_tasks._attach_tags being a local copy of routes._attach_tags).
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _effective_interval_seconds(source: ImportSource) -> int:
    # Same clamp-never-lower-than-the-floor discipline as
    # PATCH /import/sources/{id}'s write path (import_routes.py), applied
    # again here defensively -- refresh_interval_seconds is a plain nullable
    # column an admin could still edit directly, and NULL (never configured
    # by the client) must fall back to the floor rather than being treated
    # as "due immediately"/"never due".
    return max(
        source.refresh_interval_seconds or settings.confluence_refresh_min_interval_seconds,
        settings.confluence_refresh_min_interval_seconds,
    )


def _is_due(source: ImportSource, now: datetime) -> bool:
    if not source.refresh_enabled:
        return False
    if source.last_refresh_at is None:
        return True
    return _aware_utc(source.last_refresh_at) + timedelta(seconds=_effective_interval_seconds(source)) <= now


def _has_active_run(db, source_id: str) -> bool:
    return (
        db.scalar(
            select(ImportRun.id)
            .where(ImportRun.source_id == source_id)
            .where(ImportRun.status.in_([ImportRunStatus.PENDING, ImportRunStatus.RUNNING]))
            .limit(1)
        )
        is not None
    )


def _start_refresh_run(db, source: ImportSource) -> bool:
    """Start a new refresh run, copying the scope + options of the source's
    last successful run. Returns False (no-op) when there is nothing to
    refresh from yet -- a source that has never finished a run has no scope
    to repeat, and refresh_enabled alone does not invent one."""
    last_successful = db.scalar(
        select(ImportRun)
        .where(ImportRun.source_id == source.id)
        .where(ImportRun.status == ImportRunStatus.FINISHED)
        .order_by(ImportRun.created_at.desc())
        .limit(1)
    )
    if last_successful is None:
        return False

    options = dict(last_successful.options) if isinstance(last_successful.options, dict) else {}
    # Re-clamped here even though the copied snapshot was already clamped at
    # its own creation time: settings (e.g. import_max_pages) may have
    # changed since -- same clamp-never-raise discipline as
    # import_routes.create_import_run / import_tasks.import_confluence.
    options['max_pages'] = min(options.get('max_pages') or settings.import_max_pages, settings.import_max_pages)
    raw_max_depth = options.get('max_depth')
    options['max_depth'] = (
        settings.import_max_depth if raw_max_depth is None else min(int(raw_max_depth), settings.import_max_depth)
    )
    # The "existing ImportRun pattern" flag AUFGABE 1 asks for -- options is
    # already an arbitrary settings-snapshot JSON dict (see the model
    # docstring), so a refresh run is marked here rather than via a new
    # column/migration. import_tasks.py's per-page diff logic and the
    # last_refresh_at/last_refresh_error bookkeeping both key off this.
    options['is_refresh'] = True

    frontier = [[last_successful.scope_value, 0]] if last_successful.scope_type == 'page' else []
    run = ImportRun(
        source_id=source.id,
        owner_id=source.owner_id,
        kind=last_successful.kind,
        scope_type=last_successful.scope_type,
        scope_value=last_successful.scope_value,
        options=options,
        state={'frontier': frontier, 'visited': {}, 'errors': []},
    )
    db.add(run)
    db.commit()
    # Enqueued by name only, exactly like import_routes.create_import_run --
    # this module never imports the worker task module.
    celery_app.send_task('import_confluence', args=[run.id, 0])
    logger.info('confluence refresh: started run %s for source %s', run.id, source.id)
    return True


def _dispatch_due_refreshes() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        sources = db.scalars(select(ImportSource).where(ImportSource.refresh_enabled.is_(True))).all()
        for source in sources:
            if not _is_due(source, now):
                continue
            if _has_active_run(db, source.id):
                continue  # Doppelstart-Schutz: an earlier run for this source is still in flight.
            try:
                _start_refresh_run(db, source)
            except Exception:
                logger.exception('confluence refresh: failed to start a run for source %s', source.id)
                db.rollback()
    finally:
        db.close()


@celery_app.task(name=TICK_TASK_NAME, bind=True, acks_late=True, reject_on_worker_lost=True)
def confluence_refresh_tick(self, lock_token: str | None = None) -> None:
    """One scan-and-dispatch cycle, then self-re-enqueue -- see the module
    docstring for the singleton design. `lock_token=None` is the sentinel
    the worker_ready kickstart uses (it doesn't hold the lock yet, unlike
    every self-re-enqueued continuation, which always passes its held
    token); acks_late+reject_on_worker_lost means a hard worker kill mid-tick
    gets redelivered, which -- combined with the lock still being valid --
    is exactly what keeps the chain alive across that crash instead of
    silently dying with it.
    """
    try:
        token = _acquire_or_renew(lock_token)
    except Exception:
        # A Redis error here (unlike a clean None return) is transient, not
        # a legitimate loss of leadership -- re-enqueue with the SAME token
        # this execution was called with (never a freshly acquired one, since
        # acquire/renew itself is what just failed) so the next tick simply
        # retries instead of the whole self-re-enqueuing chain silently
        # dying on one Redis blip.
        logger.exception('confluence refresh: acquire/renew of the leadership lock failed; retrying next tick')
        self.app.send_task(TICK_TASK_NAME, args=[lock_token], countdown=settings.confluence_refresh_tick_seconds)
        return
    if token is None:
        logger.info('confluence refresh: leadership lock unavailable (already held elsewhere, or lease lost); standing down')
        return
    try:
        _dispatch_due_refreshes()
    except Exception:
        logger.exception('confluence refresh tick failed')
    finally:
        self.app.send_task(TICK_TASK_NAME, args=[token], countdown=settings.confluence_refresh_tick_seconds)
