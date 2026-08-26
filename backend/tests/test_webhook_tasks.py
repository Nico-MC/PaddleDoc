"""Outbound webhook delivery: signature correctness and payload shape
(app/services/webhooks.py), the deliver_webhook worker task's happy path /
final-4xx / retried-5xx paths (app/workers/webhook_tasks.py), and the
job-completion dispatch hook (app/workers/tasks.py's process_job) creating
deliveries only for matching events/owner and never breaking job completion
on a webhook failure.

Drives deliver_webhook directly against the shared sqlite test DB
(SessionLocal monkeypatched to conftest's TestingSessionLocal), same pattern
as test_openwebui_tasks.py; send_webhook_request is mocked at the
app.workers.webhook_tasks seam so no real network is needed.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.models import ImportRun, Job, JobStatus, WebhookConnection, WebhookDelivery
from app.services import security
from app.services.webhooks import build_job_payload, build_run_payload, send_webhook_request
from app.workers import webhook_tasks
from app.workers.webhook_tasks import deliver_webhook
from tests.conftest import TestingSessionLocal, create_test_user


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setattr(webhook_tasks, 'SessionLocal', TestingSessionLocal)
    return TestingSessionLocal()


def _make_connection(db, owner_id: str, *, events=('job.finished', 'job.failed'), secret: str | None = None, enabled: bool = True) -> WebhookConnection:
    connection = WebhookConnection(
        owner_id=owner_id,
        name='n8n',
        url='https://n8n.example.com/webhook/abc',
        events=list(events),
        enabled=enabled,
        secret_encrypted=security.encrypt_webhook_secret(secret) if secret else None,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def _make_job(db, owner_id: str | None, *, status=JobStatus.FINISHED, filename='report.pdf', markdown='hello', error_message=None, tags=None) -> Job:
    job = Job(
        original_filename=filename,
        upload_path=f'/tmp/{filename}',
        status=status,
        result_markdown=markdown if status == JobStatus.FINISHED else None,
        error_message=error_message,
        owner_id=owner_id,
        document_version=1,
        content_sha256='deadbeef' * 8,
        processing_info={'settings': {'profile_id': 'ppocrv6_small', 'folder': 'inbox', 'subfolder': 'q3'}},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _make_delivery(db, connection: WebhookConnection, *, job_id: str | None = None, import_run_id: str | None = None, event: str = 'job.finished') -> WebhookDelivery:
    delivery = WebhookDelivery(
        connection_id=connection.id,
        connection_name=connection.name,
        owner_id=connection.owner_id,
        event=event,
        job_id=job_id,
        import_run_id=import_run_id,
        status='pending',
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


# --- send_webhook_request: signature correctness -----------------------------

def test_send_webhook_request_signs_body_with_known_secret() -> None:
    payload = {'event': 'job.finished', 'timestamp': '2026-08-25T00:00:00+00:00'}
    secret = 'shh-its-a-secret'

    captured = {}

    class _FakeResponse:
        status_code = 200
        body = b'{}'

    def _fake_safe_fetch(url, *, method, headers, body, timeout, max_bytes, allowed_private_hosts):
        captured['headers'] = headers
        captured['body'] = body
        return _FakeResponse()

    with patch('app.services.webhooks.safe_fetch', side_effect=_fake_safe_fetch):
        http_status, error_message = send_webhook_request(
            'https://example.com/hook', payload, secret, allowed_private_hosts=frozenset()
        )

    assert http_status == 200
    assert error_message is None
    assert captured['headers']['X-PaddleDoc-Event'] == 'job.finished'
    expected_hex = hmac.new(secret.encode('utf-8'), captured['body'], hashlib.sha256).hexdigest()
    assert captured['headers']['X-PaddleDoc-Signature'] == f'sha256={expected_hex}'


def test_send_webhook_request_no_secret_sends_no_signature_header() -> None:
    payload = {'event': 'job.failed'}
    captured = {}

    class _FakeResponse:
        status_code = 200
        body = b'{}'

    def _fake_safe_fetch(url, *, method, headers, body, timeout, max_bytes, allowed_private_hosts):
        captured['headers'] = headers
        return _FakeResponse()

    with patch('app.services.webhooks.safe_fetch', side_effect=_fake_safe_fetch):
        send_webhook_request('https://example.com/hook', payload, None, allowed_private_hosts=frozenset())

    assert 'X-PaddleDoc-Signature' not in captured['headers']


def test_send_webhook_request_transport_failure_returns_zero_status() -> None:
    from app.services.safe_fetch import SafeFetchError

    with patch('app.services.webhooks.safe_fetch', side_effect=SafeFetchError('blocked: private address')):
        http_status, error_message = send_webhook_request(
            'https://example.com/hook', {'event': 'job.finished'}, None, allowed_private_hosts=frozenset()
        )
    assert http_status == 0
    assert 'blocked' in error_message


def test_send_webhook_request_4xx_returns_status_and_detail() -> None:
    class _FakeResponse:
        status_code = 422
        body = b'bad payload'

    with patch('app.services.webhooks.safe_fetch', return_value=_FakeResponse()):
        http_status, error_message = send_webhook_request(
            'https://example.com/hook', {'event': 'job.finished'}, None, allowed_private_hosts=frozenset()
        )
    assert http_status == 422
    assert error_message == 'bad payload'


# --- Payload builders ---------------------------------------------------------

def test_build_job_payload_shape_finished_includes_markdown() -> None:
    db = TestingSessionLocal()
    try:
        user = create_test_user(username='webhook_payload_user', email='webhook_payload_user@example.com')
        job = _make_job(db, user.id, markdown='# hi')
        payload = build_job_payload(db, job, 'job.finished', include_markdown=True)

        assert payload['event'] == 'job.finished'
        assert payload['markdown'] == '# hi'
        assert payload['error_message'] is None
        assert payload['job']['id'] == job.id
        assert payload['job']['filename'] == 'report.pdf'
        assert payload['job']['status'] == 'FINISHED'
        assert payload['job']['folder'] == 'inbox'
        assert payload['job']['subfolder'] == 'q3'
        assert payload['job']['profile_id'] == 'ppocrv6_small'
        assert payload['job']['document_version'] == 1
        assert payload['job']['content_sha256'] == job.content_sha256
        assert payload['download_url'].endswith(f'/api/v1/jobs/{job.id}/download')
        datetime.fromisoformat(payload['timestamp'])  # parses without raising
    finally:
        db.close()


def test_build_job_payload_failed_has_null_markdown_and_error_message() -> None:
    db = TestingSessionLocal()
    try:
        user = create_test_user(username='webhook_payload_user2', email='webhook_payload_user2@example.com')
        job = _make_job(db, user.id, status=JobStatus.FAILED, error_message='OCR timed out')
        payload = build_job_payload(db, job, 'job.failed', include_markdown=False)

        assert payload['markdown'] is None
        assert payload['error_message'] == 'OCR timed out'
        assert payload['job']['status'] == 'FAILED'
    finally:
        db.close()


def test_build_run_payload_shape() -> None:
    run = ImportRun(
        source_id=None,
        owner_id=None,
        kind='confluence',
        scope_type='space',
        scope_value='ENG',
        options={},
        state={'frontier': [], 'visited': {}, 'errors': []},
        pages_imported=7,
        pages_failed=2,
    )
    run.id = 'run-1'
    payload = build_run_payload(run)
    assert payload == {
        'event': 'import_run.finished',
        'timestamp': payload['timestamp'],
        'run': {
            'id': 'run-1',
            'scope_type': 'space',
            'scope_value': 'ENG',
            'pages_imported': 7,
            'pages_failed': 2,
        },
    }
    datetime.fromisoformat(payload['timestamp'])


# --- deliver_webhook worker task ---------------------------------------------

def test_deliver_webhook_happy_path_marks_sent(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user', email='webhook_task_user@example.com')
    connection = _make_connection(db, user.id, secret='sekret')
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery_id = delivery.id

    with patch('app.workers.webhook_tasks.send_webhook_request', return_value=(200, None)) as mock_send:
        deliver_webhook(delivery_id)

    mock_send.assert_called_once()
    args = mock_send.call_args[0]
    assert args[0] == connection.url
    assert args[1]['event'] == 'job.finished'
    assert args[1]['markdown'] == job.result_markdown  # job.finished -> include_markdown=True
    assert args[2] == 'sekret'

    db.expire_all()
    refreshed = db.get(WebhookDelivery, delivery_id)
    assert refreshed.status == 'sent'
    assert refreshed.http_status == 200
    assert refreshed.error_message is None
    assert refreshed.attempts == 1


def test_deliver_webhook_import_run_payload(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user_run', email='webhook_task_user_run@example.com')
    connection = _make_connection(db, user.id, events=('import_run.finished',))
    run = ImportRun(
        owner_id=user.id, kind='confluence', scope_type='space', scope_value='ENG',
        options={}, state={'frontier': [], 'visited': {}, 'errors': []},
        pages_imported=3, pages_failed=1,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    delivery = _make_delivery(db, connection, import_run_id=run.id, event='import_run.finished')
    delivery_id = delivery.id

    with patch('app.workers.webhook_tasks.send_webhook_request', return_value=(200, None)) as mock_send:
        deliver_webhook(delivery_id)

    payload = mock_send.call_args[0][1]
    assert payload['event'] == 'import_run.finished'
    assert payload['run']['id'] == run.id
    assert payload['run']['pages_imported'] == 3

    db.expire_all()
    assert db.get(WebhookDelivery, delivery_id).status == 'sent'


def test_deliver_webhook_4xx_is_final_no_retry_enqueued(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user3', email='webhook_task_user3@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery_id = delivery.id

    with (
        patch('app.workers.webhook_tasks.send_webhook_request', return_value=(422, 'bad payload')),
        patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task,
    ):
        deliver_webhook(delivery_id)

    mock_send_task.assert_not_called()
    db.expire_all()
    refreshed = db.get(WebhookDelivery, delivery_id)
    assert refreshed.status == 'failed'
    assert refreshed.http_status == 422
    assert refreshed.error_message == 'bad payload'
    assert refreshed.attempts == 1


def test_deliver_webhook_5xx_stays_pending_and_reenqueues_with_backoff(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user4', email='webhook_task_user4@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery_id = delivery.id

    with (
        patch('app.workers.webhook_tasks.send_webhook_request', return_value=(503, 'Service Unavailable')),
        patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task,
    ):
        deliver_webhook(delivery_id)

    mock_send_task.assert_called_once()
    args, kwargs = mock_send_task.call_args
    assert args[0] == webhook_tasks.DELIVER_TASK_NAME
    assert kwargs['args'] == [delivery_id]
    assert kwargs['countdown'] == webhook_tasks._BACKOFF_SECONDS[0]

    db.expire_all()
    refreshed = db.get(WebhookDelivery, delivery_id)
    assert refreshed.status == 'pending'  # not terminal yet
    assert refreshed.http_status == 503
    assert refreshed.attempts == 1


def test_deliver_webhook_exhausts_retries_then_fails(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user5', email='webhook_task_user5@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery.attempts = webhook_tasks._MAX_ATTEMPTS - 1  # this call is the last allowed attempt
    db.commit()
    delivery_id = delivery.id

    with (
        patch('app.workers.webhook_tasks.send_webhook_request', return_value=(0, 'connection refused')),
        patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task,
    ):
        deliver_webhook(delivery_id)

    mock_send_task.assert_not_called()
    db.expire_all()
    refreshed = db.get(WebhookDelivery, delivery_id)
    assert refreshed.status == 'failed'
    assert refreshed.attempts == webhook_tasks._MAX_ATTEMPTS


def test_deliver_webhook_connection_deleted_marks_failed(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user6', email='webhook_task_user6@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery_id = delivery.id

    db.delete(connection)
    db.commit()

    with patch('app.workers.webhook_tasks.send_webhook_request') as mock_send:
        deliver_webhook(delivery_id)

    mock_send.assert_not_called()
    db.expire_all()
    refreshed = db.get(WebhookDelivery, delivery_id)
    assert refreshed.status == 'failed'
    assert 'deleted' in refreshed.error_message


def test_deliver_webhook_disabled_connection_marks_failed(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user7', email='webhook_task_user7@example.com')
    connection = _make_connection(db, user.id, enabled=False)
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery_id = delivery.id

    with patch('app.workers.webhook_tasks.send_webhook_request') as mock_send:
        deliver_webhook(delivery_id)

    mock_send.assert_not_called()
    db.expire_all()
    refreshed = db.get(WebhookDelivery, delivery_id)
    assert refreshed.status == 'failed'
    assert 'disabled' in refreshed.error_message


def test_deliver_webhook_non_pending_is_a_noop(db_session) -> None:
    db = db_session
    user = create_test_user(username='webhook_task_user8', email='webhook_task_user8@example.com')
    connection = _make_connection(db, user.id)
    job = _make_job(db, user.id)
    delivery = _make_delivery(db, connection, job_id=job.id, event='job.finished')
    delivery.status = 'sent'
    db.commit()
    delivery_id = delivery.id

    with patch('app.workers.webhook_tasks.send_webhook_request') as mock_send:
        deliver_webhook(delivery_id)

    mock_send.assert_not_called()


# --- Job-completion dispatch hook (app/workers/tasks.py) --------------------

def test_process_job_finished_dispatches_only_matching_events(db_session) -> None:
    """Two connections for the same owner: one subscribed to job.finished
    (gets a delivery), one only to job.failed (does not)."""
    db = db_session
    user = create_test_user(username='webhook_hook_user', email='webhook_hook_user@example.com')
    matching = _make_connection(db, user.id, events=('job.finished',))
    non_matching = _make_connection(db, user.id, events=('job.failed',))
    job = _make_job(db, user.id)

    with patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task:
        webhook_tasks.dispatch_job_event(db, job, 'job.finished')

    mock_send_task.assert_called_once()
    db.expire_all()
    deliveries = db.query(WebhookDelivery).filter(WebhookDelivery.job_id == job.id).all()
    assert len(deliveries) == 1
    assert deliveries[0].connection_id == matching.id
    assert deliveries[0].event == 'job.finished'
    assert deliveries[0].status == 'pending'


def test_dispatch_job_event_ignores_other_owners_connections(db_session) -> None:
    db = db_session
    owner = create_test_user(username='webhook_hook_owner', email='webhook_hook_owner@example.com')
    other = create_test_user(username='webhook_hook_other', email='webhook_hook_other@example.com')
    _make_connection(db, other.id, events=('job.finished',))  # different owner, must not fire
    job = _make_job(db, owner.id)

    with patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task:
        webhook_tasks.dispatch_job_event(db, job, 'job.finished')

    mock_send_task.assert_not_called()


def test_dispatch_job_event_noop_when_webhooks_disabled(db_session, monkeypatch) -> None:
    db = db_session
    monkeypatch.setattr(webhook_tasks.settings, 'webhooks_enabled', False)
    user = create_test_user(username='webhook_hook_user2', email='webhook_hook_user2@example.com')
    _make_connection(db, user.id, events=('job.finished',))
    job = _make_job(db, user.id)

    with patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task:
        webhook_tasks.dispatch_job_event(db, job, 'job.finished')

    mock_send_task.assert_not_called()


def test_dispatch_job_event_respects_pending_cap(db_session, monkeypatch) -> None:
    db = db_session
    monkeypatch.setattr(webhook_tasks.settings, 'webhook_max_pending_deliveries_per_user', 0)
    user = create_test_user(username='webhook_hook_user3', email='webhook_hook_user3@example.com')
    _make_connection(db, user.id, events=('job.finished',))
    job = _make_job(db, user.id)

    with patch.object(webhook_tasks.celery_app, 'send_task') as mock_send_task:
        webhook_tasks.dispatch_job_event(db, job, 'job.finished')  # must not raise

    mock_send_task.assert_not_called()
    assert db.query(WebhookDelivery).filter(WebhookDelivery.job_id == job.id).count() == 0


def test_process_job_completion_hook_swallows_webhook_dispatch_errors(monkeypatch, tmp_path) -> None:
    """Drives the real app/workers/tasks.process_job task end to end with
    webhook_tasks.dispatch_job_event patched to raise: the job must still
    land FINISHED (the try/except around the hook in tasks.py must swallow
    the error), not FAILED and not propagate the exception out of the task.
    """
    from app.core.config import settings
    from app.workers import tasks

    monkeypatch.setattr(tasks, 'SessionLocal', TestingSessionLocal)
    monkeypatch.setattr(
        tasks, 'convert_to_markdown_with_details',
        lambda *args, **kwargs: ('# hook-test result', {'page_count': 1}),
    )
    monkeypatch.setattr(tasks.webhook_tasks, 'dispatch_job_event', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    upload_path = settings.uploads_dir / 'inbox' / 'hook-job.pdf'
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b'%PDF-1.4 fake upload content')

    db = TestingSessionLocal()
    user = create_test_user(username='webhook_hook_user4', email='webhook_hook_user4@example.com')
    db.add(Job(
        id='hook-job',
        original_filename='hook-job.pdf',
        upload_path=str(upload_path),
        status=JobStatus.PENDING,
        owner_id=user.id,
        processing_info={'settings': {'storage_folder': 'inbox'}},
    ))
    db.commit()
    db.close()

    tasks.process_job('hook-job')  # must not raise despite the dispatch hook throwing

    db = TestingSessionLocal()
    job = db.get(Job, 'hook-job')
    assert job.status == JobStatus.FINISHED
    assert job.error_message is None
    db.close()


def test_process_job_failed_path_swallows_webhook_dispatch_errors(monkeypatch, tmp_path) -> None:
    """Companion to the FINISHED-path swallow test above: drives process_job
    into a FAILED terminal state (converter raises) with dispatch_job_event
    ALSO raising -- the job must still land FAILED with its real error
    message, not crash the task or mask the conversion failure with the
    webhook error.
    """
    from app.core.config import settings
    from app.workers import tasks

    monkeypatch.setattr(tasks, 'SessionLocal', TestingSessionLocal)
    monkeypatch.setattr(
        tasks, 'convert_to_markdown_with_details',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('conversion exploded')),
    )
    monkeypatch.setattr(tasks.webhook_tasks, 'dispatch_job_event', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('webhook boom')))
    settings.uploads_dir = tmp_path / 'uploads'
    settings.results_dir = tmp_path / 'results'

    upload_path = settings.uploads_dir / 'inbox' / 'hook-fail-job.pdf'
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b'%PDF-1.4 fake upload content')

    db = TestingSessionLocal()
    user = create_test_user(username='webhook_hook_user5', email='webhook_hook_user5@example.com')
    db.add(Job(
        id='hook-fail-job',
        original_filename='hook-fail-job.pdf',
        upload_path=str(upload_path),
        status=JobStatus.PENDING,
        owner_id=user.id,
        processing_info={'settings': {'storage_folder': 'inbox'}},
    ))
    db.commit()
    db.close()

    tasks.process_job('hook-fail-job')  # must not raise

    db = TestingSessionLocal()
    job = db.get(Job, 'hook-fail-job')
    assert job.status == JobStatus.FAILED
    assert 'conversion exploded' in (job.error_message or '')
