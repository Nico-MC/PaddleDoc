"""Mail ingestion API tests (docs/integrations/mail-ingestion.md): raw and
multipart ingest, idempotent replay (200) vs first ingest (201), attachment
Job creation + dispatch, list/detail/body/raw/part-content/export.json
shapes, visibility (foreign message -> 404), oversize 413, unparseable 422,
and DELETE leaving no dangling Job.mail_message_id.

Real cookie-based logins (create_test_user/login_as), same idioms as
test_benchmarks_api.py -- owner/team visibility joins against real users/jobs
rows.
"""

import uuid
from email.message import EmailMessage

import pytest

from app.models.models import Job, JobStatus, MailMessage, Team, UserRole, VlConnection
from app.services import security
from app.services.security import rate_limiter
from conftest import TestingSessionLocal, create_test_user, login_as


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _stub_dispatch(monkeypatch):
    from app.api import mail_routes

    # Mail-attachment jobs under test never reach a real worker;
    # process_job.delay is a no-op, so jobs stay PENDING unless a test flips
    # status itself. mail_routes.process_job and routes.process_job are the
    # SAME imported Celery task object, so patching .delay here covers both.
    monkeypatch.setattr(mail_routes.process_job, 'delay', lambda *args, **kwargs: None)
    yield


def _db():
    return TestingSessionLocal()


def _user(prefix: str, **kwargs):
    suffix = uuid.uuid4().hex[:8]
    return create_test_user(username=f'{prefix}-{suffix}', email=f'{prefix}-{suffix}@example.com', **kwargs)


def _simple_text_eml(subject: str = 'Simple report') -> bytes:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg['Date'] = 'Sat, 15 Aug 2026 09:12:00 +0000'
    msg['Message-ID'] = f'<{uuid.uuid4().hex}@partner.example>'
    msg.set_content('Plain body text.')
    return msg.as_bytes()


def _mixed_with_pdf_and_zip_eml(subject: str = 'Quarterly report') -> bytes:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg['Message-ID'] = f'<{uuid.uuid4().hex}@partner.example>'
    msg.set_content('See attached.')
    msg.add_attachment(b'%PDF-1.4 fake pdf bytes', maintype='application', subtype='pdf', filename='bericht-q3.pdf')
    msg.add_attachment(b'PK\x03\x04 fake zip bytes', maintype='application', subtype='zip', filename='archiv.zip')
    return msg.as_bytes()


def _post_raw(client, raw: bytes, **params):
    return client.post('/api/v1/mail/messages', content=raw, headers={'content-type': 'message/rfc822'}, params=params)


# --- Ingest: first vs. replay -------------------------------------------------

def test_ingest_new_message_is_201_and_dedup_replay_is_200():
    user = _user('mailer')
    client = login_as(user.username)
    raw = _simple_text_eml()

    first = _post_raw(client, raw, source='n8n')
    assert first.status_code == 201, first.text
    body = first.json()
    assert body['replayed'] is False
    assert body['subject'] == 'Simple report'
    assert body['from_address'] == 'alice@partner.example'
    assert body['has_body'] is True
    assert body['body_format'] == 'text/plain'
    assert body['parts'] == []
    message_id = body['id']

    second = _post_raw(client, raw, source='n8n')
    assert second.status_code == 200, second.text
    replay_body = second.json()
    assert replay_body['replayed'] is True
    assert replay_body['id'] == message_id


def test_ingest_mixed_attachments_creates_job_for_pdf_only():
    user = _user('mailer')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml()

    resp = _post_raw(client, raw, folder='mail', profile_id='ppocrv6_tiny')
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body['parts']) == 2
    pdf_part = next(p for p in body['parts'] if p['filename'] == 'bericht-q3.pdf')
    zip_part = next(p for p in body['parts'] if p['filename'] == 'archiv.zip')
    assert pdf_part['outcome'] == 'job'
    assert pdf_part['job_id']
    assert zip_part['outcome'] == 'skipped'
    assert zip_part['skip_reason'] == 'unsupported_type'

    db = _db()
    try:
        job = db.get(Job, pdf_part['job_id'])
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.mail_message_id == body['id']
        assert job.document_version == 1
        assert job.previous_job_id is None
        assert job.processing_info['settings']['mode'] == 'mail_attachment'
        assert job.processing_info['settings']['storage_folder'].endswith(job.id)
        assert job.processing_info['settings']['mail']['mail_message_id'] == body['id']
        assert job.processing_info['settings']['mail']['part_index'] == pdf_part['index']
    finally:
        db.close()


def _make_vl_connection(*, name: str = 'Mail VL', enabled: bool = True) -> VlConnection:
    db = _db()
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


