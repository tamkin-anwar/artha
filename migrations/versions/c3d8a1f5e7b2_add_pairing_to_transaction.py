"""add pairing and bill_match_declined to transaction

Revision ID: c3d8a1f5e7b2
Revises: b7c1e9a4d2f0
Create Date: 2026-10-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d8a1f5e7b2'
down_revision = 'b7c1e9a4d2f0'
branch_labels = None
depends_on = None


def upgrade():
    # sa.false(), not sa.text('0') -- see CLAUDE.md's "Any new db.Boolean
    # column migration" convention (Postgres rejects the text() form).
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pairing', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('bill_match_declined', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.drop_column('bill_match_declined')
        batch_op.drop_column('pairing')
