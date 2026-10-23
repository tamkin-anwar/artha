"""add bill_matched to transaction

Revision ID: b7c1e9a4d2f0
Revises: f2dff026dfda
Create Date: 2026-10-22 15:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7c1e9a4d2f0'
down_revision = 'f2dff026dfda'
branch_labels = None
depends_on = None


def upgrade():
    # sa.false(), not sa.text('0') -- the text() form works against local
    # SQLite but breaks Postgres (psycopg2.errors.DatatypeMismatch); see
    # CLAUDE.md's "Any new db.Boolean column migration" convention.
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bill_matched', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.drop_column('bill_matched')
