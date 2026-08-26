"""Comprehensive API-level mail-ingestion tests (docs/integrations/mail-ingestion.md).

Complements test_mail_routes_api.py (shape / happy-path coverage) with the
scenarios the design doc's own test checklist calls out that weren't yet
exercised at the API layer: the idempotent-replay job re-dispatch (mocked
Celery dispatch, asserted by call count/args -- not just the 200/201 status),
the concurrent-duplicate IntegrityError race, multipart/signed (S/MIME) and
multipart/related ingested through the real endpoint (not just the pure
parser in test_mail_ingest_service.py), attachment-only/body-only messages,
mid-stream 413 abort on both the raw-stream and multipart branches, download
header + byte-fidelity checks on /raw and /parts/{index}/content, list
filters (message_id/sha256/source) + real pagination total, export.json's
completeness states (pending/all-terminal/FAILED error_message/body-only),
and DELETE's dangling-FK guarantee on SQLite (queried via raw SQL, since this
whole suite runs without PRAGMA foreign_keys and the FK's ON DELETE SET NULL
is inert here).

Real cookie-based logins (create_test_user/login_as), same idioms as
test_mail_routes_api.py / test_benchmarks_api.py -- owner/team visibility
joins against real users/jobs rows.
"""

import base64
import uuid
from email.message import EmailMessage

import pytest
from sqlalchemy import select, text

from app.models.models import Job, JobStatus, MailMessage, Team, UserRole
from app.services.mail_ingest import compute_content_sha256
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
    # process_job.delay is a no-op by default, so jobs stay PENDING unless a
    # test flips status itself. mail_routes.process_job and routes.process_job
    # are the SAME imported Celery task object, so patching .delay here
    # covers both. Individual tests below re-patch this (same monkeypatch
    # fixture instance, function-scoped) to capture calls when they need to
    # assert dispatch actually happened.
    monkeypatch.setattr(mail_routes.process_job, 'delay', lambda *args, **kwargs: None)
    yield


def _db():
    return TestingSessionLocal()


def _user(prefix: str, **kwargs):
    suffix = uuid.uuid4().hex[:8]
    return create_test_user(username=f'{prefix}-{suffix}', email=f'{prefix}-{suffix}@example.com', **kwargs)


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


def _mark_job(job_id: str, *, status: JobStatus, result_markdown: str | None = None, error_message: str | None = None) -> None:
    db = _db()
    try:
        job = db.get(Job, job_id)
        job.status = status
        job.result_markdown = result_markdown
        job.error_message = error_message
        db.commit()
    finally:
        db.close()


def _post_raw(client, raw: bytes, **params):
    return client.post('/api/v1/mail/messages', content=raw, headers={'content-type': 'message/rfc822'}, params=params)


# --- fixtures (small, self-contained -- mirrors test_mail_ingest_service.py's) ---

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


def _two_pdf_attachments_eml(subject: str = 'Two attachments') -> bytes:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('See attached.')
    msg.add_attachment(b'%PDF-1.4 first pdf', maintype='application', subtype='pdf', filename='first.pdf')
    msg.add_attachment(b'%PDF-1.4 second pdf', maintype='application', subtype='pdf', filename='second.pdf')
    return msg.as_bytes()


