"""Confluence-refresh tests: the due-source selection + double-start guard
in app/workers/refresh_tasks.py (_dispatch_due_refreshes), and the per-page
version-compare/seed logic inside app/workers/import_tasks.py's
import_confluence task that only activates when a run's options carry
is_refresh=True (see refresh_tasks.py's module docstring for why the diff
logic lives there and not here).

Drives real DB rows against the shared sqlite test DB (SessionLocal
monkeypatched to conftest's TestingSessionLocal), same pattern as
test_import_tasks.py; celery_app.send_task is captured, never actually
dispatched -- no real Redis/broker needed. _dispatch_due_refreshes is called
directly rather than going through confluence_refresh_tick, so the tick's
Redis leadership lock is deliberately not exercised here (consistent with
the existing risk note that the lock path itself has no pytest coverage
yet -- see the implementers' handover notes).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.models import (
    ImportAuthType,
    ImportPageState,
    ImportRun,
    ImportRunStatus,
    ImportSource,
    Job,
    JobStatus,
)
from app.services import security
from app.services.confluence import Page
from app.workers import import_tasks
from app.workers import refresh_tasks
from app.workers.celery_app import celery_app
from app.workers.import_tasks import import_confluence
from app.workers.refresh_tasks import _dispatch_due_refreshes, confluence_refresh_tick
from tests.conftest import TestingSessionLocal, create_test_user

BASE_URL = 'https://acme.example.com'


def _page(page_id: str, title: str, html: str, *, version: int) -> Page:
    return Page(
        id=page_id, title=title, version=version, html=html,
        url=f'{BASE_URL}/wiki/spaces/KEY/pages/{page_id}/{title.replace(" ", "-")}',
    )


class FakeClient:
    """Single-page PageSource -- these tests only exercise the per-page
    diff, never discovery, so children/attachments are always empty."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.fetched: list[str] = []

    def fetch_page(self, page_id: str) -> Page:
        self.fetched.append(page_id)
        assert page_id == self.page.id
        return self.page

    def iter_children(self, page_id: str):
        return iter(())

    def iter_attachments(self, page_id: str):
        return iter(())

    def resolve_space_root(self, space_key: str) -> str:  # pragma: no cover - not exercised (scope_type='page')
        raise NotImplementedError


def _db():
    return TestingSessionLocal()


def _make_owner():
    # Unique per call (the sqlite test DB persists across every test in the
    # session, not just this file), mirroring test_import_tasks._make_owner.
    suffix = uuid.uuid4().hex[:8]
    return create_test_user(username=f'refresher-{suffix}', email=f'refresher-{suffix}@example.com')


def _make_source(
    owner_id: str,
    *,
    refresh_enabled: bool = True,
    refresh_interval_seconds: int | None = 900,
    last_refresh_at: datetime | None = None,
) -> str:
    db = _db()
    try:
        source = ImportSource(
            owner_id=owner_id,
            name='Refresh Test Confluence',
            base_url=BASE_URL,
            server_kind='cloud',
            api_base_path='/wiki/api/v2',
            auth_type=ImportAuthType.CLOUD_BASIC,
            auth_username='refresher@example.com',
            credential_encrypted=security.encrypt_import_credential('api-token'),
            refresh_enabled=refresh_enabled,
            refresh_interval_seconds=refresh_interval_seconds,
            last_refresh_at=last_refresh_at,
        )
        db.add(source)
        db.commit()
        return source.id
    finally:
        db.close()


