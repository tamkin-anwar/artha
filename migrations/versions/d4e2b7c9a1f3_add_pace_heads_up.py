"""add pace heads-up preference, prompt flag and budget marker

Revision ID: d4e2b7c9a1f3
Revises: c3d8a1f5e7b2
Create Date: 2026-10-23 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e2b7c9a1f3'
down_revision = 'c3d8a1f5e7b2'
branch_labels = None
depends_on = None


def upgrade():
    # sa.false(), not sa.text('0') -- see CLAUDE.md's "Any new db.Boolean
    # column migration" convention (Postgres rejects the text() form).
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_pace_heads_up', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('pace_prompt_answered', sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table('budget', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pace_alerted_month', sa.Date(), nullable=True))


def downgrade():
    with op.batch_alter_table('budget', schema=None) as batch_op:
        batch_op.drop_column('pace_alerted_month')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('pace_prompt_answered')
        batch_op.drop_column('notify_pace_heads_up')
