"""add transfer account

Revision ID: f6a4d9e3c5b7
Revises: e5f3c8d2b4a6
Create Date: 2026-10-23 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a4d9e3c5b7'
down_revision = 'e5f3c8d2b4a6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'transfer_account',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=80), nullable=False),
        sa.Column('label', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'key', name='uq_transfer_account_user_key'),
    )
    with op.batch_alter_table('transfer_account', schema=None) as batch_op:
        batch_op.create_index('ix_transfer_account_user_id', ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('transfer_account', schema=None) as batch_op:
        batch_op.drop_index('ix_transfer_account_user_id')
    op.drop_table('transfer_account')
