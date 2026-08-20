"""Verifies the 0004_auth and 0005_import migrations' upgrade/downgrade round-trips.

0001_init / 0002_job_processing_info / 0002_add_password_protection use
postgres-only DDL (`DO $$ ... END $$` blocks, native ENUM) and cannot be
applied to a fresh sqlite database, so we can't just `alembic upgrade head`
from an empty db here. Instead we hand-build the pre-0004 schema (mirroring
exactly what those revisions produce) directly with sqlalchemy core, stamp
alembic to 0003, and drive 0004 itself through the real alembic machinery.
0004 was written to be sqlite-compatible (plain op.create_table/add_column,
no postgres-specific DDL) specifically so this works.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)

from app.core.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _build_legacy_metadata() -> MetaData:
    """The pre-0004 (i.e. post-0003) schema, built independently of the
    current ORM models (which already include the 0004 additions)."""
    metadata = MetaData()
    Table(
        'jobs', metadata,
        Column('id', String(36), primary_key=True),
        Column('original_filename', String(255), nullable=False),
        Column('upload_path', String(1024), nullable=False),
        Column('upload_content', LargeBinary, nullable=True),
        Column('upload_mime_type', String(128), nullable=True),
        Column('upload_size_bytes', Integer, nullable=True),
        Column('result_path', String(1024), nullable=True),
        Column('result_markdown', Text, nullable=True),
        Column('status', String(32), nullable=False),
        Column('error_message', Text, nullable=True),
        Column('processing_info', JSON, nullable=True),
        Column('password_hash', String(255), nullable=True),
        Column('created_at', DateTime(timezone=True), nullable=False),
        Column('updated_at', DateTime(timezone=True), nullable=False),
    )
    Table(
        'documents', metadata,
        Column('id', String(36), primary_key=True),
        Column('filename', String(255), nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False),
    )
    Table(
        'chunks', metadata,
        Column('id', String(36), primary_key=True),
        Column('document_id', String(36), ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        Column('content', Text, nullable=False),
        Column('chunk_type', String(64), nullable=False),
        Column('metadata', JSON, nullable=False),
    )
    Table(
        'tags', metadata,
        Column('id', String(36), primary_key=True),
        Column('name', String(64), nullable=False, unique=True),
    )
    Table(
        'job_tags', metadata,
        Column('job_id', String(36), ForeignKey('jobs.id', ondelete='CASCADE'), primary_key=True),
        Column('tag_id', String(36), ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True),
    )
    Table(
        'job_markdown_versions', metadata,
        Column('id', String(36), primary_key=True),
        Column('job_id', String(36), ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        Column('version', Integer, nullable=False),
        Column('content', Text, nullable=False),
        Column('created_at', DateTime(timezone=True), nullable=False),
        UniqueConstraint('job_id', 'version', name='uq_job_markdown_versions_job_id_version'),
    )
    return metadata


def _alembic_config() -> Config:
    # Built programmatically (not Config('alembic.ini')) so it doesn't
    # depend on the process cwd -- the ini's `script_location = alembic` is
    # only correct relative to the backend/ directory, and tests may be run
    # from the repo root.
    cfg = Config()
    cfg.set_main_option('script_location', str(BACKEND_DIR / 'alembic'))
    return cfg


def test_0004_auth_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade: 0004 should add the auth tables + jobs.owner_id ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert expected in tables, f'{expected} missing after upgrade'

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'owner_id' in job_columns
    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks
    ), job_fks

    sessions_indexes = {ix['name'] for ix in insp.get_indexes('sessions')}
    assert {'ix_sessions_user_id', 'ix_sessions_expires_at'} <= sessions_indexes

    # sqlite reflection can't describe expression-based indexes ("Skipped
    # unsupported reflection of expression-based index"), so confirm the
    # case-insensitive-unique-email index directly via sqlite_master and by
    # exercising the constraint.
    with engine.begin() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_users_email_lower'")
        ).scalar_one()
        assert 'UNIQUE' in ddl.upper()
        assert 'lower(email)' in ddl

        conn.execute(text(
            "INSERT INTO users (id, username, email, role, is_active, created_at, updated_at) "
            "VALUES ('u1', 'alice', 'Alice@Example.com', 'user', 1, '2026-01-01', '2026-01-01')"
        ))
        try:
            conn.execute(text(
                "INSERT INTO users (id, username, email, role, is_active, created_at, updated_at) "
                "VALUES ('u2', 'bob', 'alice@example.com', 'user', 1, '2026-01-01', '2026-01-01')"
            ))
        except Exception:
            pass
        else:
            raise AssertionError('case-insensitive duplicate email was not rejected')

    # --- downgrade: everything 0004 added should disappear ---
    command.downgrade(cfg, '0003_job_markdown_versions')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for removed in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert removed not in tables, f'{removed} still present after downgrade'
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'owner_id' not in job_columns

    # --- re-upgrade: should cleanly re-apply from the 0003 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert expected in tables


def test_0005_import_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0005.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004 to 0005): import tables + jobs.import_run_id ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('import_sources', 'import_runs', 'job_artifacts'):
        assert expected in tables, f'{expected} missing after upgrade'

    source_columns = {c['name'] for c in insp.get_columns('import_sources')}
    assert {
        'id', 'owner_id', 'name', 'base_url', 'server_kind', 'api_base_path', 'auth_type',
        'auth_username', 'credential_encrypted', 'last_validated_at', 'last_test_at',
        'created_at', 'updated_at',
    } <= source_columns
    run_columns = {c['name'] for c in insp.get_columns('import_runs')}
    assert {
        'id', 'source_id', 'owner_id', 'kind', 'status', 'scope_type', 'scope_value',
        'root_page_title', 'options', 'error_message', 'cancel_requested', 'chunk_seq',
        'pages_discovered', 'pages_imported', 'pages_failed', 'attachments_saved',
        'artifact_bytes', 'content_bytes', 'current_page_title', 'state',
        'created_at', 'updated_at', 'started_at', 'finished_at',
    } <= run_columns
    artifact_columns = {c['name'] for c in insp.get_columns('job_artifacts')}
    assert {
        'id', 'job_id', 'kind', 'filename', 'content_type', 'content', 'size_bytes',
        'source_url', 'sha256', 'created_at',
    } <= artifact_columns

    source_fks = insp.get_foreign_keys('import_sources')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in source_fks
    ), source_fks
    run_fks = insp.get_foreign_keys('import_runs')
    assert any(
        fk['referred_table'] == 'import_sources' and fk['constrained_columns'] == ['source_id'] for fk in run_fks
    ), run_fks
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in run_fks
    ), run_fks
    artifact_fks = insp.get_foreign_keys('job_artifacts')
    assert any(
        fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['job_id'] for fk in artifact_fks
    ), artifact_fks

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'import_run_id' in job_columns
    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'import_runs' and fk['constrained_columns'] == ['import_run_id']
        for fk in job_fks
    ), job_fks
    # The batch_alter_table rebuild must not have dropped 0004's FK.
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks
    ), job_fks

    assert 'ix_import_sources_owner_id' in {ix['name'] for ix in insp.get_indexes('import_sources')}
    run_indexes = {ix['name'] for ix in insp.get_indexes('import_runs')}
    assert {'ix_import_runs_owner_id', 'ix_import_runs_source_id'} <= run_indexes
    assert 'ix_job_artifacts_job_id' in {ix['name'] for ix in insp.get_indexes('job_artifacts')}
    jobs_indexes = {ix['name'] for ix in insp.get_indexes('jobs')}
    assert {'ix_jobs_import_run_id', 'ix_jobs_owner_id'} <= jobs_indexes

    artifact_uniques = insp.get_unique_constraints('job_artifacts')
    assert any(
        uc['name'] == 'uq_job_artifacts_job_id_filename' and set(uc['column_names']) == {'job_id', 'filename'}
        for uc in artifact_uniques
    ), artifact_uniques

    # --- downgrade one revision: only the 0005 additions should disappear ---
    command.downgrade(cfg, '0004_auth')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for removed in ('import_sources', 'import_runs', 'job_artifacts'):
        assert removed not in tables, f'{removed} still present after downgrade'
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'import_run_id' not in job_columns
    # 0004's schema must survive a 0005-only downgrade untouched.
    assert 'owner_id' in job_columns
    for kept in ('teams', 'auth_providers', 'users', 'sessions', 'collections'):
        assert kept in tables, f'{kept} unexpectedly dropped by 0005 downgrade'

    # --- re-upgrade: should cleanly re-apply from the 0004 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('import_sources', 'import_runs', 'job_artifacts'):
        assert expected in tables
    assert 'import_run_id' in {c['name'] for c in insp.get_columns('jobs')}


def test_0006_worker_logs_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0006.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004/0005 to 0006): worker_log_entries ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    assert 'worker_log_entries' in insp.get_table_names()

    columns = {c['name'] for c in insp.get_columns('worker_log_entries')}
    assert {
        'id', 'created_at', 'level', 'logger_name', 'worker_name',
        'task_id', 'task_name', 'message', 'exc_text',
    } <= columns

    indexes = {ix['name'] for ix in insp.get_indexes('worker_log_entries')}
    assert {
        'ix_worker_log_entries_created_at',
        'ix_worker_log_entries_level',
        'ix_worker_log_entries_worker_name',
    } <= indexes

    # --- downgrade one revision: only the 0006 addition should disappear ---
    command.downgrade(cfg, '0005_import')

    insp = inspect(engine)
    assert 'worker_log_entries' not in insp.get_table_names()
    # 0005's schema must survive a 0006-only downgrade untouched.
    assert 'import_sources' in insp.get_table_names()

    # --- re-upgrade: should cleanly re-apply from the 0005 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    assert 'worker_log_entries' in insp.get_table_names()


def test_0007_versioning_tokens_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0007.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade to just before 0007, then insert a pre-existing job row the
    # "old" way (no content_sha256/document_version/previous_job_id columns
    # exist yet at this point) -- this is what actually proves 0007's
    # server_default backfills real legacy rows, rather than a row inserted
    # after the columns already exist. ---
    command.upgrade(cfg, '0006_worker_logs')
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO jobs (id, original_filename, upload_path, status, created_at, updated_at) "
            "VALUES ('legacy-job-1', 'legacy.pdf', '/tmp/legacy.pdf', 'PENDING', '2026-01-01', '2026-01-01')"
        ))

    # --- upgrade (0007): jobs columns + api_tokens ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    assert 'api_tokens' in insp.get_table_names()

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert {'content_sha256', 'document_version', 'previous_job_id'} <= job_columns

    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['previous_job_id'] for fk in job_fks
    ), job_fks
    # The batch_alter_table rebuild must not have dropped earlier FKs.
    assert any(fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks), job_fks
    assert any(
        fk['referred_table'] == 'import_runs' and fk['constrained_columns'] == ['import_run_id'] for fk in job_fks
    ), job_fks

    jobs_indexes = {ix['name'] for ix in insp.get_indexes('jobs')}
    assert {'ix_jobs_content_sha256', 'ix_jobs_previous_job_id'} <= jobs_indexes

    token_columns = {c['name'] for c in insp.get_columns('api_tokens')}
    assert {
        'id', 'user_id', 'name', 'token_hash', 'token_prefix',
        'created_at', 'last_used_at', 'expires_at',
    } <= token_columns

    token_fks = insp.get_foreign_keys('api_tokens')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['user_id'] for fk in token_fks
    ), token_fks

    token_indexes = {ix['name'] for ix in insp.get_indexes('api_tokens')}
    assert {'ix_api_tokens_user_id', 'ix_api_tokens_token_hash'} <= token_indexes

    token_uniques = insp.get_unique_constraints('api_tokens')
    assert any(uc['name'] == 'uq_api_tokens_token_hash' for uc in token_uniques), token_uniques

    # The NOT-NULL document_version column (added with a server_default) must
    # have backfilled the row inserted before 0007 ran; content_sha256 and
    # previous_job_id are nullable additions, so the legacy row gets NULL for
    # both rather than any synthesized value.
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT document_version, content_sha256, previous_job_id FROM jobs WHERE id = 'legacy-job-1'"
        )).one()
        assert row.document_version == 1
        assert row.content_sha256 is None
        assert row.previous_job_id is None

    # --- downgrade one revision: only the 0007 additions should disappear ---
    command.downgrade(cfg, '0006_worker_logs')

    insp = inspect(engine)
    assert 'api_tokens' not in insp.get_table_names()
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert not ({'content_sha256', 'document_version', 'previous_job_id'} & job_columns)
    # 0006's schema must survive a 0007-only downgrade untouched.
    assert 'worker_log_entries' in insp.get_table_names()

    # --- re-upgrade: should cleanly re-apply from the 0006 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    assert 'api_tokens' in insp.get_table_names()
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert {'content_sha256', 'document_version', 'previous_job_id'} <= job_columns


def test_0008_vl_benchmarks_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0008.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004-0007 to 0008): vl_connections + benchmark_runs
    # + jobs.benchmark_run_id ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('vl_connections', 'benchmark_runs'):
        assert expected in tables, f'{expected} missing after upgrade'

    vl_columns = {c['name'] for c in insp.get_columns('vl_connections')}
    assert {
        'id', 'name', 'base_url', 'model', 'api_key_encrypted', 'system_prompt',
        'enabled', 'created_at', 'updated_at',
    } <= vl_columns

    run_columns = {c['name'] for c in insp.get_columns('benchmark_runs')}
    assert {
        'id', 'owner_id', 'original_filename', 'content_sha256', 'created_at', 'updated_at',
    } <= run_columns

    run_fks = insp.get_foreign_keys('benchmark_runs')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in run_fks
    ), run_fks
    assert 'ix_benchmark_runs_owner_id' in {ix['name'] for ix in insp.get_indexes('benchmark_runs')}
    assert 'ix_benchmark_runs_content_sha256' in {ix['name'] for ix in insp.get_indexes('benchmark_runs')}

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'benchmark_run_id' in job_columns
    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'benchmark_runs' and fk['constrained_columns'] == ['benchmark_run_id']
        for fk in job_fks
    ), job_fks
    # The batch_alter_table rebuild must not have dropped earlier FKs.
    assert any(fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks), job_fks
    assert any(
        fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['previous_job_id'] for fk in job_fks
    ), job_fks

    jobs_indexes = {ix['name'] for ix in insp.get_indexes('jobs')}
    assert 'ix_jobs_benchmark_run_id' in jobs_indexes

    # --- downgrade one revision: only the 0008 additions should disappear ---
    command.downgrade(cfg, '0007_versioning_tokens')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for removed in ('vl_connections', 'benchmark_runs'):
        assert removed not in tables, f'{removed} still present after downgrade'
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'benchmark_run_id' not in job_columns
    # 0007's schema must survive a 0008-only downgrade untouched.
    assert 'content_sha256' in job_columns
    assert 'api_tokens' in tables

    # --- re-upgrade: should cleanly re-apply from the 0007 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('vl_connections', 'benchmark_runs'):
        assert expected in tables
    assert 'benchmark_run_id' in {c['name'] for c in insp.get_columns('jobs')}


def test_0009_mail_ingestion_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0009.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004-0008 to 0009 for real, same as the 0008 test
    # -- stamping straight to 0008 would skip physically creating `users` /
    # `benchmark_runs` / etc., which the assertions below (and mail_messages'
    # own FK to users) depend on): mail_messages + jobs.mail_message_id ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert 'mail_messages' in tables, 'mail_messages missing after upgrade'

    mail_columns = {c['name'] for c in insp.get_columns('mail_messages')}
    assert {
        'id', 'owner_id', 'content_sha256', 'rfc_message_id', 'subject', 'from_address',
        'recipients', 'sent_at', 'source', 'raw_content', 'raw_size_bytes', 'body_format',
        'body_markdown', 'parts', 'parse_error', 'created_at', 'updated_at',
    } <= mail_columns

    mail_fks = insp.get_foreign_keys('mail_messages')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in mail_fks
    ), mail_fks

    mail_uniques = {uc['name'] for uc in insp.get_unique_constraints('mail_messages')}
    assert 'uq_mail_messages_owner_id_content_sha256' in mail_uniques

    mail_indexes = {ix['name'] for ix in insp.get_indexes('mail_messages')}
    assert 'ix_mail_messages_owner_id' in mail_indexes
    assert 'ix_mail_messages_content_sha256' in mail_indexes
    assert 'ix_mail_messages_rfc_message_id' in mail_indexes

    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'mail_message_id' in job_columns
    job_fks = insp.get_foreign_keys('jobs')
    assert any(
        fk['referred_table'] == 'mail_messages' and fk['constrained_columns'] == ['mail_message_id']
        for fk in job_fks
    ), job_fks
    # The batch_alter_table rebuild must not have dropped earlier FKs.
    assert any(fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in job_fks), job_fks
    assert any(
        fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['previous_job_id'] for fk in job_fks
    ), job_fks

    jobs_indexes = {ix['name'] for ix in insp.get_indexes('jobs')}
    assert 'ix_jobs_mail_message_id' in jobs_indexes

    # --- downgrade one revision: only the 0009 additions should disappear ---
    command.downgrade(cfg, '0008_vl_benchmarks')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert 'mail_messages' not in tables, 'mail_messages still present after downgrade'
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'mail_message_id' not in job_columns
    # 0008's schema must survive a 0009-only downgrade untouched.
    assert 'benchmark_run_id' in job_columns
    assert 'benchmark_runs' in tables

    # --- re-upgrade: should cleanly re-apply from the 0008 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert 'mail_messages' in tables
    assert 'mail_message_id' in {c['name'] for c in insp.get_columns('jobs')}


def test_0010_openwebui_migration_upgrade_downgrade_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0010.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade (through 0004-0009 to 0010 for real, same reasoning as the
    # 0009 test -- openwebui_pushes/import_page_states both FK onto `jobs`
    # and `users`, which only physically exist after the real chain runs):
    # openwebui_connections + openwebui_pushes + import_page_states +
    # import_sources refresh columns ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('openwebui_connections', 'openwebui_pushes', 'import_page_states'):
        assert expected in tables, f'{expected} missing after upgrade'

    conn_columns = {c['name'] for c in insp.get_columns('openwebui_connections')}
    assert {'id', 'name', 'base_url', 'api_key_encrypted', 'owner_id', 'created_at', 'updated_at'} <= conn_columns

    push_columns = {c['name'] for c in insp.get_columns('openwebui_pushes')}
    assert {
        'id', 'connection_id', 'connection_name', 'job_id', 'knowledge_id', 'knowledge_name', 'status',
        'error_message', 'openwebui_file_id', 'replaced_file_id', 'pushed_content_sha256', 'owner_id',
        'created_at', 'updated_at',
    } <= push_columns

    page_state_columns = {c['name'] for c in insp.get_columns('import_page_states')}
    assert {'id', 'source_id', 'page_id', 'page_version', 'job_id', 'title', 'url', 'updated_at'} <= page_state_columns

    source_columns = {c['name'] for c in insp.get_columns('import_sources')}
    assert {'refresh_enabled', 'refresh_interval_seconds', 'last_refresh_at', 'last_refresh_error'} <= source_columns

    conn_fks = insp.get_foreign_keys('openwebui_connections')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in conn_fks
    ), conn_fks

    push_fks = insp.get_foreign_keys('openwebui_pushes')
    assert any(
        fk['referred_table'] == 'openwebui_connections' and fk['constrained_columns'] == ['connection_id']
        for fk in push_fks
    ), push_fks
    assert any(fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['job_id'] for fk in push_fks), push_fks
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in push_fks
    ), push_fks

    page_state_fks = insp.get_foreign_keys('import_page_states')
    assert any(
        fk['referred_table'] == 'import_sources' and fk['constrained_columns'] == ['source_id']
        for fk in page_state_fks
    ), page_state_fks
    assert any(
        fk['referred_table'] == 'jobs' and fk['constrained_columns'] == ['job_id'] for fk in page_state_fks
    ), page_state_fks

    push_indexes = {ix['name'] for ix in insp.get_indexes('openwebui_pushes')}
    assert {
        'ix_openwebui_pushes_job_id', 'ix_openwebui_pushes_owner_id', 'ix_openwebui_pushes_job_id_created_at',
    } <= push_indexes
    assert 'ix_import_page_states_source_id' in {ix['name'] for ix in insp.get_indexes('import_page_states')}

    page_state_uniques = {uc['name'] for uc in insp.get_unique_constraints('import_page_states')}
    assert 'uq_import_page_states_source_id_page_id' in page_state_uniques

    # The batch_alter_table rebuild of import_sources must not have dropped
    # earlier columns/FKs.
    source_fks = insp.get_foreign_keys('import_sources')
    assert any(
        fk['referred_table'] == 'users' and fk['constrained_columns'] == ['owner_id'] for fk in source_fks
    ), source_fks

    # --- exercise the new columns/tables with real rows: refresh_enabled's
    # server_default backfills to false, and UNIQUE(source_id, page_id)
    # rejects a duplicate (same discipline as the 0004 email-uniqueness and
    # 0009 owner_id/content_sha256 checks above) ---
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, username, email, role, is_active, created_at, updated_at) "
            "VALUES ('u-openwebui', 'owui', 'owui@example.com', 'user', 1, '2026-01-01', '2026-01-01')"
        ))
        conn.execute(text(
            "INSERT INTO jobs (id, original_filename, upload_path, status, created_at, updated_at, document_version) "
            "VALUES ('j-openwebui', 'doc.pdf', '/tmp/doc.pdf', 'FINISHED', '2026-01-01', '2026-01-01', 1)"
        ))
        conn.execute(text(
            "INSERT INTO openwebui_connections (id, name, base_url, api_key_encrypted, owner_id, created_at, updated_at) "
            "VALUES ('c-openwebui', 'OWUI', 'https://owui.example.com', 'enc', 'u-openwebui', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(text(
            "INSERT INTO openwebui_pushes "
            "(id, connection_id, connection_name, job_id, knowledge_id, knowledge_name, status, owner_id, created_at, updated_at) "
            "VALUES ('p-openwebui', 'c-openwebui', 'OWUI', 'j-openwebui', 'kb1', 'Docs', 'pending', 'u-openwebui', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(text(
            "INSERT INTO import_sources "
            "(id, owner_id, name, base_url, server_kind, api_base_path, auth_type, auth_username, credential_encrypted, created_at, updated_at) "
            "VALUES ('s-openwebui', 'u-openwebui', 'Confluence', 'https://conf.example.com', '', '', 'pat_bearer', '', 'enc', '2026-01-01', '2026-01-01')"
        ))
        refresh_enabled = conn.execute(
            text("SELECT refresh_enabled FROM import_sources WHERE id = 's-openwebui'")
        ).scalar_one()
        assert refresh_enabled in (0, False)

        conn.execute(text(
            "INSERT INTO import_page_states (id, source_id, page_id, page_version, job_id, title, url, updated_at) "
            "VALUES ('ps-openwebui', 's-openwebui', '123', 3, 'j-openwebui', 'A page', 'https://conf.example.com/x', '2026-01-01')"
        ))
        try:
            conn.execute(text(
                "INSERT INTO import_page_states (id, source_id, page_id, page_version, title, url, updated_at) "
                "VALUES ('ps-openwebui-dup', 's-openwebui', '123', 4, 'dup', '', '2026-01-01')"
            ))
        except Exception:
            pass
        else:
            raise AssertionError('duplicate (source_id, page_id) was not rejected')

    # --- downgrade one revision: only the 0010 additions should disappear ---
    command.downgrade(cfg, '0009_mail_ingestion')

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for removed in ('openwebui_connections', 'openwebui_pushes', 'import_page_states'):
        assert removed not in tables, f'{removed} still present after downgrade'
    source_columns = {c['name'] for c in insp.get_columns('import_sources')}
    assert not ({'refresh_enabled', 'refresh_interval_seconds', 'last_refresh_at', 'last_refresh_error'} & source_columns)
    # 0009's schema must survive a 0010-only downgrade untouched.
    assert 'mail_messages' in tables
    job_columns = {c['name'] for c in insp.get_columns('jobs')}
    assert 'mail_message_id' in job_columns

    # --- re-upgrade: should cleanly re-apply from the 0009 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for expected in ('openwebui_connections', 'openwebui_pushes', 'import_page_states'):
        assert expected in tables
    source_columns = {c['name'] for c in insp.get_columns('import_sources')}
    assert {'refresh_enabled', 'refresh_interval_seconds', 'last_refresh_at', 'last_refresh_error'} <= source_columns


def test_migration_history_has_a_single_head():
    """Two migrations added on different branches easily end up with the same
    down_revision, which leaves alembic with two heads and makes every
    `upgrade head` fail. Merging feat/openwebui-upload into the security-audit
    work produced exactly that (two 0010_* revisions), so this guards the
    shape of the chain rather than any one migration's contents.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Built programmatically for the same reason the other tests here do it:
    # no dependency on the working directory alembic.ini is read from.
    cfg = Config()
    cfg.set_main_option('script_location', str(BACKEND_DIR / 'alembic'))
    heads = ScriptDirectory.from_config(cfg).get_heads()

    assert len(heads) == 1, f'alembic history has diverged into {len(heads)} heads: {heads}'


