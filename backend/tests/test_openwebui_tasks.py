"""The push_openwebui worker task: the pending->running claim + stale-lease
reclaim (mirrors process_job's pattern), the full success sequence (upload
-> wait for processing -> attach to knowledge -> best-effort replace of the
job's previous push), and the failure path. Drives the task function
directly against the shared sqlite test DB (SessionLocal monkeypatched to
conftest's TestingSessionLocal), same pattern as test_import_tasks.py; the
OpenWebUI service calls are mocked at the app.workers.openwebui_tasks seam
so no real network is needed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import update

from app.models.models import Job, JobStatus, OpenWebUIConnection, OpenWebUIPush
from app.services import security
from app.services.openwebui import OpenWebUIError
from app.workers import openwebui_tasks
from app.workers.openwebui_tasks import push_openwebui
from tests.conftest import TestingSessionLocal, create_test_user


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setattr(openwebui_tasks, 'SessionLocal', TestingSessionLocal)
    return TestingSessionLocal()


def _make_connection(db, owner_id: str) -> OpenWebUIConnection:
    connection = OpenWebUIConnection(
        owner_id=owner_id,
        name='OWUI',
        base_url='https://owui.example.com',
        api_key_encrypted=security.encrypt_openwebui_api_key('sk-test'),
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def _make_job(db, owner_id: str, filename: str = 'report.pdf', markdown: str = '---\nx: 1\n---\n\nhello') -> Job:
    job = Job(
        original_filename=filename,
        upload_path=f'/tmp/{filename}',
        status=JobStatus.FINISHED,
        result_markdown=markdown,
        owner_id=owner_id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_push_openwebui_success_and_replace(db_session) -> None:
    db = db_session
    user = create_test_user(username='owui_task_user', email='owui_task_user@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)

    # Simulate a prior finished push (whose file should be replaced by this one).
    prev_push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='finished',
        openwebui_file_id='old-file-id',
        pushed_content_sha256='deadbeef',
        owner_id=user.id,
    )
    db.add(prev_push)
    db.commit()

    push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='pending',
        owner_id=user.id,
    )
    db.add(push)
    db.commit()
    db.refresh(push)
    push_id = push.id

    with (
        patch('app.workers.openwebui_tasks.upload_file', return_value='new-file-id') as mock_upload,
        patch('app.workers.openwebui_tasks.wait_for_processing') as mock_wait,
        patch('app.workers.openwebui_tasks.add_to_knowledge') as mock_add,
        patch('app.workers.openwebui_tasks.remove_from_knowledge') as mock_remove,
        patch('app.workers.openwebui_tasks.delete_file') as mock_delete,
    ):
        push_openwebui(push_id)

    mock_upload.assert_called_once()
    args, kwargs = mock_upload.call_args
    assert args[0] == connection.base_url
    assert args[1] == 'sk-test'
    assert args[2] == 'report.md'  # stem(original_filename) + '.md'
    assert args[3] == job.result_markdown.encode('utf-8')

    mock_wait.assert_called_once()
    assert mock_wait.call_args.kwargs['timeout_seconds'] == float(openwebui_tasks.settings.openwebui_push_timeout_seconds)

    mock_add.assert_called_once_with(
        connection.base_url, 'sk-test', 'kb1', 'new-file-id',
        allowed_private_hosts=frozenset(openwebui_tasks.settings.openwebui_private_host_allowlist),
    )
    mock_remove.assert_called_once()
    mock_delete.assert_called_once()
    assert mock_remove.call_args[0][3] == 'old-file-id'
    assert mock_delete.call_args[0][2] == 'old-file-id'

    db.expire_all()
    refreshed = db.get(OpenWebUIPush, push_id)
    assert refreshed.status == 'finished'
    assert refreshed.openwebui_file_id == 'new-file-id'
    assert refreshed.replaced_file_id == 'old-file-id'
    assert refreshed.pushed_content_sha256 is not None
    assert refreshed.error_message is None


def test_push_openwebui_replace_errors_are_best_effort_and_do_not_fail_the_push(db_session) -> None:
    db = db_session
    user = create_test_user(username='owui_task_user4', email='owui_task_user4@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)

    prev_push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='finished',
        openwebui_file_id='old-file-id',
        pushed_content_sha256='deadbeef',
        owner_id=user.id,
    )
    db.add(prev_push)
    db.commit()

    push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='pending',
        owner_id=user.id,
    )
    db.add(push)
    db.commit()
    db.refresh(push)
    push_id = push.id

    with (
        patch('app.workers.openwebui_tasks.upload_file', return_value='new-file-id'),
        patch('app.workers.openwebui_tasks.wait_for_processing'),
        patch('app.workers.openwebui_tasks.add_to_knowledge'),
        patch(
            'app.workers.openwebui_tasks.remove_from_knowledge',
            side_effect=OpenWebUIError('remove failed: HTTP 500'),
        ) as mock_remove,
        patch(
            'app.workers.openwebui_tasks.delete_file',
            side_effect=OpenWebUIError('delete failed: HTTP 500'),
        ) as mock_delete,
    ):
        push_openwebui(push_id)

    mock_remove.assert_called_once()
    mock_delete.assert_called_once()  # still attempted even though remove already failed

    db.expire_all()
    refreshed = db.get(OpenWebUIPush, push_id)
    # The new push succeeded despite BOTH best-effort cleanup calls failing.
    assert refreshed.status == 'finished'
    assert refreshed.openwebui_file_id == 'new-file-id'
    assert refreshed.replaced_file_id == 'old-file-id'  # returned regardless of cleanup outcome
    assert refreshed.error_message is None


def test_push_openwebui_upload_failure_marks_failed(db_session) -> None:
    db = db_session
    user = create_test_user(username='owui_task_user2', email='owui_task_user2@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)

    push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='pending',
        owner_id=user.id,
    )
    db.add(push)
    db.commit()
    db.refresh(push)
    push_id = push.id

    with patch('app.workers.openwebui_tasks.upload_file', side_effect=OpenWebUIError('boom: HTTP 500')):
        push_openwebui(push_id)

    db.expire_all()
    refreshed = db.get(OpenWebUIPush, push_id)
    assert refreshed.status == 'failed'
    assert 'boom' in refreshed.error_message


def test_push_openwebui_post_upload_failure_deletes_orphaned_file(db_session) -> None:
    """wait_for_processing failing after a successful upload must not leave
    the just-uploaded file orphaned on the OpenWebUI server: best-effort
    delete_file(the new file_id), then the push still fails normally."""
    db = db_session
    user = create_test_user(username='owui_task_user5', email='owui_task_user5@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)

    push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='pending',
        owner_id=user.id,
    )
    db.add(push)
    db.commit()
    db.refresh(push)
    push_id = push.id

    with (
        patch('app.workers.openwebui_tasks.upload_file', return_value='new-file-id'),
        patch(
            'app.workers.openwebui_tasks.wait_for_processing',
            side_effect=OpenWebUIError('processing failed: HTTP 500'),
        ),
        patch('app.workers.openwebui_tasks.add_to_knowledge') as mock_add,
        patch('app.workers.openwebui_tasks.delete_file') as mock_delete,
    ):
        push_openwebui(push_id)

    mock_delete.assert_called_once()
    assert mock_delete.call_args[0][2] == 'new-file-id'
    mock_add.assert_not_called()  # never reached: wait_for_processing failed first

    db.expire_all()
    refreshed = db.get(OpenWebUIPush, push_id)
    assert refreshed.status == 'failed'
    assert 'processing failed' in refreshed.error_message
    assert refreshed.openwebui_file_id is None  # the finished transition never ran


def test_push_openwebui_add_to_knowledge_failure_deletes_orphaned_file(db_session) -> None:
    """Same orphan-cleanup contract, but for the OTHER post-upload step
    (add_to_knowledge) failing after wait_for_processing already
    succeeded."""
    db = db_session
    user = create_test_user(username='owui_task_user6', email='owui_task_user6@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)

    push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='pending',
        owner_id=user.id,
    )
    db.add(push)
    db.commit()
    db.refresh(push)
    push_id = push.id

    with (
        patch('app.workers.openwebui_tasks.upload_file', return_value='new-file-id'),
        patch('app.workers.openwebui_tasks.wait_for_processing'),
        patch(
            'app.workers.openwebui_tasks.add_to_knowledge',
            side_effect=OpenWebUIError('attach failed: HTTP 500'),
        ),
        patch('app.workers.openwebui_tasks.delete_file') as mock_delete,
    ):
        push_openwebui(push_id)

    mock_delete.assert_called_once()
    assert mock_delete.call_args[0][2] == 'new-file-id'

    db.expire_all()
    refreshed = db.get(OpenWebUIPush, push_id)
    assert refreshed.status == 'failed'
    assert 'attach failed' in refreshed.error_message


def test_push_openwebui_claim_pattern_stale_reclaim(db_session) -> None:
    db = db_session
    user = create_test_user(username='owui_task_user3', email='owui_task_user3@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)

    push = OpenWebUIPush(
        connection_id=connection.id,
        connection_name=connection.name,
        job_id=job.id,
        knowledge_id='kb1',
        knowledge_name='Docs',
        status='running',
        owner_id=user.id,
    )
    db.add(push)
    db.commit()
    db.refresh(push)
    push_id = push.id

    # Fresh 'running' (not stale): a second execution must no-op, not steal it.
    with patch('app.workers.openwebui_tasks.upload_file') as mock_upload:
        push_openwebui(push_id)
    mock_upload.assert_not_called()

    # Make it stale, then a reclaim should proceed.
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=openwebui_tasks.settings.openwebui_push_timeout_seconds + 120)
    db.execute(update(OpenWebUIPush).where(OpenWebUIPush.id == push_id).values(updated_at=stale_at))
    db.commit()

    with (
        patch('app.workers.openwebui_tasks.upload_file', return_value='fid') as mock_upload,
        patch('app.workers.openwebui_tasks.wait_for_processing'),
        patch('app.workers.openwebui_tasks.add_to_knowledge'),
    ):
        push_openwebui(push_id)
    mock_upload.assert_called_once()

    db.expire_all()
    refreshed = db.get(OpenWebUIPush, push_id)
    assert refreshed.status == 'finished'
