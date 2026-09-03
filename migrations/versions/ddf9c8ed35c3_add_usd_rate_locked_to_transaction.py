"""add usd_rate_locked to transaction

Revision ID: ddf9c8ed35c3
Revises: 261d0d1f8439
Create Date: 2026-09-02 22:05:05.481717

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ddf9c8ed35c3'
down_revision = '261d0d1f8439'
branch_labels = None
depends_on = None


def upgrade():
    # Note: autogenerate also proposed dropping/recreating
    # user.calendar_feed_token's unique constraint here — an artifact of
    # this dev DB's own migration history (a manual local patch created
    # it as an INDEX instead of a CONSTRAINT while debugging the prior
    # migration), not a real schema change. The prior migration
    # (261d0d1f8439) already creates it correctly as a named CONSTRAINT
    # on a clean install, so that block is intentionally omitted here.
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('usd_rate_locked', sa.Numeric(precision=18, scale=8), nullable=True))


def downgrade():
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.drop_column('usd_rate_locked')