def _signed_mixed_with_pdf_eml() -> bytes:
    """Hand-built multipart/signed (S/MIME) wrapping a multipart/mixed body +
    PDF, plus the detached pkcs7 signature -- same shape as
    test_mail_ingest_service.py's fixture, exercised here through the real
    ingest endpoint instead of the bare parser."""
    inner = (
        'Content-Type: multipart/mixed; boundary="innerBoundary"\r\n'
        '\r\n'
        '--innerBoundary\r\n'
        'Content-Type: text/plain; charset="utf-8"\r\n'
        'Content-Transfer-Encoding: 7bit\r\n'
        '\r\n'
        'Signed body text.\r\n'
        '\r\n'
        '--innerBoundary\r\n'
        'Content-Type: application/pdf\r\n'
        'Content-Transfer-Encoding: base64\r\n'
        'Content-Disposition: attachment; filename="contract.pdf"\r\n'
        '\r\n' + base64.b64encode(b'%PDF-1.4 fake pdf bytes').decode() + '\r\n'
        '--innerBoundary--\r\n'
    )
    signature = base64.b64encode(b'fake-signature-bytes').decode()
    raw = (
        'Subject: Signed report\r\n'
        'From: alice@partner.example\r\n'
        'To: billing@firma.example\r\n'
        f'Message-ID: <{uuid.uuid4().hex}@partner.example>\r\n'
        'MIME-Version: 1.0\r\n'
        'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
        'micalg=sha-256; boundary="outerBoundary"\r\n'
        '\r\n'
        '--outerBoundary\r\n' + inner + '--outerBoundary\r\n'
        'Content-Type: application/pkcs7-signature; name="smime.p7s"\r\n'
        'Content-Transfer-Encoding: base64\r\n'
        'Content-Disposition: attachment; filename="smime.p7s"\r\n'
        '\r\n' + signature + '\r\n'
        '--outerBoundary--\r\n'
    )
    return raw.encode('utf-8')


def _related_inline_png_eml() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'Newsletter'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('<p>See <img src="cid:logo123"></p>', subtype='html')
    msg.add_related(
        b'\x89PNG fake bytes', maintype='image', subtype='png', filename='logo.png', cid='<logo123>', disposition='inline'
    )
    return msg.as_bytes()


def _attachment_only_eml() -> bytes:
    msg = EmailMessage()
    msg['Subject'] = 'Attachment only'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.make_mixed()
    msg.add_attachment(b'%PDF-1.4 fake pdf bytes', maintype='application', subtype='pdf', filename='doc.pdf')
    return msg.as_bytes()


# --- Idempotent replay: no duplicate rows + PENDING re-dispatch ------------------

def test_replay_creates_no_duplicate_row_and_redispatches_pending_jobs(monkeypatch):
    from app.api import mail_routes

    delayed: list[tuple] = []
    monkeypatch.setattr(mail_routes.process_job, 'delay', lambda *args, **kwargs: delayed.append(args))

    user = _user('mailreplay')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml()

    first = _post_raw(client, raw)
    assert first.status_code == 201, first.text
    message_id = first.json()['id']
    job_id = next(p for p in first.json()['parts'] if p['outcome'] == 'job')['job_id']
    assert [args[0] for args in delayed] == [job_id]  # dispatched exactly once on first ingest

    # No live worker in tests, so the job is still PENDING -- the replay must
    # re-dispatch it (crash-window recovery, design doc step 6).
    second = _post_raw(client, raw)
    assert second.status_code == 200, second.text
    replay_body = second.json()
    assert replay_body['replayed'] is True
    assert replay_body['id'] == message_id
    assert [args[0] for args in delayed] == [job_id, job_id]  # re-dispatched, same job, not a new one

    db = _db()
    try:
        rows = db.scalars(
            select(MailMessage).where(MailMessage.owner_id == user.id, MailMessage.content_sha256 == compute_content_sha256(raw))
        ).all()
        assert len(rows) == 1
        jobs = db.scalars(select(Job).where(Job.mail_message_id == message_id)).all()
        assert len(jobs) == 1  # replay created no second child job either
    finally:
        db.close()


def test_replay_does_not_redispatch_already_terminal_jobs(monkeypatch):
    from app.api import mail_routes

    delayed: list[tuple] = []
    monkeypatch.setattr(mail_routes.process_job, 'delay', lambda *args, **kwargs: delayed.append(args))

    user = _user('mailreplayterm')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml()

    first = _post_raw(client, raw)
    job_id = next(p for p in first.json()['parts'] if p['outcome'] == 'job')['job_id']
    _mark_job(job_id, status=JobStatus.FINISHED, result_markdown='# done')
    delayed.clear()

    second = _post_raw(client, raw)
    assert second.status_code == 200, second.text
    assert second.json()['replayed'] is True
    assert delayed == []  # an already-terminal job must never be redispatched


