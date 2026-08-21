"""POST /api/v1/jobs/{job_id}/restart with an optional profile_id override.

Companion to the plain-restart coverage in test_api.py and the import-page
restart guard in test_import_api.py -- this file focuses solely on the
profile_id body parameter added to the restart endpoint.
"""

import pytest

from app.api.deps import get_current_user
from app.main import app
from app.models.models import Job, JobStatus, User, UserRole, VlConnection
from app.services import security
from app.services.security import rate_limiter
from conftest import TestingSessionLocal, client

# Every module in this suite shares one Redis-backed rate-limit bucket keyed
# by client id, and TestClient always presents as "testclient" -- without a
# reset here, unrelated requests from earlier test files (or earlier tests
# in this one) can push a later /restart call over the 60-req/min ceiling
# and 429 it. See test_job_authz.py / test_benchmarks_api.py for the same
# pattern.
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield


_TEST_ADMIN_USER = User(
    id='test-admin-bypass',
    username='test-admin-bypass',
    email='test-admin-bypass@example.com',
    role=UserRole.ADMIN,
    is_active=True,
)


def _ensure_bypass_user_row() -> None:
    with TestingSessionLocal() as db:
        if db.get(User, _TEST_ADMIN_USER.id) is None:
            db.add(
                User(
                    id=_TEST_ADMIN_USER.id,
                    username=_TEST_ADMIN_USER.username,
                    email=_TEST_ADMIN_USER.email,
                    role=UserRole.ADMIN,
                    is_active=True,
                )
            )
            db.commit()


@pytest.fixture(autouse=True)
def _bypass_auth():
    _ensure_bypass_user_row()
    app.dependency_overrides[get_current_user] = lambda: _TEST_ADMIN_USER
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _make_job(job_id: str, tmp_path, *, mode: str = 'single', profile_id: str = 'ppocrv6_small') -> Job:
    db = TestingSessionLocal()
    job = Job(
        id=job_id,
        original_filename='scan.pdf',
        upload_path=str(tmp_path / f'{job_id}.pdf'),
        upload_content=b'x',
        upload_mime_type='application/pdf',
        upload_size_bytes=1,
        status=JobStatus.FAILED,
        processing_info={
            'settings': {
                'profile_id': profile_id,
                'mode': mode,
                'email': 'user@example.com',
                'department': 'legal',
            }
        },
    )
    db.add(job)
    db.commit()
    db.close()
    return job


