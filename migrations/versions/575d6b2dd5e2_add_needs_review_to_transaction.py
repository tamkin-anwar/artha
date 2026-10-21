"""add needs_review to transaction

Revision ID: 575d6b2dd5e2
Revises: 98d521c4c62a
Create Date: 2026-10-21 02:01:39.070361

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '575d6b2dd5e2'
down_revision = '98d521c4c62a'
branch_labels = None
depends_on = None


def upgrade():
    # sa.false(), not sa.text('0') -- the text() form works against local
    # SQLite but breaks Postgres (psycopg2.errors.DatatypeMismatch); see
    # CLAUDE.md's "Any new db.Boolean column migration" convention.
    # Autogenerate also proposed swapping user.uq_user_calendar_feed_token
    # for an unnamed constraint; that's SQLite reflection noise, unrelated
    # to this change, so it's left out.
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('needs_review', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.drop_column('needs_review')
