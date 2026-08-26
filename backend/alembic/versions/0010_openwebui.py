"""add OpenWebUI push integration (openwebui_connections, openwebui_pushes),
Confluence refresh state (import_page_states, import_sources refresh columns)

Revision ID: 0010_openwebui
Revises: 0009_mail_ingestion
Create Date: 2026-08-17

sqlite-compatible on purpose (plain op.create_table / batch_alter_table, no
postgres-only DDL) so tests/test_migrations.py can drive it through real
alembic against sqlite, same as 0004_auth/.../0009_mail_ingestion.
"""

from alembic import op
import sqlalchemy as sa


revision = '0010_openwebui'
down_revision = '0009_mail_ingestion'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'openwebui_connections',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('base_url', sa.String(length=1024), nullable=False),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index('ix_openwebui_connections_owner_id', 'openwebui_connections', ['owner_id'], if_not_exists=True)

    op.create_table(
        'openwebui_pushes',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'connection_id',
            sa.String(length=36),
            sa.ForeignKey('openwebui_connections.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('connection_name', sa.String(length=255), nullable=False),
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('knowledge_id', sa.String(length=255), nullable=False),
        sa.Column('knowledge_name', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('openwebui_file_id', sa.String(length=255), nullable=True),
        sa.Column('replaced_file_id', sa.String(length=255), nullable=True),
        sa.Column('pushed_content_sha256', sa.String(length=64), nullable=True),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index('ix_openwebui_pushes_job_id', 'openwebui_pushes', ['job_id'], if_not_exists=True)
    op.create_index('ix_openwebui_pushes_owner_id', 'openwebui_pushes', ['owner_id'], if_not_exists=True)
    op.create_index(
        'ix_openwebui_pushes_job_id_created_at', 'openwebui_pushes', ['job_id', 'created_at'], if_not_exists=True
    )

    op.create_table(
        'import_page_states',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column(
            'source_id', sa.String(length=36), sa.ForeignKey('import_sources.id', ondelete='CASCADE'), nullable=False
        ),
        sa.Column('page_id', sa.String(length=64), nullable=False),
        sa.Column('page_version', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.String(length=36), sa.ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('title', sa.String(length=512), nullable=False, server_default=''),
        sa.Column('url', sa.String(length=2048), nullable=False, server_default=''),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('source_id', 'page_id', name='uq_import_page_states_source_id_page_id'),
        if_not_exists=True,
    )
    op.create_index('ix_import_page_states_source_id', 'import_page_states', ['source_id'], if_not_exists=True)

    with op.batch_alter_table('import_sources') as batch_op:
        batch_op.add_column(sa.Column('refresh_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('refresh_interval_seconds', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('last_refresh_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('last_refresh_error', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('import_sources') as batch_op:
        batch_op.drop_column('last_refresh_error')
        batch_op.drop_column('last_refresh_at')
        batch_op.drop_column('refresh_interval_seconds')
        batch_op.drop_column('refresh_enabled')

    op.drop_index('ix_import_page_states_source_id', table_name='import_page_states')
    op.drop_table('import_page_states')

    op.drop_index('ix_openwebui_pushes_job_id_created_at', table_name='openwebui_pushes')
    op.drop_index('ix_openwebui_pushes_owner_id', table_name='openwebui_pushes')
    op.drop_index('ix_openwebui_pushes_job_id', table_name='openwebui_pushes')
    op.drop_table('openwebui_pushes')

    op.drop_index('ix_openwebui_connections_owner_id', table_name='openwebui_connections')
    op.drop_table('openwebui_connections')
