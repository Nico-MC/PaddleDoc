"""OpenWebUI push Celery task: upload a job's current markdown to an
OpenWebUI knowledge collection, then best-effort clean up the file from the
job's previous push onto the same connection+knowledge (see
app/models/models.py's OpenWebUIPush docstring for the full data-model
rationale).

Registered from app/workers/tasks.py (the `celery -A app.workers.tasks`
entrypoint) via an explicit import, mirroring app/workers/import_tasks.py;
the API enqueues by name only (`push_openwebui`).
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.models import Job, OpenWebUIConnection, OpenWebUIPush
from app.services import security
from app.services.openwebui import OpenWebUIError, add_to_knowledge, delete_file, remove_from_knowledge, upload_file, wait_for_processing
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

PUSH_TASK_NAME = 'push_openwebui'

_ERROR_MESSAGE_MAX_CHARS = 2000


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + '...'


def _resolve_markdown_path(job: Job) -> Path | None:
    """Local copy of routes._resolve_markdown_path's disk-fallback lookup
    for legacy rows written before result_markdown existed -- the worker
    must not import the API module (mirrors import_tasks._attach_tags being
    a local copy of routes._attach_tags for the same reason). Returns None
    instead of raising HTTPException when nothing is found."""
    info = dict(job.processing_info) if isinstance(job.processing_info, dict) else {}
    editor = dict(info.get('editor')) if isinstance(info.get('editor'), dict) else {}
    latest = editor.get('latest_result_path') if isinstance(editor, dict) else None
    if isinstance(latest, str):
        path = Path(latest).resolve()
        if path.exists():
            return path

    edited_dir = (settings.results_dir / 'edited').resolve()
    if edited_dir.exists():
        candidates = sorted(edited_dir.glob(f'{job.id}.v*.md'))
        if candidates:
            return candidates[-1].resolve()

    if not job.result_path:
        return None
    path = Path(job.result_path).resolve()
    return path if path.exists() else None


def _resolve_markdown_content(job: Job) -> str | None:
    """DB-first with disk fallback -- local copy of
    routes._resolve_markdown_content's logic (see _resolve_markdown_path
    above for why this can't just be imported). Returns None (never raises)
    if no markdown can be found anywhere."""
    if job.result_markdown:
        return job.result_markdown
    path = _resolve_markdown_path(job)
    if path is None:
        return None
    return path.read_text(encoding='utf-8')


def _claim_push(db, push_id: str, now: datetime) -> bool:
    """Claim-then-stale-reclaim, mirroring process_job's pattern in
    app/workers/tasks.py: a normal pending->running claim, then a
    time-bounded reclaim of a 'running' push whose worker died mid-flight."""
    claimed = db.execute(
        update(OpenWebUIPush)
        .where(OpenWebUIPush.id == push_id)
        .where(OpenWebUIPush.status == 'pending')
        .values(status='running', error_message=None, updated_at=now)
    )
    if claimed.rowcount:
        db.commit()
        return True

    # Recovery path for acks_late redelivery. Unlike process_job's fixed
    # 2-minute retry window, a push can legitimately run for up to
    # openwebui_push_timeout_seconds (the wait_for_processing budget), so
    # the stale window must clear that plus headroom, or a still-running
    # push would be wrongly reclaimed out from under itself.
    stale_cutoff = now - timedelta(seconds=settings.openwebui_push_timeout_seconds + 60)
    claimed = db.execute(
        update(OpenWebUIPush)
        .where(OpenWebUIPush.id == push_id)
        .where(OpenWebUIPush.status == 'running')
        .where(OpenWebUIPush.updated_at < stale_cutoff)
        .values(status='running', error_message=None, updated_at=now)
    )
    if claimed.rowcount:
        db.commit()
        return True
    db.rollback()
    return False


def _fail_push(db, push_id: str, message: str) -> None:
    db.rollback()
    push = db.get(OpenWebUIPush, push_id)
    if push is not None:
        push.status = 'failed'
        push.error_message = _truncate(message, _ERROR_MESSAGE_MAX_CHARS)
        db.commit()


def _replace_previous_push(
    db, push: OpenWebUIPush, connection: OpenWebUIConnection, api_key: str, allowed_hosts: frozenset[str]
) -> str | None:
    """Best-effort replace of the file from the job's last *finished* push
    onto the same connection+knowledge: remove it from the knowledge
    collection, then delete the file itself. Errors here are logged, never
    fatal -- the new push has already succeeded by the time this runs (see
    the OpenWebUIPush docstring in app/models/models.py)."""
    previous = db.scalars(
        select(OpenWebUIPush)
        .where(OpenWebUIPush.job_id == push.job_id)
        .where(OpenWebUIPush.connection_id == push.connection_id)
        .where(OpenWebUIPush.knowledge_id == push.knowledge_id)
        .where(OpenWebUIPush.status == 'finished')
        .where(OpenWebUIPush.id != push.id)
        .where(OpenWebUIPush.openwebui_file_id.is_not(None))
        .order_by(OpenWebUIPush.created_at.desc())
        .limit(1)
    ).first()
    if previous is None or not previous.openwebui_file_id:
        return None

    old_file_id = previous.openwebui_file_id
    try:
        remove_from_knowledge(
            connection.base_url, api_key, push.knowledge_id, old_file_id, allowed_private_hosts=allowed_hosts
        )
    except OpenWebUIError:
        logger.warning(
            'openwebui push %s: could not remove predecessor file %s from knowledge %s (best-effort, ignored)',
            push.id, old_file_id, push.knowledge_id, exc_info=True,
        )
    try:
        delete_file(connection.base_url, api_key, old_file_id, allowed_private_hosts=allowed_hosts)
    except OpenWebUIError:
        logger.warning(
            'openwebui push %s: could not delete predecessor file %s (best-effort, ignored)',
            push.id, old_file_id, exc_info=True,
        )
    return old_file_id


@celery_app.task(name=PUSH_TASK_NAME, bind=True, acks_late=True, reject_on_worker_lost=True)
def push_openwebui(self, push_id: str) -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        if not _claim_push(db, push_id, now):
            # Another live execution owns the push, or it is already terminal.
            return

        push = db.get(OpenWebUIPush, push_id)
        if push is None:
            return

        try:
            connection = db.get(OpenWebUIConnection, push.connection_id) if push.connection_id else None
            if connection is None:
                raise OpenWebUIError('connection was deleted; the push cannot continue')

            job = db.get(Job, push.job_id)
            if job is None:
                raise OpenWebUIError('job was deleted; the push cannot continue')
            content = _resolve_markdown_content(job)
            if not content:
                raise OpenWebUIError('job has no markdown content to push')

            # Quality-gate check (see app/services/quality_gate.py): a job
            # processed before the gate existed, or one that fell back to a
            # path that never ran it, simply has no 'quality_gate' dict --
            # that absence must pass through, not block. Only an explicit
            # recommendation of 'block' stops the push. There is currently no
            # per-push/per-job override carrier to bypass this (see the
            # OpenWebUIPush/Job models) -- a document graded this poorly
            # cannot be pushed deliberately until one is added.
            info = job.processing_info if isinstance(job.processing_info, dict) else {}
            execution = info.get('execution') if isinstance(info.get('execution'), dict) else {}
            quality_gate = execution.get('quality_gate') if isinstance(execution.get('quality_gate'), dict) else {}
            if quality_gate.get('recommendation') == 'block':
                grade = quality_gate.get('grade')
                score = quality_gate.get('score')
                raise OpenWebUIError(f'quality gate {grade} (score {score}): document was not pushed')

            try:
                api_key = security.decrypt_openwebui_api_key(connection.api_key_encrypted)
            except ValueError as exc:
                raise OpenWebUIError(str(exc)) from exc

            allowed_hosts = frozenset(settings.openwebui_private_host_allowlist)
            # DB-first-content dictates WHAT to push; original_filename
            # (never the current markdown, which has no filename of its
            # own) dictates the name it's pushed under.
            stem = Path(job.original_filename).stem.strip() or job.id
            filename = f'{stem}.md'
            content_bytes = content.encode('utf-8')

            file_id = upload_file(
                connection.base_url, api_key, filename, content_bytes, allowed_private_hosts=allowed_hosts
            )
            try:
                wait_for_processing(
                    connection.base_url, api_key, file_id,
                    timeout_seconds=float(settings.openwebui_push_timeout_seconds),
                    allowed_private_hosts=allowed_hosts,
                )
                # Only after processing has actually completed may the file be
                # attached to the knowledge collection.
                add_to_knowledge(
                    connection.base_url, api_key, push.knowledge_id, file_id, allowed_private_hosts=allowed_hosts
                )
            except Exception:
                # Upload already succeeded, so file_id now exists on the
                # OpenWebUI server -- if either step below it fails, that
                # file would otherwise be orphaned there forever (nothing
                # else ever learns its id). Best-effort delete before
                # falling through to the outer except, same
                # log-and-ignore pattern as _replace_previous_push.
                try:
                    delete_file(connection.base_url, api_key, file_id, allowed_private_hosts=allowed_hosts)
                except OpenWebUIError:
                    logger.warning(
                        'openwebui push %s: could not delete orphaned file %s after a post-upload failure '
                        '(best-effort, ignored)',
                        push.id, file_id, exc_info=True,
                    )
                raise

            replaced_file_id = _replace_previous_push(db, push, connection, api_key, allowed_hosts)

            push.status = 'finished'
            push.openwebui_file_id = file_id
            push.replaced_file_id = replaced_file_id
            push.pushed_content_sha256 = hashlib.sha256(content_bytes).hexdigest()
            push.error_message = None
            db.commit()
        except OpenWebUIError as exc:
            _fail_push(db, push_id, str(exc))
        except Exception as exc:  # pragma: no cover - defensive terminal transition
            logger.exception('openwebui push %s failed: %s', push_id, exc)
            _fail_push(db, push_id, str(exc))
    finally:
        db.close()