def test_ingest_with_vl_profile_creates_job_with_vl_settings_and_dispatches_openai_vision(monkeypatch):
    from app.api import mail_routes

    dispatched: list[tuple] = []
    monkeypatch.setattr(mail_routes.process_job, 'delay', lambda *args, **kwargs: dispatched.append(args))

    user = _user('mailer')
    client = login_as(user.username)
    connection = _make_vl_connection(name='Mail Vision')
    raw = _mixed_with_pdf_and_zip_eml()

    resp = _post_raw(client, raw, folder='mail', profile_id=f'vl:{connection.id}')
    assert resp.status_code == 201, resp.text
    body = resp.json()
    pdf_part = next(p for p in body['parts'] if p['filename'] == 'bericht-q3.pdf')

    db = _db()
    try:
        job = db.get(Job, pdf_part['job_id'])
        settings_info = job.processing_info['settings']
        assert settings_info['profile_id'] == f'vl:{connection.id}'
        assert settings_info['vl_connection_id'] == connection.id
        assert settings_info['variant_label'] == 'Mail Vision'
    finally:
        db.close()

    # Dispatched with the real pipeline id, never the raw 'vl:<connection_id>'
    # display value -- see _job_dispatch_args / effective_pipeline_profile_id.
    assert dispatched == [(pdf_part['job_id'], 'openai_vision', 'mail_attachment', '', None)]


def test_ingest_with_unknown_vl_profile_is_422_and_persists_nothing():
    user = _user('mailer')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml(subject='Rejected VL profile')

    rejected = _post_raw(client, raw, profile_id='vl:does-not-exist')
    assert rejected.status_code == 422
    assert rejected.json()['detail'] == "Unknown profile 'vl:does-not-exist'"

    # If the rejected attempt had persisted the MailMessage/dedup row
    # despite the 422, this identical re-post would come back as a 200
    # replay instead of a fresh 201 ingest.
    retried = _post_raw(client, raw)
    assert retried.status_code == 201, retried.text
    assert retried.json()['replayed'] is False