def test_concurrent_duplicate_integrity_error_falls_back_to_replay(monkeypatch):
    """Simulates the design doc's concurrent-duplicate race: another request
    commits a colliding (owner_id, content_sha256) row after this request's
    own dedup SELECT already missed. The route's db.commit() must hit the
    real UniqueConstraint, roll back, re-fetch, and return the winner as a
    replay -- not error out and not leave two rows behind."""
    from app.api import mail_routes

    user = _user('mailrace')
    client = login_as(user.username)
    raw = _simple_text_eml()
    sha = compute_content_sha256(raw)

    db = _db()
    winner_id = str(uuid.uuid4())
    try:
        winner = MailMessage(
            id=winner_id,
            owner_id=user.id,
            content_sha256=sha,
            subject='Winner of the race',
            raw_content=raw,
            raw_size_bytes=len(raw),
            parts=[],
        )
        db.add(winner)
        db.commit()
    finally:
        db.close()

    original = mail_routes._find_visible_mail_by_hash
    calls = {'n': 0}

    def _flaky(db_, user_, content_sha256):
        calls['n'] += 1
        if calls['n'] == 1:
            return None  # force a miss so the handler proceeds to build + commit a colliding row
        return original(db_, user_, content_sha256)

    monkeypatch.setattr(mail_routes, '_find_visible_mail_by_hash', _flaky)

    resp = _post_raw(client, raw)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['replayed'] is True
    assert body['id'] == winner_id  # falls back to the row that actually won the race

    assert calls['n'] == 2  # the forced miss, then the post-rollback re-fetch

    db = _db()
    try:
        rows = db.scalars(select(MailMessage).where(MailMessage.owner_id == user.id, MailMessage.content_sha256 == sha)).all()
        assert len(rows) == 1  # this request's own insert was rolled back, not left behind
        assert rows[0].id == winner_id
    finally:
        db.close()


def test_ingest_with_new_tag_and_multiple_attachments_does_not_false_409():
    """Regression: `_attach_tags` is called once per attachment Job, all
    inside one uncommitted transaction (autoflush=False). Without a flush
    after creating a brand-new Tag, the second attachment's call can't see
    the first's still-pending Tag insert and creates a duplicate Tag row
    with the same (unique) name -- an IntegrityError at commit that used to
    be misdiagnosed as the message-hash dedup race and turned into a false
    409, permanently losing a genuinely new message (retrying identical
    bytes+tags failed identically every time)."""
    from app.models.models import Tag

    user = _user('mailtagrace')
    client = login_as(user.username)
    raw = _two_pdf_attachments_eml()
    fresh_tag = f'brandnewtag-{uuid.uuid4().hex[:8]}'

    resp = _post_raw(client, raw, tags=f'{fresh_tag},{fresh_tag}')
    assert resp.status_code == 201, resp.text
    body = resp.json()
    job_ids = [p['job_id'] for p in body['parts'] if p['outcome'] == 'job']
    assert len(job_ids) == 2

    db = _db()
    try:
        tag_rows = db.scalars(select(Tag).where(Tag.name == fresh_tag)).all()
        assert len(tag_rows) == 1  # no duplicate Tag row from the per-attachment loop
        jobs = db.scalars(select(Job).where(Job.id.in_(job_ids))).all()
        assert len(jobs) == 2
        for job in jobs:
            assert [t.name for t in job.tags] == [fresh_tag]
    finally:
        db.close()


# --- Mail-attachment jobs must never become a version-chain predecessor --------

