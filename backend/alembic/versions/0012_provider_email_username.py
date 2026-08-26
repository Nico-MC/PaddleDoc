"""add auth_providers.use_email_as_username

Revision ID: 0012_provider_email_username
Revises: 0011_login_lockout
Create Date: 2026-08-20

Per-provider switch: when set, the claimed email address becomes the user's
username (at provisioning, and retroactively at login for existing users)
instead of the preferred_username claim. Needed for IdPs such as Microsoft
Entra whose preferred_username is a UPN, not something human-readable.

Revision id kept <= 32 chars on purpose: alembic's version table stores
version_num as VARCHAR(32), and PostgreSQL enforces that where SQLite (the
test database) silently doesn't -- a longer id passes every test and then
aborts the whole transactional-DDL upgrade on the final version UPDATE in
production (seen live as a backend CrashLoopBackOff on k8s).

Plain ADD COLUMN, no batch_alter_table -- same reasoning as 0011: this table
carries no functional/expression index that a sqlite table rebuild would
silently drop, so there's nothing a rebuild buys here.
"""

from alembic import op
import sqlalchemy as sa


revision = '0012_provider_email_username'
down_revision = '0011_login_lockout'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'auth_providers',
        sa.Column('use_email_as_username', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('auth_providers', 'use_email_as_username')
