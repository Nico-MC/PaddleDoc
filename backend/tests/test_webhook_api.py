"""Outbound webhook API surface: connections CRUD + test cooldown, and the
/send create + GET /deliveries flow (owner-private connection visibility,
kill-switch, event validation). Drives the real TestClient/conftest wiring,
same pattern as test_openwebui_api.py; the webhook transport call and the
Celery dispatch are mocked out at the app.api.webhook_routes seam so no real
network/broker is needed.
"""

from unittest.mock import patch

from app.core.config import settings
from app.models.models import Job, JobStatus, WebhookConnection, WebhookDelivery
from app.services import security
from tests.conftest import TestingSessionLocal, create_test_user, login_as


def _make_finished_job(owner_id: str, filename: str = 'report.pdf') -> str:
    db = TestingSessionLocal()
    try:
        job = Job(
            original_filename=filename,
            upload_path=f'/tmp/{filename}',
            status=JobStatus.FINISHED,
            result_markdown='---\ntitle: x\n---\n\nhello world',
            owner_id=owner_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def test_webhook_connections_and_send_flow() -> None:
    user = create_test_user(username='wh_user', email='wh_user@example.com')
    authed = login_as('wh_user')

    # --- connections CRUD ---
    resp = authed.post(
        '/api/v1/webhooks/connections',
        json={
            'name': 'My n8n',
            'url': 'https://n8n.internal.example.com/webhook/abc123',
            'secret': 'sk-test-123',
            'events': ['job.finished', 'job.failed'],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # URL is preserved verbatim, path/query included -- unlike OpenWebUI's
    # base_url this is never reshaped.
    assert body['url'] == 'https://n8n.internal.example.com/webhook/abc123'
    assert body['enabled'] is True
    assert body['events'] == ['job.finished', 'job.failed']
    assert body['has_secret'] is True
    assert 'secret' not in body
    connection_id = body['id']

    resp = authed.get('/api/v1/webhooks/connections')
    assert resp.status_code == 200
    assert [c['id'] for c in resp.json()['items']] == [connection_id]

    resp = authed.patch(f'/api/v1/webhooks/connections/{connection_id}', json={'name': 'Renamed'})
    assert resp.status_code == 200
    assert resp.json()['name'] == 'Renamed'

    # secret omitted entirely -> stored secret unchanged.
    resp = authed.patch(f'/api/v1/webhooks/connections/{connection_id}', json={'enabled': False})
    assert resp.status_code == 200, resp.text
    assert resp.json()['enabled'] is False
    assert resp.json()['has_secret'] is True
    db = TestingSessionLocal()
    try:
        connection = db.get(WebhookConnection, connection_id)
        assert security.decrypt_webhook_secret(connection.secret_encrypted) == 'sk-test-123'
    finally:
        db.close()

    # secret explicitly empty -> stored secret cleared (unlike OpenWebUI's
    # api_key, an empty value here means "remove it", not "keep it" -- see
    # WebhookConnectionUpdateRequest.secret's docstring).
    resp = authed.patch(f'/api/v1/webhooks/connections/{connection_id}', json={'secret': ''})
    assert resp.status_code == 200, resp.text
    assert resp.json()['has_secret'] is False
    db = TestingSessionLocal()
    try:
        connection = db.get(WebhookConnection, connection_id)
        assert connection.secret_encrypted is None
    finally:
        db.close()

    # A non-empty secret does rotate/restore the stored value; re-enable too.
    resp = authed.patch(
        f'/api/v1/webhooks/connections/{connection_id}', json={'secret': 'sk-rotated-456', 'enabled': True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()['has_secret'] is True
    assert resp.json()['enabled'] is True
    db = TestingSessionLocal()
    try:
        connection = db.get(WebhookConnection, connection_id)
        assert security.decrypt_webhook_secret(connection.secret_encrypted) == 'sk-rotated-456'
    finally:
        db.close()

    # Invalid events -> 422.
    resp = authed.post(
        '/api/v1/webhooks/connections',
        json={'name': 'Bad', 'url': 'https://example.com/hook', 'events': ['not.a.real.event']},
    )
    assert resp.status_code == 422

    # Empty events list -> 422.
    resp = authed.post(
        '/api/v1/webhooks/connections',
        json={'name': 'Bad', 'url': 'https://example.com/hook', 'events': []},
    )
    assert resp.status_code == 422

    # Non-http(s) scheme -> 422.
    resp = authed.post(
        '/api/v1/webhooks/connections',
        json={'name': 'Bad', 'url': 'ftp://example.com/hook', 'events': ['job.finished']},
    )
    assert resp.status_code == 422

    # Cross-user 404 (strictly owner-private, like OpenWebUIConnection; no
    # GET-by-id endpoint exists in the contract, only list/patch/delete/test,
    # so PATCH is what exercises _get_owned_connection here).
    create_test_user(username='wh_other', email='wh_other@example.com')
    other = login_as('wh_other')
    resp = other.patch(f'/api/v1/webhooks/connections/{connection_id}', json={'name': 'nope'})
    assert resp.status_code == 404

    # --- test endpoint: cooldown (429 + Retry-After) ---
    with patch('app.api.webhook_routes.send_webhook_request') as mock_send:
        mock_send.return_value = (200, None)
        resp = authed.post(f'/api/v1/webhooks/connections/{connection_id}/test')
        assert resp.status_code == 200, resp.text
        assert resp.json() == {'ok': True, 'detail': 'Delivered', 'http_status': 200}
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == 'https://n8n.internal.example.com/webhook/abc123'
        assert args[1]['event'] == 'test'
        assert args[2] == 'sk-rotated-456'
        assert kwargs['allowed_private_hosts'] == frozenset(settings.webhook_private_host_allowlist)

        resp = authed.post(f'/api/v1/webhooks/connections/{connection_id}/test')
        assert resp.status_code == 429, resp.text
        assert 'Retry-After' in resp.headers
        assert mock_send.call_count == 1  # cooldown short-circuited before calling the transport again

    # --- send: happy path (delivery created + enqueued) ---
    good_job_id = _make_finished_job(user.id)

    with patch('app.api.webhook_routes.celery_app') as mock_celery:
        resp = authed.post(
            '/api/v1/webhooks/send',
            json={'connection_id': connection_id, 'job_id': good_job_id},
        )
        assert resp.status_code == 201, resp.text
        item = resp.json()
        assert item['job_id'] == good_job_id
        assert item['connection_id'] == connection_id
        assert item['event'] == 'job.finished'
        assert item['status'] == 'pending'
        mock_celery.send_task.assert_called_once()
        assert mock_celery.send_task.call_args[0][0] == 'deliver_webhook'
        delivery_id = item['id']

    db = TestingSessionLocal()
    try:
        delivery = db.get(WebhookDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == 'pending'
        assert delivery.job_id == good_job_id
        assert delivery.connection_id == connection_id
        assert delivery.connection_name == 'Renamed'
        assert delivery.owner_id == user.id
    finally:
        db.close()

    # --- send: job not found -> 404 ---
    resp = authed.post(
        '/api/v1/webhooks/send', json={'connection_id': connection_id, 'job_id': 'does-not-exist'}
    )
    assert resp.status_code == 404

    # --- send: job not finished -> 409 ---
    db = TestingSessionLocal()
    try:
        pending_job = Job(
            original_filename='still-running.pdf', upload_path='/tmp/still-running.pdf',
            status=JobStatus.RUNNING, owner_id=user.id,
        )
        db.add(pending_job)
        db.commit()
        db.refresh(pending_job)
        pending_job_id = pending_job.id
    finally:
        db.close()
    resp = authed.post(
        '/api/v1/webhooks/send', json={'connection_id': connection_id, 'job_id': pending_job_id}
    )
    assert resp.status_code == 409

    # --- send: another user's job is invisible -> 404 ---
    stranger = create_test_user(username='wh_stranger', email='wh_stranger@example.com')
    stranger_job_id = _make_finished_job(stranger.id)
    resp = authed.post(
        '/api/v1/webhooks/send', json={'connection_id': connection_id, 'job_id': stranger_job_id}
    )
    assert resp.status_code == 404

    # --- send: disabled connection -> 409 ---
    authed.patch(f'/api/v1/webhooks/connections/{connection_id}', json={'enabled': False})
    resp = authed.post(
        '/api/v1/webhooks/send', json={'connection_id': connection_id, 'job_id': good_job_id}
    )
    assert resp.status_code == 409
    authed.patch(f'/api/v1/webhooks/connections/{connection_id}', json={'enabled': True})

    # --- GET /deliveries: owner-scoped ---
    resp = authed.get('/api/v1/webhooks/deliveries')
    assert resp.status_code == 200
    assert any(d['id'] == delivery_id for d in resp.json()['items'])

    resp = other.get('/api/v1/webhooks/deliveries')
    assert resp.status_code == 200
    assert resp.json()['items'] == []

    # --- delete connection: 204, delivery history stays queryable with
    # connection_id nulled (ORM nullifies it on flush -- same mechanism as
    # openwebui_routes.delete_openwebui_connection nulling
    # OpenWebUIPush.connection_id) ---
    resp = authed.delete(f'/api/v1/webhooks/connections/{connection_id}')
    assert resp.status_code == 204
    assert resp.content == b''

    db = TestingSessionLocal()
    try:
        connection = db.get(WebhookConnection, connection_id)
        assert connection is None
        delivery = db.get(WebhookDelivery, delivery_id)
        assert delivery is not None
        assert delivery.connection_id is None
        assert delivery.connection_name == 'Renamed'  # snapshot survives
    finally:
        db.close()


def test_webhook_pending_delivery_cap() -> None:
    user = create_test_user(username='wh_cap_user', email='wh_cap_user@example.com')
    authed = login_as('wh_cap_user')
    original_cap = settings.webhook_max_pending_deliveries_per_user
    settings.webhook_max_pending_deliveries_per_user = 1
    try:
        resp = authed.post(
            '/api/v1/webhooks/connections',
            json={'name': 'Cap test', 'url': 'https://example.com/hook', 'events': ['job.finished']},
        )
        connection_id = resp.json()['id']

        job_one = _make_finished_job(user.id, filename='one.pdf')
        job_two = _make_finished_job(user.id, filename='two.pdf')

        with patch('app.api.webhook_routes.celery_app'):
            resp = authed.post('/api/v1/webhooks/send', json={'connection_id': connection_id, 'job_id': job_one})
            assert resp.status_code == 201, resp.text

            resp = authed.post('/api/v1/webhooks/send', json={'connection_id': connection_id, 'job_id': job_two})
            assert resp.status_code == 409, resp.text
    finally:
        settings.webhook_max_pending_deliveries_per_user = original_cap


def test_webhook_kill_switch() -> None:
    create_test_user(username='wh_killswitch', email='wh_killswitch@example.com')
    authed = login_as('wh_killswitch')
    original = settings.webhooks_enabled
    settings.webhooks_enabled = False
    try:
        resp = authed.get('/api/v1/webhooks/connections')
        assert resp.status_code == 404
    finally:
        settings.webhooks_enabled = original