def test_unrelated_upload_does_not_version_chain_onto_mail_attachment_job(monkeypatch, tmp_path):
    """Regression: `_find_predecessor_job` (routes.py) must exclude
    mail-attachment jobs from candidacy. Without that exclusion, an ordinary
    `/upload` of an unrelated file that happens to share a filename with a
    mail attachment gets silently stamped as document_version=2 /
    previous_job_id=<the mail job> -- chaining an unrelated sender's
    attachment into an unrelated document's version history, exactly what
    the design doc's version-chain-bypass rationale says must not happen."""
    from app.api import routes

    routes.settings.uploads_dir = tmp_path / 'uploads'
    routes.settings.results_dir = tmp_path / 'results'

    user = _user('mailversionchain')
    client = login_as(user.username)

    msg = EmailMessage()
    msg['Subject'] = 'Invoice from partner'
    msg['From'] = 'alice@partner.example'
    msg['To'] = 'billing@firma.example'
    msg.set_content('See attached.')
    msg.add_attachment(b'%PDF-1.4 mail attachment bytes', maintype='application', subtype='pdf', filename='invoice.pdf')
    mail_resp = _post_raw(client, msg.as_bytes())
    assert mail_resp.status_code == 201, mail_resp.text
    mail_job_id = next(p['job_id'] for p in mail_resp.json()['parts'] if p['outcome'] == 'job')

    db = _db()
    try:
        mail_job = db.get(Job, mail_job_id)
        assert mail_job.document_version == 1
        assert mail_job.previous_job_id is None
    finally:
        db.close()

    upload_resp = client.post(
        '/api/v1/upload',
        files={'file': ('invoice.pdf', b'%PDF-1.4 completely unrelated upload bytes', 'application/pdf')},
        data={'profile_id': 'ppocrv6_tiny'},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    upload_job_id = upload_resp.json()['job_id']
    assert upload_job_id != mail_job_id

    upload_detail = client.get(f'/api/v1/jobs/{upload_job_id}').json()
    assert upload_detail['document_version'] == 1
    assert upload_detail['previous_job_id'] is None


# --- S/MIME and inline-image classification through the real endpoint -----------

def test_ingest_signed_message_finds_inner_pdf_not_signature():
    user = _user('mailsigned')
    client = login_as(user.username)
    raw = _signed_mixed_with_pdf_eml()

    resp = _post_raw(client, raw)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['body_format'] == 'text/plain'
    outcomes = {p['filename']: p['outcome'] for p in body['parts']}
    assert outcomes['contract.pdf'] == 'job'
    assert outcomes['smime.p7s'] == 'skipped'

    pdf_part = next(p for p in body['parts'] if p['filename'] == 'contract.pdf')
    assert pdf_part['job_id']

    db = _db()
    try:
        job = db.get(Job, pdf_part['job_id'])
        assert job is not None
        assert job.upload_content == b'%PDF-1.4 fake pdf bytes'
        assert job.status == JobStatus.PENDING
        assert job.mail_message_id == body['id']
    finally:
        db.close()

    part_resp = client.get(f"/api/v1/mail/messages/{body['id']}/parts/{pdf_part['index']}/content")
    assert part_resp.status_code == 200
    assert part_resp.content == b'%PDF-1.4 fake pdf bytes'


def test_ingest_related_inline_png_is_inline_not_job():
    user = _user('mailinline')
    client = login_as(user.username)
    raw = _related_inline_png_eml()

    resp = _post_raw(client, raw)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['body_format'] == 'text/html'
    assert len(body['parts']) == 1
    png_part = body['parts'][0]
    assert png_part['outcome'] == 'inline'
    assert png_part['filename'] == 'logo.png'
    assert png_part['job_id'] is None

    db = _db()
    try:
        assert db.scalars(select(Job).where(Job.mail_message_id == body['id'])).all() == []  # no OCR job for inline
    finally:
        db.close()

    body_resp = client.get(f"/api/v1/mail/messages/{body['id']}/body")
    assert body_resp.status_code == 200
    assert '![inline attachment: logo.png]' in body_resp.text
    assert 'cid:' not in body_resp.text

    part_resp = client.get(f"/api/v1/mail/messages/{body['id']}/parts/0/content")
    assert part_resp.status_code == 200
    assert part_resp.content == b'\x89PNG fake bytes'
    assert part_resp.headers['content-type'].startswith('image/png')


# --- Body-only / attachment-only messages ----------------------------------------

def test_ingest_body_only_message_has_no_parts_and_body_is_fetchable():
    user = _user('mailbodyonly')
    client = login_as(user.username)
    raw = _simple_text_eml(subject='Body only report')

    resp = _post_raw(client, raw)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['parts'] == []
    assert body['has_body'] is True

    body_resp = client.get(f"/api/v1/mail/messages/{body['id']}/body")
    assert body_resp.status_code == 200
    assert 'Plain body text' in body_resp.text


def test_ingest_attachment_only_message_has_no_body():
    user = _user('mailattachonly')
    client = login_as(user.username)
    raw = _attachment_only_eml()

    resp = _post_raw(client, raw)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['has_body'] is False
    assert body['body_format'] is None
    assert len(body['parts']) == 1
    assert body['parts'][0]['outcome'] == 'job'

    body_resp = client.get(f"/api/v1/mail/messages/{body['id']}/body")
    assert body_resp.status_code == 404


# --- Oversize / unparseable ingest -----------------------------------------------

def test_ingest_oversized_raw_stream_is_413_and_stores_nothing(monkeypatch):
    from app.core.config import settings

    user = _user('mailoversize')
    client = login_as(user.username)
    monkeypatch.setattr(settings, 'max_mail_message_bytes', 200)

    raw = _mixed_with_pdf_and_zip_eml()
    assert len(raw) > 200  # comfortably over the lowered cap once base64 attachments are included

    resp = _post_raw(client, raw)
    assert resp.status_code == 413

    db = _db()
    try:
        assert db.scalars(select(MailMessage).where(MailMessage.owner_id == user.id)).all() == []
        assert db.scalars(select(Job).where(Job.owner_id == user.id)).all() == []
    finally:
        db.close()


def test_ingest_oversized_multipart_form_is_413_on_declared_content_length(monkeypatch):
    from app.core.config import settings

    user = _user('mailoversizeform')
    client = login_as(user.username)
    monkeypatch.setattr(settings, 'max_mail_message_bytes', 200)

    raw = _mixed_with_pdf_and_zip_eml()
    resp = client.post('/api/v1/mail/messages', files={'file': ('message.eml', raw, 'message/rfc822')})
    assert resp.status_code == 413

    db = _db()
    try:
        assert db.scalars(select(MailMessage).where(MailMessage.owner_id == user.id)).all() == []
    finally:
        db.close()


def test_ingest_unparseable_message_is_422_and_stores_nothing():
    user = _user('mailbad')
    client = login_as(user.username)

    resp = _post_raw(client, b'')
    assert resp.status_code == 422

    db = _db()
    try:
        assert db.scalars(select(MailMessage).where(MailMessage.owner_id == user.id)).all() == []
    finally:
        db.close()


# --- Download fidelity: /raw and /parts/{index}/content --------------------------

def test_raw_and_part_content_headers_and_byte_fidelity():
    user = _user('maildownload')
    client = login_as(user.username)
    raw = _mixed_with_pdf_and_zip_eml(subject='Header check')
    ingest = _post_raw(client, raw)
    assert ingest.status_code == 201, ingest.text
    payload = ingest.json()
    message_id = payload['id']
    pdf_part = next(p for p in payload['parts'] if p['outcome'] == 'job')
    zip_part = next(p for p in payload['parts'] if p['outcome'] == 'skipped')

    raw_resp = client.get(f'/api/v1/mail/messages/{message_id}/raw')
    assert raw_resp.status_code == 200
    assert raw_resp.content == raw  # byte-identical to what was POSTed
    assert raw_resp.headers['content-type'].startswith('message/rfc822')
    assert raw_resp.headers['x-content-type-options'] == 'nosniff'
    assert raw_resp.headers['cache-control'] == 'private, max-age=3600'
    assert 'attachment' in raw_resp.headers['content-disposition']
    assert f'{message_id}.eml' in raw_resp.headers['content-disposition']

    part_resp = client.get(f"/api/v1/mail/messages/{message_id}/parts/{pdf_part['index']}/content")
    assert part_resp.status_code == 200
    assert part_resp.content == b'%PDF-1.4 fake pdf bytes'
    assert part_resp.headers['content-type'].startswith('application/pdf')
    assert part_resp.headers['x-content-type-options'] == 'nosniff'
    assert part_resp.headers['cache-control'] == 'private, max-age=3600'
    assert 'attachment' in part_resp.headers['content-disposition']
    assert 'bericht-q3.pdf' in part_resp.headers['content-disposition']

    # Skipped parts stay downloadable too (design doc: "incl. inline and skipped ones").
    zip_resp = client.get(f"/api/v1/mail/messages/{message_id}/parts/{zip_part['index']}/content")
    assert zip_resp.status_code == 200
    assert zip_resp.content == b'PK\x03\x04 fake zip bytes'

    missing_resp = client.get(f'/api/v1/mail/messages/{message_id}/parts/999/content')
    assert missing_resp.status_code == 404


# --- Visibility: list + every retrieval endpoint, team/foreign/admin ------------

def test_list_and_retrieval_visibility_across_owner_teammate_stranger_admin():
    team_id = _make_team('mailvis')
    owner = _user('mailvisowner', team_id=team_id)
    teammate = _user('mailvisteammate', team_id=team_id)
    stranger = _user('mailvisstranger')
    admin = _user('mailvisadmin', role=UserRole.ADMIN)

    owner_client = login_as(owner.username)
    teammate_client = login_as(teammate.username)
    stranger_client = login_as(stranger.username)
    admin_client = login_as(admin.username)

    subject = f'Visibility probe {uuid.uuid4().hex[:8]}'
    ingest = _post_raw(owner_client, _mixed_with_pdf_and_zip_eml(subject=subject))
    assert ingest.status_code == 201, ingest.text
    message_id = ingest.json()['id']

    def _listed_ids(client):
        return {item['id'] for item in client.get('/api/v1/mail/messages', params={'q': subject}).json()['items']}

    assert message_id in _listed_ids(owner_client)
    assert message_id in _listed_ids(teammate_client)
    assert message_id not in _listed_ids(stranger_client)
    assert message_id in _listed_ids(admin_client)

    assert teammate_client.get(f'/api/v1/mail/messages/{message_id}').status_code == 200
    assert admin_client.get(f'/api/v1/mail/messages/{message_id}').status_code == 200

    # 404, never 403, on every foreign-user retrieval endpoint.
    for suffix in ('', '/body', '/raw', '/parts/0/content', '/export.json'):
        resp = stranger_client.get(f'/api/v1/mail/messages/{message_id}{suffix}')
        assert resp.status_code == 404, f'{suffix}: got {resp.status_code}'
    assert stranger_client.delete(f'/api/v1/mail/messages/{message_id}').status_code == 404


# --- export.json completeness states ---------------------------------------------

def test_export_json_pending_then_all_terminal_with_failed_error_message():
    user = _user('mailexport')
    client = login_as(user.username)
    raw = _two_pdf_attachments_eml(subject='Export completeness')
    ingest = _post_raw(client, raw)
    assert ingest.status_code == 201, ingest.text
    payload = ingest.json()
    message_id = payload['id']
    job_ids = [p['job_id'] for p in payload['parts'] if p['outcome'] == 'job']
    assert len(job_ids) == 2

    pending_export = client.get(f'/api/v1/mail/messages/{message_id}/export.json')
    assert pending_export.status_code == 200
    assert pending_export.json()['complete'] is False  # both jobs still PENDING

    _mark_job(job_ids[0], status=JobStatus.FINISHED, result_markdown='# ok')
    _mark_job(job_ids[1], status=JobStatus.FAILED, error_message='OCR blew up')

    done = client.get(f'/api/v1/mail/messages/{message_id}/export.json').json()
    assert done['schema'] == 'paddledoc.mail-export/1'
    assert done['message']['id'] == message_id
    assert done['complete'] is True

    finished = next(a for a in done['attachments'] if a['job_id'] == job_ids[0])
    failed = next(a for a in done['attachments'] if a['job_id'] == job_ids[1])
    assert finished['job_status'] == 'FINISHED'
    assert finished['markdown'] == '# ok'
    assert 'error_message' not in finished
    assert failed['job_status'] == 'FAILED'
    assert failed['error_message'] == 'OCR blew up'
    assert 'markdown' not in failed


def test_export_json_body_only_message_is_trivially_complete():
    user = _user('mailexportbody')
    client = login_as(user.username)
    ingest = _post_raw(client, _simple_text_eml(subject='Body only export'))
    assert ingest.status_code == 201, ingest.text

    export = client.get(f"/api/v1/mail/messages/{ingest.json()['id']}/export.json").json()
    assert export['attachments'] == []
    assert export['complete'] is True
    assert export['body']['markdown']


# --- List filters + pagination total ---------------------------------------------

def test_list_filters_by_rfc_message_id_sha256_and_source():
    user = _user('mailfilter')
    client = login_as(user.username)
    tag = uuid.uuid4().hex[:8]
    raw = _simple_text_eml(subject=f'Filter probe {tag}')
    ingest = _post_raw(client, raw, source='gateway-x')
    assert ingest.status_code == 201, ingest.text
    body = ingest.json()
    message_id = body['id']
    rfc_id = body['rfc_message_id']
    sha = body['content_sha256']
    assert rfc_id  # fixture sets a Message-ID header

    by_message_id = client.get('/api/v1/mail/messages', params={'message_id': rfc_id}).json()
    assert [item['id'] for item in by_message_id['items']] == [message_id]
    assert by_message_id['total'] == 1

    by_sha = client.get('/api/v1/mail/messages', params={'sha256': sha}).json()
    assert [item['id'] for item in by_sha['items']] == [message_id]
    assert by_sha['total'] == 1

    by_source = client.get('/api/v1/mail/messages', params={'source': 'gateway-x', 'q': f'Filter probe {tag}'}).json()
    assert [item['id'] for item in by_source['items']] == [message_id]

    no_match = client.get('/api/v1/mail/messages', params={'source': f'no-such-source-{tag}'}).json()
    assert no_match['items'] == []
    assert no_match['total'] == 0


def test_list_pagination_reports_real_total_independent_of_limit_offset():
    user = _user('mailpage')
    client = login_as(user.username)
    tag = uuid.uuid4().hex[:8]
    for i in range(3):
        resp = _post_raw(client, _simple_text_eml(subject=f'Page probe {tag} {i}'))
        assert resp.status_code == 201, resp.text

    full = client.get('/api/v1/mail/messages', params={'q': f'Page probe {tag}'}).json()
    assert full['total'] == 3
    assert len(full['items']) == 3

    page = client.get('/api/v1/mail/messages', params={'q': f'Page probe {tag}', 'limit': 1, 'offset': 1}).json()
    assert page['total'] == 3  # real total, not just this page's length
    assert len(page['items']) == 1


# --- DELETE: dangling-FK guarantee + full removal ---------------------------------

def test_delete_without_delete_jobs_leaves_no_dangling_mail_message_id_on_sqlite():
    user = _user('maildelete')
    client = login_as(user.username)
    ingest = _post_raw(client, _mixed_with_pdf_and_zip_eml())
    assert ingest.status_code == 201, ingest.text
    payload = ingest.json()
    job_id = next(p for p in payload['parts'] if p['outcome'] == 'job')['job_id']

    resp = client.delete(f"/api/v1/mail/messages/{payload['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {'id': payload['id'], 'deleted_jobs': 0}

    db = _db()
    try:
        assert db.get(MailMessage, payload['id']) is None
        # Direct SQL, not an ORM relationship read: this whole suite runs
        # SQLite without PRAGMA foreign_keys, so the FK's ON DELETE SET NULL
        # never actually fires -- the route must issue the UPDATE itself.
        row = db.execute(text('SELECT mail_message_id FROM jobs WHERE id = :id'), {'id': job_id}).fetchone()
        assert row is not None
        assert row[0] is None
        dangling = db.execute(
            text('SELECT COUNT(*) FROM jobs WHERE mail_message_id = :mid'), {'mid': payload['id']}
        ).scalar()
        assert dangling == 0
        assert db.get(Job, job_id) is not None  # the job itself survives, just unlinked
    finally:
        db.close()


def test_delete_with_delete_jobs_removes_jobs():
    user = _user('maildeletejobs')
    client = login_as(user.username)
    ingest = _post_raw(client, _mixed_with_pdf_and_zip_eml())
    assert ingest.status_code == 201, ingest.text
    payload = ingest.json()
    job_id = next(p for p in payload['parts'] if p['outcome'] == 'job')['job_id']

    resp = client.delete(f"/api/v1/mail/messages/{payload['id']}", params={'delete_jobs': 'true'})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {'id': payload['id'], 'deleted_jobs': 1}

    db = _db()
    try:
        assert db.get(MailMessage, payload['id']) is None
        assert db.get(Job, job_id) is None
    finally:
        db.close()