def test_restart_without_body_keeps_existing_behavior(monkeypatch, tmp_path):
    from app.api import routes

    _make_job('restart-no-body', tmp_path, profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post('/api/v1/jobs/restart-no-body/restart')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'queued'
    assert body['profile_id'] == 'ppocrv6_small'
    assert delayed == [('restart-no-body', 'ppocrv6_small', 'single', 'user@example.com', 'legal')]

    db = TestingSessionLocal()
    try:
        row = db.get(Job, 'restart-no-body')
        assert row.status == JobStatus.PENDING
        settings_info = row.processing_info['settings']
        # No profile override was requested -- the stored settings block
        # must be untouched (no previous_profile_id/requested_profile_id).
        assert settings_info == {
            'profile_id': 'ppocrv6_small',
            'mode': 'single',
            'email': 'user@example.com',
            'department': 'legal',
        }
        assert row.processing_info['execution']['detail'] == 'Job was manually restarted from the jobs list.'
    finally:
        db.close()


def test_restart_with_profile_id_persists_and_enqueues_new_profile(monkeypatch, tmp_path):
    from app.api import routes

    _make_job('restart-with-profile', tmp_path, profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-with-profile/restart',
        json={'profile_id': 'ppocrv6_medium'},
    )
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'queued'
    assert body['profile_id'] == 'ppocrv6_medium'
    assert delayed == [('restart-with-profile', 'ppocrv6_medium', 'single', 'user@example.com', 'legal')]

    db = TestingSessionLocal()
    try:
        row = db.get(Job, 'restart-with-profile')
        assert row.status == JobStatus.PENDING
        settings_info = row.processing_info['settings']
        assert settings_info['previous_profile_id'] == 'ppocrv6_small'
        assert settings_info['requested_profile_id'] == 'ppocrv6_medium'
        assert settings_info['profile_id'] == 'ppocrv6_medium'
        assert row.processing_info['execution']['detail'] == (
            'Job was manually restarted with profile ppocrv6_medium (previous: ppocrv6_small).'
        )
    finally:
        db.close()


def test_restart_with_unknown_profile_id_is_rejected_and_job_untouched(monkeypatch, tmp_path):
    from app.api import routes

    _make_job('restart-unknown-profile', tmp_path, profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-unknown-profile/restart',
        json={'profile_id': 'not-a-real-profile'},
    )
    assert response.status_code == 422
    assert response.json()['detail'] == "Unknown profile 'not-a-real-profile'"
    assert delayed == []

    db = TestingSessionLocal()
    try:
        row = db.get(Job, 'restart-unknown-profile')
        # Job must be left exactly as it was -- still FAILED, original profile.
        assert row.status == JobStatus.FAILED
        assert row.processing_info['settings']['profile_id'] == 'ppocrv6_small'
        assert 'previous_profile_id' not in row.processing_info['settings']
    finally:
        db.close()


def test_restart_import_page_job_still_409_with_profile_body(monkeypatch, tmp_path):
    from app.api import routes

    _make_job('restart-import-page', tmp_path, mode='import', profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-import-page/restart',
        json={'profile_id': 'ppocrv6_medium'},
    )
    assert response.status_code == 409
    assert 'Imported pages cannot be restarted' in response.json()['detail']
    assert delayed == []

    db = TestingSessionLocal()
    try:
        row = db.get(Job, 'restart-import-page')
        assert row.status == JobStatus.FAILED
        assert row.processing_info['settings']['profile_id'] == 'ppocrv6_small'
    finally:
        db.close()


# --- 'vl:<connection_id>' profile switching (dynamic capabilities) -------------


def _make_vl_connection(*, name: str = 'Restart VL', enabled: bool = True) -> VlConnection:
    db = TestingSessionLocal()
    try:
        connection = VlConnection(
            name=name,
            base_url='https://vl.example.com',
            model='vl-model',
            api_key_encrypted=security.encrypt_vl_api_key('secret-key'),
            system_prompt='',
            enabled=enabled,
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)
        db.expunge(connection)
        return connection
    finally:
        db.close()


def test_restart_static_to_vl_sets_vl_settings_and_dispatches_openai_vision(monkeypatch, tmp_path):
    from app.api import routes

    connection = _make_vl_connection(name='Prod Vision')
    _make_job('restart-static-to-vl', tmp_path, profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-static-to-vl/restart',
        json={'profile_id': f'vl:{connection.id}'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # The response/display value stays the 'vl:<connection_id>' selection...
    assert body['profile_id'] == f'vl:{connection.id}'
    # ...but the Celery dispatch gets the real pipeline id, matching the
    # benchmark 'vl' variant's profile_id (see effective_pipeline_profile_id).
    assert delayed == [('restart-static-to-vl', 'openai_vision', 'single', 'user@example.com', 'legal')]

    db = TestingSessionLocal()
    try:
        settings_info = db.get(Job, 'restart-static-to-vl').processing_info['settings']
        assert settings_info['profile_id'] == f'vl:{connection.id}'
        assert settings_info['vl_connection_id'] == connection.id
        assert settings_info['variant_label'] == 'Prod Vision'
        assert settings_info['previous_profile_id'] == 'ppocrv6_small'
    finally:
        db.close()


def test_restart_vl_to_static_removes_vl_settings(monkeypatch, tmp_path):
    from app.api import routes

    connection = _make_vl_connection()
    _make_job('restart-vl-to-static', tmp_path, profile_id=f'vl:{connection.id}')
    # Seed the settings the vl: selection would already carry (as if it had
    # been chosen at upload time), so the switch-away cleanup is exercised.
    db = TestingSessionLocal()
    try:
        row = db.get(Job, 'restart-vl-to-static')
        row.processing_info = {
            'settings': {
                **row.processing_info['settings'],
                'vl_connection_id': connection.id,
                'variant_label': connection.name,
            }
        }
        db.commit()
    finally:
        db.close()

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-vl-to-static/restart',
        json={'profile_id': 'ppocrv6_medium'},
    )
    assert response.status_code == 200, response.text
    assert delayed == [('restart-vl-to-static', 'ppocrv6_medium', 'single', 'user@example.com', 'legal')]

    db = TestingSessionLocal()
    try:
        settings_info = db.get(Job, 'restart-vl-to-static').processing_info['settings']
        assert settings_info['profile_id'] == 'ppocrv6_medium'
        assert 'vl_connection_id' not in settings_info
        assert 'variant_label' not in settings_info
    finally:
        db.close()


def test_restart_with_unknown_vl_profile_is_rejected_and_job_untouched(monkeypatch, tmp_path):
    from app.api import routes

    _make_job('restart-unknown-vl', tmp_path, profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-unknown-vl/restart',
        json={'profile_id': 'vl:does-not-exist'},
    )
    assert response.status_code == 422
    assert response.json()['detail'] == "Unknown profile 'vl:does-not-exist'"
    assert delayed == []

    db = TestingSessionLocal()
    try:
        row = db.get(Job, 'restart-unknown-vl')
        assert row.status == JobStatus.FAILED
        assert row.processing_info['settings']['profile_id'] == 'ppocrv6_small'
    finally:
        db.close()


def test_restart_with_disabled_vl_profile_is_rejected(monkeypatch, tmp_path):
    from app.api import routes

    disabled = _make_vl_connection(enabled=False)
    _make_job('restart-disabled-vl', tmp_path, profile_id='ppocrv6_small')

    delayed: list[tuple] = []
    monkeypatch.setattr(routes.process_job, 'delay', lambda *args: delayed.append(args))

    response = client.post(
        '/api/v1/jobs/restart-disabled-vl/restart',
        json={'profile_id': f'vl:{disabled.id}'},
    )
    assert response.status_code == 422
    assert response.json()['detail'] == f"Unknown profile 'vl:{disabled.id}'"
    assert delayed == []
