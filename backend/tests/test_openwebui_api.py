"""OpenWebUI push API surface: connections CRUD + test cooldown, knowledge
listing, and the /pushes create/list flow (mixed valid/invalid job_ids,
owner-private connection visibility, kill-switch). Drives the real
TestClient/conftest wiring, same pattern as test_import_api.py; the
OpenWebUI service calls and the Celery dispatch are mocked out at the
app.api.openwebui_routes seam so no real network/broker is needed.
"""

import hashlib
from unittest.mock import patch

from app.core.config import settings
from app.models.models import Job, JobStatus, OpenWebUIConnection, OpenWebUIPush
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


def test_openwebui_connections_and_pushes_flow() -> None:
    user = create_test_user(username='owui_user', email='owui_user@example.com')
    authed = login_as('owui_user')

    # --- connections CRUD ---
    resp = authed.post(
        '/api/v1/openwebui/connections',
        json={'name': 'My OpenWebUI', 'base_url': 'https://owui.example.com/', 'api_key': 'sk-test-123'},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['base_url'] == 'https://owui.example.com'  # trailing slash stripped
    assert body['has_api_key'] is True
    assert 'api_key' not in body
    connection_id = body['id']

    resp = authed.get('/api/v1/openwebui/connections')
    assert resp.status_code == 200
    assert [c['id'] for c in resp.json()['items']] == [connection_id]

    resp = authed.patch(f'/api/v1/openwebui/connections/{connection_id}', json={'name': 'Renamed'})
    assert resp.status_code == 200
    assert resp.json()['name'] == 'Renamed'

    # api_key is write-only: a PATCH that omits it, or sends an empty
    # string, keeps the previously stored key rather than blanking it.
    resp = authed.patch(f'/api/v1/openwebui/connections/{connection_id}', json={'api_key': ''})
    assert resp.status_code == 200, resp.text
    assert resp.json()['has_api_key'] is True
    assert 'api_key' not in resp.json()
    db = TestingSessionLocal()
    try:
        connection = db.get(OpenWebUIConnection, connection_id)
        assert security.decrypt_openwebui_api_key(connection.api_key_encrypted) == 'sk-test-123'
    finally:
        db.close()

    # A non-empty api_key does rotate the stored value.
    resp = authed.patch(f'/api/v1/openwebui/connections/{connection_id}', json={'api_key': 'sk-rotated-456'})
    assert resp.status_code == 200, resp.text
    db = TestingSessionLocal()
    try:
        connection = db.get(OpenWebUIConnection, connection_id)
        assert security.decrypt_openwebui_api_key(connection.api_key_encrypted) == 'sk-rotated-456'
    finally:
        db.close()

    # Cross-user 404 (strictly owner-private, like ImportSource; no
    # GET-by-id endpoint exists in the contract, only list/patch/delete/test/
    # knowledge, so PATCH is what exercises _get_owned_connection here).
    create_test_user(username='owui_other', email='owui_other@example.com')
    other = login_as('owui_other')
    resp = other.patch(f'/api/v1/openwebui/connections/{connection_id}', json={'name': 'nope'})
    assert resp.status_code == 404

    # --- test endpoint: cooldown (429 + Retry-After) ---
    with patch('app.api.openwebui_routes.test_connection') as mock_test:
        mock_test.return_value = None
        resp = authed.post(f'/api/v1/openwebui/connections/{connection_id}/test')
        assert resp.status_code == 200, resp.text
        assert resp.json() == {'ok': True, 'detail': 'Connected'}
        mock_test.assert_called_once()
        _args, kwargs = mock_test.call_args
        assert kwargs['allowed_private_hosts'] == frozenset(settings.openwebui_private_host_allowlist)

        resp = authed.post(f'/api/v1/openwebui/connections/{connection_id}/test')
        assert resp.status_code == 429, resp.text
        assert 'Retry-After' in resp.headers
        assert mock_test.call_count == 1  # cooldown short-circuited before calling the service again

    # --- knowledge listing (live call, mocked) ---
    with patch('app.api.openwebui_routes.list_knowledge') as mock_list:
        mock_list.return_value = [{'id': 'kb1', 'name': 'Docs', 'description': None}]
        resp = authed.get(f'/api/v1/openwebui/connections/{connection_id}/knowledge')
        assert resp.status_code == 200, resp.text
        assert resp.json()['items'] == [{'id': 'kb1', 'name': 'Docs', 'description': None}]

    # --- pushes: mixed valid/invalid job_ids ---
    good_job_id = _make_finished_job(user.id)

    with patch('app.api.openwebui_routes.celery_app') as mock_celery:
        resp = authed.post(
            '/api/v1/openwebui/pushes',
            json={
                'connection_id': connection_id,
                'knowledge_id': 'kb1',
                'knowledge_name': 'Docs',
                'job_ids': [good_job_id, 'does-not-exist'],
            },
        )
        assert resp.status_code == 201, resp.text
        items = resp.json()['items']
        assert len(items) == 2
        by_job = {item['job_id']: item for item in items}
        assert by_job[good_job_id]['status'] == 'pending'
        assert by_job[good_job_id]['content_stale'] is False
        assert by_job['does-not-exist']['status'] == 'failed'
        assert by_job['does-not-exist']['error_message'] == 'Job not found'
        mock_celery.send_task.assert_called_once()
        assert mock_celery.send_task.call_args[0][0] == 'push_openwebui'

    push_id = by_job[good_job_id]['id']
    db = TestingSessionLocal()
    try:
        push = db.get(OpenWebUIPush, push_id)
        assert push is not None
        assert push.status == 'pending'
        assert push.job_id == good_job_id
        assert push.connection_id == connection_id
    finally:
        db.close()

    # --- all-invalid job_ids -> 400 ---
    resp = authed.post(
        '/api/v1/openwebui/pushes',
        json={'connection_id': connection_id, 'knowledge_id': 'kb1', 'knowledge_name': 'Docs', 'job_ids': ['nope']},
    )
    assert resp.status_code == 400, resp.text

    # --- GET /pushes ---
    resp = authed.get('/api/v1/openwebui/pushes')
    assert resp.status_code == 200
    assert any(item['id'] == push_id for item in resp.json()['items'])

    resp = authed.get(f'/api/v1/openwebui/pushes?job_id={good_job_id}')
    assert resp.status_code == 200
    ids = [item['id'] for item in resp.json()['items']]
    assert push_id in ids

    # A teammate-less other user cannot see this job -> 404 on the job_id filter.
    resp = other.get(f'/api/v1/openwebui/pushes?job_id={good_job_id}')
    assert resp.status_code == 404

    # --- delete connection: 204, push history stays queryable with
    # connection_id nulled (ORM nullifies it on flush -- same mechanism as
    # import_routes.delete_import_source nulling import_runs.source_id;
    # sqlite here runs without PRAGMA foreign_keys, so this isn't the DB's
    # ON DELETE SET NULL firing, it's SQLAlchemy's own unit-of-work) ---
    resp = authed.delete(f'/api/v1/openwebui/connections/{connection_id}')
    assert resp.status_code == 204
    assert resp.content == b''

    db = TestingSessionLocal()
    try:
        connection = db.get(OpenWebUIConnection, connection_id)
        assert connection is None
        push = db.get(OpenWebUIPush, push_id)
        assert push is not None
        assert push.connection_id is None
        assert push.connection_name == 'Renamed'  # snapshot survives
    finally:
        db.close()


def test_openwebui_push_content_stale_reflects_current_job_markdown() -> None:
    user = create_test_user(username='owui_stale_user', email='owui_stale_user@example.com')
    authed = login_as('owui_stale_user')
    job_id = _make_finished_job(user.id)

    db = TestingSessionLocal()
    try:
        connection = OpenWebUIConnection(
            owner_id=user.id, name='OWUI', base_url='https://owui.example.com',
            api_key_encrypted=security.encrypt_openwebui_api_key('sk-test'),
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)

        job = db.get(Job, job_id)
        content_sha256 = hashlib.sha256(job.result_markdown.encode('utf-8')).hexdigest()
        # A push that already finished, with the sha256 it pushed snapshotted
        # (see OpenWebUIPush's docstring) -- content_stale is derived from
        # this at read time, never stored as its own boolean.
        push = OpenWebUIPush(
            connection_id=connection.id, connection_name=connection.name, job_id=job_id,
            knowledge_id='kb1', knowledge_name='Docs', status='finished',
            openwebui_file_id='file-1', pushed_content_sha256=content_sha256, owner_id=user.id,
        )
        db.add(push)
        db.commit()
        db.refresh(push)
        push_id = push.id
    finally:
        db.close()

    resp = authed.get(f'/api/v1/openwebui/pushes?job_id={job_id}')
    assert resp.status_code == 200, resp.text
    item = next(i for i in resp.json()['items'] if i['id'] == push_id)
    assert item['content_stale'] is False  # pushed content still matches the job

    # Edit the job's markdown after the push -- the same push must now read
    # as stale, with no write to the push row itself.
    db = TestingSessionLocal()
    try:
        job = db.get(Job, job_id)
        job.result_markdown = job.result_markdown + '\nedited after push'
        db.commit()
    finally:
        db.close()

    resp = authed.get(f'/api/v1/openwebui/pushes?job_id={job_id}')
    assert resp.status_code == 200, resp.text
    item = next(i for i in resp.json()['items'] if i['id'] == push_id)
    assert item['content_stale'] is True


def test_openwebui_kill_switch() -> None:
    create_test_user(username='owui_killswitch', email='owui_killswitch@example.com')
    authed = login_as('owui_killswitch')
    original = settings.openwebui_enabled
    settings.openwebui_enabled = False
    try:
        resp = authed.get('/api/v1/openwebui/connections')
        assert resp.status_code == 404
    finally:
        settings.openwebui_enabled = original
