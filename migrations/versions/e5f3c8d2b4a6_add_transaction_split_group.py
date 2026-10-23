"""add transaction split group

Revision ID: e5f3c8d2b4a6
Revises: d4e2b7c9a1f3
Create Date: 2026-10-23 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f3c8d2b4a6'
down_revision = 'd4e2b7c9a1f3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('split_group', sa.String(length=32), nullable=True))
        batch_op.create_index('ix_transaction_split_group', ['split_group'], unique=False)


def downgrade():
    with op.batch_alter_table('transaction', schema=None) as batch_op:
        batch_op.drop_index('ix_transaction_split_group')
        batch_op.drop_column('split_group')