def _make_finished_run(owner_id: str, source_id: str, *, scope_value: str = 'P1') -> str:
    """The 'last successful run' _start_refresh_run copies scope/options
    from -- a refresh has nothing to repeat without one (see its
    docstring)."""
    db = _db()
    try:
        run = ImportRun(
            source_id=source_id,
            owner_id=owner_id,
            kind='confluence',
            status=ImportRunStatus.FINISHED,
            scope_type='page',
            scope_value=scope_value,
            options={
                'max_pages': 50, 'max_depth': 10, 'include_attachments': True, 'ocr_attachments': False,
                'ocr_profile_id': None, 'folder': '', 'subfolder': '', 'tags': [], 'email': '',
            },
            state={'frontier': [], 'visited': {}, 'errors': []},
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _make_refresh_run(owner_id: str, source_id: str, *, scope_value: str) -> str:
    """A refresh-flagged run already dispatched (mirrors what
    refresh_tasks._start_refresh_run would have created), ready to drive
    import_confluence directly."""
    db = _db()
    try:
        run = ImportRun(
            source_id=source_id,
            owner_id=owner_id,
            kind='confluence',
            scope_type='page',
            scope_value=scope_value,
            options={
                'max_pages': 50, 'max_depth': 10, 'include_attachments': True, 'ocr_attachments': False,
                'ocr_profile_id': None, 'folder': '', 'subfolder': '', 'tags': [], 'email': '',
                'is_refresh': True,
            },
            state={'frontier': [[scope_value, 0]], 'visited': {}, 'errors': []},
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _make_normal_run(owner_id: str, source_id: str, *, scope_value: str) -> str:
    """A plain (non-refresh) run, i.e. what POST /import/runs creates."""
    db = _db()
    try:
        run = ImportRun(
            source_id=source_id,
            owner_id=owner_id,
            kind='confluence',
            scope_type='page',
            scope_value=scope_value,
            options={
                'max_pages': 50, 'max_depth': 10, 'include_attachments': True, 'ocr_attachments': False,
                'ocr_profile_id': None, 'folder': '', 'subfolder': '', 'tags': [], 'email': '',
                'is_refresh': False,
            },
            state={'frontier': [[scope_value, 0]], 'visited': {}, 'errors': []},
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _make_prior_job(owner_id: str, page_id: str, *, version: int, document_version: int = 1, import_run_id: str | None = None) -> str:
    db = _db()
    try:
        job = Job(
            original_filename=f'{page_id}.md',
            upload_path=f'imports/{page_id}/prior.html',
            status=JobStatus.FINISHED,
            owner_id=owner_id,
            import_run_id=import_run_id,
            document_version=document_version,
            result_markdown=f'---\ntitle: Old {page_id}\n---\n\nold body v{version}',
            processing_info={
                'settings': {
                    'mode': 'import',
                    'import': {'source_page_id': page_id, 'source_page_version': version, 'source_url': f'{BASE_URL}/wiki/x'},
                },
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def _make_page_state(source_id: str, page_id: str, *, version: int, job_id: str) -> None:
    db = _db()
    try:
        db.add(
            ImportPageState(
                source_id=source_id, page_id=page_id, page_version=version, job_id=job_id,
                title='Old Title', url=f'{BASE_URL}/wiki/x',
            )
        )
        db.commit()
    finally:
        db.close()


def _get_run(run_id: str) -> ImportRun:
    db = _db()
    try:
        run = db.get(ImportRun, run_id)
        db.refresh(run)
        db.expunge(run)
        return run
    finally:
        db.close()


def _get_page_state(source_id: str, page_id: str) -> ImportPageState | None:
    db = _db()
    try:
        row = db.scalar(
            select(ImportPageState).where(ImportPageState.source_id == source_id, ImportPageState.page_id == page_id)
        )
        if row is not None:
            db.refresh(row)
            db.expunge(row)
        return row
    finally:
        db.close()


def _get_job(job_id: str) -> Job:
    db = _db()
    try:
        job = db.get(Job, job_id)
        db.refresh(job)
        db.expunge(job)
        return job
    finally:
        db.close()


@pytest.fixture()
def sent(monkeypatch):
    monkeypatch.setattr(import_tasks, 'SessionLocal', TestingSessionLocal)
    monkeypatch.setattr(refresh_tasks, 'SessionLocal', TestingSessionLocal)
    captured: list[tuple[str, list]] = []
    monkeypatch.setattr(celery_app, 'send_task', lambda name, args=None, **kw: captured.append((name, list(args or []))))
    return captured


@pytest.fixture()
def client_holder(monkeypatch):
    holder: dict[str, FakeClient] = {}
    monkeypatch.setattr(import_tasks, 'create_client', lambda **kwargs: holder['client'])
    return holder


# --- Tick / dispatch -------------------------------------------------------

def test_dispatch_starts_a_run_only_for_the_due_source(sent) -> None:
    owner = _make_owner()
    now = datetime.now(timezone.utc)

    due_source_id = _make_source(owner.id, last_refresh_at=now - timedelta(seconds=1000))
    _make_finished_run(owner.id, due_source_id)

    not_due_source_id = _make_source(owner.id, last_refresh_at=now)
    _make_finished_run(owner.id, not_due_source_id)

    disabled_source_id = _make_source(owner.id, refresh_enabled=False, last_refresh_at=now - timedelta(seconds=1000))
    _make_finished_run(owner.id, disabled_source_id)

    # Due (never refreshed before) but nothing to repeat yet -- must no-op,
    # not crash, per _start_refresh_run's docstring.
    _make_source(owner.id, last_refresh_at=None)

    _dispatch_due_refreshes()

    assert len(sent) == 1
    name, args = sent[0]
    assert name == 'import_confluence'
    run = _get_run(args[0])
    assert run.source_id == due_source_id
    assert run.options['is_refresh'] is True


def test_tick_still_reenqueues_next_tick_when_lock_acquire_raises(sent, monkeypatch) -> None:
    """A Redis error out of _acquire_or_renew (e.g. a connection blip) must
    not kill the self-re-enqueuing chain: the next tick is still sent, with
    the SAME token this execution was called with -- a transient failure is
    not the same as another chain legitimately taking over leadership."""

    def _boom(_lock_token):
        raise Exception('redis blip')

    monkeypatch.setattr(refresh_tasks, '_acquire_or_renew', _boom)

    confluence_refresh_tick('token-abc')

    assert len(sent) == 1
    name, args = sent[0]
    assert name == 'confluence_refresh_tick'
    assert args == ['token-abc']


def test_dispatch_skips_source_with_an_already_active_run(sent) -> None:
    owner = _make_owner()
    source_id = _make_source(owner.id, last_refresh_at=datetime.now(timezone.utc) - timedelta(seconds=1000))
    _make_finished_run(owner.id, source_id)

    # An in-flight run for this exact source blocks a new dispatch --
    # Doppelstart-Schutz.
    db = _db()
    try:
        db.add(
            ImportRun(
                source_id=source_id, owner_id=owner.id, kind='confluence', status=ImportRunStatus.RUNNING,
                scope_type='page', scope_value='P1', options={}, state={'frontier': [], 'visited': {}, 'errors': []},
            )
        )
        db.commit()
    finally:
        db.close()

    _dispatch_due_refreshes()

    assert sent == []


# --- Per-page diff (import_confluence with options['is_refresh']=True) -----

def test_refresh_run_unchanged_page_creates_no_new_job(sent, client_holder) -> None:
    owner = _make_owner()
    source_id = _make_source(owner.id)
    page_id = 'P1'
    prior_job_id = _make_prior_job(owner.id, page_id, version=3)
    _make_page_state(source_id, page_id, version=3, job_id=prior_job_id)

    client_holder['client'] = FakeClient(_page(page_id, 'Root Page', '<p>same body</p>', version=3))

    run_id = _make_refresh_run(owner.id, source_id, scope_value=page_id)
    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    # No job created for an unchanged page: pages_imported deliberately does
    # not count it (see _import_one_page's case-b comment).
    assert run.pages_imported == 0
    assert run.state['visited'][page_id] == prior_job_id

    state_row = _get_page_state(source_id, page_id)
    assert state_row.page_version == 3
    assert state_row.job_id == prior_job_id


def test_refresh_run_changed_page_creates_chained_job_and_updates_state(sent, client_holder) -> None:
    owner = _make_owner()
    source_id = _make_source(owner.id)
    page_id = 'P1'
    prior_job_id = _make_prior_job(owner.id, page_id, version=3, document_version=2)
    _make_page_state(source_id, page_id, version=3, job_id=prior_job_id)

    client_holder['client'] = FakeClient(_page(page_id, 'Root Page', '<p>updated body</p>', version=4))

    run_id = _make_refresh_run(owner.id, source_id, scope_value=page_id)
    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.pages_imported == 1

    new_job_id = run.state['visited'][page_id]
    assert new_job_id != prior_job_id
    new_job = _get_job(new_job_id)
    # Chained onto the predecessor exactly like a manual re-upload.
    assert new_job.previous_job_id == prior_job_id
    assert new_job.document_version == 3

    state_row = _get_page_state(source_id, page_id)
    assert state_row.page_version == 4
    assert state_row.job_id == new_job_id


def test_refresh_run_unchanged_page_with_deleted_job_reimports_instead_of_skipping(sent, client_holder) -> None:
    owner = _make_owner()
    source_id = _make_source(owner.id)
    page_id = 'P1'
    prior_job_id = _make_prior_job(owner.id, page_id, version=3)
    _make_page_state(source_id, page_id, version=3, job_id=prior_job_id)

    # The page's predecessor job was deleted independently of its state row
    # (e.g. a manual document delete) -- the state row still points at it.
    db = _db()
    try:
        job = db.get(Job, prior_job_id)
        db.delete(job)
        db.commit()
    finally:
        db.close()

    # Remote version is UNCHANGED from what the (now-deleted) predecessor
    # last saw. The naive unchanged-fast-path would skip this page forever,
    # meaning a manually-deleted document is never restored by a refresh.
    client_holder['client'] = FakeClient(_page(page_id, 'Root Page', '<p>same body</p>', version=3))

    run_id = _make_refresh_run(owner.id, source_id, scope_value=page_id)
    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    # Re-imported, not skipped: pages_imported counts it.
    assert run.pages_imported == 1

    new_job_id = run.state['visited'][page_id]
    assert new_job_id != prior_job_id
    new_job = _get_job(new_job_id)
    # Fresh version-1 chain start: nothing to chain onto since the
    # predecessor is gone.
    assert new_job.document_version == 1
    assert new_job.previous_job_id is None

    state_row = _get_page_state(source_id, page_id)
    assert state_row.page_version == 3
    assert state_row.job_id == new_job_id


def test_first_refresh_seeds_page_states_from_historical_jobs(sent, client_holder) -> None:
    owner = _make_owner()
    source_id = _make_source(owner.id)
    page_id = 'P1'

    # A page job from a PAST (normal, non-refresh) run against this source --
    # no ImportPageState exists for it yet.
    old_run_id = _make_finished_run(owner.id, source_id, scope_value=page_id)
    historical_job_id = _make_prior_job(owner.id, page_id, version=2, import_run_id=old_run_id)
    assert _get_page_state(source_id, page_id) is None  # nothing seeded yet

    # The remote page hasn't changed since that historical import.
    client_holder['client'] = FakeClient(_page(page_id, 'Root Page', '<p>same body</p>', version=2))

    run_id = _make_refresh_run(owner.id, source_id, scope_value=page_id)
    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    # Seeding must have run BEFORE the compare: had it not, existing_state
    # would be None and this page would import as brand new (pages_imported
    # == 1). Seeding correctly making it read as unchanged is what this
    # asserts.
    assert run.pages_imported == 0
    assert run.state['visited'][page_id] == historical_job_id

    state_row = _get_page_state(source_id, page_id)
    assert state_row is not None
    assert state_row.page_version == 2
    assert state_row.job_id == historical_job_id


def test_normal_run_writes_page_state_referencing_its_own_new_job(sent, client_holder) -> None:
    """A first-time import must INSERT the page-state row pointing at the job
    it just created -- in the same transaction as that job's own INSERT.

    ImportPageState has the raw jobs.id FK but no relationship() to Job, so
    SQLAlchemy's unit of work does not know the two are ordered and writes
    import_page_states first unless _import_one_page flushes the job. That
    made every page of every import fail on PostgreSQL with
    ForeignKeyViolation on import_page_states_job_id_fkey, while SQLite --
    which ignores foreign keys unless conftest pins PRAGMA foreign_keys=ON --
    happily stored the dangling reference.
    """
    owner = _make_owner()
    source_id = _make_source(owner.id)
    page_id = 'P1'
    assert _get_page_state(source_id, page_id) is None

    client_holder['client'] = FakeClient(_page(page_id, 'Root Page', '<p>body</p>', version=7))

    run_id = _make_normal_run(owner.id, source_id, scope_value=page_id)
    import_confluence(run_id, 0)

    run = _get_run(run_id)
    assert run.status == ImportRunStatus.FINISHED
    assert run.state['errors'] == []
    assert run.pages_failed == 0
    assert run.pages_imported == 1

    new_job_id = run.state['visited'][page_id]
    state_row = _get_page_state(source_id, page_id)
    assert state_row is not None
    assert state_row.page_version == 7
    # The FK actually resolves -- the referenced job row is really there.
    assert state_row.job_id == new_job_id
    assert _get_job(new_job_id) is not None
