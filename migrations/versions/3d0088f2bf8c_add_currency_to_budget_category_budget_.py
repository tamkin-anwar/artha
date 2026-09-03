"""add currency to budget, category_budget, and scenario

Revision ID: 3d0088f2bf8c
Revises: ddf9c8ed35c3
Create Date: 2026-09-02 23:38:17.495720

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3d0088f2bf8c'
down_revision = 'ddf9c8ed35c3'
branch_labels = None
depends_on = None


def upgrade():
    # Note: autogenerate also proposed dropping/recreating
    # user.calendar_feed_token's unique constraint here — the same dev-DB
    # history artifact already explained in migration 261d0d1f8439's own
    # upgrade() comment, not a real schema change. Omitted here too.
    with op.batch_alter_table('budget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=3), nullable=True))

    with op.batch_alter_table('category_budget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=3), nullable=True))

    with op.batch_alter_table('scenario', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=3), nullable=True))


def downgrade():
    with op.batch_alter_table('scenario', schema=None) as batch_op:
        batch_op.drop_column('currency')

    with op.batch_alter_table('category_budget', schema=None) as batch_op:
        batch_op.drop_column('currency')

    with op.batch_alter_table('budget', schema=None) as batch_op:
        batch_op.drop_column('currency')