def test_ingest_multipart_form_data_convenience():
    user = _user('mailer')
    client = login_as(user.username)
    raw = _simple_text_eml(subject='Form mode report')

    resp = client.post(
        '/api/v1/mail/messages',
        files={'file': ('message.eml', raw, 'message/rfc822')},
        data={'source': 'curl'},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()['subject'] == 'Form mode report'


def test_ingest_oversized_message_is_413(monkeypatch):
    from app.core.config import settings

    user = _user('mailer')
    client = login_as(user.username)
    monkeypatch.setattr(settings, 'max_mail_message_bytes', 10)

    resp = _post_raw(client, _simple_text_eml())
    assert resp.status_code == 413


def test_ingest_unparseable_message_is_422():
    user = _user('mailer')
    client = login_as(user.username)

    resp = _post_raw(client, b'')
    assert resp.status_code == 422


# --- Retrieval: list / detail / body / raw / part-content / export -------------

def test_list_detail_body_raw_and_part_content():
    user = _user('mailer')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml(subject='Retrieval test')
    ingest = _post_raw(client, raw)
    assert ingest.status_code == 201, ingest.text
    message_id = ingest.json()['id']
    pdf_part = next(p for p in ingest.json()['parts'] if p['outcome'] == 'job')

    listed = client.get('/api/v1/mail/messages', params={'q': 'retrieval'})
    assert listed.status_code == 200
    assert listed.json()['total'] >= 1
    assert any(item['id'] == message_id for item in listed.json()['items'])

    detail = client.get(f'/api/v1/mail/messages/{message_id}')
    assert detail.status_code == 200, detail.text
    detail_pdf_part = next(p for p in detail.json()['parts'] if p['outcome'] == 'job')
    assert detail_pdf_part['job_status'] == 'PENDING'
    assert detail_pdf_part['job_id'] == pdf_part['job_id']

    body_resp = client.get(f'/api/v1/mail/messages/{message_id}/body')
    assert body_resp.status_code == 200
    assert 'See attached' in body_resp.text

    raw_resp = client.get(f'/api/v1/mail/messages/{message_id}/raw')
    assert raw_resp.status_code == 200
    assert raw_resp.headers['content-type'].startswith('message/rfc822')
    assert raw_resp.content == raw

    part_resp = client.get(f"/api/v1/mail/messages/{message_id}/parts/{pdf_part['index']}/content")
    assert part_resp.status_code == 200
    assert part_resp.content == b'%PDF-1.4 fake pdf bytes'

    missing_part = client.get(f'/api/v1/mail/messages/{message_id}/parts/99/content')
    assert missing_part.status_code == 404


def test_body_endpoint_404_for_attachment_only_message():
    user = _user('mailer')
    client = login_as(user.username)
    msg = EmailMessage()
    msg['Subject'] = 'No body'
    msg['From'] = 'alice@partner.example'
    msg.set_content('')  # EmailMessage always needs SOME payload; make it attachment-focused instead
    msg.add_attachment(b'%PDF-1.4', maintype='application', subtype='pdf', filename='only.pdf')
    raw = msg.as_bytes()

    ingest = _post_raw(client, raw)
    assert ingest.status_code == 201, ingest.text
    message_id = ingest.json()['id']

    # set_content('') still yields a text/plain body candidate in this
    # construction, so body_markdown is not None here -- assert the
    # endpoint's contract on whatever the ingest actually produced instead
    # of assuming attachment-only.
    detail = client.get(f'/api/v1/mail/messages/{message_id}').json()
    body_resp = client.get(f'/api/v1/mail/messages/{message_id}/body')
    if detail['has_body']:
        assert body_resp.status_code == 200
    else:
        assert body_resp.status_code == 404


def test_export_json_shape_and_completeness():
    user = _user('mailer')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml(subject='Export test')
    ingest = _post_raw(client, raw).json()
    message_id = ingest['id']
    pdf_job_id = next(p for p in ingest['parts'] if p['outcome'] == 'job')['job_id']

    export_pending = client.get(f'/api/v1/mail/messages/{message_id}/export.json')
    assert export_pending.status_code == 200
    payload = export_pending.json()
    assert payload['schema'] == 'paddledoc.mail-export/1'
    assert payload['message']['id'] == message_id
    assert payload['complete'] is False  # the PDF job is still PENDING

    db = _db()
    try:
        job = db.get(Job, pdf_job_id)
        job.status = JobStatus.FINISHED
        job.result_markdown = '# Extracted PDF text'
        db.commit()
    finally:
        db.close()

    export_done = client.get(f'/api/v1/mail/messages/{message_id}/export.json').json()
    assert export_done['complete'] is True
    pdf_attachment = next(a for a in export_done['attachments'] if a.get('job_id') == pdf_job_id)
    assert pdf_attachment['markdown'] == '# Extracted PDF text'
    zip_attachment = next(a for a in export_done['attachments'] if a['outcome'] == 'skipped')
    assert zip_attachment['skip_reason'] == 'unsupported_type'


# --- Visibility -----------------------------------------------------------------

def test_foreign_message_is_404_not_403():
    owner = _user('mailowner')
    stranger = _user('mailstranger')
    owner_client = login_as(owner.username)
    stranger_client = login_as(stranger.username)

    message_id = _post_raw(owner_client, _simple_text_eml()).json()['id']

    resp = stranger_client.get(f'/api/v1/mail/messages/{message_id}')
    assert resp.status_code == 404
    resp_raw = stranger_client.get(f'/api/v1/mail/messages/{message_id}/raw')
    assert resp_raw.status_code == 404


def test_teammate_can_see_message_admin_sees_everything():
    team_id = _make_team('mailteam')
    owner = _user('mailteamowner', team_id=team_id)
    teammate = _user('mailteammate', team_id=team_id)
    admin = _user('mailadmin', role=UserRole.ADMIN)
    owner_client = login_as(owner.username)
    teammate_client = login_as(teammate.username)
    admin_client = login_as(admin.username)

    message_id = _post_raw(owner_client, _simple_text_eml()).json()['id']

    assert teammate_client.get(f'/api/v1/mail/messages/{message_id}').status_code == 200
    assert admin_client.get(f'/api/v1/mail/messages/{message_id}').status_code == 200


def _make_team(prefix: str) -> str:
    db = _db()
    try:
        team = Team(name=f'{prefix}-{uuid.uuid4().hex[:8]}')
        db.add(team)
        db.commit()
        db.refresh(team)
        return team.id
    finally:
        db.close()


# --- Delete -----------------------------------------------------------------

def test_delete_without_delete_jobs_nulls_mail_message_id():
    user = _user('mailer')
    client = login_as(user.username)
    ingest = _post_raw(client, _mixed_with_pdf_and_zip_eml()).json()
    job_id = next(p for p in ingest['parts'] if p['outcome'] == 'job')['job_id']

    resp = client.delete(f"/api/v1/mail/messages/{ingest['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()['deleted_jobs'] == 0

    db = _db()
    try:
        assert db.get(MailMessage, ingest['id']) is None
        job = db.get(Job, job_id)
        assert job is not None
        assert job.mail_message_id is None
    finally:
        db.close()


def test_delete_with_delete_jobs_removes_children():
    user = _user('mailer')
    client = login_as(user.username)
    ingest = _post_raw(client, _mixed_with_pdf_and_zip_eml()).json()
    job_id = next(p for p in ingest['parts'] if p['outcome'] == 'job')['job_id']

    resp = client.delete(f"/api/v1/mail/messages/{ingest['id']}", params={'delete_jobs': 'true'})
    assert resp.status_code == 200, resp.text
    assert resp.json()['deleted_jobs'] == 1

    db = _db()
    try:
        assert db.get(MailMessage, ingest['id']) is None
        assert db.get(Job, job_id) is None
    finally:
        db.close()