def test_0012_provider_use_email_as_username_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / 'migration_scratch_0012.db'
    db_url = f'sqlite:///{db_path}'
    monkeypatch.setattr(settings, 'database_url', db_url)

    engine = create_engine(db_url, future=True)
    _build_legacy_metadata().create_all(bind=engine)

    cfg = _alembic_config()
    command.stamp(cfg, '0003_job_markdown_versions')

    # --- upgrade through the real 0004..0012 chain (auth_providers itself
    # only physically exists once 0004 has run) ---
    command.upgrade(cfg, 'head')

    insp = inspect(engine)
    provider_columns = {c['name'] for c in insp.get_columns('auth_providers')}
    assert 'use_email_as_username' in provider_columns

    # The server_default must make the flag land as false for pre-existing
    # rows -- insert without the column and read it back.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO auth_providers "
            "(id, slug, display_name, issuer_url, client_id, client_secret_encrypted, scopes, enabled, created_at, updated_at) "
            "VALUES ('p1', 'entra', 'Entra', 'https://issuer.example', 'cid', 'not-a-real-secret', 'openid profile email', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        stored = conn.execute(
            text("SELECT use_email_as_username FROM auth_providers WHERE id = 'p1'")
        ).scalar_one()
    assert stored in (0, False)

    # --- downgrade one revision: only the 0012 column disappears ---
    command.downgrade(cfg, '0011_login_lockout')
    insp = inspect(engine)
    provider_columns = {c['name'] for c in insp.get_columns('auth_providers')}
    assert 'use_email_as_username' not in provider_columns
    # 0011's schema must survive a 0012-only downgrade untouched.
    user_columns = {c['name'] for c in insp.get_columns('users')}
    assert {'failed_login_count', 'last_failed_login_at', 'locked_until'} <= user_columns

    # --- re-upgrade: cleanly re-applies from the 0011 baseline ---
    command.upgrade(cfg, 'head')
    insp = inspect(engine)
    provider_columns = {c['name'] for c in insp.get_columns('auth_providers')}
    assert 'use_email_as_username' in provider_columns
