"""Outbound webhook delivery Celery task: sends one WebhookDelivery row's
event payload to its connection's URL, with retry/backoff for
transport-level and 5xx failures.

Registered from app/workers/tasks.py (the `celery -A app.workers.tasks`
entrypoint) via an explicit import, mirroring app/workers/openwebui_tasks.py;
app/api/webhook_routes.py and the completion hooks in app/workers/tasks.py /
app/workers/import_tasks.py enqueue by name only (`deliver_webhook`).

Unlike OpenWebUIPush, WebhookDelivery.status has no 'running' state (just
'pending' | 'sent' | 'failed' -- see app/models/models.py's docstring), so
there is no claim/reclaim dance here: a delivery simply stays 'pending'
across retries (each retry re-sends the same row, bumping `attempts`) until
it lands on a terminal 'sent' or 'failed'. Retries are self-re-enqueues with
a countdown -- the same `self.app.send_task(..., countdown=...)` pattern
app/workers/refresh_tasks.py uses for its tick chain -- rather than Celery's
built-in `self.retry`, since a countdown-based re-send is all a delivery
needs and this keeps the task's own return path linear (no exception-driven
control flow).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.models import Job, WebhookConnection, WebhookDelivery
from app.services import security
from app.services.webhooks import build_job_payload, build_run_payload, send_webhook_request
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

DELIVER_TASK_NAME = 'deliver_webhook'

_ERROR_MESSAGE_MAX_CHARS = 2000
# Total attempts a delivery gets before it is left 'failed' for good
# (attempt 1 is the first send, not a "retry") -- "max ~5 Versuche" per the
# feature contract.
_MAX_ATTEMPTS = 5
# Backoff before each retry, indexed by the attempt number that just failed
# (attempt 1 failed -> wait _BACKOFF_SECONDS[0] before attempt 2, etc.).
# Capped/repeats its last entry if _MAX_ATTEMPTS ever grows past this list.
_BACKOFF_SECONDS = (30, 60, 120, 240)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + '...'


def _backoff_seconds(attempt: int) -> int:
    index = min(attempt - 1, len(_BACKOFF_SECONDS) - 1)
    return _BACKOFF_SECONDS[max(index, 0)]


def _finish(db, delivery: WebhookDelivery, *, status: str, http_status: int | None, error_message: str | None) -> None:
    delivery.status = status
    delivery.http_status = http_status
    delivery.error_message = _truncate(error_message, _ERROR_MESSAGE_MAX_CHARS) if error_message else None
    delivery.updated_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(name=DELIVER_TASK_NAME, bind=True, acks_late=True, reject_on_worker_lost=True)
def deliver_webhook(self, delivery_id: str) -> None:
    db = SessionLocal()
    try:
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        if delivery.status != 'pending':
            # Already terminal (a duplicate/redelivered task execution) --
            # nothing left to do.
            return

        connection = db.get(WebhookConnection, delivery.connection_id) if delivery.connection_id else None
        if connection is None:
            _finish(
                db, delivery, status='failed', http_status=None,
                error_message='webhook connection was deleted; the delivery cannot continue',
            )
            return
        if not connection.enabled:
            _finish(
                db, delivery, status='failed', http_status=None,
                error_message='webhook connection is disabled',
            )
            return

        secret: str | None = None
        if connection.secret_encrypted:
            try:
                secret = security.decrypt_webhook_secret(connection.secret_encrypted)
            except ValueError as exc:
                _finish(db, delivery, status='failed', http_status=None, error_message=str(exc))
                return

        try:
            if delivery.job_id:
                job = db.get(Job, delivery.job_id)
                if job is None:
                    _finish(
                        db, delivery, status='failed', http_status=None,
                        error_message='job was deleted; the delivery cannot continue',
                    )
                    return
                payload = build_job_payload(
                    db, job, delivery.event, include_markdown=delivery.event != 'job.failed'
                )
            elif delivery.import_run_id:
                from app.models.models import ImportRun

                run = db.get(ImportRun, delivery.import_run_id)
                if run is None:
                    _finish(
                        db, delivery, status='failed', http_status=None,
                        error_message='import run was deleted; the delivery cannot continue',
                    )
                    return
                payload = build_run_payload(run)
            else:
                _finish(
                    db, delivery, status='failed', http_status=None,
                    error_message='delivery has neither a job nor an import run to build a payload from',
                )
                return
        except Exception as exc:  # pragma: no cover - defensive, payload building should not raise
            logger.exception('webhook delivery %s: failed to build payload: %s', delivery_id, exc)
            _finish(db, delivery, status='failed', http_status=None, error_message=str(exc))
            return

        allowed_hosts = frozenset(settings.webhook_private_host_allowlist)
        http_status, error_message = send_webhook_request(connection.url, payload, secret, allowed_hosts)

        attempts = delivery.attempts + 1
        delivery.attempts = attempts
        delivery.connection_name = connection.name  # keep the snapshot fresh in case it was just renamed

        if error_message is None:
            _finish(db, delivery, status='sent', http_status=http_status, error_message=None)
            return

        # http_status == 0 is send_webhook_request's sentinel for a
        # transport-level failure (never got a real response); 4xx is a
        # receiving-endpoint rejection that a retry cannot fix (bad URL,
        # auth, payload shape, ...) and is final immediately. Everything
        # else (0, or 5xx) is retried with backoff up to _MAX_ATTEMPTS.
        is_final_client_error = 400 <= http_status < 500
        if is_final_client_error or attempts >= _MAX_ATTEMPTS:
            delivery.updated_at = datetime.now(timezone.utc)
            delivery.status = 'failed'
            delivery.http_status = http_status or None
            delivery.error_message = _truncate(error_message, _ERROR_MESSAGE_MAX_CHARS)
            db.commit()
            return

        # Retryable failure with attempts remaining: stay 'pending', record
        # this attempt's detail so GET /webhooks/deliveries shows progress,
        # and self-re-enqueue after a backoff -- same
        # self.app.send_task(..., countdown=...) pattern as
        # refresh_tasks.confluence_refresh_tick's chain.
        delivery.updated_at = datetime.now(timezone.utc)
        delivery.http_status = http_status or None
        delivery.error_message = _truncate(error_message, _ERROR_MESSAGE_MAX_CHARS)
        db.commit()
        self.app.send_task(DELIVER_TASK_NAME, args=[delivery_id], countdown=_backoff_seconds(attempts))
    except Exception as exc:  # pragma: no cover - defensive terminal transition
        logger.exception('webhook delivery %s failed: %s', delivery_id, exc)
        db.rollback()
        delivery = db.get(WebhookDelivery, delivery_id)
        if delivery is not None:
            delivery.status = 'failed'
            delivery.error_message = _truncate(str(exc), _ERROR_MESSAGE_MAX_CHARS)
            delivery.updated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


# --- Dispatch: create+enqueue deliveries for a just-finished job/run --------
#
# Called from the two real completion hooks (app/workers/tasks.py's
# process_job, right after a terminal FINISHED/FAILED commit, and
# app/workers/import_tasks.py's _finalize_run, right after the run's
# FINISHED commit) -- both already hold an open `db` session at that point,
# so these take it rather than opening their own. Both hook call sites wrap
# their call to these in try/except+logger.exception per the feature
# contract: a webhook dispatch failure must never take down a job/run
# completion that has already succeeded and committed.
#
# These only ever create pending WebhookDelivery rows and enqueue
# deliver_webhook by name (celery_app.send_task) -- no network call happens
# here, matching app/api/webhook_routes.send_webhook's own dispatch shape.

def _enabled_connections_for_event(db, owner_id: str, event: str) -> list[WebhookConnection]:
    connections = db.scalars(
        select(WebhookConnection)
        .where(WebhookConnection.owner_id == owner_id)
        .where(WebhookConnection.enabled.is_(True))
    ).all()
    # Filtered in Python, not SQL: `events` is a JSON list column (no
    # portable containment operator across sqlite/postgres), same reasoning
    # as WebhookConnection.status/event being plain strings rather than a
    # queryable enum.
    return [connection for connection in connections if event in (connection.events or [])]


def _pending_delivery_count(db, owner_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(WebhookDelivery).where(
            WebhookDelivery.owner_id == owner_id,
            WebhookDelivery.status == 'pending',
        )
    ) or 0


def _dispatch(db, owner_id: str | None, event: str, *, job_id: str | None, import_run_id: str | None) -> None:
    if not settings.webhooks_enabled or not owner_id:
        return
    connections = _enabled_connections_for_event(db, owner_id, event)
    if not connections:
        return

    # Cap checked once up front and incremented locally per created row --
    # same single-count-then-decrement-headroom shape as
    # webhook_routes.send_webhook's cap check, just amortized over however
    # many connections this owner has subscribed to this event instead of
    # always exactly 1.
    pending = _pending_delivery_count(db, owner_id)
    cap = settings.webhook_max_pending_deliveries_per_user
    for connection in connections:
        if pending >= cap:
            logger.warning(
                'webhook dispatch: pending-delivery cap reached for user %s (event %s); '
                'skipping connection %s (%s)',
                owner_id, event, connection.id, connection.name,
            )
            continue
        delivery = WebhookDelivery(
            connection_id=connection.id,
            connection_name=connection.name,
            owner_id=owner_id,
            event=event,
            job_id=job_id,
            import_run_id=import_run_id,
            status='pending',
        )
        db.add(delivery)
        db.commit()
        pending += 1
        celery_app.send_task(DELIVER_TASK_NAME, args=[delivery.id])


def dispatch_job_event(db, job: Job, event: str) -> None:
    """event is 'job.finished' or 'job.failed'. No-op if WEBHOOKS_ENABLED is
    off, the job has no owner, or the owner has no enabled connection
    subscribed to `event`."""
    _dispatch(db, job.owner_id, event, job_id=job.id, import_run_id=None)


def dispatch_run_event(db, run) -> None:
    """event is always 'import_run.finished'. No-op if WEBHOOKS_ENABLED is
    off, the run has no owner, or the owner has no enabled connection
    subscribed to it."""
    _dispatch(db, run.owner_id, 'import_run.finished', job_id=None, import_run_id=run.id)
