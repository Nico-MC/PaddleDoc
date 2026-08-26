"""add outbound webhook integration (webhook_connections, webhook_deliveries)

Revision ID: 0013_webhooks
Revises: 0012_provider_email_username
Create Date: 2026-08-25

sqlite-compatible on purpose (plain op.create_table, no postgres-only DDL) so
tests/test_migrations.py can drive it through real alembic against sqlite,
same as 0010_openwebui.

Revision id kept <= 32 chars on purpose: alembic's version table stores
version_num as VARCHAR(32), and PostgreSQL enforces that where SQLite (the
test database) silently doesn't -- see 0012's docstring for the production
CrashLoopBackOff this bit before.
"""

from alembic import op
import sqlalchemy as sa


revision = '0013_webhooks'
down_revision = '0012_provider_email_username'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'webhook_connections',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('secret_encrypted', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        # List of subscribed event strings (subset of 'job.finished' /
        # 'job.failed' / 'import_run.finished') -- see app/schemas/webhooks.py
        # for the validated set. server_default keeps old raw-SQL/test
        # inserts that don't set it explicitly NOT NULL-safe, same reasoning
        # as import_runs.state's '{}' default in 0005_import.
        sa.Column('events', sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index('ix_webhook_connections_owner_id', 'webhook_connections', ['owner_id'], if_not_exists=True)

    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'connection_id',
            sa.String(length=36),
            sa.ForeignKey('webhook_connections.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('connection_name', sa.String(length=255), nullable=False),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('event', sa.String(length=32), nullable=False),
        # SET NULL (not CASCADE like openwebui_pushes.job_id): a delivery row
        # is an audit/log entry that must outlive the job it was about, same
        # reasoning as ImportPageState.job_id.
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True),
        sa.Column(
            'import_run_id', sa.String(length=36), sa.ForeignKey('import_runs.id', ondelete='SET NULL'), nullable=True
        ),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index('ix_webhook_deliveries_connection_id', 'webhook_deliveries', ['connection_id'], if_not_exists=True)
    op.create_index('ix_webhook_deliveries_owner_id', 'webhook_deliveries', ['owner_id'], if_not_exists=True)
    op.create_index('ix_webhook_deliveries_job_id', 'webhook_deliveries', ['job_id'], if_not_exists=True)
    op.create_index(
        'ix_webhook_deliveries_import_run_id', 'webhook_deliveries', ['import_run_id'], if_not_exists=True
    )
    # Drives the owner-scoped, created_at-desc GET /deliveries list -- same
    # shape reasoning as ix_openwebui_pushes_job_id_created_at.
    op.create_index(
        'ix_webhook_deliveries_owner_id_created_at',
        'webhook_deliveries',
        ['owner_id', 'created_at'],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('ix_webhook_deliveries_owner_id_created_at', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_import_run_id', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_job_id', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_owner_id', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_connection_id', table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')

    op.drop_index('ix_webhook_connections_owner_id', table_name='webhook_connections')
    op.drop_table('webhook_connections')
