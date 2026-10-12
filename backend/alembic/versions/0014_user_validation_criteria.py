"""Store RAGAS quality-criterion instructions per user.

Revision ID: 0014_user_validation_criteria
Revises: 0013_webhooks
"""

from alembic import op
import sqlalchemy as sa


revision = '0014_user_validation_criteria'
down_revision = '0013_webhooks'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_validation_criteria',
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('criteria', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('user_validation_criteria')
