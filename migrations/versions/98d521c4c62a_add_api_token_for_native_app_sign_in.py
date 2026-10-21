"""add api_token for native app sign-in

Revision ID: 98d521c4c62a
Revises: 8cca5a959964
Create Date: 2026-10-20 20:08:46.578996

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '98d521c4c62a'
down_revision = '8cca5a959964'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping/recreating the user table's
# calendar_feed_token uniqueness (an index vs. constraint naming mismatch
# it flags on every run, unrelated to this change); trimmed out, same as
# the migrations before this one.
def upgrade():
    op.create_table('api_token',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('device_name', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('api_token', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_token_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('api_token', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_token_user_id'))

    op.drop_table('api_token')
